"""开机自启动：直接读写**操作系统**的自启条目。

设计取舍
--------
"是否开机自启"的**唯一事实来源是操作系统的自启配置本身**（Windows 注册表 Run 项 /
Linux XDG autostart / macOS LaunchAgent）。若再往 settings 表存一份镜像，用户手动删掉
系统条目后两边就会长期不一致，且没有任何时机能可靠地重新同步。因此本模块**只碰系统侧**，
``is_enabled()`` 每次都现读，``set_enabled()`` 直接增删条目。

各平台落点
----------
- Windows：``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``（仅当前用户，
  无需管理员权限）。
- Linux：``$XDG_CONFIG_HOME/autostart/keyrealm.desktop``（XDG 桌面规范，
  GNOME / KDE / XFCE 等均识别）。
- macOS：``~/Library/LaunchAgents/com.keyrealm.app.plist``。

启动命令由 :func:`launch_argv` 按**冻结与否**推导，不写死任何本机路径。
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .paths import app_dir, is_frozen

_log = logging.getLogger(__name__)

APP_ID = "Keyrealm"                           # Windows 注册表值名 / 通用标识
APP_NAME = "Keyrealm"
LINUX_DESKTOP_FILE = "keyrealm.desktop"
# 写进 .desktop 的归属标记：关闭时据此确认「这个文件是本程序建的」再删。
LINUX_MARKER = "# Managed by Keyrealm"
MACOS_LABEL = "com.keyrealm.app"

# ── 改名（Credential Vault → Keyrealm）留下的旧自启条目 ─────────────────
# 旧条目按旧名字写在系统里，新名字**读不到**它们。不处理的话症状是：开关显示「已关闭」，
# 系统里却仍留着一条指向旧 exe 的条目——开机会弹一次「找不到文件」，而用户在界面上把开关
# 打开后变成**两条**。所以启动时迁移一次：旧条目存在 == 用户当初就是开着的，删掉旧的重建新的，
# 把「开着」这个意图保留下来（**不**改成默认关闭——那等于替用户改设置）。
#
# 只认三种「确实是我们写的」证据：注册表值名、.desktop 里的归属标记、LaunchAgent 的 Label；
# 对不上就一个字节都不动。
LEGACY_WIN_APP_ID = "CredentialVault"
LEGACY_LINUX_DESKTOP_FILE = "credential-vault.desktop"
LEGACY_LINUX_MARKER = "# Managed by Credential Vault"
LEGACY_MACOS_LABEL = "com.credentialvault.app"


class AutostartError(RuntimeError):
    """自启条目读写失败（权限不足、路径不可写、文件非本程序所有等）。"""


def launch_argv() -> list[str]:
    """推导开机启动时执行的命令行。

    - **冻结**（启动器 + onedir）：优先执行**入口目录的启动器**（应用自己跑在 ``runtime/`` 里，
      启动器才是稳定入口；用户改过名时用 ``KEYREALM_LAUNCHER``）；
    - **源码运行**：``<python> <项目根>/main.py --gui``（源码模式必须显式指定 --gui）。

    末尾固定带 ``--autostart``：桌面端据此判断「这次是开机自启」，再结合
    「关闭主窗口时」的默认行为决定是否静默驻留托盘。
    """
    if is_frozen():
        override = os.environ.get("KEYREALM_LAUNCHER", "").strip()
        if override:
            try:
                if Path(override).is_file():
                    return [override, "--autostart"]
            except OSError:
                pass
        candidate = app_dir() / Path(sys.executable).name
        if (app_dir() / "runtime").is_dir() and candidate.is_file():
            return [str(candidate), "--autostart"]
        return [sys.executable, "--autostart"]
    return [sys.executable, str(app_dir() / "main.py"), "--gui", "--autostart"]


def platform_name() -> str:
    """当前自启实现所对应的平台标识（供界面显示）。"""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ── Windows：HKCU\\...\\Run ──────────────────────────────────────────

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_enabled() -> bool:
    import winreg  # noqa: PLC0415 - 仅 Windows 可用

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_ID)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # 权限异常等一律按「读不到」处理，但留痕
        _log.debug("读取 Windows 自启项失败：%s", exc)
        return False


def _win_set(enabled: bool) -> None:
    import winreg  # noqa: PLC0415

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                # list2cmdline 给出 Windows 命令行所需的标准加引号形式
                winreg.SetValueEx(
                    key, APP_ID, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_argv())
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass  # 本来就没有 → 目标状态已达成
    except OSError as exc:
        raise AutostartError(f"写入注册表失败：{exc}") from exc


# ── Linux：XDG autostart ────────────────────────────────────────────


def _linux_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / LINUX_DESKTOP_FILE


def _desktop_exec(argv: list[str]) -> str:
    """按 Desktop Entry 规范拼 Exec 值。

    该值**不经过 shell**，所以无需 shell 转义；规范要求对 ``"`` `` ` `` ``$`` ``\\``
    加反斜杠转义，含空白或上述字符的参数用双引号包裹。字面量 ``%`` 须写成 ``%%``，
    否则会被当成字段码。
    """
    parts = []
    for arg in argv:
        if arg and not any(c in arg for c in ' \t\n"\'`$\\'):
            parts.append(arg)
            continue
        escaped = (
            arg.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts).replace("%", "%%")


def _linux_enabled() -> bool:
    return _linux_path().is_file()


def _linux_set(enabled: bool) -> None:
    path = _linux_path()
    if not enabled:
        if not path.exists():
            return
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AutostartError(f"读取 {path} 失败：{exc}") from exc
        if LINUX_MARKER not in head:
            # 同名文件但不是本程序写的 → 拒绝删除，交回用户处理
            raise AutostartError(f"{path} 不是本程序创建，拒绝删除；请手动处理")
        try:
            path.unlink()
        except OSError as exc:
            raise AutostartError(f"删除 {path} 失败：{exc}") from exc
        return

    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=本地凭据保险库\n"
        f"Exec={_desktop_exec(launch_argv())}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"{LINUX_MARKER}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise AutostartError(f"写入 {path} 失败：{exc}") from exc


# ── macOS：LaunchAgent ──────────────────────────────────────────────


def _macos_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def _macos_enabled() -> bool:
    return _macos_path().is_file()


def _macos_set(enabled: bool) -> None:
    path = _macos_path()
    if not enabled:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AutostartError(f"删除 {path} 失败：{exc}") from exc
        return
    payload = {
        "Label": MACOS_LABEL,
        "ProgramArguments": launch_argv(),
        "RunAtLoad": True,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(payload))
    except OSError as exc:
        raise AutostartError(f"写入 {path} 失败：{exc}") from exc


# ── 旧条目迁移（改名用，幂等）─────────────────────────────────────

def _win_migrate_legacy() -> bool:
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.QueryValueEx(key, LEGACY_WIN_APP_ID)
            except FileNotFoundError:
                return False                      # 没有旧条目：正常路径，不是错误
            try:
                winreg.DeleteValue(key, LEGACY_WIN_APP_ID)
            except FileNotFoundError:
                pass
            winreg.SetValueEx(
                key, APP_ID, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_argv())
            )
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("迁移旧的 Windows 自启项失败：%s", exc)
        return False


def _linux_legacy_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / LEGACY_LINUX_DESKTOP_FILE


def _linux_migrate_legacy() -> bool:
    old = _linux_legacy_path()
    if not old.is_file():
        return False
    try:
        if LEGACY_LINUX_MARKER not in old.read_text(encoding="utf-8", errors="replace"):
            # 同名但不是本程序写的 → 一个字节都不动
            _log.warning("%s 缺少归属标记，按「不是本程序创建」处理，未改动", old)
            return False
        old.unlink()
    except OSError as exc:
        _log.warning("删除旧的自启文件 %s 失败：%s", old, exc)
        return False
    try:
        _linux_set(True)
    except AutostartError as exc:
        _log.warning("按新名字重建自启文件失败：%s", exc)
        return False
    return True


def _macos_legacy_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_MACOS_LABEL}.plist"


def _macos_migrate_legacy() -> bool:
    old = _macos_legacy_path()
    if not old.is_file():
        return False
    try:
        payload = plistlib.loads(old.read_bytes())
    except Exception as exc:  # noqa: BLE001 - 损坏就当作不是我们的
        _log.warning("解析 %s 失败，未改动：%s", old, exc)
        return False
    if not isinstance(payload, dict) or payload.get("Label") != LEGACY_MACOS_LABEL:
        _log.warning("%s 的 Label 不是本程序，未改动", old)
        return False
    try:
        old.unlink()
    except OSError as exc:
        _log.warning("删除旧的 LaunchAgent %s 失败：%s", old, exc)
        return False
    try:
        _macos_set(True)
    except AutostartError as exc:
        _log.warning("按新名字重建 LaunchAgent 失败：%s", exc)
        return False
    return True


def migrate_legacy() -> bool:
    """迁移改名前的旧自启条目（缘由见上方 ``LEGACY_*`` 的说明）。返回是否发生了迁移。

    **幂等**：没有旧条目时立刻返回 ``False``，不做任何写入。应用启动时调一次即可。
    """
    if sys.platform == "win32":
        return _win_migrate_legacy()
    if sys.platform == "darwin":
        return _macos_migrate_legacy()
    if os.name == "posix":
        return _linux_migrate_legacy()
    return False


# ── 对外接口 ───────────────────────────────────────────────────────

def is_supported() -> bool:
    """当前平台是否实现了自启（三大桌面平台之外一律不支持）。"""
    return sys.platform in ("win32", "darwin") or os.name == "posix"


def is_enabled() -> bool:
    """现读系统侧的自启状态。读取失败按「未启用」处理并留痕。"""
    try:
        if sys.platform == "win32":
            return _win_enabled()
        if sys.platform == "darwin":
            return _macos_enabled()
        return _linux_enabled()
    except Exception as exc:  # noqa: BLE001 - 只读探测不该把界面打挂
        _log.debug("读取自启状态失败：%s", exc)
        return False


def set_enabled(enabled: bool) -> None:
    """开启 / 关闭自启。失败抛 :class:`AutostartError`（消息可直接给用户看）。"""
    if sys.platform == "win32":
        _win_set(bool(enabled))
    elif sys.platform == "darwin":
        _macos_set(bool(enabled))
    else:
        _linux_set(bool(enabled))
    _log.info("开机自启动已%s", "开启" if enabled else "关闭")


def ensure_current() -> bool:
    """启动时把已存在的自启条目刷新成当前命令行（补上 ``--autostart`` 等新参数）。

    旧版本写入的条目没有这个参数；不刷新的话，升级后第一次开机仍会弹出主窗口。
    未启用 / 条目不属于本程序时静默跳过。
    """
    if not is_enabled():
        return False
    try:
        set_enabled(True)
        return True
    except AutostartError as exc:
        _log.debug("刷新自启条目失败：%s", exc)
        return False


def describe_target() -> str:
    """自启条的落点描述（供界面/日志展示，便于用户自行核查）。"""
    if sys.platform == "win32":
        return f"HKEY_CURRENT_USER\\{_WIN_RUN_KEY}"
    if sys.platform == "darwin":
        return str(_macos_path())
    return str(_linux_path())


def status() -> dict:
    """界面用的一份完整状态。"""
    return {
        "supported": is_supported(),
        "enabled": is_enabled(),
        "platform": platform_name(),
        "target": describe_target(),
    }
