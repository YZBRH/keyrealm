#!/usr/bin/env python3
"""Keyrealm 启动器：稳定入口 + onedir 运行时安装器。

发行布局::

    Keyrealm(.exe)          本程序（PyInstaller onefile，体积小）
    runtime/                真正的应用（PyInstaller onedir）
      Keyrealm(.exe)
      _internal/
    plugins/  data/         用户内容，更新时原样保留

- **正常启动**：把命令行参数原样交给 ``runtime`` 里的应用，等待退出并透传退出码。
- **更新**（``--apply-update <kind>``）：应用退出后替换整个 ``runtime/`` 目录，再让新
  运行时完成数据库迁移与重启。启动器本身不参与应用运行，因此可以在更新时被替换，
  也不会像 onefile 那样每次启动都把整包解压到临时目录。

启动器只依赖标准库，更新安装逻辑刻意独立于 ``backend/``——它是整个程序里最稳定的一层。
"""
from __future__ import annotations

import ctypes
import hashlib
from datetime import datetime, timezone
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Optional

RUNTIME_DIR = "runtime"
RUNTIME_OLD = "runtime.old"
UPDATE_NEW = ".update-new"
MAX_WAIT_SECONDS = 120.0
MAX_FINISH_SECONDS = 180.0

# 与 backend/single_instance.py 保持一致：安装期间由启动器占住同一把锁，
# 既等应用退出，也挡住「替换 runtime 时又启动一个实例」。
LOCK_MUTEX_NAME = "Keyrealm_SingleInstance"
_LOCK_FILE = "data/.instance.lock"
_lock_handle = None

# 与 backend/updater.py 一致：交棒进程的「更新代理」不能漏给重启后的新应用。
ENV_RESTORE_PROXY = "KEYREALM_UPDATE_RESTORE_ENV"
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# 桌面版启动器是 windowed（没有控制台）；这些参数说明用户其实是在命令行里操作，
# 需要先把父进程的控制台接上，否则运行时里的 --cli / --version 输出会没有落点。
_CLI_FLAGS = {"--cli", "--web", "--version", "-h", "--help", "--print-paths", "--self-update"}


# ── 基础路径 ──────────────────────────────────────────────

def root_dir() -> Path:
    """程序入口目录（启动器所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_name() -> str:
    """运行时可执行文件与启动器同名（``Keyrealm`` / ``Keyrealm-CLI``，Windows 带 .exe）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return "Keyrealm.exe" if os.name == "nt" else "Keyrealm"


def _executable_in(directory: Path) -> Optional[str]:
    """目录顶层的应用可执行文件（runtime/ 下只有应用本体，其余都在 _internal/）。

    优先按可执行位判断；跨平台解压出来的包可能没有可执行位，再按文件名 / 首个文件回退。
    """
    try:
        files = [entry for entry in sorted(Path(directory).iterdir()) if entry.is_file()]
    except OSError:
        return None
    if not files:
        return None
    # 先按名字认应用本体（PyInstaller 5 的 onedir 去掉 _internal 时顶层会混着很多 .so，
    # 只按可执行位判断可能挑错）；再按可执行位；最后退回首个文件。
    for entry in files:
        if entry.stem.lower().startswith("keyrealm"):
            return entry.name
    if os.name != "nt":
        for entry in files:
            if os.access(entry, os.X_OK):
                return entry.name
    return None


def runtime_name(root: Path) -> str:
    """应用可执行文件名。启动器被改名时，按 runtime/ 里的可执行文件回退。"""
    root = Path(root)
    if (root / RUNTIME_DIR / app_name()).is_file():
        return app_name()
    return _executable_in(root / RUNTIME_DIR) or app_name()


def app_exe(root: Path) -> Path:
    return Path(root) / RUNTIME_DIR / runtime_name(root)


