#!/usr/bin/env python3
"""构建 Keyrealm 单文件可执行程序。

**默认输出到仓库根（`.git` 同级）**；用 `--output DIR` 可指定别处（`--output release`
即旧行为）。跨平台：Windows 上产出 `Keyrealm.exe`，Linux/macOS 上产出 `Keyrealm`。
**要生成 Windows exe，必须在 Windows 上运行本脚本**（PyInstaller 不能跨平台交叉编译）。

打包边界
--------
- 进 exe：后端代码 + frontend/dist（只读资源）。
- 不进 exe、以外部目录放在**输出目录**下：
    · plugins/  —— 插件**唯一事实来源**，用户可直接编辑（改完重启即生效）；打包源码，
                   但**不带 `.state.json`（启用/禁用）与 `.model_cache.json`（模型清单缓存）**
                   ——都是本机运行时状态，不入分发包
    · data/     —— 运行时自动创建（数据库、主密码校验、日志）
  release/ 因此是一个自包含、可便携分发的目录，且不被 git 追踪。

用法
----
    python scripts/build.py               # 需要已存在 frontend/dist
    python scripts/build.py --frontend    # 先执行 npm run build 再打包
    python scripts/build.py --console     # 保留控制台窗口（调试用）
    python scripts/build.py --smoke       # 构建后跑 --print-paths 校验路径（自动开控制台）

构建前置检查
------------
开始打包前按 requirements.txt（**含 extras 展开**）核对当前 Python 环境，缺包就打印清单
并中止，提示 pip install -r requirements.txt；确实要忽略可加 --allow-missing-deps。
extras 必须查：httpx 是在函数体内 import socksio 的，构建机没装时 PyInstaller 既不报错
也不打包，问题要到 exe 里真的用 socks5 代理才暴露。
同一条检查在 `Keyrealm.spec` 里也有一份（那四个隐式依赖），**默认缺一个即中止构建**；
`--allow-missing-deps` 会以 CV_ALLOW_MISSING_DEPS=1 透传给 spec，两边口径一致。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 中文 Windows 控制台默认 cp1252/GBK，打印中文与 ✓/✅ 会 UnicodeEncodeError（CI 的 Windows runner 上真踩到过：
# 「目标平台 : …」这行直接抛异常、构建整步失败）。与其它脚本（check_schema / migrate_* / 更新脚本）同一套处理：
# 把 stdout / stderr 钉成 UTF-8（errors="replace" 兜底，宁可显示成问号也别崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 无 stdout（windowed）等场景忽略
        pass

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "Keyrealm.spec"
REQUIREMENTS = ROOT / "requirements.txt"
FRONTEND = ROOT / "frontend"
FRONTEND_DIST = FRONTEND / "dist"
DIST = ROOT / "dist"      # PyInstaller 产物中转（gitignored）
WORK = ROOT / "build"     # PyInstaller 中间文件（gitignored）

IS_WINDOWS = platform.system() == "Windows"
EXE_NAME = "Keyrealm.exe" if IS_WINDOWS else "Keyrealm"

# 构建由**窗口化**的 exe（或应用内「准备阶段」）拉起时，进程本身没有控制台——
# 此时 spawn 控制台子进程（npm / PyInstaller / git）Windows 会**新开一个黑框**。
# 统一加 CREATE_NO_WINDOW 压掉；子进程再套子进程时它也一并继承（不再有新控制台）。
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW


def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    """执行子进程，实时透传输出，失败即退出。"""
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n", flush=True)
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                            creationflags=_NO_WINDOW)
    if result.returncode != 0:
        sys.exit(f"命令失败（退出码 {result.returncode}）：{' '.join(map(str, cmd))}")


def _extra_deps(dist: str, extra: str) -> list[str]:
    """取某发行包在指定 extra 下声明的依赖名（如 ``httpx[socks]`` → ``socksio``）。

    为什么必须展开 extras：httpx 是在**函数体内** ``import socksio`` 的，构建机没装时
    PyInstaller 既不报错也不打包，问题要到 exe 里真的用 socks5 代理才暴露（表现为
    ``the 'socksio' package is not installed``）。这类「隐性依赖」只能顺着 requirements
    的 extras 找出来提前检查。
    """
    from importlib.metadata import PackageNotFoundError, metadata

    try:
        meta = metadata(dist)
    except PackageNotFoundError:
        return []
    names = []
    for line in meta.get_all("Requires-Dist") or []:
        if not re.search(rf"""extra\s*==\s*['"]{re.escape(extra)}['"]""", line):
            continue
        m = re.match(r"\s*([A-Za-z0-9._-]+)", line)
        if m:
            names.append(m.group(1))
    return names


