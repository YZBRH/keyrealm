# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：Keyrealm 单文件可执行程序。

设计要点
--------
- **onefile**：产出单个可执行文件（Windows 为 Keyrealm.exe）。
- **只打包代码 + frontend/dist**：前端产物作为只读资源随包（运行时从 sys._MEIPASS 取）。
- **不打包 plugins/ 与 data/**：这两者是「用户可改 / 运行时可写」内容，由构建脚本
  以外部目录形式放到 exe 同级（见 scripts/build.py）。这样用户能直接编辑插件、
  数据也不会写进临时解压目录而丢失。

console 开关
------------
默认 GUI 模式（console=False，无黑框）。调试构建可设环境变量 CV_CONSOLE=1 保留控制台，
便于运行 `Keyrealm --print-paths` 校验冻结后的路径解析。
"""
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 中文 Windows 控制台默认 cp1252/GBK，而本 spec 有多处中文 print（"[spec] 应用图标：…" 等）——
# 不钉 UTF-8 会在 Windows 上抛 UnicodeEncodeError（无论经 scripts/build.py 还是直接 pyinstaller Keyrealm.spec）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# spec 文件所在目录 = 项目根
ROOT = Path(SPECPATH).resolve()

# 前端构建产物（必须先 npm run build）
FRONTEND_DIST = ROOT / "frontend" / "dist"

_console = os.environ.get("CV_CONSOLE") == "1"

# 应用图标（Windows exe 需 .ico）。文件缺失时传 None，回退 PyInstaller 默认图标。
_ICON_PATH = ROOT / "assets" / "icon.ico"
_icon = str(_ICON_PATH) if _ICON_PATH.exists() else None
if _icon:
    print(f"[spec] 应用图标：{_ICON_PATH}")
else:
    print(
        "[spec] 警告：未找到 assets/icon.ico，exe 将使用 PyInstaller 默认图标。"
        "请先运行 python scripts/make_icon.py 生成图标。"
    )

datas = []
binaries = []
hiddenimports = []

# 随包只读资源：前端 dist → 包内 frontend/dist
if FRONTEND_DIST.exists():
    datas.append((str(FRONTEND_DIST), "frontend/dist"))

# 应用图标 → 包内 assets/。运行时以 resource_dir()/assets/icon.ico 显式
# 设置窗口/任务栏图标（不依赖 pywebview 从 exe 提取的 16px 小图标）。
ASSETS_DIR = ROOT / "assets"
if ASSETS_DIR.exists():
    datas.append((str(ASSETS_DIR), "assets"))

# 逐个收集第三方依赖的数据文件、二进制与隐藏子模块。
# 这些库大量使用动态导入 / 平台特定后端，手工列 hiddenimports 极易漏，交给 collect_all。
for _pkg in [
    "webview",        # pywebview（各平台 WebView 后端）
    "clr",            # pythonnet（Windows 后端，非 Windows 上收集失败会被忽略）
    "clr_loader",
    "pythonnet",
    "pystray",        # 系统托盘（各平台后端）
    "PIL",            # Pillow（托盘图标）
    "apscheduler",    # 定时任务
    "mcp",            # MCP Server
    "uvicorn",        # ASGI 服务器
    "starlette",
    "sse_starlette",  # MCP SSE 传输
    "anyio",
    "sniffio",
    "httpx",          # 插件验证 HTTP 客户端
    "httpcore",
    "h11",
    "certifi",
    "click",
    "cryptography",   # AES-GCM / PBKDF2
    "pydantic",
    "pydantic_core",
    "annotated_types",
]:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        # 平台特有的可选依赖（如 Windows 的 clr）在当前系统不存在，跳过即可
        pass

# ── 隐式依赖：缺一个就**中止构建** ─────────────────────────
#
# 下面几项都已经在 requirements.txt 里（socksio 由 httpx[socks] 带出），构建环境本该齐全。
# 它们全都只在**函数体内**或**运行期动态加载的插件里**被 import：PyInstaller 的静态分析看不到，
# 漏掉时构建**不报任何错**，代价却要在 exe 里才显形——「一用 socks5 代理就英文 ImportError」
# 「配了代理却静默直连」。所以这里缺了就停下，宁可不产出，也不产出一个悄悄缺功能的包。
#
# 逃生口：CV_ALLOW_MISSING_DEPS=1（`python scripts/build.py --allow-missing-deps` 会设置它）。
_ALLOW_MISSING_DEPS = os.environ.get("CV_ALLOW_MISSING_DEPS") == "1"


def require(pkg: str, why: str, install: str) -> None:
    """收集一个「PyInstaller 静态分析看不到」的依赖。导入失败时默认中止构建。

    只能用 .extend() 而不是 `datas += ...`：函数体内的 `+=` 会把 datas 变成局部名。

    **必须把真实异常打出来**：以前这里 `except Exception:` 吞掉细节、只说「未安装」，
    于是「包装了但跑不起来」（SyntaxError / AttributeError / 某个子模块 import 失败）
    和「压根没装」长得一模一样，白白让人去重装一遍。
    """
    try:
        __import__(pkg)
    except Exception as exc:  # noqa: BLE001 - 这里要的就是「什么都别放过」
        detail = f"{type(exc).__name__}: {exc}"
        if _ALLOW_MISSING_DEPS:
            print(f"[spec] 警告：导入 {pkg} 失败 → 打出的程序{why}（已由 --allow-missing-deps "
                  f"放行）；实际报错：{detail}")
            return
        raise SystemExit(
            f"[spec] 构建中止：构建环境**导入 {pkg} 失败** —— 打出的程序{why}。\n"
            f"[spec]           真实报错：{detail}\n"
            f"[spec]           · 若是 `ModuleNotFoundError`（真的没装）→ {install}\n"
            f"[spec]           · 若是别的（SyntaxError / AttributeError / 子模块失败…）→ "
            f"**它装着，只是这个版本跑不起来**，别重装，按上面的报错查。\n"
            f"[spec]           确实要忽略缺依赖继续构建：加 --allow-missing-deps"
            f"（或直接设 CV_ALLOW_MISSING_DEPS=1）"
        )
    _d, _b, _h = collect_all(pkg)
    datas.extend(_d)
    binaries.extend(_b)
    hiddenimports.extend(_h)
    print(f"[spec] 已收集 {pkg}")


# SOCKS 代理支持：httpx 在**函数体内**静态 import socksio（httpx/_transports/default.py），
# 构建机没装这个包时 PyInstaller 既不报错也不会打包它，症状是 exe 里一用 socks5 代理
# 就抛英文 ImportError("Using SOCKS proxy, but the 'socksio' package is not installed")。
require(
    "socksio",
    "**无法使用 socks5 代理**",
    'pip install "httpx[socks]"',
)

# 邮箱（SMTP/IMAP）走 SOCKS/HTTP CONNECT 隧道要靠 PySocks（import 名是 socks）。
# 插件是运行时动态加载的，PyInstaller 静态分析看不到它，必须显式收集。
require(
    "socks",
    "**无法给邮箱设置代理**",
    "pip install PySocks",
)

# Telegram api_id/api_hash 校验（plugins/im/telegram_apihash）靠 telethon；它同样只在插件里被
# import，PyInstaller 静态分析看不到，必须显式收集。
require(
    "telethon",
    "**无法校验 Telegram api_id/api_hash**",
    "pip install -r requirements.txt（含 telethon + python-socks）",
)

# telethon 的 socks 支持另需 python-socks，**这一条要一起收**：缺它时 telethon 不报错，
# 而是**静默忽略 proxy= 直连**（插件因此拒绝联网校验，不会漏出直连出口，但配了代理的凭据
# 只能降级成「未验证」）。注意别指望 `telethon[socks]`：telethon 的轮子里没有 socks extra
# （Provides-Extra 只有 cryptg），装不来 python-socks，所以 requirements.txt 里是单独一行。
require(
    "python_socks",
    "**无法校验配了代理的 Telegram 凭据**",
    "pip install python-socks（telethon 没有 socks extra，写 telethon[socks] 装不来它）",
)

# plugins/ 下的标准库导入：**必须手工补**，否则打包后的 exe 一用到就崩。
#
# 原因：插件是运行时由 backend 用 importlib **动态加载**的，main.py 从不 `import plugins`，
# PyInstaller 的静态分析因此完全看不到插件用到的模块。第三方库（httpx 等）恰好后端也在用、
# 已被收集，但那些**只有插件用**的标准库（imaplib / smtplib / email / zipfile / xml / html …）
# 不会被带进去。症状是 exe 里一执行对应操作就 `ModuleNotFoundError: No module named 'imaplib'`
# ——构建期毫无警告、开发机也复现不出（开发机是完整解释器，什么都有）。
# 这里扫一遍 plugins/**/*.py 的顶层 import，把其中属于标准库的名字补进 hiddenimports，
# 新增插件用到新的标准库时自动生效，无需再回来手工维护这张清单。
#
# `plugins/` 是插件的**唯一来源**（不再有 plugins_builtin/ 与 plugins_optin/ 两份副本），
# 因此这里只扫它一处即可覆盖全部插件用到的标准库。
def _plugin_stdlib_imports() -> list[str]:
    roots = [ROOT / "plugins"]
    pat = re.compile(
        r"^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import)\b", re.M
    )
    names: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in pat.finditer(text):
                mod = (m.group(1) or m.group(2) or "").split(".")[0]
                # 跳过 __future__ 这类伪模块；它进了 hiddenimports 只会让 PyInstaller 报警告
                if mod and not mod.startswith("_") and mod in sys.stdlib_module_names:
                    names.add(mod)
    return sorted(names)


_plugin_mods = _plugin_stdlib_imports()
if _plugin_mods:
    hiddenimports += _plugin_mods
    print(f"[spec] 插件标准库依赖：已收集 {', '.join(_plugin_mods)}")

# 本地 backend 包的全部子模块（api/crypto/database/mcp_server/... ）
try:
    hiddenimports += collect_submodules("backend")
except Exception:
    pass
hiddenimports += [
    "backend",
    "backend.api",
    "backend.crypto",
    "backend.database",
    "backend.mcp_server",
    "backend.paths",
    "backend.plugin_base",
    "backend.plugin_manager",
    "backend.proxy",
    "backend.proxy_test",
    "backend.scheduler",
    "backend.tray",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # plugins/data/tests/docs 均为外部内容或开发资源，不进包
    excludes=["plugins", "data", "tests", "docs", "pytest", "PyInstaller"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Keyrealm",
    icon=_icon,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 不使用 UPX：UPX 压缩会破坏 Windows exe 内嵌的图标资源（表现为图标回退为
    # PyInstaller 默认图标），且易触发杀软误报。体积换稳定，关闭。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