def _child_env(root: Path) -> dict:
    env = dict(os.environ)
    # 应用要知道「用户内容在哪」：运行时自己在 runtime/ 下，默认会把 exe 同级当程序目录。
    env["KEYREALM_APP_DIR"] = str(root)
    # 启动器可能被用户改名；把真实入口路径告诉应用，更新/重启才能找对文件。
    env["KEYREALM_LAUNCHER"] = str(Path(root) / app_name())
    # onefile 启动器的临时解压目录不能漏给子进程（否则子进程会复用、并随启动器退出失效）。
    env.pop("KEYREALM_UPDATE_TOKEN", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop("_MEIPASS2", None)
    env.pop("_PYI_APPLICATION_HOME_DIR", None)
    return env


def _detached_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def _log_file(data_dir: Path, prefix: str) -> Path:
    logs = Path(data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.log"


# ── 单实例锁（与 backend/single_instance.py 同一把锁）──────────

def _acquire_update_lock(root: Path) -> bool:
    """尝试占住应用的单实例锁；拿到 = 应用已经退出（且更新期间没人能再启动）。"""
    global _lock_handle
    root = Path(root)
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, LOCK_MUTEX_NAME)
            if not handle:
                return False                      # 建不了锁就不能保证互斥，更新必须中止
            if ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            _lock_handle = handle
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        import fcntl

        lock_path = root / _LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.write(str(os.getpid()))
        handle.flush()
        _lock_handle = handle
        return True
    except Exception:  # noqa: BLE001
        return False


def _release_update_lock() -> None:
    global _lock_handle
    handle, _lock_handle = _lock_handle, None
    if handle is None:
        return
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(handle)
        else:
            handle.close()
    except Exception:  # noqa: BLE001
        pass


def _wait_for_update_lock(root: Path, timeout: float = MAX_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _acquire_update_lock(root):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# ── 正常启动 ──────────────────────────────────────────────

def _attach_console_if_needed(argv: list[str]) -> None:
    """Windows：windowed 启动器在命令行里被调用时，接上父进程的控制台。

    双击启动（Explorer）时没有父控制台，调用失败即可，不会凭空多出黑框。
    """
    if os.name != "nt":
        return
    if not any(arg in _CLI_FLAGS for arg in argv):
        return
    try:
        if not ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
            return
        # 附着后重新绑定 stdio：窗口化进程原来的 sys.stdout 是 None，直接 print 会丢。
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def _one_shot_argv(argv: list[str]) -> bool:
    """这次启动是否只是「跑一下就退」的命令（CLI 版或 --cli / --version 等）。"""
    if "cli" in app_name().lower():
        return True
    return any(arg in ("--cli", "--version", "--print-paths", "-h", "--help") for arg in argv)


def _trivial_argv(argv: list[str]) -> bool:
    """只打印帮助 / 版本 / 路径的命令：退出码 0 不能证明应用真的可用。"""
    return not argv or any(arg in ("-h", "--help", "--version", "--print-paths") for arg in argv)


def _remove_old_files(old_runtime: Path, old_launcher: Path) -> None:
    shutil.rmtree(old_runtime, ignore_errors=True)
    try:
        old_launcher.unlink()
    except OSError:
        pass


def _cleanup_old_after_start(root: Path, proc: subprocess.Popen, argv: list[str],
                             delay: float = 8.0) -> None:
    """新应用**正常跑起来**之后再删上一版运行时 / 启动器。

    更新收尾只负责替换与迁移；若新运行时因打包问题启动即崩，``runtime.old/`` 与
    ``<launcher>.old`` 要留着作为回滚副本。
    - GUI / Web 等常驻进程：存活超过 8 秒即认为可用；
    - CLI 一次性命令通常几秒内退出：**退出码为 0** 也说明新运行时能正常跑（更新后
      自动重启的 CLI 往往只打印帮助就退出），此时同样清理；非 0 则保留回滚副本。
    """
    root = Path(root)
    old_runtime = root / RUNTIME_OLD
    old_launcher = root / (app_name() + ".old")
    if not old_runtime.is_dir() and not old_launcher.exists():
        return

    def _worker() -> None:
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                if rc == 0 and _one_shot_argv(argv) and not _trivial_argv(argv):
                    _remove_old_files(old_runtime, old_launcher)
                return                    # 非 0 / 桌面版快速退出 → 保留回滚副本
            time.sleep(0.5)
        if proc.poll() is None:
            _remove_old_files(old_runtime, old_launcher)

    threading.Thread(target=_worker, daemon=True).start()


def _recover_runtime(root: Path) -> bool:
    """上次安装在两次 rename 之间被打断时，用 ``runtime.old/`` 把运行时换回来。"""
    root = Path(root)
    old = root / RUNTIME_OLD
    if not old.is_dir() or not _executable_in(old):
        return False
    try:
        shutil.rmtree(root / RUNTIME_DIR, ignore_errors=True)
        os.replace(old, root / RUNTIME_DIR)
    except OSError:
        return False
    try:
        log = _log_file(root / "data", "launcher")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("检测到运行时缺失，已从 runtime.old 恢复\n")
    except OSError:
        pass
    return True


def _app_data_dir(root: Path, argv: list[str]) -> Path:
    """应用实际使用的数据目录：CLI ``--data-dir`` > 入口配置 > ``<入口>/data``。"""
    arg = _opt(argv, "--data-dir", "")
    if arg:
        path = Path(os.path.expandvars(os.path.expanduser(arg)))
        # 与 backend/paths.py 一致：命令行相对路径按**当前工作目录**解析（应用继承启动器的 cwd）。
        return path if path.is_absolute() else Path.cwd() / path
    cfg = _read_json(Path(root) / "keyrealm.conf.json")
    value = str(cfg.get("data_dir") or "").strip()
    if value:
        path = Path(os.path.expandvars(os.path.expanduser(value)))
        return path if path.is_absolute() else Path(root) / path
    return Path(root) / "data"


def _materialize_symlinks(root: Path) -> None:
    """把「兼容旧更新器」而按普通文件存储的符号链接还原成链接。

    旧版 1.4.7 的更新器不认识 zip 符号链接，会把链接目标当文本写成普通文件（Qt 的
    `.so` 版本链接因此变成坏文件）。所以 1.5+ 的发行包把链接**按目标内容**存一份普通
    文件，同时在 ``.update-meta/symlinks.json`` 留清单；新版启动器装好/启动时据此还原，
    收回重复的磁盘占用。只处理 ``runtime/`` 内、且目标仍在 runtime 内的条目。
    """
    root = Path(root)
    manifest = root / ".update-meta" / "symlinks.json"
    if not manifest.is_file():
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(data, dict):
        return

    runtime_root = (root / RUNTIME_DIR).resolve()
    failed = False
    for rel, target in data.items():
        rel_path = Path(str(rel))
        target_text = str(target or "")
        target_path = Path(target_text)
        if not rel_path.parts or rel_path.is_absolute() or ".." in rel_path.parts:
            continue
        if rel_path.parts[0] != RUNTIME_DIR or not target_text or target_path.is_absolute():
            continue
        path = root / rel_path
        tmp = path.with_name(path.name + ".keyrealm-link")
        try:
            if path.is_symlink():
                continue
            if path.exists() and not path.is_file():
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                resolved.relative_to(runtime_root)
            except ValueError:
                continue
            if not resolved.exists():
                continue
            try:
                tmp.unlink()
            except OSError:
                pass
            os.symlink(target_text, tmp)     # 先建临时链接，再原子替换，失败不丢文件
            os.replace(tmp, path)
        except OSError:
            failed = True
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
    if not failed:
        try:
            manifest.unlink()
        except OSError:
            pass


def run_app(root: Path, argv: list[str]) -> int:
    """启动 runtime 里的应用，透传退出码。"""
    root = Path(root)
    target = app_exe(root)
    if not target.is_file() and _recover_runtime(root):
        target = app_exe(root)
    _attach_console_if_needed(argv)
    if not target.is_file():
        _fatal(root, f"找不到应用程序：{target}\n请重新解压完整的发行包（不要单独移动文件）。")
        return 1

    _materialize_symlinks(root)
    started = time.monotonic()
    try:
        # **不强制 cwd**：CLI 的相对路径（--data-dir、export/import 的文件名）必须按
        # 用户当前工作目录解析；程序目录由 KEYREALM_APP_DIR 传给应用。
        proc = subprocess.Popen([str(target), *argv], env=_child_env(root))
    except OSError as exc:
        _fatal(root, f"无法启动应用程序：{exc}")
        return 1

    _cleanup_old_after_start(root, proc, argv)

    # POSIX：Ctrl+C 由进程组里的应用处理，启动器只负责等待，别自己中断退出。
    old_handler = None
    if os.name != "nt":
        old_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    rc = 0
    try:
        while True:
            try:
                rc = proc.wait()
                break
            except KeyboardInterrupt:
                # Windows：控制台事件会先送到启动器；继续等应用自行收尾。
                continue
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)

    # 桌面 GUI 刚启动就非 0 退出：多半是新运行时不可用。不自动回滚（库可能已经迁移），
    # 但把回滚副本位置与恢复方式明确告诉用户。
    old_launcher = root / (app_name() + ".old")
    if (rc != 0 and time.monotonic() - started < 6.0
            and "cli" not in app_name().lower()
            and all(arg == "--gui" for arg in argv)):
        data_dir = _app_data_dir(root, argv)
        backup_hint = (
            f"1) 解压 {data_dir / 'backup'} 内最新的 keyrealm-backup-*.zip 覆盖程序目录"
            f"（若数据目录是自定义的，把备份里的 data/ 解到该目录）；\n")
        if _executable_in(root / RUNTIME_OLD):
            message = (
                f"Keyrealm 启动失败（退出码 {rc}）。\n\n"
                f"回滚到上一版本：关闭本窗口后——\n"
                f"{backup_hint}"
                f"2) 把 {root / RUNTIME_OLD} 改名为 {root / RUNTIME_DIR}；\n"
                f"3) 重新启动 Keyrealm。"
            )
        elif old_launcher.is_file():
            # 首次从 1.4.7 onefile 迁移时没有 runtime.old，旧程序被更新器改名为 <启动器>.old。
            message = (
                f"Keyrealm 启动失败（退出码 {rc}）。\n\n"
                f"回滚到上一版本：关闭本窗口后——\n"
                f"{backup_hint}"
                f"2) 把 {old_launcher} 改名为 {root / app_name()}；\n"
                f"3) 重新启动 Keyrealm。"
            )
        else:
            message = (
                f"Keyrealm 启动失败（退出码 {rc}）。\n\n"
                f"{backup_hint}"
                f"2) 重新启动 Keyrealm。"
            )
        log = _write_error_log(root, message, data_dir=data_dir)
        if log:
            message += f"\n\n日志：{log}"
        _show_error_box(message)

    # CLI 命令通常瞬间退出，上面的后台线程可能来不及清理就被进程退出杀掉；
    # 退出码 0 已能证明新运行时可用，这里同步清理（桌面版仍交给后台线程）。
    if rc == 0 and _one_shot_argv(argv) and not _trivial_argv(argv):
        _remove_old_files(root / RUNTIME_OLD, root / (app_name() + ".old"))
    return rc


# ── 更新安装 ──────────────────────────────────────────────

REQUEST_TTL_SECONDS = 24 * 3600


def _request_fresh(request: dict) -> bool:
    """更新请求是否仍在有效期内（与 backend/updater.py 的 24h 保持一致）。"""
    try:
        stamp = datetime.fromisoformat(str(request.get("requested_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() <= REQUEST_TTL_SECONDS


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _write_status(data_dir: Path, payload: dict) -> None:
    path = Path(data_dir) / ".update-status.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _fail_status(root: Path, data_dir: Path, kind: str, message: str,
                 *, log_path: Optional[Path] = None) -> None:
    """写一条**最终失败**状态（带 finished_at / log，界面「上次更新」才会显示并通知）。"""
    try:
        (Path(data_dir) / ".update-request.json").unlink()
    except OSError:
        pass
    launcher_log = _write_error_log(root, message, data_dir=data_dir)
    _write_status(data_dir, {
        "phase": "failed", "ok": False, "kind": kind, "message": message,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # 收尾进程的输出日志比启动器的一行错误更有用（若有）。
        "log": str(log_path or launcher_log or ""),
    })
    _show_error_box(message)


def _opt(argv: list[str], name: str, default: str = "") -> str:
    for index, item in enumerate(argv):
        if item == name:
            if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                return argv[index + 1]
            return default
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def _unsafe_zip_name(name: str) -> bool:
    """zip 条目名是否可能逃出解压目录。

    同时按 POSIX 与 Windows 规则判断：Windows 上 ``C:evil``（驱动器相对路径）既不是
    绝对路径、也不含 ``..``，仅靠 ``Path`` 会漏掉。
    """
    if not name or name.endswith("/"):
        return True
    win, posix = PureWindowsPath(name), Path(name)
    return (bool(win.drive) or win.is_absolute() or posix.is_absolute()
            or ".." in win.parts or ".." in posix.parts)


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """解压发布包到 ``dest``：剥掉唯一顶层目录，跳过用户 ``data/``。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        tops = set()
        for info in infos:
            raw = info.filename
            if _unsafe_zip_name(raw):
                continue                  # 恶意条目不参与顶层目录判断
            if "/" in raw:
                tops.add(raw.split("/", 1)[0])
        strip = (next(iter(tops)) + "/") if len(tops) == 1 else ""
        for info in infos:
            name = info.filename
            if strip and name.startswith(strip):
                name = name[len(strip):]
            if not name or name.endswith("/"):
                continue
            parts = Path(name).parts
            if not parts or _unsafe_zip_name(name) or parts[0] == "data":
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            raw_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(raw_mode):
                # 包里的符号链接（Qt 的 .so 版本链接等）：只允许落在解压目录内。
                link = zf.read(info).decode("utf-8", "replace")
                link_path = Path(link)
                if not link or link_path.is_absolute() or PureWindowsPath(link).drive:
                    continue
                try:
                    (target.parent / link_path).resolve().relative_to(dest.resolve())
                except (OSError, ValueError):
                    continue
                try:
                    if target.is_symlink() or target.exists():
                        target.unlink()
                    os.symlink(link, target)
                except (OSError, NotImplementedError):
                    pass
                continue
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # Windows 的 zip 权限位没有意义，乱设可能把文件标成只读；只在 POSIX 还原。
            mode = raw_mode & 0o777
            if mode and os.name != "nt":
                try:
                    os.chmod(target, mode)
                except OSError:
                    pass


def install_bundle(src: Path, root: Path, *, running_exe: Optional[Path] = None) -> None:
    """把 ``src``（含 ``runtime/``）安装到 ``root``，替换整个运行时目录。

    调用方必须保证应用已经退出：Windows 下用中的 DLL 不能被覆盖，而重命名一个
    已退出进程的目录是安全的。用户内容（``plugins/`` / ``data/`` / 配置文件）不受影响。
    """
    src, root = Path(src), Path(root)
    src_runtime = src / RUNTIME_DIR
    # 包里的应用名以包内 runtime/ 为准：启动器被改名后，包里的可执行文件仍是原名。
    bundle_app = _executable_in(src_runtime)
    if not bundle_app:
        raise RuntimeError("更新包缺少 runtime/ 或在其中找不到应用程序")

    # 启动器自身也可能随包更新；运行中的 exe 不能原地覆盖，先改名腾位再放新的。
    live_exe = Path(running_exe) if running_exe else root / app_name()

    # 1) 插件等用户内容先合并（`.update-meta` 例外：它是给新 runtime 用的链接清单，
    #    必须等 runtime 换成功后再落盘，否则新清单会被套到旧 runtime 上）。
    for entry in src.iterdir():
        if entry.name in {RUNTIME_DIR, "data", ".update-meta"}:
            continue
        target = root / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        elif entry.name == bundle_app:
            continue                      # 启动器单独换（支持用户改名）
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)

    # 2) 启动器先写 `.new`（不动现场）；copy2 直接写主文件时中途断电会留下截断的启动器。
    bundle_launcher = src / bundle_app
    staged: Optional[Path] = None
    if bundle_launcher.is_file():
        staged = live_exe.with_name(live_exe.name + ".new")
        try:
            staged.unlink()
        except OSError:
            pass
        try:
            shutil.copy2(bundle_launcher, staged)
        except OSError:
            try:
                staged.unlink()
            except OSError:
                pass
            raise

    # 3) 提交点：整体替换 runtime。失败时现场完全不动。
    old = root / RUNTIME_OLD
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    live = root / RUNTIME_DIR
    moved = False
    if live.exists():
        try:
            os.replace(live, old)
            moved = True
        except OSError:
            if staged is not None:
                try:
                    staged.unlink()
                except OSError:
                    pass
            raise
    try:
        os.replace(src_runtime, live)
    except OSError:
        if moved:
            try:
                os.replace(old, live)
            except OSError:
                pass
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                pass
        raise

    # 4) 换启动器（原子 rename）。失败则把 runtime 一起回滚，避免「新 runtime + 旧启动器」。
    aside: Optional[Path] = None
    if staged is not None:
        if live_exe.is_file():
            aside = live_exe.with_name(live_exe.name + ".old")
            try:
                aside.unlink()
            except OSError:
                pass
            try:
                os.replace(live_exe, aside)
            except OSError:
                aside = None
        try:
            os.replace(staged, live_exe)
        except OSError:
            if aside is not None:
                try:
                    os.replace(aside, live_exe)
                except OSError:
                    pass
            failed = root / "runtime.bad"
            shutil.rmtree(failed, ignore_errors=True)
            try:
                os.replace(live, failed)
                try:
                    os.replace(old, live)
                except OSError:
                    os.replace(failed, live)   # 回滚失败：至少把新 runtime 放回来
                    raise
                shutil.rmtree(failed, ignore_errors=True)
            except OSError:
                pass
            raise

    # 5) 新 runtime 已就位：落新的链接清单。旧清单先删——复制失败时宁可不物化，
    #    也不能拿旧清单作用到新运行时上（链接目标可能已经不存在）。
    meta_target = root / ".update-meta"
    try:
        (meta_target / "symlinks.json").unlink()
    except OSError:
        pass
    try:
        if (src / ".update-meta").is_dir():
            shutil.copytree(src / ".update-meta", meta_target, dirs_exist_ok=True)
    except OSError as exc:
        print(f"更新元数据复制失败（不影响运行）：{exc}", file=sys.stderr, flush=True)

    if os.name != "nt":
        for target in (live_exe, live / bundle_app):
            try:
                target.chmod(target.stat().st_mode | 0o111)
            except OSError:
                pass
    # 旧运行时（runtime.old）**先留着**：新应用正常跑起来后由启动器删除。启动失败时它是最直接的
    # 回滚副本（新运行时已在运行，不能删除自己所在的目录，但可以改名回来）。
    _materialize_symlinks(root)


def spawn_finish(root: Path, data_dir: Path, kind: str) -> tuple[subprocess.Popen, Path]:
    """让**新**运行时收尾：迁移数据库、写状态（**不重启、不抢锁**）。

    启动器正持有单实例锁，并且会在收尾成功、**释放锁之后**自己重启应用——这样
    「应用已退出 → 安装 → 迁移 → 重启」全程都有锁，不会有第二个实例插进来。
    返回 (进程句柄, 输出日志路径)。
    """
    root, data_dir = Path(root), Path(data_dir)
    exe = app_exe(root)
    log = _log_file(data_dir, "update-install")
    argv = [str(exe), "--self-update", "finish", kind,
            "--no-lock", "--no-restart", "--data-dir", str(data_dir)]
    sink = open(log, "ab")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(root), env=_child_env(root),
            stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
            **_detached_kwargs(),
        )
    finally:
        sink.close()
    return proc, log


def _restored_proxy_env() -> dict:
    """重启新应用时把「更新代理」还原成更新前的值（没有记录就不动环境）。"""
    raw = os.environ.get(ENV_RESTORE_PROXY, "").strip()
    if not raw:
        return {}
    try:
        saved = json.loads(raw)
    except ValueError:
        return {}
    env = {key: str(saved.get(key) or "") for key in _PROXY_ENV_KEYS}
    env[ENV_RESTORE_PROXY] = ""      # 私有变量不往新应用里带
    return env


def _relaunch_app(root: Path, data_dir: Path, request: dict) -> bool:
    """收尾成功后重启应用（此时启动器已释放单实例锁）。"""
    root, data_dir = Path(root), Path(data_dir)
    exe = root / app_name()
    if not exe.is_file():
        return False
    relaunch = request.get("relaunch") if isinstance(request, dict) else None
    argv = [str(arg) for arg in ((relaunch or {}).get("argv") or [])]
    env = {**_child_env(root), **_restored_proxy_env()}
    try:
        log = _log_file(data_dir, "relaunch")
        sink = open(log, "ab")
    except OSError:
        return False
    try:
        subprocess.Popen(
            [str(exe), *argv], cwd=str(root), env=env,
            stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
            **_detached_kwargs(),
        )
    except OSError:
        return False
    finally:
        sink.close()
    return True


def apply_update(root: Path, argv: list[str]) -> int:
    """``--apply-update`` 模式：等应用退出 → 安装新运行时 → 新运行时收尾 → 重启。"""
    root = Path(root)
    kind = _opt(argv, "--apply-update", "formal") or "formal"
    data_dir = _app_data_dir(root, argv).resolve()
    request = _read_json(data_dir / ".update-request.json")
    if request and not _request_fresh(request):
        request = {}                       # 陈旧请求不得影响本次安装的渠道 / 重启参数
    prepared = _read_json(data_dir / ".update-prepared.json")
    request_kind = str(request.get("kind") or "")
    if request_kind in ("formal", "test"):
        kind = request_kind
    restart = bool(request.get("restart", True))

    # 抢应用的单实例锁：拿到 = 应用已退出，而且更新/迁移/重启期间没人能再启动实例。
    _write_status(data_dir, {"phase": "waiting", "ok": None, "kind": kind,
                             "message": "等待应用退出…"})
    if not _wait_for_update_lock(root):
        _fail_status(root, data_dir, kind,
                     "无法取得单实例锁（应用未退出，或数据目录不可写），更新已中止（未改动任何文件）")
        return 1

    ok = False
    try:
        src: Optional[Path] = None
        asset: Optional[Path] = None
        extracted = False
        try:
            if kind == "formal":
                asset = Path(str(prepared.get("asset_path") or ""))
                if not asset.is_file():
                    raise RuntimeError("找不到已下载的更新包")
                want = str(prepared.get("asset_sha256") or "")
                if want and _sha256(asset) != want:
                    raise RuntimeError("更新包校验失败（SHA-256 不匹配）")
                _write_status(data_dir, {"phase": "extract", "ok": None, "kind": kind,
                                         "message": "解压更新包…"})
                src = root / UPDATE_NEW
                shutil.rmtree(src, ignore_errors=True)
                _extract_zip(asset, src)
                extracted = True
            else:
                src = Path(str(prepared.get("staging_dir") or ""))
                if not src.is_dir():
                    raise RuntimeError("找不到已构建的更新产物")
                _write_status(data_dir, {"phase": "install", "ok": None, "kind": kind,
                                         "message": "安装新版本…"})

            install_bundle(src, root, running_exe=root / app_name())
            if extracted:
                shutil.rmtree(root / UPDATE_NEW, ignore_errors=True)
            elif src is not None and src.parent == root:
                shutil.rmtree(src, ignore_errors=True)      # 测试版：构建落点 .update-staged/
            if asset is not None:
                try:
                    asset.unlink()
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001 - 交棒进程只能靠状态文件报告
            if extracted:
                shutil.rmtree(root / UPDATE_NEW, ignore_errors=True)
            _fail_status(root, data_dir, kind, f"安装失败：{exc}")
            return 1

        # 先写「收尾中」再起进程，避免收尾很快完成时被这里的状态覆盖回 migrate。
        _write_status(data_dir, {"phase": "migrate", "ok": None, "kind": kind,
                                 "message": "安装完成，正在迁移…"})
        try:
            proc, finish_log = spawn_finish(root, data_dir, kind)
        except Exception as exc:  # noqa: BLE001 - 文件已装好，但收尾进程没起来
            _fail_status(root, data_dir, kind,
                         f"新版本已安装，但收尾进程启动失败：{exc}。请手动重新启动程序，"
                         "数据库迁移会在解锁时自动补齐。")
            return 1
        print(f"更新已安装，收尾进程 pid={proc.pid}", flush=True)

        # 等收尾进程退出：它正常退出即完成；异常退出且自己没来得及写状态时，这里补一条失败，
        # 避免界面只看到永远停在「迁移中」。
        while True:
            try:
                rc = proc.wait(timeout=MAX_FINISH_SECONDS)
                break
            except subprocess.TimeoutExpired:
                # 收尾进程仍在迁移：必须继续持锁等待。先释放会让新的 GUI/CLI 实例
                # 在迁移中途启动并并发操作同一个数据库。
                print("更新收尾仍在进行，继续等待…", flush=True)
                if not _read_json(data_dir / ".update-status.json").get("finished_at"):
                    _write_status(data_dir, {"phase": "migrate", "ok": None, "kind": kind,
                                             "message": "数据库迁移仍在进行…"})
        if rc != 0:
            status = _read_json(data_dir / ".update-status.json")
            if not status.get("finished_at"):
                _fail_status(root, data_dir, kind,
                             f"更新收尾进程异常退出（退出码 {rc}），请查看日志；"
                             "数据库迁移会在下次解锁时自动补齐。", log_path=finish_log)
            return 1
        ok = True
    finally:
        # 重启新应用前必须放锁（它要自己拿这把锁）。
        _release_update_lock()

    if ok and restart and not _relaunch_app(root, data_dir, request):
        message = "更新已完成，但自动重启失败；请手动启动 Keyrealm。"
        _write_error_log(root, message, data_dir=data_dir)
        _show_error_box(message)
    return 0 if ok else 1


# ── 错误提示 ──────────────────────────────────────────────

def _write_error_log(root: Path, message: str, *, data_dir: Optional[Path] = None) -> Optional[Path]:
    """把错误写到 stderr 与日志，返回日志路径（写失败返回 None）。"""
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        log = _log_file(Path(data_dir) if data_dir else Path(root) / "data", "launcher")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        return log
    except Exception:  # noqa: BLE001
        return None


def _show_error_box(message: str) -> None:
    """windowed 启动器没有控制台，用系统弹窗提示（CLI 版 / 有控制台时不弹）。"""
    if os.name != "nt":
        # Linux / macOS 没有统一的 MessageBox：优先桌面通知，失败就算了（日志里仍有全文）。
        if "cli" in app_name().lower():
            return
        cmds = []
        if sys.platform == "darwin":
            cmds.append(["osascript", "-e",
                         f'display alert "Keyrealm" message {json.dumps(message)}'])
        elif sys.platform.startswith("linux"):
            cmds.append(["notify-send", "Keyrealm", message])
            cmds.append(["zenity", "--error", f"--text={message}"])
        for cmd in cmds:
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **_detached_kwargs())
                return
            except OSError:
                continue
        return
    if "cli" in app_name().lower():
        return                            # CLI 版：错误已在日志 / 终端里，不弹窗打断脚本
    try:
        if ctypes.windll.kernel32.GetConsoleWindow():
            return                        # 有控制台：错误已经打到 stderr / 日志
    except Exception:  # noqa: BLE001
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "Keyrealm", 0x10)
    except Exception:  # noqa: BLE001
        pass


def _fatal(root: Path, message: str) -> None:
    """记录错误并（windowed 时）弹窗。"""
    _write_error_log(root, message)
    _show_error_box(message)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = root_dir()
    if "--apply-update" in args:
        return apply_update(root, args)
    return run_app(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