def required_distributions() -> list[str]:
    """requirements.txt 声明的发行包名（extras 展开），按出现顺序去重。"""
    out: list[str] = []
    if not REQUIREMENTS.exists():
        return out
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):   # 跳过 -r / -e / --index-url 等指令行
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[([^\]]*)\])?", line)
        if not m:
            continue
        base, extras = m.group(1), m.group(2) or ""
        out.append(base)
        for extra in (e.strip() for e in extras.split(",")):
            if extra:
                out += _extra_deps(base, extra)

    seen, uniq = set(), []
    for name in out:
        key = name.lower().replace("_", "-")
        if key not in seen:
            seen.add(key)
            uniq.append(name)
    return uniq


def missing_distributions() -> list[str]:
    """当前 Python 环境里没装的依赖。

    用 importlib.metadata 判定「发行包是否已安装」，而不是 import 探测——发行名与
    顶层模块名常常不同（pywebview 的模块叫 webview），按发行名才准。
    """
    from importlib.metadata import PackageNotFoundError, version

    missing = []
    for name in required_distributions():
        try:
            version(name)
        except PackageNotFoundError:
            missing.append(name)
    return missing


def fail_missing_deps(missing: list[str], allow_missing: bool) -> None:
    """打缺依赖清单与安装命令；默认中止，``--allow-missing-deps`` 可继续。

    只报「缺什么、怎么装」——它跑在哪个解释器由构建头部的 `解释器 : …` 一行给出。
    """
    print()
    print("!" * 62)
    print(f"依赖检查未通过：缺少 {len(missing)} 个依赖")
    for name in missing:
        print(f"  · {name}")
    print()
    print("  pip install -r requirements.txt")
    if allow_missing:
        print()
        print("  --allow-missing-deps：忽略缺失继续构建（产物可能不完整）。")
    print("!" * 62)
    print()
    if not allow_missing:
        sys.exit(1)


def run_capture(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """跑子进程并捕获输出，**两边都按 UTF-8**。

    Windows 上 ``subprocess.run(text=True)`` 默认用本地编码解码子进程输出（简体中文环境是
    GBK），而 Python 子进程写出来的是 UTF-8 —— 子进程只要输出一个 ``✓`` 或中文，父进程就会在
    读取线程里抛 ``UnicodeDecodeError``（表现为一段看不懂的 traceback，构建看起来"莫名失败"）。
    这里显式指定 ``encoding``，并用 ``PYTHONIOENCODING``/``PYTHONUTF8`` 把子进程也钉成 UTF-8，
    再加 ``errors="replace"`` 兜底：宁可显示成问号，也不能因为一个字符把整个构建搞崩。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(kwargs.pop("env", None) or {})
    kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, **kwargs)


def check_schema() -> None:
    """构建前置检查：在临时库上真跑一遍建表 + 各表 CRUD。

    建表语句写在 backend/database.py 里，只有**真连上 SQLite** 才会被解析；而单元测试
    通常把 database 模块整个打桩，于是「SQL 写错」这类问题一路测试全绿、打包后运行才崩
    （如把 SQLite 保留字 `values` 当列名 → 建表 syntax error）。
    所以放在打包前跑。
    """
    script = ROOT / "scripts" / "check_schema.py"
    result = run_capture([sys.executable, str(script)])
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit("建表自检未通过：上面是具体错误（数据库 SQL 有问题，修好再打包）")
    print("建表自检   : 通过 ✅")


def check_environment(allow_missing: bool) -> None:
    """构建前置检查：打包工具 + 依赖齐全。缺依赖默认中止（可被 flag 放行）。

    放在最前：缺包时先停下来，别白跑一遍前端构建 + PyInstaller 打包。
    """
    if importlib.util.find_spec("PyInstaller") is None:
        sys.exit("当前 Python 环境未安装 PyInstaller：pip install pyinstaller")

    total = required_distributions()
    missing = missing_distributions()
    if missing:
        print(f"依赖检查   : {len(total) - len(missing)}/{len(total)} 已安装，"
              f"缺少 {len(missing)} 个（见下）")
    else:
        print(f"依赖检查   : {len(total)}/{len(total)} 已安装 ✅")
    print("=" * 60)

    if missing:
        fail_missing_deps(missing, allow_missing)


def build_frontend() -> None:
    """执行 npm run build 生成 frontend/dist。"""
    if not (FRONTEND / "package.json").exists():
        sys.exit("未找到 frontend/package.json，无法构建前端")
    npm = "npm.cmd" if IS_WINDOWS else "npm"
    if shutil.which(npm) is None:
        sys.exit("未找到 npm，请先安装 Node.js，或去掉 --frontend 手动构建")

    node_modules = FRONTEND / "node_modules"
    # node_modules **存在**不等于能用，故不能只看目录在不在：
    #   1) 从 Linux/macOS 直接拷过来的 node_modules，`.bin/vite` 是 POSIX 脚本，
    #      Windows 找的是 `.bin/vite.cmd` —— 于是 `npm run build` 报
    #      「'vite' is not recognized as an internal or external command」；
    #   2) 装的时候用了 `--omit=dev` / `--production`，而 vite 在 devDependencies 里，被跳过。
    # 两种都不是代码问题，但 npm 那句英文报错看着很像代码问题。这里先自己判一次并给出修法。
    vite_bin = node_modules / ".bin" / ("vite.cmd" if IS_WINDOWS else "vite")
    if node_modules.exists() and not vite_bin.exists():
        sys.exit(
            "frontend/node_modules 里没有可用的 vite（多半是跨平台拷贝过来的 node_modules，"
            "或安装时用了 --omit=dev）。\n"
            "请删掉该目录后重装：\n"
            f"    {'rmdir /s /q' if IS_WINDOWS else 'rm -rf'} \"{node_modules}\"\n"
            f"    cd \"{FRONTEND}\" && npm install"
        )
    if not node_modules.exists():
        # 用 `npm ci` 而不是 `npm install`：ci **严格按 lockfile 装、绝不改写它**。
        # `npm install` 会在 lockfile 与 npm 版本/平台有漂移时顺手重写 `package-lock.json`，
        # 把**部署目录的工作区弄脏**——而测试版更新要 `git fetch`，脏树会被拦下（或被迫丢弃改动）。
        # 没有 lockfile 时才退回 `npm install`（此时没有可被改写的锁文件）。
        if (FRONTEND / "package-lock.json").is_file():
            _run([npm, "ci"], cwd=FRONTEND)
        else:
            _run([npm, "install"], cwd=FRONTEND)
    _run([npm, "run", "build"], cwd=FRONTEND)


def write_build_info(gui: str = "") -> None:
    """把当前 commit 短 hash 写进 `backend/_build_info.py`，供 exe 内显示「构建版本」。

    冻结的 exe 里**没有 git**，所以必须在打包**之前**落盘；该文件由本脚本生成、不入库
    （见 .gitignore）。源码运行时它不存在 → `backend/version.build_id()` 会现查 git。
    工作区有未提交改动时追加 `-dirty`，避免拿一个「改了东西却没提交」的 exe 当发布版。

    顺带记下**构建时选定的界面后端**（``--gui``）：Linux 上这个选择要跟着包走，
    运行时才不必再设 `KEYREALM_GUI`（见 main.py 的 ``_preferred_gui``）。
    """
    commit = "unknown"
    try:
        out = run_capture(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT))
        if out.returncode == 0 and out.stdout.strip():
            commit = out.stdout.strip()
            dirty = run_capture(["git", "status", "--porcelain"], cwd=str(ROOT))
            if dirty.stdout.strip():
                commit += "-dirty"
    except Exception as e:  # noqa: BLE001 - 万一没装 git：标 unknown，不拦构建
        print(f"警告：无法获取 git 版本，构建版本将标为 unknown（{e}）", file=sys.stderr)

    (ROOT / "backend" / "_build_info.py").write_text(
        '"""由 scripts/build.py 生成 —— **勿手工编辑、不入库**。"""\n'
        f'COMMIT = "{commit}"\n'
        f'GUI = "{gui}"      # 空 = 平台默认（Linux 上默认偏好 Qt）\n',
        encoding="utf-8",
    )
    print(f"构建版本   : {commit}")
    if gui:
        print(f"界面后端   : {gui}")


def run_pyinstaller(console: bool, allow_missing: bool = False) -> None:
    """调用当前解释器的 PyInstaller，按 spec 打包到 dist/。

    ``--allow-missing-deps`` 必须**透传给 spec**：spec 自己也会对那几个「静态分析看不到」
    的隐式依赖（socksio / PySocks / telethon / python-socks）做检查，缺了默认直接中止构建。
    不透传的话，本脚本放行了、spec 照样会停，flag 等于白给。
    """
    if not FRONTEND_DIST.exists():
        sys.exit(
            "未找到 frontend/dist —— 前端尚未构建。\n"
            "请先执行： cd frontend && npm install && npm run build\n"
            "或直接用： python scripts/build.py --frontend"
        )

    env = dict(os.environ)
    # PyInstaller 进程里会 exec Keyrealm.spec（有中文 print）——Windows 上默认 cp1252 会 UnicodeEncodeError，
    # 故给子进程钉 UTF-8（与 run_capture 同一套；spec 自己也 reconfigure 了一道，双保险）。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if console:
        env["CV_CONSOLE"] = "1"
    else:
        env.pop("CV_CONSOLE", None)
    # 与 --allow-missing-deps 打通：spec 里对那四个隐式依赖默认「缺了即中止」，
    # 这里显式跑一次 PyInstaller（而非经本脚本）时，逃生口就是这个环境变量。
    if allow_missing:
        env["CV_ALLOW_MISSING_DEPS"] = "1"
    else:
        env.pop("CV_ALLOW_MISSING_DEPS", None)

    _run(
        [
            sys.executable, "-m", "PyInstaller",
            "--clean", "--noconfirm",
            "--distpath", str(DIST),
            "--workpath", str(WORK),
            str(SPEC),
        ],
        cwd=ROOT,
        env=env,
    )

    built = DIST / EXE_NAME
    if not built.exists():
        sys.exit(f"PyInstaller 结束但未找到产物：{built}")


def assemble_release(out_dir: Path) -> Path:
    """把 dist/ 中的可执行文件与外部 plugins/ 组装进 ``out_dir``。

    ``out_dir`` 默认是**仓库根**（`.git` 同级）；``--output release`` 即旧行为。
    """
    built = DIST / EXE_NAME
    if not built.exists():
        sys.exit(f"未找到构建产物：{built}")

    # 不整体清空 out_dir：保留其中已存在的 data/（运行时数据，备份在 data/backup/）等无关文件，
    # 只替换本脚本负责的 exe（与、必要时、plugins/）。
    out_dir.mkdir(parents=True, exist_ok=True)

    exe_dest = out_dir / EXE_NAME
    if exe_dest.exists():
        # 覆盖前先留一份 .old：更新流程万一在后续步骤（迁移/重启）失败可据此回滚。
        # Keyrealm* 已在 .gitignore 中，不会污染工作区。
        old = out_dir / (EXE_NAME + ".old")
        try:
            old.unlink()
        except OSError:
            pass
        exe_dest.replace(old)
    shutil.copy2(built, exe_dest)

    # 外部可改插件目录：复制源码形式的 plugins/（manifest.json + plugin.py）
    # 排除 __pycache__/*.pyc，避免把构建机的字节码带进分发包；
    # 同时排除 .state.json*（启用/禁用状态）与 .model_cache.json*（模型清单缓存）——都是本机运行时状态。
    src_plugins = (ROOT / "plugins").resolve()
    dest_plugins = (out_dir / "plugins").resolve()
    at_repo_root = dest_plugins == src_plugins
    # **关键守卫**：输出就在仓库根时，plugins/ 本就是活的源码目录——若照旧 rmtree 再 copytree，
    # 等于先把源码删了再从自己复制（灾难）。这种情况直接跳过（源码即产物）。
    if at_repo_root:
        pass
    elif src_plugins.exists():
        if dest_plugins.exists():
            shutil.rmtree(dest_plugins)
        shutil.copytree(
            src_plugins,
            dest_plugins,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                          ".state.json*", ".model_cache.json*"),
        )
    else:
        print("警告：未找到 plugins/，产物将不含内置插件", file=sys.stderr)

    # 约定：plugins/ 是插件的唯一事实来源；data/installed_plugins/ 是旧版本（zip 安装时代）
    # 遗留的运行期缓存。只在**非仓库根**输出下清理它——输出=仓库根时 data/ 是用户真实数据目录，不碰。
    if not at_repo_root:
        stale_cache = out_dir / "data" / "installed_plugins"
        if stale_cache.exists():
            shutil.rmtree(stale_cache)

    # data/ 其余内容不预建，应用首次运行时自动创建
    if not exe_dest.is_file():
        sys.exit(f"装配后未找到产物：{exe_dest}（输出目录是否被清理/占用？）")
    return exe_dest


def smoke_test(exe: Path, out_dir: Path) -> None:
    """运行 exe --print-paths 校验冻结后的路径解析（需控制台构建）。"""
    print("\n=== 冒烟校验：--print-paths ===", flush=True)
    result = run_capture([str(exe), "--print-paths"], timeout=120)
    print(result.stdout, end="")
    if result.stderr:
        print("[stderr]", result.stderr, end="")
    # 校验关键路径落在 out_dir 下、frontend 随包
    ok = True
    out = result.stdout
    if str(out_dir) not in out:
        print(f"✗ app_dir / db / plugins 未指向输出目录（{out_dir}）")
        ok = False
    if "frontend_index" in out and "_MEI" not in out.split("frontend_index", 1)[1].splitlines()[0]:
        # 冻结后 frontend 应来自 _MEIPASS（临时解压目录，路径含 _MEI）
        print("✗ frontend_index 未指向包内 _MEIPASS 资源")
        ok = False
    print("冒烟校验：", "通过 ✓" if ok else "存在问题 ✗")


def clean_intermediates() -> None:
    for d in (DIST, WORK):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 Keyrealm 单文件可执行程序（默认输出到仓库根）")
    ap.add_argument("--frontend", action="store_true", help="构建前先执行 npm run build")
    ap.add_argument("--output", "-o", default=str(ROOT),
                    help="产物落点目录（默认：仓库根，即 .git 同级；传 release 即旧行为）")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口（调试）")
    ap.add_argument("--smoke", action="store_true", help="构建后运行 --print-paths 校验（隐含 --console）")
    ap.add_argument("--keep-intermediates", action="store_true", help="保留 dist/ 与 build/ 中间产物")
    ap.add_argument("--allow-missing-deps", action="store_true",
                    help="依赖缺失时仍继续构建（产物可能缺功能，默认中止）")
    ap.add_argument("--gui", choices=("qt", "gtk"), default=None,
                    help="仅 Linux 有效：界面后端 qt（默认，自包含）/ gtk（用系统 GTK）。"
                         "选中的那个会烘进包里，运行时不必再设环境变量")
    args = ap.parse_args()

    console = args.console or args.smoke
    out_dir = Path(args.output).expanduser().resolve()

    gui = (args.gui or "").strip().lower()
    if gui and platform.system() != "Linux":
        print(f"注意：--gui {gui} 只对 Linux 有意义，本机是 {platform.system()}，本次忽略",
              file=sys.stderr)
        gui = ""
    if gui:
        # 透传给 PyInstaller 子进程：spec 据此决定收集 Qt 还是 gi（见 Keyrealm.spec）
        os.environ["KEYREALM_GUI"] = gui

    if platform.system() == "Linux":
        gui_note = gui or "（未指定 → 默认 Qt；没装 Qt 则退回 GTK）"
    else:
        gui_note = "（用系统自带的 WebView，无需选择）"

    print("=" * 60)
    print(f"目标平台   : {platform.system()} {platform.machine()}")
    print(f"Python     : {sys.version.split()[0]}")
    print(f"解释器     : {sys.executable}")
    print(f"产物名     : {EXE_NAME}")
    print(f"输出目录   : {out_dir}")
    print(f"控制台模式 : {console}")
    print(f"界面后端   : {gui_note}")
    print("=" * 60)

    check_environment(args.allow_missing_deps)
    check_schema()
    # 打包前落盘 commit hash（exe 里没有 git）——必须在 run_pyinstaller 之前
    write_build_info(gui)

    if args.frontend:
        build_frontend()
    run_pyinstaller(console, args.allow_missing_deps)
    exe = assemble_release(out_dir)

    if not args.keep_intermediates:
        clean_intermediates()

    if args.smoke:
        try:
            smoke_test(exe, out_dir)
        except Exception as e:
            print(f"冒烟校验执行失败（可忽略，多为无显示环境）：{e}", file=sys.stderr)

    # 汇总产物
    at_root = out_dir == ROOT
    print("\n" + "=" * 60)
    print(f"构建完成 → {out_dir}")
    print("=" * 60)
    if exe.is_file():
        print(f"  {EXE_NAME:<20s} {human_size(exe.stat().st_size)}")
    else:
        # 走到这里说明装配之后产物又被删了（例如把 output 指到了会被清理的目录）。
        # 明确报出来而不是在 stat() 上抛 FileNotFoundError。
        sys.exit(f"✗ 产物不见了：{exe}")
    if at_root:
        print("  落点即仓库根：plugins/ 就是源码目录本身（未另存副本），data/ 为运行时数据目录。")
    else:
        plugins_dir = out_dir / "plugins"
        if plugins_dir.exists():
            # 两层结构：类别目录（llm/、api/…）下挂服务子插件，根级也可能有平铺插件
            flat, categories = [], []
            for entry in sorted(plugins_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "manifest.json").exists():
                    flat.append(entry.name)
                else:
                    subs = [d.name for d in sorted(entry.iterdir())
                            if d.is_dir() and (d / "manifest.json").exists()]
                    categories.append(f"{entry.name}/（{len(subs)} 个服务）")
            parts = categories + flat
            print(f"  {'plugins/':<20s} 外部可改插件：{', '.join(parts)}")
        total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
        print(f"  {'合计':<18s} {human_size(total)}")
    if IS_WINDOWS:
        print("\n运行前提：Windows 需已安装 WebView2 Runtime（Win10/11 通常已内置）。")
    print(f"data/ 将在首次运行时自动创建于 {out_dir} 下。")


if __name__ == "__main__":
    main()
