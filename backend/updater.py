"""程序内更新（从源码编译）—— 检查 / 准备 / 交棒（构建·迁移·替换）/ 重启的**唯一实现**。

设计要点
--------
- **唯一实现，双入口复用**：本模块既被应用内（`backend/api.py` 转发）调用，也被
  `scripts/{build_update,db_update,restart}.py` 等 CLI 复用（它们只做 argparse → 转发到这里）。
  逻辑只写一份。（`check_version.py` 是纯 CLI 检查入口——「检查更新」已由应用内承担，
  故**只剩它一个 .py**，不再配 `.sh`/`.bat` 薄引导。）
- **放 `backend/` 而不是 `scripts/`**：冻结的 exe 只打包 `main.py` 的 import 图（`backend/` +
  `frontend/dist` + `assets`），`scripts/` 不进 exe —— 应用内的 `check_update()` 必须在 exe 内
  可调用。本模块只依赖标准库级内部模块（`paths`/`version`/`database`/`single_instance`），
  因此**任何 Python 3.11+ 都能 import 它**（含 venv 尚未创建时）。
- **只读检查不碰工作区**：`check_update()` 用 `git clone --depth 1` 到临时目录读远端版本，
  用完即删；真正前进工作区的是 `fetch_latest()`（`git fetch --depth 1` + `merge --ff-only`）。
- **两段式：准备（应用内）+ 交棒（脱离进程）**：
  - `prepare_update()` 在**应用进程内**跑（程序不退出、可报进度、可取消）——
    备份 + 下载预编译包 / 拉源码 + 装依赖 + 构建到 `staging_dir`；结果写 `prepared_file`。
  - `begin_handoff()` 再写请求文件、起脱离进程、延时关应用；脱离进程跑 `apply_update()`——
    用准备产物**走快路径**（解压替换 / 安装 staging → 迁移 → 重启），**无产物则回落完整流程**
    （`scripts/build_update.py` CLI 走的正是这条）。
  - **交棒顺序不可颠倒**：必须**先把更新进程起起来、再退出应用**。更新进程第一件事是
    `wait_for_app_exit()`（借单实例锁判断应用是否已退）。
  - **构建 / 迁移 / 替换必须留在交棒阶段**：`build.py` 会覆盖运行中的可执行文件、
    `run_migrations` 带单实例锁，两者都要求应用已退出。

两档更新方式**不同**：
- **正式版（formal）**：对比 **GitHub Release 的最新正式版标签**（`vX.Y.Z`），有新版就**下载对应
  平台的预编译包**（`Keyrealm-<版本>-<平台>.zip`）解压替换当前程序、迁移数据库、重启——**不编译**。
- **测试版（test）**：跟到 `main` 最新提交（版本号 **或** commit 任一不同即算有更新），
  **从源码构建**（本机没有源码会先拉一份到程序目录），再迁移、重启。
"""
from __future__ import annotations

import ast
import base64
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import database as db
from . import logger
from . import single_instance
from . import version
from .paths import app_dir, is_frozen

_log = logger.get_logger(__name__)

REPO_URL = "https://github.com/YZBRH/keyrealm.git"
REPO_SLUG = "YZBRH/keyrealm"
DEFAULT_BRANCH = "main"
CLONE_DEPTH = 1
CHANNELS = ("formal", "test")

# 打包后的 exe 是**窗口化**的（`Keyrealm.spec` 里 console=False）——此时 spawn 任何控制台子进程
# （git / pip / venv / npm）Windows 都会**弹一个黑框**出来。统一加 `CREATE_NO_WINDOW` 压掉它。
# POSIX 上取 0（`creationflags` 非零才报错，0 即默认）。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW

SETTING_CHANNEL = "update_channel"
# 启动时是否自动检查更新（缺省 = 开启，见 get_auto_check）
SETTING_AUTO_CHECK = "update_auto_check"

# 备份里默认跳过的 data/ 子目录：
# - ``logs``：可能很大，且运行中一直被写入；
# - ``installed_plugins``：旧缓存；
# - ``webview``：**WebView2 的用户数据目录**（Cache / Local Storage …）。它由**正在运行的**
#   WebView 独占，备份时一读就 `Permission denied`——而它只是可再生的缓存（localStorage 里的
#   偏好丢了也不影响凭据）。**应用内「准备阶段」做备份时它必然被锁**，故必须跳过；
# - ``backup``：更新前的备份 zip 就落在这里（``data/backup/``）——不跳过的话，每次备份都会把
#   上一份备份一起打进去，越滚越大。
_BACKUP_SKIP_DIRS = {"logs", "installed_plugins", "webview", "backup"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remote_url() -> str:
    """远端地址。环境变量 ``KEYREALM_UPDATE_REMOTE`` 可覆盖（离线自测指向本地 bare 仓库）。"""
    return os.environ.get("KEYREALM_UPDATE_REMOTE", "").strip() or REPO_URL


# 明确表示「强制直连、不要走全局默认代理」的取值
_NO_PROXY = ("direct", "none", "off", "0", "false")

# 会被代理覆盖的标准环境变量（小写 + 大写都设，覆盖 git / pip / npm 各家读法）
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


# 更新代理的三种模式（与应用内「使用代理」同一套语义）
MODE_GLOBAL = "global"    # 跟随「网络与代理」的全局默认代理
MODE_DIRECT = "direct"    # 强制直连
MODE_PROXY = "proxy"      # 指定「代理列表」里的某个代理
_MODES = (MODE_GLOBAL, MODE_DIRECT, MODE_PROXY)

SETTING_PROXY_MODE = "update_proxy_mode"
SETTING_PROXY_ID = "update_proxy_id"


def get_update_proxy() -> dict:
    """当前「更新代理」选择：``{"mode", "proxy_id", "resolved"}``。

    ``resolved`` 是解析后的实际代理地址（``""`` = 直连），供界面显示。
    """
    mode = (db.get_setting(SETTING_PROXY_MODE, "") or "").strip().lower()
    if mode not in _MODES:
        mode = MODE_GLOBAL
    pid = (db.get_setting(SETTING_PROXY_ID, "") or "").strip() if mode == MODE_PROXY else ""
    return {"mode": mode, "proxy_id": pid, "resolved": update_proxy() or ""}


def set_update_proxy(mode: str, proxy_id: str = "") -> dict:
    """设置更新走哪个代理。``mode`` ∈ global / direct / proxy（== 指定 ``proxy_id``）。"""
    mode = (mode or "").strip().lower()
    if mode not in _MODES:
        return {"ok": False, "message": f"无效的更新代理模式：{mode or '(空)'}"}
    pid = (proxy_id or "").strip()
    if mode == MODE_PROXY:
        try:
            from . import proxy_store
            if not proxy_store.get_proxy_url(pid):
                return {"ok": False, "message": "指定的代理不存在"}
        except Exception:  # noqa: BLE001
            return {"ok": False, "message": "读取代理列表失败"}
    db.set_setting(SETTING_PROXY_MODE, mode)
    db.set_setting(SETTING_PROXY_ID, pid if mode == MODE_PROXY else "")
    return {"ok": True, **get_update_proxy()}


def update_proxy() -> Optional[str]:
    """本次更新（git / pip / npm）实际走哪个代理，返回代理 URL 或 ``None``（直连）。

    优先级：
    1. 环境变量 ``KEYREALM_UPDATE_PROXY``（可设为 ``direct`` 强制直连）——CLI / 排障逃生口；
    2. 应用内「更新代理」选择（``settings.update_proxy_mode`` / ``update_proxy_id``）：
       ``global`` 跟随「网络与代理」的全局默认代理、``direct`` 直连、``proxy`` 指定某个代理。

    引用了已被删掉的代理 → 回落到直连（``proxy_store.get_proxy_url`` 返回 None）。
    """
    raw = os.environ.get("KEYREALM_UPDATE_PROXY", "").strip()
    if raw:
        return None if raw.lower() in _NO_PROXY else raw
    mode = (db.get_setting(SETTING_PROXY_MODE, "") or "").strip().lower()
    if mode not in _MODES:
        mode = MODE_GLOBAL
    try:
        from . import proxy_store
        if mode == MODE_DIRECT:
            return None
        if mode == MODE_PROXY:
            return proxy_store.get_proxy_url(db.get_setting(SETTING_PROXY_ID, "")) or None
        return proxy_store.global_url() or None
    except Exception:  # noqa: BLE001 - 取不到就当直连
        return None


def proxy_env() -> dict:
    """把本次更新该用的代理铺成一组环境变量，交给所有子进程（git / pip / npm）。

    **直连时把各变量置空串**，以压掉从 shell 继承来的同名变量——否则用户 shell 里若有
    ``http_proxy``，选了「直连」也仍会走它。``no_proxy`` 保证 localhost 不走代理。
    """
    url = update_proxy()
    env = {key: (url or "") for key in _PROXY_ENV_KEYS}
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


# ``proxy_env()`` 会把上面那组变量**覆盖**掉，交棒进程因此记不住「更新前是什么」。
# 起交棒进程时顺手把原值存进这个私有变量，重启新应用时用它还原（见 `_restored_proxy_env`）。
ENV_RESTORE_PROXY = "KEYREALM_UPDATE_RESTORE_ENV"


def _restored_proxy_env() -> Optional[dict]:
    """重启新应用时该用的代理环境变量：**还原成更新前的原值**。

    没有记录（交棒进程不是 ``_spawn_updater`` 起的，如手跑 ``--self-update``）时返回
    ``None``——此时本进程的环境就是干净的，原样继承即可，不该拿空值去覆盖它。
    """
    raw = os.environ.get(ENV_RESTORE_PROXY, "").strip()
    if not raw:
        return None
    try:
        saved = json.loads(raw)
    except ValueError:
        return None
    env = {key: str(saved.get(key) or "") for key in _PROXY_ENV_KEYS}
    env[ENV_RESTORE_PROXY] = ""      # 私有变量不往新应用里带
    return env


# GitHub 访问令牌（PAT）：**可选**——认证后 API 速率上限更高；仓库转为私有时必需。
# 取值优先级：显式入参 > 环境变量 ``ENV_TOKEN``。应用内由 ``backend/api.py`` 解密 settings 后
# 写入本进程 env（见 ``api._sync_update_token``）；四个独立脚本（``scripts/*.py``）直接读环境变量。
# 令牌**绝不**进 URL / argv（会出现在 ``ps`` 与错误信息里），只经 env 传给 git。
ENV_TOKEN = "KEYREALM_UPDATE_TOKEN"
# settings 里的落库键：值 = 主密码加密后的密文（明文绝不落库）。
SETTING_TOKEN = "update_token"


def _resolve_token(token: Optional[str] = None) -> str:
    return (token or os.environ.get(ENV_TOKEN, "") or "").strip()


def _git_auth_env(token: Optional[str] = None) -> dict:
    """有令牌时给 git 注入 HTTPS Basic 认证。

    经 **env 注入 git 配置**（``GIT_CONFIG_COUNT/KEY_0/VALUE_0``）而不是把令牌写进 URL 或 argv
    —— 后者会出现在 ``ps`` 与错误信息里。用户名固定用 GitHub 的 ``x-access-token``、密码取令牌。
    """
    tok = _resolve_token(token)
    if not tok:
        return {}
    import base64

    basic = base64.b64encode(f"x-access-token:{tok}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


# PyInstaller onefile：父进程把临时解压目录经 `_MEIPASS2` 传给子进程，子进程会**复用**同一份
# `_MEI` 而不自己解压；可父进程一退出就删掉那份目录 —— 子进程再去读 `base_library.zip` 就是
# `FileNotFoundError`。官方为此提供 `PYINSTALLER_RESET_ENVIRONMENT=1`：让子进程**忽略继承的
# `_MEIPASS2`**、解压自己的一份。我们所有「脱离启动的 PyInstaller 子进程」（交棒进程、重启起来
# 的新 exe）都必须带上它——否则就会出现「替换成功、重启却报 `_MEI...\base_library.zip` 缺失」。
_PYI_ENV = {"PYINSTALLER_RESET_ENVIRONMENT": "1"}


def _child_env(extra: Optional[dict] = None, token: Optional[str] = None) -> dict:
    """子进程环境 = 当前环境 + 隔离 PyInstaller 临时目录 + 更新代理 + 访问令牌认证（+ 额外项）。"""
    return {**os.environ, **_PYI_ENV, **proxy_env(), **_git_auth_env(token), **(extra or {})}


def _short(text: Any, limit: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


def _sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    """文件 SHA-256（分块读，避免大包整块进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ── 路径 ──────────────────────────────────────────────────

def repo_root(root: Optional[Any] = None) -> Path:
    return Path(root) if root else app_dir()


def is_source_checkout(root: Optional[Any] = None) -> bool:
    """当前是不是 git 源码仓库（仓库根有 ``.git``）。不是的话「从源码编译」无从谈起。"""
    return (repo_root(root) / ".git").is_dir()


def _data_dir(root: Optional[Any] = None) -> Path:
    return repo_root(root) / "data"


def backups_dir(root: Optional[Any] = None) -> Path:
    # 放 `data/` 下：与用户数据同处、随备份/迁移一起走，且 `data/` 本就被 gitignore 覆盖
    return repo_root(root) / "data" / "backup"


def status_file(root: Optional[Any] = None) -> Path:
    return _data_dir(root) / ".update-status.json"


def request_file(root: Optional[Any] = None) -> Path:
    return _data_dir(root) / ".update-request.json"


def prepared_file(root: Optional[Any] = None) -> Path:
    """「准备阶段」产物清单（应用内写好，交棒进程读它走快路径）。"""
    return _data_dir(root) / ".update-prepared.json"


def staging_dir(root: Optional[Any] = None) -> Path:
    """测试版构建的**落点**：不覆盖运行中的程序文件，交棒时再由 ``_install_staged`` 安装。

    **刻意不放在 ``build/`` 或 ``dist/`` 下**：那是 PyInstaller 的中间目录，``build.py`` 收尾会
    ``clean_intermediates()`` **把整个 ``build/`` 删掉**——放里面会在装配完成后连同产物一起被清掉
    （踩过：``build/staged/Keyrealm.exe`` 刚写好就被删，随后 ``exe.stat()`` 抛 FileNotFoundError）。
    """
    return repo_root(root) / ".update-staged"


def migrations_dirs(root: Optional[Any] = None) -> list:
    """迁移脚本可能来自两处：源码树 ``scripts/migrations/`` 与下载包解出的 ``.update-meta/migrations/``。"""
    root = repo_root(root)
    return [root / "scripts" / "migrations", root / ".update-meta" / "migrations"]


def _exe_name() -> str:
    return "Keyrealm.exe" if os.name == "nt" else "Keyrealm"


# ── 版本比较 ──────────────────────────────────────────────

def _parse_ver(value: Any) -> list[int]:
    parts: list[int] = []
    for chunk in str(value or "").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return parts or [0]


def _semver_gt(a: Any, b: Any) -> bool:
    """``a`` 是否严格大于 ``b``（三段数值比较，不引入 packaging 依赖）。"""
    pa, pb = _parse_ver(a), _parse_ver(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _read_version_module(text: str) -> tuple[Optional[str], Optional[str]]:
    """从 ``version.py`` 源码里取 ``APP_VERSION`` / ``SCHEMA_VERSION``（用 ast，不 eval）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, None
    app = schema = None

    def _const(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "APP_VERSION":
                app = _const(node.value)
            elif target.id == "SCHEMA_VERSION":
                schema = _const(node.value)
    return app, schema


# ── 平台 / 正式版 Release 资产 ────────────────────────────

def _machine_arch() -> str:
    """本机架构，统一成发布包里的写法：``x86_64`` / ``arm64``（其余原样返回）。"""
    m = (platform.machine() or "").lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return m or "unknown"


def _linux_flavor(arch: str | None = None) -> str:
    """Linux 上**当前程序**所对应的发布包 flavor（它自己是哪个档），与 CI 产物命名一致。

    冻结包：取构建时烘进的 ``_build_info.LINUX_FLAVOR``（``linux-general-<arch>`` 或
    ``linux-gtk-<系统>-<arch>``）；源码运行 / 该字段为空时退回 ``linux-general-<arch>``
    （源码跑的就是本机库，等同通用版）。用途是让「正式版更新」按**同一个档**升级。
    """
    try:
        from . import _build_info
        flav = str(getattr(_build_info, "LINUX_FLAVOR", "") or "").strip()
        if flav:
            return flav
    except Exception:  # noqa: BLE001 - 源码运行 / 从未构建过
        pass
    return f"linux-general-{arch or _machine_arch()}"


def platform_pkg() -> str:
    """当前平台的发布包后缀，与 CI 产物命名一致。

    Windows / macOS 用简写：``windows-x64`` / ``macos-arm64``；
    Linux 带 flavor：``linux-general-<arch>``（Qt 通用版）或 ``linux-gtk-<系统>-<arch>``（GTK 专项档）。
    """
    arch = _machine_arch()
    if os.name == "nt":
        return f"windows-{'x64' if arch == 'x86_64' else arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return _linux_flavor(arch)


def release_asset_name(ver: str) -> str:
    return f"Keyrealm-{ver}-{platform_pkg()}.zip"


def release_asset_url(ver: str) -> str:
    """按 CI 的命名约定拼出 Release 资产下载地址（无需 GitHub API，也就没有速率限制）。

    ``KEYREALM_UPDATE_ASSET_BASE`` 可覆盖基址（默认 GitHub 的 ``releases/download``），
    便于离线自测下载段（指向本地静态服务）。**私有**仓库才需要令牌走 API；公开仓库直接走公开下载 URL，见 ``download_release_asset``。
    """
    tag = ver if ver.startswith("v") else f"v{ver}"
    base = os.environ.get("KEYREALM_UPDATE_ASSET_BASE", "").strip().rstrip("/")
    if not base:
        base = f"https://github.com/{REPO_SLUG}/releases/download"
    return f"{base}/{tag}/{release_asset_name(ver)}"


def latest_release_tag(*, timeout: float = 30) -> dict:
    """取远端**最新正式版标签**（``vX.Y.Z``），走 ``git ls-remote --tags``（受更新代理管辖）。

    返回 ``{"ok": True, "version": "1.3.0", "tag": "v1.3.0"}``；无标签或失败时 ``ok=False``。
    """
    if not git_available():
        return {"ok": False, "error": "no_git", "message": "本机未安装 git，无法检查更新"}
    url = _remote_url()
    try:
        r = _git(["ls-remote", "--tags", url], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "访问远端仓库超时" + _NET_HINT}
    if r.returncode != 0:
        return {"ok": False, "error": "network",
                "message": f"无法访问远端仓库：{_short(r.stderr) or '未知错误'}" + _git_fail_hint(r.stderr)}
    versions: list[str] = []
    for line in (r.stdout or "").splitlines():
        ref = line.split("\t", 1)[-1].strip()
        if ref.endswith("^{}"):        # 标注 tag 的解引用附带行，跳过
            continue
        name = ref.rsplit("/", 1)[-1]
        if name.startswith("v") and _parse_ver(name[1:]) != [0]:
            versions.append(name[1:])
    if not versions:
        return {"ok": False, "error": "no_release",
                "message": "远端还没有正式版标签（vX.Y.Z）——可能尚未发过 Release。"}
    best = versions[0]
    for v in versions[1:]:
        if _semver_gt(v, best):
            best = v
    return {"ok": True, "version": best, "tag": f"v{best}"}


def download_release_asset(ver: str, dest: Path, *, token: Optional[str] = None,
                           progress_cb: Optional[Callable[[int, int], None]] = None,
                           should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """下载某正式版的预编译包到 ``dest``（**流式**，可报进度、可取消）。

    - **有令牌**：走 GitHub API 的资产端点——先 `/releases/tags/<tag>` 找到同名资产，
      再 `/releases/assets/<id>` 带 `Accept: application/octet-stream` 取字节（认证后不受匿名限流）。
    - **无令牌**（公开仓库）：直接用 `releases/download/<tag>/<asset>` 公开 URL（基址可经
      ``KEYREALM_UPDATE_ASSET_BASE`` 覆盖，便于离线自测）。
    两者都走「更新代理」，先写 `.part` 再原子替换。``progress_cb(done, total)`` 按块回调
    （``total`` 取 ``Content-Length``，未知为 0）；``should_cancel()`` 为真则中止并清掉 `.part`。
    返回含 ``sha256``（供交棒阶段校验产物未损坏）。
    """
    tok = _resolve_token(token)
    version = ver[1:] if str(ver).startswith("v") else str(ver)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "缺少 httpx，无法下载预编译包"}

    def _canceled() -> bool:
        return bool(should_cancel and should_cancel())

    try:
        with httpx.Client(proxy=update_proxy(),
                          timeout=httpx.Timeout(connect=15.0, read=180.0, write=180.0, pool=15.0),
                          follow_redirects=True, trust_env=False) as client:
            if tok:
                api = f"https://api.github.com/repos/{REPO_SLUG}"
                gh = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
                meta = client.get(f"{api}/releases/tags/v{version}", headers=gh)
                if meta.status_code != 200:
                    return {"ok": False,
                            "message": f"读取 Release 失败（HTTP {meta.status_code}）"
                                       f"——请确认「访问令牌」有效且能访问本仓库"}
                name = release_asset_name(version)
                asset_id = next((a.get("id") for a in (meta.json().get("assets") or [])
                                 if a.get("name") == name), None)
                if not asset_id:
                    return {"ok": False, "message": f"该 Release 里没有 {name}（该平台可能尚未发布对应包）"}
                url = f"{api}/releases/assets/{asset_id}"
                req_headers = {"Authorization": f"Bearer {tok}",
                               "Accept": "application/octet-stream"}
            else:
                url = release_asset_url(version)
                req_headers = {}

            with client.stream("GET", url, headers=req_headers) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "message": f"下载失败（HTTP {resp.status_code}）"}
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                if progress_cb:
                    progress_cb(0, total)
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(64 * 1024):
                        if _canceled():
                            return {"ok": False, "canceled": True, "message": "已取消下载"}
                        fh.write(chunk)
                        done += len(chunk)
                        if progress_cb:
                            progress_cb(done, total)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "message": f"下载失败：{type(exc).__name__}"}
    if _canceled():
        tmp.unlink(missing_ok=True)
        return {"ok": False, "canceled": True, "message": "已取消下载"}
    try:
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "message": f"写入失败：{exc}"}
    return {"ok": True, "bytes": dest.stat().st_size, "sha256": _sha256(dest)}


def update_meta_dir(root: Optional[Any] = None) -> Path:
    """下载包里的更新元数据目录（``version.json`` + ``migrations/``）。"""
    return repo_root(root) / ".update-meta"


def read_release_meta(directory: Path) -> dict:
    """读 ``.update-meta/version.json``（``{app_version, schema_version, commit}``）；不存在返回 {}。"""
    return _read_json(Path(directory) / "version.json") or {}


def _zip_schema(zip_path: Path) -> str:
    """读发布包内 ``.update-meta/version.json`` 的 ``schema_version``（**不解压**，应用内即可预览迁移目标）。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".update-meta/version.json")]
            if not names:
                return ""
            data = json.loads(zf.read(names[0]).decode("utf-8", "replace"))
            return str(data.get("schema_version") or "")
    except Exception:  # noqa: BLE001
        return ""


def extract_release_zip(zip_path: Path, app_dir: Path) -> dict:
    """把发布包解压覆盖到程序目录：**剥离顶层目录**、**跳过 ``data/``**（绝不覆盖用户数据）。

    交棒进程自己就跑在 ``<exe>`` 上，而 Windows 下**运行中的 exe 不能原地覆盖**
    （`WinError 32`，Python 里是 `PermissionError` / `Errno 13`）、**但可以改名**。所以包里
    带 exe 时先把它挪到 ``<exe>.old`` 让出路径再解压；中途失败就挪回来——宁可停在旧版本，
    也不能把程序目录弄成「没有 exe」。
    """
    app_dir = Path(app_dir)
    exe_name = _exe_name()
    aside: Optional[Path] = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            tops = {i.filename.split("/", 1)[0] for i in infos if "/" in i.filename}
            strip = (next(iter(tops)) + "/") if len(tops) == 1 else ""

            def _rel(info) -> str:
                name = info.filename
                return name[len(strip):] if strip and name.startswith(strip) else name

            names = [_rel(i) for i in infos]
            if exe_name in names:                   # 包里有 exe 才让位（没有就别动能跑的程序）
                aside = _move_exe_aside(app_dir)

            for info, name in zip(infos, names):
                if not name or name.endswith("/"):
                    continue
                if name.split("/", 1)[0] == "data":     # 用户数据目录原样保留
                    continue
                target = app_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                mode = (info.external_attr >> 16) & 0o777   # 还原可执行位（macOS/Linux）
                if mode:
                    try:
                        os.chmod(target, mode)
                    except OSError:
                        pass
    except Exception as exc:  # noqa: BLE001
        if aside is not None:                       # 让了位却没装成 → 把旧的挪回来
            try:
                os.replace(aside, app_dir / exe_name)
            except OSError:
                pass
        return {"ok": False, "message": f"解压失败：{exc}"}
    return {"ok": True}


def source_meta(root: Optional[Any] = None) -> dict:
    """读程序目录里**源码**的 ``backend/version.py``（拉取后的新值），返回 app/schema 版本。"""
    vf = repo_root(root) / "backend" / "version.py"
    if not vf.is_file():
        return {}
    app, schema = _read_version_module(vf.read_text(encoding="utf-8", errors="replace"))
    return {"app_version": app or "", "schema_version": schema or ""}


# ── 观测：本地 / 远端 ─────────────────────────────────────

def local_info(root: Optional[Any] = None) -> dict:
    root = repo_root(root)
    return {
        "root": str(root),
        "version": version.APP_VERSION,
        "commit": version.build_id(),
        "schema_version": version.SCHEMA_VERSION,
        "frozen": is_frozen(),
        "source_ok": is_source_checkout(root),
    }


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(args: list[str], cwd: Optional[Path] = None, timeout: float = 60,
         token: Optional[str] = None) -> subprocess.CompletedProcess:
    # 代理**必须经 ``-c http.proxy`` 传**：git 的 ``http.proxy`` 配置优先级**高于**
    # ``http_proxy``/``all_proxy`` 环境变量——用户全局 git 配置里若已有 ``http.proxy``，
    # 光设环境变量会被它盖掉，更新代理就形同虚设（本机就踩到过：全局配了 127.0.0.1:10809）。
    # 故这里统一注入命令级 ``-c http.proxy=<本次解析出的代理或空>``，让 git 严格按我们的解析走
    # （空值 = 强制直连，同样压过全局配置）。环境变量仍保留，供 pip / npm 等其它子进程使用。
    proxy = update_proxy()
    return subprocess.run(
        ["git", "-c", f"http.proxy={proxy or ''}", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        env=_child_env(token=token),
        creationflags=_NO_WINDOW,
    )


_NET_HINT = "；若本机访问 GitHub 需要代理，请在「设置 → 网络与代理」配置默认代理（更新会复用该代理）后重试"
# 鉴权失败时的追加提示：本仓库已公开，报鉴权错多半是「又转回私有」或令牌失效
_AUTH_HINT = "；若仓库已转为私有（或令牌失效），请到「设置 → 更新 → 访问令牌」填一个 PAT（Contents: read）后重试"


def _git_fail_hint(stderr: str) -> str:
    text = str(stderr or "")
    if "Username" in text or "Authentication" in text or "403" in text or "401" in text:
        return _AUTH_HINT
    return _NET_HINT


def remote_info(*, branch: str = DEFAULT_BRANCH, root: Optional[Any] = None,
                timeout: float = 30) -> dict:
    """只读地取远端 main 的版本号与 commit（**不改工作区**）。

    ``git ls-remote`` 拿 commit（极小请求）；需要版本号时再 ``clone --depth 1`` 到临时目录读
    ``backend/version.py``，读完删掉。任何失败都收敛成 ``{"ok": False, ...}``，不抛异常。
    """
    if not git_available():
        return {"ok": False, "error": "no_git",
                "message": "本机未安装 git，无法检查更新（请先安装 git）"}
    url = _remote_url()
    try:
        ls = _git(["ls-remote", url, f"refs/heads/{branch}"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "访问远端仓库超时（网络不可用？）" + _NET_HINT}
    if ls.returncode != 0:
        return {"ok": False, "error": "network",
                "message": f"无法访问远端仓库：{_short(ls.stderr) or '未知错误'}" + _git_fail_hint(ls.stderr)}
    line = (ls.stdout or "").strip().splitlines()
    sha = line[0].split()[0] if line and line[0].split() else ""
    commit = sha[:7]
    if not commit:
        return {"ok": False, "error": "branch", "message": f"远端没有分支 {branch}"}

    tmp = Path(tempfile.mkdtemp(prefix="keyrealm-update-"))
    try:
        cl = _git(["clone", "--depth", str(CLONE_DEPTH), "--single-branch",
                   "--branch", branch, url, str(tmp)], timeout=max(timeout, 90))
        if cl.returncode != 0:
            return {"ok": False, "error": "clone",
                    "message": f"浅克隆失败：{_short(cl.stderr) or '未知错误'}" + _git_fail_hint(cl.stderr)}
        vfile = tmp / "backend" / "version.py"
        text = vfile.read_text(encoding="utf-8", errors="replace") if vfile.is_file() else ""
        app, schema = _read_version_module(text)
        return {"ok": True, "version": app or "", "schema_version": schema or "",
                "commit": commit, "commit_full": sha, "via": "git"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "拉取远端源码超时" + _NET_HINT}
    except Exception as exc:  # noqa: BLE001 - 检查边界，绝不抛
        return {"ok": False, "error": "error", "message": f"检查更新失败：{type(exc).__name__}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare(kind: str, local: dict, remote: dict) -> dict:
    """按档位判定是否有更新。"""
    lv = str(local.get("version") or "")
    rv = str(remote.get("version") or "")
    if kind == "test":
        lc = str(local.get("commit") or "").replace("-dirty", "")
        rc = str(remote.get("commit") or "").replace("-dirty", "")
        ver_diff = rv != lv
        com_diff = bool(lc and rc and lc != rc)
        if ver_diff or com_diff:
            reason = "远端版本号不同" if ver_diff else "远端有新提交"
            return {"has_update": True, "reason": reason}
        return {"has_update": False, "reason": "版本号与提交均与远端一致"}
    # formal
    if _semver_gt(rv, lv):
        return {"has_update": True, "reason": f"远端版本更高（{lv} → {rv}）"}
    return {"has_update": False, "reason": "已是最新正式版"}


# ── 更新内容（检查到新版本时给界面展示）────────────────────
# 「检查更新」发现有新版时，顺带取回**这次更新带来什么**：
# - 正式版：目标标签上 ``CHANGELOG.md`` 里该版本的段落（本仓库的更新日志惯例）；
#   拿不到再退回该 Release 的说明正文。
# - 测试版：当前 commit → 远端 commit 之间的提交标题（GitHub compare API）。
# **取不到不致命**：只是「拿不到更新内容」，不影响检查结果本身，故整段吞异常。
_GH_API = f"https://api.github.com/repos/{REPO_SLUG}"
_NOTES_TIMEOUT = 20.0
_NOTES_MAX_COMMITS = 100     # 提交列表上限；超出只展示最近 N 条（总数照常报）


def _gh_json(path: str, *, token: str = "", timeout: float = _NOTES_TIMEOUT) -> dict:
    """GET ``api.github.com/repos/<slug><path>``（走更新代理）。失败收敛成 ``{"ok": False, ...}``。"""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "缺少 httpx"}
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "keyrealm-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(proxy=update_proxy(), timeout=timeout,
                          follow_redirects=True, trust_env=False) as client:
            resp = client.get(f"{_GH_API}{path}", headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"访问 GitHub 失败：{type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"GitHub 返回 HTTP {resp.status_code}"}
    try:
        return {"ok": True, "data": resp.json()}
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "GitHub 返回的不是 JSON"}


def _plain_inline(text: str) -> str:
    """去掉行内 markdown（链接 / 粗体 / 行内代码），留纯文本供界面直接显示。"""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # [文字](链接) → 文字
    return text.replace("**", "").replace("`", "").strip()


def _sections_from_lines(lines: list[str]) -> list[dict]:
    """把 markdown 行切成 ``[{"title", "items"}]``：``###`` 起小节、``- `` 起条目、续行并入上一条。"""
    sections: list[dict] = []

    def _ensure() -> dict:
        if not sections:
            sections.append({"title": "", "items": []})
        return sections[-1]

    cur: Optional[dict] = None
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("### "):
            cur = {"title": _plain_inline(line[4:]), "items": []}
            sections.append(cur)
            continue
        if not line.strip():
            continue
        if line.lstrip().startswith(("- ", "* ")):
            cur = _ensure() if cur is None else cur
            cur["items"].append(_plain_inline(line.lstrip()[2:]))
            continue
        # 非条目行：缩进的续行并入上一条；否则当作独立条目
        if cur and cur["items"] and raw[:1] in (" ", "\t"):
            cur["items"][-1] += " " + _plain_inline(line)
        else:
            cur = _ensure() if cur is None else cur
            cur["items"].append(_plain_inline(line))
    return [s for s in sections if s["items"] or s["title"]]


def parse_changelog_section(text: str, version: str) -> list[dict]:
    """从 CHANGELOG.md 全文里取出 ``## [<version>]`` 那一段，切成分节结构；找不到返回 ``[]``。

    标题行容忍 ``## [1.4.2] - 2026-10-15`` 与 ``## 1.4.2`` 两种写法；取到下一个 ``## `` 为止。
    """
    want = str(version).strip().lstrip("vV")
    body: list[str] = []
    inside = False
    for raw in (text or "").splitlines():
        if raw.startswith("## "):
            if inside:
                break
            m = re.match(r"^##\s*\[?v?([0-9][^\s\]]*)", raw)
            inside = bool(m) and m.group(1) == want
            continue
        if inside:
            body.append(raw)
    return _sections_from_lines(body)


def _notes_release(latest: dict, token: str) -> dict:
    """正式版：目标版本在远端 CHANGELOG 里的段落；退回该 Release 的说明正文。"""
    ver = str(latest.get("version") or "").strip()
    if not ver:
        return {"ok": False, "message": "缺少目标版本号"}
    got = _gh_json(f"/contents/CHANGELOG.md?ref=v{ver}", token=token)
    markdown = ""
    if got.get("ok"):
        raw = (got.get("data") or {}).get("content") or ""
        try:
            markdown = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            markdown = ""
    sections = parse_changelog_section(markdown, ver) if markdown else []
    if sections:
        return {"ok": True, "kind": "release", "version": ver, "sections": sections}
    rel = _gh_json(f"/releases/tags/v{ver}", token=token)
    if rel.get("ok"):
        body = str((rel.get("data") or {}).get("body") or "").strip()
        if body:
            return {"ok": True, "kind": "release", "version": ver,
                    "sections": _sections_from_lines(body.splitlines())}
    return {"ok": False, "message": f"未能取到 v{ver} 的更新说明（CHANGELOG 与 Release 说明都为空）"}


def _resolve_full_sha(short: str, token: str) -> str:
    """把短 sha 解析成全 sha。compare 端点**只认全 sha**（给 7 位短 sha 会 404）。取不到返回空串。"""
    text = str(short or "").strip()
    if len(text) >= 40:
        return text
    if not text:
        return ""
    got = _gh_json(f"/commits/{text}", token=token)
    if not got.get("ok"):
        return ""
    return str((got.get("data") or {}).get("sha") or "").strip()


def _notes_commits(current: dict, latest: dict, token: str) -> dict:
    """测试版：当前 commit → 远端 commit 之间的提交标题（GitHub compare API）。

    两边都得是**全 sha**：远端那个由 ``remote_info`` 顺带带回（``commit_full``），
    本地这个现查一次——项目各处存的是 7 位短 sha。
    """
    base_short = str(current.get("commit") or "").replace("-dirty", "").strip()
    head_short = str(latest.get("commit") or "").replace("-dirty", "").strip()
    if not base_short or not head_short:
        return {"ok": False, "message": "缺少本地 / 远端提交号，无法列出提交"}
    if base_short == head_short:
        return {"ok": True, "kind": "commits", "commits": [], "total": 0}
    base = _resolve_full_sha(base_short, token)
    if not base:
        return {"ok": False, "message": f"解析本地提交 {base_short} 失败（它可能不在远端仓库里）"}
    head = str(latest.get("commit_full") or "").strip() or _resolve_full_sha(head_short, token)
    if not head:
        return {"ok": False, "message": f"解析远端提交 {head_short} 失败"}
    if base == head:
        return {"ok": True, "kind": "commits", "commits": [], "total": 0}
    got = _gh_json(f"/compare/{base}...{head}", token=token)
    if not got.get("ok"):
        return {"ok": False, "message": got.get("message") or "取提交列表失败"}
    data = got.get("data") or {}
    commits: list[dict] = []
    for item in (data.get("commits") or []):
        message = str(((item.get("commit") or {}).get("message")) or "")
        subject = message.splitlines()[0].strip() if message else ""
        commits.append({"sha": str(item.get("sha") or "")[:7], "subject": subject})
    total = int(data.get("total_commits") or len(commits))
    # compare 是「由旧到新」，界面按「最新在前」更顺眼
    return {"ok": True, "kind": "commits", "total": total,
            "commits": list(reversed(commits[-_NOTES_MAX_COMMITS:]))}


def update_notes(kind: str, current: dict, latest: dict, *, token: Optional[str] = None) -> dict:
    """取「本次更新带来什么」的展示内容：正式版看 CHANGELOG 段落，测试版看提交列表。

    取不到返回 ``{"ok": False, "message": ...}``——**只影响展示**，不影响检查结果。
    """
    try:
        tok = _resolve_token(token)
        if kind == "test":
            return _notes_commits(current, latest, tok)
        return _notes_release(latest, tok)
    except Exception as exc:  # noqa: BLE001 - 展示用内容，绝不拖垮检查
        return {"ok": False, "message": f"获取更新内容失败：{type(exc).__name__}"}


def _attach_notes(result: dict, kind: str, local: dict) -> dict:
    """发现有新版本时挂上 ``notes``（取不到就挂 ``{"ok": False}``，不影响其余字段）。"""
    if result.get("has_update"):
        result["notes"] = update_notes(kind, local, result.get("latest") or {})
    return result


def check_update(kind: str = "formal", *, branch: str = DEFAULT_BRANCH,
                 timeout: float = 30, root: Optional[Any] = None) -> dict:
    kind = kind if kind in CHANNELS else "formal"
    local = local_info(root)
    dbst = db_status(root=root)
    proxy = update_proxy() or ""

    if kind == "formal":
        # 正式版比对「GitHub Release 的最新正式版标签」——更新时直接下载预编译包，不编译。
        rel = latest_release_tag(timeout=timeout)
        if not rel.get("ok"):
            return {"ok": False, "kind": kind, "current": local,
                    "error": rel.get("error"), "message": rel.get("message"), "proxy": proxy}
        ver = rel["version"]
        has = _semver_gt(ver, local["version"])
        reason = (f"远端正式版更高（{local['version']} → {ver}）" if has else "已是最新正式版")
        prep = prepared_ready(kind, root=root)
        # 只认与「当前最新 Release」同版本、且确实比本机新的准备产物；否则界面会出现
        # 「已是最新版本」却同时提示「本地已准备好」的自相矛盾。
        prep = prep if has and str(prep.get("version") or "") == ver else {}
        return _attach_notes({
            "ok": True, "kind": kind, "has_update": has, "reason": reason,
            "current": local,
            "latest": {"version": ver, "commit": "", "schema_version": ""},
            "asset": release_asset_name(ver), "platform": platform_pkg(),
            "prepared": prepared_info(prep),
            "db_change": bool(dbst.get("needs_migration")), "db_chain": dbst.get("chain", []),
            "source_ok": local["source_ok"], "proxy": proxy,
        }, kind, local)

    # 测试版：沿用源码口径（跟 main 的版本号 + commit），更新时从源码构建。
    remote = remote_info(branch=branch, root=root, timeout=timeout)
    if not remote.get("ok"):
        return {"ok": False, "kind": kind, "current": local,
                "error": remote.get("error"), "message": remote.get("message"), "proxy": proxy}
    cmp = compare(kind, local, remote)
    prep = prepared_ready(kind, root=root)
    # 测试版准备阶段会**先把源码拉到目标 commit**，所以下次检查时 compare 往往已经是
    # 「与远端一致」；只有准备产物仍指向远端同一提交，才把它当作「待安装的更新」。
    if prep and str(prep.get("commit") or "") != str(remote.get("commit") or ""):
        prep = {}
    has = bool(cmp["has_update"] or prep)
    reason = cmp["reason"] if cmp["has_update"] or not prep else "本地已有准备完成但未安装的更新"
    return _attach_notes({
        "ok": True, "kind": kind,
        "has_update": has, "reason": reason,
        "current": local,
        "latest": {"version": remote.get("version", ""), "commit": remote.get("commit", ""),
                   "commit_full": remote.get("commit_full", ""),
                   "schema_version": remote.get("schema_version", "")},
        "prepared": prepared_info(prep),
        "db_change": bool(dbst.get("needs_migration")), "db_chain": dbst.get("chain", []),
        "source_ok": local["source_ok"], "proxy": proxy,
    }, kind, local)


# ── 通道（正式/测试）持久化 ───────────────────────────────

def get_channel() -> str:
    value = (db.get_setting(SETTING_CHANNEL, "") or "").strip().lower()
    return value if value in CHANNELS else "formal"


def set_channel(channel: str) -> dict:
    value = (channel or "").strip().lower()
    if value not in CHANNELS:
        return {"ok": False, "message": f"无效的更新渠道：{channel}"}
    db.set_setting(SETTING_CHANNEL, value)
    return {"ok": True, "channel": value}


def get_auto_check() -> bool:
    """启动时是否**自动检查更新**。**默认开启**（缺省值按开启处理，只认显式的 "0" 为关）。"""
    return (db.get_setting(SETTING_AUTO_CHECK, "1") or "").strip() != "0"


def set_auto_check(enabled) -> dict:
    value = bool(enabled)
    db.set_setting(SETTING_AUTO_CHECK, "1" if value else "0")
    _log.info("自动检测更新已%s", "开启" if value else "关闭")
    return {"ok": True, "enabled": value}


# ── 数据库逐级迁移 ────────────────────────────────────────

def _load_module(path: Path):
    import importlib.util

    name = f"keyrealm_migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载迁移脚本：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover_migration_chain(frm: str, to: str, *, root: Optional[Any] = None,
                             dirs: Optional[list] = None) -> list[tuple]:
    """串出「frm → to」的连续迁移链，返回 ``[(from, to, path), …]``。

    只提供「低一级 → 高一级」的脚本；本函数把它们串起来，**绝不跨级**。断链时返回已找到的
    前缀（调用方据此报「缺哪一跳」）。脚本来自 ``migrations_dirs()``（源码树 + 下载包）。
    """
    directories = [Path(d) for d in (dirs if dirs is not None else migrations_dirs(root))]
    by_from: dict[str, tuple[str, Path]] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("m_*__to_*.py")):
            try:
                mod = _load_module(path)
            except Exception as exc:  # noqa: BLE001 - 坏脚本跳过，别拦整条链
                _log.warning("迁移脚本无法加载 %s：%s", path.name, exc)
                continue
            f = getattr(mod, "FROM_VERSION", "")
            t = getattr(mod, "TO_VERSION", "")
            if f and t:
                by_from.setdefault(f, (t, path))
    steps: list[tuple] = []
    cur = frm
    seen: set[str] = set()
    while cur != to and cur not in seen and len(steps) < 200:
        seen.add(cur)
        nxt = by_from.get(cur)
        if not nxt:
            break
        t, path = nxt
        steps.append((cur, t, path))
        cur = t
    return steps


def db_status(*, root: Optional[Any] = None) -> dict:
    """磁盘上的 DB schema_version 与源码里的 SCHEMA_VERSION 之差。"""
    root = repo_root(root)
    app_schema = version.SCHEMA_VERSION
    db_path = root / "data" / "credentials.db"
    if not db_path.is_file():
        return {"ok": True, "disk": "", "app": app_schema,
                "needs_migration": False, "chain": []}
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        conn.close()
        disk = str(row[0]) if row and row[0] else ""
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "app": app_schema, "message": f"无法读取数据库版本：{exc}"}
    needs = bool(disk) and _semver_gt(app_schema, disk)
    chain: list[str] = []
    if needs:
        chain = [f"{a}→{b}" for a, b, _ in discover_migration_chain(disk, app_schema, root=root)]
    return {"ok": True, "disk": disk, "app": app_schema,
            "needs_migration": needs, "chain": chain}


def _snapshot_db(conn: sqlite3.Connection, db_path: Path, tag: str) -> Path:
    """SQLite 一致性快照（WAL 下也安全）。

    **必须显式 ``close()``**：``with sqlite3.connect(...)`` 只是**事务**上下文，**不关闭连接**——
    连接还开着就去删这个文件，Windows 会报 `WinError 32（另一个程序正在使用此文件）`。
    """
    dest = Path(f"{db_path}.bak-{tag}-{time.strftime('%Y%m%d-%H%M%S')}")
    back = sqlite3.connect(str(dest))
    try:
        conn.backup(back)
    finally:
        back.close()
    return dest


def run_migrations(*, root: Optional[Any] = None, apply: bool = False,
                   guard: bool = True, backup: bool = True,
                   to: Optional[str] = None, dirs: Optional[list] = None) -> dict:
    """逐级应用数据库迁移。``apply=False``（默认）只演练。

    ``to`` 为目标结构版本，缺省取**当前运行代码**的 ``SCHEMA_VERSION``；正式版更新时目标来自
    下载包内的 ``version.json``、测试版来自拉取后的新源码，故由调用方显式传入。
    ``guard=True`` 时先抢单实例锁（拒绝在应用运行时迁移）；``apply_update`` 已先占锁，传 ``False``。
    """
    root = repo_root(root)
    st = db_status(root=root)
    if not st.get("ok"):
        return {"ok": False, "message": st.get("message", "无法读取数据库版本")}
    frm = st["disk"]
    to = (to or "").strip() or st["app"]
    if not frm:
        _log.info("数据库结构迁移：库尚未创建，无需迁移")
        return {"ok": True, "steps": [], "message": "数据库尚未创建，无需迁移"}
    if frm == to:
        _log.info("数据库结构迁移：已是最新（%s），无需迁移", to)
        return {"ok": True, "steps": [], "message": f"数据库结构已是最新（{to}）"}
    if not _semver_gt(to, frm):
        _log.info("数据库结构迁移：磁盘 %s 不低于目标 %s，跳过", frm, to)
        return {"ok": True, "steps": [],
                "message": f"磁盘版本 {frm} 不低于目标版本 {to}，无需迁移"}

    steps = discover_migration_chain(frm, to, root=root, dirs=dirs)
    labels = [f"{a}→{b}" for a, b, _ in steps]
    if not steps or steps[-1][1] != to:
        reached = steps[-1][1] if steps else frm
        _log.error("✗ 数据库结构迁移链断档：从 %s 到 %s 缺脚本（已找到：%s）",
                   frm, to, labels or "无")
        return {"ok": False, "steps": labels,
                "message": f"迁移链断档：缺少从 {reached} 升级的脚本（需 m_{reached}__to_*.py，"
                           f"放在 scripts/migrations/ 或 .update-meta/migrations/）"}
    if not apply:
        _log.info("数据库结构迁移演练：%s（共 %d 跳）",
                  " → ".join([frm] + [b for _, b, _ in steps]), len(steps))
        return {"ok": True, "dry_run": True, "steps": labels,
                "message": "演练：将逐级迁移 " + " → ".join([frm] + [b for _, b, _ in steps])}

    if guard and not single_instance.acquire():
        _log.warning("数据库结构迁移被拒：应用正在运行")
        return {"ok": False, "message": "检测到应用正在运行，请先完全退出再更新数据库"}

    db_path = root / "data" / "credentials.db"
    t0 = time.monotonic()
    _log.info("=== 数据库结构迁移开始：%s → %s，共 %d 跳（库：%s）===", frm, to, len(steps), db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    done: list[str] = []
    try:
        for i, (f, t, path) in enumerate(steps, 1):
            _log.info("[%d/%d] %s → %s（脚本 %s）", i, len(steps), f, t, Path(path).name)
            if backup:
                snap = _snapshot_db(conn, db_path, f)
                _log.info("      已留快照：%s", snap.name)
            mod = _load_module(path)
            try:                                  # 盘点只是只读预览，失败不该挡住迁移
                _log.info("      盘点：%s", mod.preview(conn))
            except Exception as exc:  # noqa: BLE001
                _log.warning("      preview 失败（不影响迁移）：%s", exc)
            hop0 = time.monotonic()
            with conn:  # 事务：异常自动回滚该跳
                mod.apply(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES "
                    "('schema_version', ?, ?)", (t, _now()))
            done.append(f"{f}→{t}")
            _log.info("      ✓ 完成 %s → %s（%.2fs，版本号已推进到 %s）",
                      f, t, time.monotonic() - hop0, t)
    except Exception as exc:  # noqa: BLE001 - 迁移失败要如实报，不吞
        _log.exception("✗ 数据库结构迁移失败（已完成 %d/%d 跳：%s；该跳已回滚）",
                       len(done), len(steps), done or "无")
        return {"ok": False, "steps": done, "db_path": str(db_path),
                "message": f"迁移失败（已完成 {len(done)} 跳）：{exc}"}
    finally:
        conn.close()
    _log.info("✓ 数据库结构迁移完成：%s → %s（%d 跳，耗时 %.2fs）",
              frm, to, len(done), time.monotonic() - t0)
    return {"ok": True, "applied": done, "db_path": str(db_path),
            "message": f"数据库已逐级迁移：{frm} → {to}"}


# ── 备份 / 恢复 ───────────────────────────────────────────

def _zip_add_tree(zf: zipfile.ZipFile, src: Path, arc_prefix: str,
                  skip_names: Optional[set] = None) -> list:
    """把 ``src`` 整棵目录塞进 zip；**读不了的文件跳过并返回说明**。

    「跳过而不是整包失败」是有意的：更新前的备份失败会**直接挡住更新**，而读取失败多半只是
    某个文件被运行中的程序占用（WebView 缓存、日志……），不该因此让用户更新不了。被跳过的
    条目会汇总到 ``make_backup`` 的返回里，如实告诉用户。
    """
    if not src.is_dir():
        return []
    skip = skip_names or set()
    skipped: list[str] = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in skip for part in rel.parts):
            continue
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            try:
                zf.write(path, f"{arc_prefix}/{rel.as_posix()}")
            except OSError as exc:      # PermissionError / 文件被占用 / 中途消失
                skipped.append(f"{rel.as_posix()}（{type(exc).__name__}）")
    return skipped


def make_backup(*, root: Optional[Any] = None, kind: str = "formal", keep: int = 1,
                export_doc: Optional[dict] = None) -> dict:
    """把可执行文件 + ``plugins/`` + ``data/``（含凭据库一致性快照）打包成 zip；**至多保留 ``keep`` 份**。

    ``export_doc``：**全量导出文档**（`backend/backup.build_backup` 的产物）。应用内更新时由
    `backend/api.py` 生成（它才拿得到主密码派生的 key）并传进来，随包落成
    ``data/keyrealm-full-backup.json``——**即使原库损坏/丢失，凭据与设置也能从这份文档还原**。
    """
    root = repo_root(root)
    bdir = backups_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)
    # 「只留一份」：写新备份前先删旧的
    for old in sorted(bdir.glob("keyrealm-backup-*.zip")):
        try:
            old.unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"keyrealm-backup-{kind}-{version.APP_VERSION}-{stamp}.zip"
    members: list[str] = []
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            exe = root / _exe_name()
            if exe.is_file():
                try:
                    zf.write(exe, exe.name)
                    members.append(exe.name)
                except OSError as exc:      # 可执行文件被占用（运行中）时同样只跳过
                    skipped.append(f"{exe.name}（{type(exc).__name__}）")
            skipped += _zip_add_tree(zf, root / "plugins", "plugins")
            # data/：DB 单独做一致性快照，其余直接收（跳过 logs / webview 等运行中会被锁的目录）
            skipped += _zip_add_tree(zf, root / "data", "data",
                                     skip_names=set(_BACKUP_SKIP_DIRS) | {"credentials.db",
                                                                          "credentials.db-wal",
                                                                          "credentials.db-shm"})
            db_path = root / "data" / "credentials.db"
            if db_path.is_file():
                # 快照文件名带 pid：撞不上上次崩溃残留的同名文件
                snap = db_path.with_name(f"credentials.db.snapshot-{os.getpid()}")
                try:
                    src = sqlite3.connect(str(db_path))
                    dst = sqlite3.connect(str(snap))
                    try:
                        src.backup(dst)      # SQLite 在线备份 API → 一致性快照
                    finally:
                        src.close()          # **必须关**：不关 Windows 上删不掉（WinError 32）
                        dst.close()
                    zf.write(snap, "data/credentials.db")
                    members.append("data/credentials.db")
                finally:
                    try:
                        snap.unlink()
                    except OSError:
                        pass                     # 删不掉也不该让整份备份失败
            # 全量导出文档：原库一旦损坏/丢失，凭据、设置、定时计划都能从它还原
            if export_doc:
                try:
                    zf.writestr("data/keyrealm-full-backup.json",
                                json.dumps(export_doc, ensure_ascii=False, indent=2))
                    members.append("data/keyrealm-full-backup.json")
                except Exception as exc:  # noqa: BLE001 - 序列化失败不该挡住更新
                    skipped.append(f"keyrealm-full-backup.json（{type(exc).__name__}）")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"备份失败：{exc}"}
    try:
        os.chmod(dest, 0o600)  # 含真实密钥，限权（Windows 上基本无效，忽略）
    except OSError:
        pass
    message = "备份含真实密钥（凭据库 / 密钥文件），请妥善保管"
    if skipped:
        shown = "、".join(skipped[:3]) + ("…" if len(skipped) > 3 else "")
        message += f"；{len(skipped)} 个文件被占用已跳过（不影响更新）：{shown}"
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
            "members": members, "skipped": skipped, "message": message}


def restore_backup(zip_path: Path, *, root: Optional[Any] = None) -> dict:
    """把备份 zip 解压覆盖回仓库根（``--rollback`` 用）。"""
    root = repo_root(root)
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return {"ok": False, "message": f"备份文件不存在：{zip_path}"}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"恢复失败：{exc}"}
    return {"ok": True, "message": f"已从备份恢复：{zip_path}"}


def latest_backup(*, root: Optional[Any] = None) -> Optional[Path]:
    items = sorted(backups_dir(root).glob("keyrealm-backup-*.zip"))
    return items[-1] if items else None


# ── 取源码（会改工作区）─────────────────────────────────────

def fetch_latest(*, root: Optional[Any] = None, branch: str = DEFAULT_BRANCH,
                 force: bool = False, discard: bool = False) -> dict:
    """``git fetch origin <branch>`` 后把工作区对齐到远端。

    脏工作区有三种处置，**按参数显式选**（`force` 优先，其次 `discard`）：

    - ``force=True``：先 ``git stash`` 再快进合并（保留一份可取回的副本）；
    - ``discard=True``：**直接丢弃本地改动**（``git reset --hard FETCH_HEAD``）——**更新走的就是这条**。
      部署目录里的改动多半是构建/依赖留下的痕迹（典型是 ``npm install`` 改写的
      ``frontend/package-lock.json``），不该拦住更新；丢弃清单会记进日志。
      **未跟踪 / 被 ignore 的文件（``data/`` 等）不受影响。**
    - 都不给：**拒绝**，避免覆盖用户未提交的改动。

    **不用 ``--depth 1``**：浅取会在 FETCH_HEAD 处切出浅边界，本地既有历史与它「接不上」，
    ``merge --ff-only`` 会报 **`refusing to merge unrelated histories`** 而**静默失败**——
    只要本地真的落后于远端就必然踩到（本机复现）。普通 fetch 只传本地缺的对象，开销可接受。
    """
    root = repo_root(root)
    if not git_available():
        return {"ok": False, "message": "未安装 git"}
    if not is_source_checkout(root):
        return {"ok": False, "message": "当前不是 git 源码仓库（未检测到 .git）"}
    # 注意：**别先对整段输出 strip** —— porcelain 每行是 `XY <路径>`，首行的状态前导空格
    # 一旦被削掉，按固定偏移取路径就会错位（踩过：`a.txt` 被取成 `.txt`）。
    status = _git(["status", "--porcelain"], cwd=root).stdout
    dirty = status.strip()
    discarded: list = []
    stashed = False
    if dirty:
        if force:
            st = _git(["stash", "push", "-u", "-m", "keyrealm-update-autostash"], cwd=root)
            stashed = st.returncode == 0
        elif discard:
            discarded = [line[3:].strip() for line in status.splitlines() if line.strip()]
        else:
            return {"ok": False,
                    "message": "工作区有未提交改动，为避免覆盖已中止。请先提交或 stash，"
                               "或加 --force（会先 git stash 再合并）"}
    try:
        fe = _git(["fetch", "origin", branch], cwd=root, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "git fetch 超时（网络不可用？）"}
    if fe.returncode != 0:
        return {"ok": False, "message": f"git fetch 失败：{_short(fe.stderr)}"}
    if discard and not stashed:
        rs = _git(["reset", "--hard", "FETCH_HEAD"], cwd=root)
        if rs.returncode != 0:
            return {"ok": False, "message": f"对齐远端失败：{_short(rs.stderr)}"}
        if discarded:
            _log.info("更新：已丢弃本地改动 %d 项（%s）",
                      len(discarded), "、".join(discarded[:5]))
        return {"ok": True, "discarded": discarded,
                "message": "源码已对齐到远端最新（本地改动已丢弃）"}
    mg = _git(["merge", "--ff-only", "FETCH_HEAD"], cwd=root)
    if mg.returncode != 0:
        return {"ok": False,
                "message": f"无法快进合并（本地已分叉，未做任何改动）：{_short(mg.stderr)}"}
    return {"ok": True, "stashed": stashed, "message": "源码已快进到远端最新"}


# ── 构建 ──────────────────────────────────────────────────

def run_build(*, root: Optional[Any] = None, frontend: bool = True,
              extra_args: Optional[list] = None, python: Optional[str] = None,
              output: Optional[Any] = None,
              should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """跑 ``scripts/build.py``。``python`` 指定用哪个解释器（默认 ``sys.executable``）。

    - 冻结 exe 里的自更新要**用 venv 的解释器**构建（``sys.executable`` 是 exe，跑不了脚本），
      故由调用方传入 ``ensure_venv()`` 的路径。
    - ``output`` 指定产物落点（默认仓库根）。**准备阶段**（应用还在跑）必须传 ``staging_dir``——
      默认落点会覆盖运行中的可执行文件，而运行中的程序文件不可覆盖。
    - 构建输出落 ``data/logs/build-*.log`` 便于排障；``should_cancel()`` 为真则终止构建。
    """
    root = repo_root(root)
    build = root / "scripts" / "build.py"
    if not build.is_file():
        return {"ok": False, "message": "未找到 scripts/build.py（本机没有源码，无法从源码构建）"}
    py = python or sys.executable
    cmd = [py, str(build)]
    if frontend:
        cmd.append("--frontend")
    if output:
        cmd += ["--output", str(output)]
    cmd += list(extra_args or [])
    _log.info("开始构建：%s", " ".join(cmd))
    log = root / "data" / "logs" / f"build-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    sink = open(log, "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(root), env=_child_env(),
                                stdout=sink, stderr=subprocess.STDOUT,
                                creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        sink.close()
        return {"ok": False, "message": f"构建无法启动：{exc}", "log": str(log)}
    finally:
        sink.close()   # 子进程持有自己的 fd，父侧随即关闭即可
    while True:
        if should_cancel and should_cancel():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"ok": False, "canceled": True, "log": str(log), "message": "已取消构建"}
        try:
            rc = proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            continue
    if rc != 0:
        tail = _short(log.read_text(encoding="utf-8", errors="replace")[-800:]) if log.is_file() else ""
        return {"ok": False, "returncode": rc, "log": str(log),
                "message": f"构建失败（退出码 {rc}）{('：' + tail) if tail else ''}"}
    return {"ok": True, "returncode": 0, "log": str(log), "message": "构建完成"}


def ensure_deps(*, root: Optional[Any] = None, extra: tuple = ("pyinstaller",),
                python: Optional[str] = None) -> dict:
    """确保当前解释器里装好 ``requirements.txt`` 与构建额外依赖（``pyinstaller``）。

    **走更新代理**（见 ``proxy_env``）——否则本机访问 PyPI 需要代理时，引导脚本里的 pip 会失败。
    已满足依赖时 pip 只会快速校验、几乎无耗时。
    """
    root = repo_root(root)
    env = _child_env()
    py = python or sys.executable
    commands: list[list[str]] = []
    req = root / "requirements.txt"
    if req.is_file():
        commands.append([py, "-m", "pip", "install", "-q", "-r", str(req)])
    commands.append([py, "-m", "pip", "install", "-q", *extra])
    for cmd in commands:
        try:
            r = subprocess.run(cmd, cwd=str(root), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"依赖安装失败：{exc}"}
        if r.returncode != 0:
            return {"ok": False, "message": f"依赖安装失败：{_short(r.stderr or r.stdout)}"}
    return {"ok": True, "message": "依赖已就绪"}


def _venv_python(root: Optional[Any] = None) -> Path:
    """项目下虚拟环境的解释器路径（Linux `.venv/bin/python` / Windows `.venv\\Scripts\\python.exe`）。"""
    base = repo_root(root) / ".venv"
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def ensure_venv(root: Optional[Any] = None) -> dict:
    """确保项目下有可用的虚拟环境，返回其解释器路径（没有就现建一个，离线可完成）。"""
    root = repo_root(root)
    venv_py = _venv_python(root)
    if venv_py.is_file():
        return {"ok": True, "python": str(venv_py)}
    sys_py = shutil.which("python3") or shutil.which("python") or sys.executable
    try:
        r = subprocess.run([sys_py, "-m", "venv", str(root / ".venv")], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"创建虚拟环境失败：{exc}"}
    if r.returncode != 0 or not venv_py.is_file():
        return {"ok": False, "message": f"创建虚拟环境失败：{_short(r.stderr) or '未知错误'}"}
    return {"ok": True, "python": str(venv_py)}


def ensure_source(*, root: Optional[Any] = None, branch: str = DEFAULT_BRANCH,
                  force: bool = False, discard: bool = True) -> dict:
    """确保程序目录是一份（最新的）源码：是仓库就快进，不是就就地初始化并检出。

    供**测试版**（从源码构建）用——从 Releases 装的二进制版本机没有源码，这里把它转成源码部署
    （``git init`` + ``fetch`` + ``checkout -f``）；``data/``、可执行文件等未跟踪文件原样保留。

    ``discard=True``（**默认**）：工作区脏时**丢弃本地改动**对齐远端——更新场景要的就是「直接拉最新」，
    部署目录里的改动多半是构建/依赖留下的痕迹。想保留就先 commit / stash，或用 ``force=True``（先 stash 再合）。
    """
    root = repo_root(root)
    if not git_available():
        return {"ok": False, "message": "未安装 git，无法从源码构建"}
    if is_source_checkout(root):
        return fetch_latest(root=root, branch=branch, force=force, discard=discard)
    url = _remote_url()
    try:
        _git(["init"], cwd=root)
        _git(["remote", "remove", "origin"], cwd=root)
        if _git(["remote", "add", "origin", url], cwd=root).returncode != 0:
            return {"ok": False, "message": "配置 git 远端失败"}
        fe = _git(["fetch", "--depth", str(CLONE_DEPTH), "origin", branch], cwd=root, timeout=180)
        if fe.returncode != 0:
            return {"ok": False, "message": f"拉取源码失败：{_short(fe.stderr)}"}
        co = _git(["checkout", "-f", "-B", branch, "FETCH_HEAD"], cwd=root)
        if co.returncode != 0:
            return {"ok": False, "message": f"检出源码失败：{_short(co.stderr)}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "拉取源码超时（网络不可用？）"}
    return {"ok": True, "message": "已把源码拉取到程序目录（转为源码部署）"}


# ── 进程：脱离 / 等待退出 / 重启 ───────────────────────────

def spawn_detached(argv: list[str], *, cwd: Path, log: Optional[Path] = None,
                   env: Optional[dict] = None) -> int:
    """起一个**脱离父进程**的子进程，返回 pid。

    Windows：``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``（切断控制台继承）；
    POSIX：``start_new_session=True``（新会话，脱离控制终端）。

    环境里固定带上 ``PYINSTALLER_RESET_ENVIRONMENT=1``（见 ``_PYI_ENV``）——重启起来的新 exe
    也是本函数起的，不带它就会复用父进程的 `_MEI` 而随父进程退出一起失效。
    """
    full_env = {**os.environ, **_PYI_ENV, **(env or {})}
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    sink = None
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        sink = open(log, "ab")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=sink or subprocess.DEVNULL,
            stderr=sink or subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        if sink is not None:
            sink.close()
    return proc.pid


def wait_for_app_exit(*, root: Optional[Any] = None, timeout: float = 90,
                      poll: float = 1.0,
                      log: Optional[Callable[[str], None]] = None) -> bool:
    """等下应用完全退出，并**顺手占住单实例锁**（更新期间不让应用启动）。

    判据就是单实例锁：应用还在跑 → `acquire()` 拿不到 → 继续等；拿到了说明应用已退。
    调用方（`apply_update`）在重启新应用前必须 `single_instance.release()`。

    ``log`` 每 ~15 秒报一次「还在等」——交棒进程没有窗口，长等待期间日志一片空白
    会让人以为卡死了。
    """
    deadline = time.monotonic() + timeout
    waited = 0.0
    next_report = 15.0
    while True:
        if single_instance.acquire():
            if log and waited:
                log(f"应用已退出（等待了 {int(waited)} 秒）")
            return True
        if time.monotonic() >= deadline:
            return False
        if log and waited >= next_report:
            log(f"仍在等待应用退出…（已等 {int(waited)}/{int(timeout)} 秒）")
            next_report += 15.0
        time.sleep(poll)
        waited += poll


def relaunch_app(*, root: Optional[Any] = None, prefer: str = "auto") -> dict:
    """重启应用（脱离当前更新进程）。

    ``prefer``：``"exe"`` 跑预编译二进制（正式版更新后）、``"source"`` 跑 ``python main.py``
    （测试版在源码部署下）、``"auto"`` 按当前形态（冻结→exe，源码→main.py）。
    """
    root = repo_root(root)
    exe = root / _exe_name()
    main_py = root / "main.py"
    if prefer == "auto":
        prefer = "source" if (not is_frozen() and main_py.is_file()) else "exe"
    if prefer == "source" and main_py.is_file():
        argv = [sys.executable, str(main_py)]
    elif exe.is_file():
        argv = [str(exe)]
    elif main_py.is_file():
        argv = [sys.executable, str(main_py)]
    else:
        return {"ok": False, "message": "找不到可启动的程序（既无可执行文件，也无 main.py）"}
    log = root / "data" / "logs" / f"relaunch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    try:
        # 交棒进程是带着「更新代理」起的（`_spawn_updater` 传了 proxy_env()）；新应用若原样
        # 继承，`plugin_sdk.proxy()` 的「未指定则回退环境变量」会把**全部插件请求**导到更新
        # 代理上——用户界面里看不出任何设置。这里还原成更新前的原值（见 `_restored_proxy_env`）。
        pid = spawn_detached(argv, cwd=root, log=log, env=_restored_proxy_env())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"重启失败：{exc}"}
    return {"ok": True, "pid": pid, "argv": argv}


# ── 状态 / 请求文件（跨进程、跨重启）────────────────────────

def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _write_status(root: Optional[Any], payload: dict) -> None:
    path = status_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": _now()}
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log.warning("写更新状态失败：%s", exc)


def read_status(root: Optional[Any] = None) -> dict:
    return _read_json(status_file(root)) or {}


def read_request(root: Optional[Any] = None) -> dict:
    return _read_json(request_file(root)) or {}


def check_file(root: Optional[Any] = None) -> Path:
    """**最近一次「检查更新」的结果**（持久化）。

    界面据此在换页 / 重启后仍能显示上次结果，**直到被新的一次检查替换**为止。
    """
    return _data_dir(root) / ".update-check.json"


def read_last_check(root: Optional[Any] = None) -> dict:
    return _read_json(check_file(root)) or {}


def write_last_check(result: dict, *, root: Optional[Any] = None) -> None:
    """把一次检查结果落盘（只挑界面要用的字段）。"""
    keys = ("ok", "kind", "has_update", "reason", "current", "latest",
            "db_change", "db_chain", "asset", "platform", "source_ok", "proxy",
            "prepared", "error", "message")
    payload = {k: result.get(k) for k in keys if k in result}
    payload["checked_at"] = _now()
    _write_json_atomic(check_file(root), payload)


# ── 准备产物清单（应用内写、交棒进程读）──────────────────────
# 交棒进程据此走「快路径」：校验 sha256 后跳过已完成的下载 / 拉源码 / 构建。
# 陈旧（超 TTL）或校验失败一律视为无效 → 回落完整流程（CLI 走的正是这条）。
PREPARED_SCHEMA = 1
# 准备产物有效期（秒）。比交棒请求（24h）长：用户在「是否立即重启」里选「稍后」后，
# 产物要留到下次更新时复用（用户可能隔几天才再点更新）。真正落盘前仍会重新校验
# 版本（正式版 = 仍是最新 Release）或 commit（测试版 = 仍是远端同一提交）。
_PREPARED_TTL = 7 * 24 * 3600
_REQUEST_TTL = 24 * 3600      # 请求文件有效期（秒）——顺带修「陈旧请求覆盖 CLI --kind」的老坑


def _write_json_atomic(path: Path, payload: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        _log.warning("写 %s 失败：%s", path.name, exc)
        return False


def read_prepared(root: Optional[Any] = None) -> dict:
    return _read_json(prepared_file(root)) or {}


def _write_prepared(root: Optional[Any], payload: dict) -> None:
    _write_json_atomic(prepared_file(root),
                       {**payload, "schema": PREPARED_SCHEMA, "prepared_at": _now()})


def _clear_prepared(root: Optional[Any] = None) -> None:
    try:
        prepared_file(root).unlink(missing_ok=True)
    except OSError:
        pass


def _age_seconds(stamp: Any) -> Optional[float]:
    """时间戳距现在多少秒（解析失败返回 None）。"""
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def _validate_prepared(root: Optional[Any], prepared: dict) -> bool:
    """准备产物是否仍可信：结构完整、未过期、formal 的包存在且 sha256 一致。"""
    if not prepared or int(prepared.get("schema") or 0) != PREPARED_SCHEMA:
        return False
    age = _age_seconds(prepared.get("prepared_at"))
    if age is None or age > _PREPARED_TTL or age < -60:
        return False
    kind = prepared.get("kind")
    if kind == "formal":
        ap = Path(str(prepared.get("asset_path") or ""))
        want = str(prepared.get("asset_sha256") or "")
        if not ap.is_file() or not want or _sha256(ap) != want:
            return False
    elif kind == "test":
        if not (repo_root(root) / "main.py").is_file():
            return False
        # 复用（以及「稍后」之后的手动交棒）必须能真的装上：构建产物还在才算数。
        staging = Path(str(prepared.get("staging_dir") or ""))
        if not staging.is_dir() or not (staging / _exe_name()).is_file():
            return False
    else:
        return False
    return True


def prepared_ready(kind: Optional[str] = None, *, root: Optional[Any] = None,
                   prepared: Optional[dict] = None) -> dict:
    """返回仍可信的准备产物清单（无效 / 渠道不符返回空 dict）。

    供三处使用：检查更新时提示「本地已有未安装的更新」、准备阶段复用（跳过重复下载 /
    构建）、以及用户在「稍后」之后点「立即重启」时交棒。
    """
    payload = prepared if isinstance(prepared, dict) else read_prepared(root)
    if not _validate_prepared(root, payload):
        return {}
    if kind and str(payload.get("kind") or "") != kind:
        return {}
    return payload


def prepared_info(prepared: Optional[dict]) -> Optional[dict]:
    """准备产物的**界面摘要**（不含本机路径等内部字段）。"""
    if not prepared:
        return None
    keys = ("kind", "version", "commit", "asset_name", "asset_bytes", "prepared_at")
    return {k: prepared.get(k) for k in keys if prepared.get(k) not in (None, "")}


# ── 编排：更新主流程 ──────────────────────────────────────

def _relaunch_spec(root: Path) -> dict:
    exe = root / _exe_name()
    if is_frozen():
        return {"frozen": True, "exe": str(exe), "argv": [], "cwd": str(root)}
    return {"frozen": False, "exe": sys.executable, "argv": [str(root / "main.py")], "cwd": str(root)}


# 阶段 → 界面文案
_STAGE_LABELS = {
    "backup": "备份", "download": "下载预编译包", "source": "获取源码",
    "deps": "准备构建环境", "build": "构建", "extract": "解压替换",
    "install": "安装新版本", "migrate": "迁移数据库", "restart": "重启",
}


def stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name, name)


def _prep_stages(kind: str) -> list:
    """**准备阶段**（应用进程内）的阶段名。"""
    return ["backup", "download"] if kind == "formal" else ["backup", "source", "deps", "build"]


def _commit_stages(kind: str) -> list:
    """**交棒阶段**（脱离进程）的阶段名。"""
    return ["extract", "migrate", "restart"] if kind == "formal" else ["install", "migrate", "restart"]


def _stages_for(kind: str) -> list:
    """两段合起来（供界面展示完整流程）。"""
    return _prep_stages(kind) + _commit_stages(kind)


ProgressCb = Callable[[int, int, str], None]   # (当前, 总量, 文案)


def _status_stage(root: Optional[Any], kind: str, stages: list, name: str,
                  message: str, done: list, percent: Optional[int] = None) -> None:
    """写一条「阶段进行中」的状态（供界面显示阶段 + 进度）。"""
    idx = stages.index(name) + 1 if name in stages else None
    _write_status(root, {
        "phase": name, "ok": None, "kind": kind, "stages": stages,
        "stage": name, "stage_label": stage_label(name),
        "stage_index": idx, "stage_total": len(stages),
        "done_stages": list(done), "percent": percent, "message": message,
    })


def _exe_bak_name() -> str:
    """更新时旧 exe 让位用的临时名。**与 ``scripts/build.py`` 的构建回滚副本同名**
    （都是「被替换掉的旧 exe」），同目录只留一份、互为覆盖，不额外清理。"""
    return _exe_name() + ".old"


def _move_exe_aside(root: Path) -> Optional[Path]:
    """把 ``root/<exe>`` 改名成 ``<exe>.old`` 让出路径；返回备份路径，没让成返回 ``None``。

    Windows 下**运行中的 exe 既不能删除、也不能原地覆盖**（覆盖报 `WinError 32`），
    但**可以改名**（映像文件是以带 ``FILE_SHARE_DELETE`` 的句柄打开的）。而交棒进程自己就跑在
    这个 exe 上（``_self_update_argv`` 用 ``sys.executable``）——这正是「替换失败、重启还是
    旧版本」的根因。改名后旧文件仍被交棒进程映射，但路径已让出，新文件直接放进去即可。
    """
    live = root / _exe_name()
    if not live.exists():
        return None
    bak = root / _exe_bak_name()
    if bak.exists():
        try:
            bak.unlink()                 # 上一次更新/构建留下的，覆盖它（同目录只留一份）
        except OSError:
            pass
    try:
        os.replace(live, bak)
    except OSError:
        return None
    return bak


def _replace_exe(src: Path, root: Path) -> None:
    """把 ``src`` 装成 ``root/<exe>``：先让位（见 `_move_exe_aside`）再放新的。"""
    _move_exe_aside(root)
    shutil.copy2(src, root / _exe_name())


def _busy_hint(exc: OSError) -> str:
    """「文件被占用」的可操作提示：Windows `WinError 32` / POSIX `ETXTBSY`、`EBUSY`。
    交棒进程自身占用已被 `_replace_exe` 的改名腾位绕开，走到这里多半是别处的残留进程。"""
    if getattr(exc, "winerror", None) == 32 or exc.errno in (errno.ETXTBSY, errno.EBUSY):
        return "（文件被占用：可能另有 Keyrealm 进程没退干净，或被杀软/同步盘锁定；关掉后重试）"
    return ""


def _install_staged(staging: Any, root: Path) -> dict:
    """把 staging 里构建好的产物安装进程序目录（换可执行文件 + 覆盖 ``plugins/``）。"""
    staging, root = Path(staging), Path(root)
    if not staging.is_dir():
        return {"ok": False, "message": "未找到构建产物（staging 目录不存在）"}
    exe_src = staging / _exe_name()
    if exe_src.is_file():
        try:
            _replace_exe(exe_src, root)
        except OSError as exc:
            return {"ok": False, "message": f"替换可执行文件失败：{exc}{_busy_hint(exc)}"}
    plugins_src = staging / "plugins"
    if plugins_src.is_dir():
        try:
            shutil.copytree(plugins_src, root / "plugins", dirs_exist_ok=True)
        except OSError as exc:
            return {"ok": False, "message": f"安装插件失败：{exc}"}
    shutil.rmtree(staging, ignore_errors=True)   # 装完即清，别把一份构建产物长期留在程序目录
    return {"ok": True}


def prepare_update(kind: str = "formal", *, root: Optional[Any] = None, frontend: bool = True,
                   do_backup: bool = True, force: bool = False,
                   progress_cb: Optional[ProgressCb] = None,
                   should_cancel: Optional[Callable[[], bool]] = None,
                   export_doc: Optional[dict] = None,
                   log: Callable[[str], None] = print) -> dict:
    """**准备阶段**（应用进程内跑，程序不退出）：备份 + 下载预编译包 / 拉源码 + 装依赖 + 构建。

    产物写 ``prepared_file``，交棒进程据此走快路径。``progress_cb(current, total, text)`` 报进度；
    ``should_cancel()`` 为真则尽快中止（下载按块、步骤之间）——但**源码一旦更新即不再接受取消**
    （工作区已前进，取消会让源码领先于数据库）。返回 ``{ok, has_update, kind, prepared, reused, ...}``；
    无更新时 ``has_update=False`` 且不写清单。若本地已有同渠道、仍匹配目标版本的产物（上次选了
    「稍后」），复用该产物并跳过下载 / 构建，``reused=True``。
    """
    root = repo_root(root)
    kind = kind if kind in CHANNELS else "formal"
    stages = _prep_stages(kind)
    done: list[str] = []

    # 换渠道时旧产物没有意义（交棒会按请求里的渠道走），直接清掉。
    existing = read_prepared(root)
    if existing and str(existing.get("kind") or "") != kind:
        _clear_prepared(root)

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    def _prog(cur: int, total: int, text: str = "") -> None:
        if progress_cb:
            progress_cb(cur, total, text)

    local = local_info(root)
    reuse: dict = {}
    if kind == "formal":
        rel = latest_release_tag()
        if not rel.get("ok"):
            return {"ok": False, "kind": kind, "message": rel.get("message") or "查询最新正式版失败"}
        to_ver, commit, to_schema = rel["version"], "", ""
        if not _semver_gt(to_ver, local["version"]):
            # 已经装上（或 Release 回退）→ 同渠道的旧产物留着只会让下次检查误报
            if str(read_prepared(root).get("kind") or "") == kind:
                _clear_prepared(root)
            return {"ok": True, "has_update": False, "kind": kind, "to_version": local["version"],
                    "message": f"已是最新正式版（{local['version']}）"}
        reuse = prepared_ready(kind, root=root)
        # 只有「当前最新 Release」的包才能复用；Release 已前进就重新下载
        if str(reuse.get("version") or "") != str(to_ver):
            reuse = {}
    else:
        remote = remote_info(root=root)
        if not remote.get("ok"):
            return {"ok": False, "kind": kind, "message": remote.get("message") or "获取远端源码信息失败"}
        cmp = compare(kind, local, remote)
        reuse = prepared_ready(kind, root=root)
        # 上次准备已把源码拉到目标 commit，故 compare 可能已是「无更新」；
        # 只要产物仍指向远端同一 commit，就把它当作待安装的更新（跳过重复构建）。
        if str(reuse.get("commit") or "") != str(remote.get("commit") or ""):
            reuse = {}
        if not cmp["has_update"] and not reuse:
            return {"ok": True, "has_update": False, "kind": kind, "remote": remote,
                    "to_version": remote.get("version") or local["version"], "message": cmp["reason"]}
        to_ver = remote.get("version") or local["version"]
        commit, to_schema = remote.get("commit", ""), remote.get("schema_version", "")

    if not reuse:
        _clear_prepared(root)
    else:
        log(f"复用本地已准备产物（{reuse.get('version') or reuse.get('commit') or ''}），"
            f"跳过{'下载' if kind == 'formal' else '拉源码与构建'}")

    if _cancelled():
        return {"ok": False, "canceled": True, "kind": kind, "message": "已取消"}

    if do_backup:
        _status_stage(root, kind, stages, "backup", "备份旧产物…", done)
        _prog(0, 0, "备份中…")
        bk = make_backup(root=root, kind=kind, export_doc=export_doc)
        if not bk.get("ok"):
            return {"ok": False, "kind": kind, "message": bk.get("message", "备份失败")}
        log(f"已备份 → {bk['path']}")
        if _cancelled():
            return {"ok": False, "canceled": True, "kind": kind, "message": "已取消"}
    done.append("backup")

    if reuse:
        # 复用上次「稍后」留下的产物：包已通过 sha256 校验 / 构建目录仍在，直接进入交棒。
        prepared = dict(reuse)
        done.extend(["download"] if kind == "formal" else ["source", "deps", "build"])
        _prog(0, 0, "复用本地已准备产物…")
    else:
        # 「更新前的版本 / 提交」必须**在拉源码之前**定下来并写进清单：test 渠道一旦对齐到新源码，
        # 现查 git（version.build_id 源码运行时是实时 git）拿到的就是**新** commit 了，事后无法还原。
        prepared = {"kind": kind, "version": to_ver, "commit": commit,
                    "from_version": local["version"], "from_commit": local["commit"]}

        if kind == "formal":
            name = release_asset_name(to_ver)
            _status_stage(root, kind, stages, "download", f"下载预编译包 {name}…", done)
            dest = backups_dir(root) / f"_asset-{to_ver}.zip"
            dl = download_release_asset(to_ver, dest,
                                        progress_cb=lambda d, t: _prog(d, t, f"下载 {name}…"),
                                        should_cancel=should_cancel)
            if not dl.get("ok"):
                return {"ok": False, "canceled": bool(dl.get("canceled")), "kind": kind,
                        "message": dl.get("message", "下载失败")}
            done.append("download")
            prepared.update({
                "asset_name": name, "asset_path": str(dest),
                "asset_bytes": dl.get("bytes"), "asset_sha256": dl.get("sha256"),
                "schema_version": _zip_schema(dest) or to_schema or local["schema_version"],
            })
            log(f"已下载（{(dl.get('bytes') or 0) // 1024} KB）")
        else:
            _status_stage(root, kind, stages, "source", "获取最新源码…", done)
            _prog(0, 0, "获取源码…")
            src = ensure_source(root=root, force=force)
            if not src.get("ok"):
                return {"ok": False, "kind": kind, "message": src.get("message", "获取源码失败")}
            log(src.get("message", "源码已就绪"))
            done.append("source")
            should_cancel = None      # 提交点：源码已更新，此后不再接受取消
            _status_stage(root, kind, stages, "deps", "准备构建环境（虚拟环境 + 依赖）…", done)
            _prog(0, 0, "准备构建环境…")
            vn = ensure_venv(root=root)
            if not vn.get("ok"):
                return {"ok": False, "kind": kind, "message": vn.get("message")}
            py = vn["python"]
            dp = ensure_deps(root=root, python=py)
            if not dp.get("ok"):
                return {"ok": False, "kind": kind, "message": dp.get("message")}
            log(dp.get("message", "依赖已就绪"))
            done.append("deps")
            _status_stage(root, kind, stages, "build", "从源码构建到临时目录（scripts/build.py --frontend）…", done)
            _prog(0, 0, "构建中（可能需要几分钟）…")
            out = staging_dir(root)
            bd = run_build(root=root, frontend=frontend, python=py, output=out)
            if not bd.get("ok"):
                return {"ok": False, "kind": kind, "message": bd.get("message", "构建失败"),
                        "log": bd.get("log")}
            log(bd.get("message", "构建完成"))
            done.append("build")
            meta = source_meta(root)
            prepared.update({
                "staging_dir": str(out), "venv_python": py,
                "asset_path": "", "asset_sha256": "", "asset_bytes": None,
                "schema_version": meta.get("schema_version") or to_schema or local["schema_version"],
            })

    _write_prepared(root, prepared)   # 复用也会刷新 prepared_at（相当于给产物续期）
    _write_status(root, {"phase": "prepared", "ok": True, "kind": kind, "stages": stages,
                         "done_stages": done, "percent": 100, "message": "准备完成"})
    return {"ok": True, "has_update": True, "kind": kind, "to_version": to_ver,
            "commit": commit, "prepared": prepared, "reused": bool(reuse), "stages": stages}


def _latest_update_log(root: Optional[Any] = None) -> str:
    logs = sorted((repo_root(root) / "data" / "logs").glob("update-*.log"))
    return str(logs[-1]) if logs else ""


def apply_update(kind: str = "formal", *, root: Optional[Any] = None, frontend: bool = True,
                 do_backup: bool = True, do_migrate: bool = True, do_restart: bool = True,
                 force: bool = False, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
    """更新**交棒主流程**：等应用退出 → 用「准备产物」走快路径（无则回落完整流程）→ 迁移 → 重启。

    - 有有效 ``prepared_file`` → 跳过备份/下载/拉源码/装依赖/构建，直接解压替换或安装 → 迁移 → 重启。
    - 无清单（如 ``scripts/build_update.py`` 单独跑）→ 回落完整流程（formal 自行下载；test 自行拉源码 + 构建）。

    由 ``main.py --self-update``（应用内交棒）或 ``scripts/build_update.py`` 调用。
    """
    root = repo_root(root)
    req = read_request(root)
    # 只采信**时效内**的请求文件；陈旧请求不得覆盖 CLI 传入的 kind（老坑）。
    if req and (_age_seconds(req.get("requested_at")) or 1e9) <= _REQUEST_TTL:
        kind = req.get("kind") or kind
    kind = kind if kind in CHANNELS else "formal"
    stages = _stages_for(kind)

    prepared = read_prepared(root)
    if not _validate_prepared(root, prepared) or str(prepared.get("kind") or "") != kind:
        prepared = {}          # 缺失 / 过期 / 渠道不符 / sha256 不符 → 回落完整流程

    # 交棒进程**没有窗口**，全程只能靠这份日志让人看懂「跑到哪、为什么停」——
    # 每个里程碑都打一行，失败原因更不能只写状态文件。
    _log.info("程序内更新开始：kind=%s prepared=%s", kind, bool(prepared))
    log(f"=== Keyrealm 自更新开始（渠道={kind}，{'使用准备产物' if prepared else '无准备产物→完整流程'}）===")

    if dry_run:
        log(f"演练（{kind}）：将依次执行 " + " → ".join(stages)
            + ("（使用准备产物）" if prepared else "（无准备产物，走完整流程）"))
        return {"ok": True, "dry_run": True, "kind": kind, "stages": stages, "prepared": bool(prepared)}

    _write_status(root, {"phase": "waiting", "ok": None, "kind": kind, "stages": stages,
                         "message": "等待应用退出…"})
    log("等待应用完全退出…（最多 90 秒）")
    if not wait_for_app_exit(root=root, log=log):
        _write_status(root, {"phase": "failed", "ok": False, "kind": kind, "stages": stages,
                             "message": "等待应用退出超时"})
        log("✗ 等待应用退出超时——更新中止，未改动任何文件。"
            "多半是程序没完全退出（托盘常驻/后台线程未停）或单实例锁没释放；"
            "请彻底退出程序后再试。")
        return {"ok": False, "message": "等待应用退出超时"}
    log("应用已退出，开始执行更新。")

    # 「更新前」的版本与提交：交棒进程里现查 git 已是**新** commit（test 渠道源码在准备阶段就
    # 前进了），所以优先用准备清单里固定下来的值；无清单（CLI 完整流程）时在此刻读——此刻还没拉源码。
    if prepared:
        frm = prepared.get("from_version") or version.APP_VERSION
        frm_commit = prepared.get("from_commit") or ""
    else:
        _l0 = local_info(root)
        frm, frm_commit = _l0["version"], _l0["commit"]
    report: list = []
    try:
        if kind == "formal":
            result = _apply_formal(root, do_backup=do_backup, do_migrate=do_migrate, log=log,
                                   prepared=prepared, stages=stages, report=report)
        else:
            result = _apply_test(root, frontend=frontend, do_backup=do_backup,
                                 do_migrate=do_migrate, force=force, log=log,
                                 prepared=prepared, stages=stages, report=report)
    except Exception as exc:  # noqa: BLE001 - 编排边界兜底，如实报错
        _log.exception("更新流程异常")
        result = {"ok": False, "message": f"更新异常：{type(exc).__name__}"}

    prefer = result.pop("prefer_restart", "auto")
    if do_restart and result.get("ok"):
        _status_stage(root, kind, stages, "restart", "重启程序…", [s["stage"] for s in report])
        single_instance.release()          # 必须先放锁，否则新应用会把自己当第二实例退出
        rl = relaunch_app(root=root, prefer=prefer)
        if rl.get("ok"):
            log(f"已重启（pid={rl.get('pid')}）")
            report.append({"stage": "restart", "ok": True, "message": "已重启", "at": _now()})
        else:
            result = {"ok": False, "message": rl.get("message", "重启失败")}
            report.append({"stage": "restart", "ok": False, "message": rl.get("message", ""), "at": _now()})
    else:
        single_instance.release()
    _clear_prepared(root)
    # 「更新后」的提交：准备清单里记着远端 commit；CLI 流程回落现查（此时源码已更新到新提交）。
    to_commit = prepared.get("commit") or ""
    if not to_commit and kind == "test":
        _p = _git(["rev-parse", "HEAD"], cwd=root)
        to_commit = (_p.stdout or "").strip() if _p.returncode == 0 else ""
    final = _finish(root, kind, frm, result, stages, report=report,
                    frm_commit=frm_commit, to_commit=to_commit)
    log(("✓ " if final.get("ok") else "✗ ") + str(final.get("message", "")))
    if not final.get("ok"):
        log("（更新失败：本次未替换程序文件，原程序仍可用。"
            "详情见本次日志与「设置 → 更新 → 上次更新」。）")
    return final


def _apply_formal(root: Path, *, do_backup: bool, do_migrate: bool, log: Callable[[str], None],
                  prepared: dict, stages: list, report: list) -> dict:
    """正式版：查最新正式版 →（备份）→ 下载预编译包 → 解压替换 → 迁移。重启偏好 ``exe``。

    ``prepared`` 有效时跳过备份与下载（准备阶段已做、且包已校验 sha256）。
    """
    if prepared:
        tmp = Path(str(prepared.get("asset_path") or ""))
        ver = str(prepared.get("version") or "")
        report.append({"stage": "backup", "ok": True, "message": "准备阶段已备份", "at": _now()})
        report.append({"stage": "download", "ok": True, "message": "准备阶段已下载并校验", "at": _now()})
        log(f"使用准备阶段已下载的包：{tmp.name}")
    else:
        _status_stage(root, "formal", stages, "download", "查询最新正式版…", [])
        rel = latest_release_tag()
        if not rel.get("ok"):
            return {"ok": False, "message": rel.get("message", "查询最新正式版失败")}
        ver = rel["version"]
        if not _semver_gt(ver, version.APP_VERSION):
            return {"ok": True, "to": version.APP_VERSION, "prefer_restart": "exe",
                    "message": f"已是最新正式版（{version.APP_VERSION}）"}
        if do_backup:
            _status_stage(root, "formal", stages, "backup", "备份旧产物…", [])
            bk = make_backup(root=root, kind="formal")
            if not bk.get("ok"):
                return {"ok": False, "message": bk.get("message", "备份失败")}
            report.append({"stage": "backup", "ok": True, "message": "已备份", "at": _now()})
            log(f"已备份 → {bk['path']}（{bk['message']}）")
        else:
            report.append({"stage": "backup", "ok": True, "message": "跳过（--no-backup）", "at": _now()})
        tmp = backups_dir(root) / f"_asset-{ver}.zip"
        _status_stage(root, "formal", stages, "download", f"下载预编译包 {release_asset_name(ver)}…", [])
        dl = download_release_asset(ver, tmp)
        if not dl.get("ok"):
            return {"ok": False, "message": f"{dl.get('message')}（该平台可能尚未发布对应包）"}
        kb = (dl.get("bytes") or 0) // 1024
        report.append({"stage": "download", "ok": True, "message": f"已下载 {kb} KB", "at": _now()})
        log(f"已下载（{kb} KB）")

    _status_stage(root, "formal", stages, "extract", "解压替换…", [s["stage"] for s in report])
    ex = extract_release_zip(tmp, root)
    tmp.unlink(missing_ok=True)
    if not ex.get("ok"):
        return {"ok": False, "message": ex.get("message")}
    report.append({"stage": "extract", "ok": True, "message": "已解压替换", "at": _now()})

    if do_migrate:
        _status_stage(root, "formal", stages, "migrate", "迁移数据库结构…", [s["stage"] for s in report])
        to_schema = str(prepared.get("schema_version")
                        or read_release_meta(update_meta_dir(root)).get("schema_version") or "")
        if to_schema:
            mg = run_migrations(root=root, apply=True, guard=False, to=to_schema)
            if not mg.get("ok"):
                return {"ok": False, "message": mg.get("message")}
            log(mg.get("message", "数据库已就绪"))
            report.append({"stage": "migrate", "ok": True, "message": mg.get("message", ""), "at": _now()})
        else:
            report.append({"stage": "migrate", "ok": True, "message": "目标结构版本未知，跳过", "at": _now()})
    else:
        report.append({"stage": "migrate", "ok": True, "message": "跳过（--no-migrate）", "at": _now()})
    return {"ok": True, "to": ver, "prefer_restart": "exe"}


def _apply_test(root: Path, *, frontend: bool, do_backup: bool, do_migrate: bool,
                force: bool, log: Callable[[str], None], prepared: dict, stages: list,
                report: list) -> dict:
    """测试版：备份 → 获取源码 → 建/补环境 → 构建 → 迁移。重启偏好按当前形态。

    ``prepared`` 有效时源码与构建产物已在准备阶段就绪，此处只需**安装 staging** 再迁移。
    """
    def _done() -> list:
        return [s["stage"] for s in report]

    if prepared:
        for st, msg in (("backup", "准备阶段已备份"), ("source", "准备阶段已获取源码"),
                        ("deps", "准备阶段已装依赖"), ("build", "准备阶段已构建到临时目录")):
            report.append({"stage": st, "ok": True, "message": msg, "at": _now()})
        vn = ensure_venv(root=root)      # 幂等兜底：确保构建环境仍在
        if not vn.get("ok"):
            return {"ok": False, "message": vn.get("message")}
        _status_stage(root, "test", stages, "install", "安装新版本（构建产物就位）…", _done())
        log(f"安装新版本到程序目录（来源：{prepared.get('staging_dir')}）…")
        ins = _install_staged(prepared.get("staging_dir"), root)
        if not ins.get("ok"):
            return {"ok": False, "message": ins.get("message")}
        report.append({"stage": "install", "ok": True, "message": "已安装", "at": _now()})
        log("已安装构建产物")
    else:
        if do_backup:
            _status_stage(root, "test", stages, "backup", "备份旧产物…", [])
            bk = make_backup(root=root, kind="test")
            if not bk.get("ok"):
                return {"ok": False, "message": bk.get("message", "备份失败")}
            report.append({"stage": "backup", "ok": True, "message": "已备份", "at": _now()})
            log(f"已备份 → {bk['path']}")
        else:
            report.append({"stage": "backup", "ok": True, "message": "跳过（--no-backup）", "at": _now()})

        _status_stage(root, "test", stages, "source", "获取最新源码…", _done())
        src = ensure_source(root=root, force=force)
        if not src.get("ok"):
            return {"ok": False, "message": src.get("message")}
        report.append({"stage": "source", "ok": True, "message": src.get("message", ""), "at": _now()})
        log(src.get("message", "源码已就绪"))

        _status_stage(root, "test", stages, "deps", "准备构建环境（虚拟环境 + 依赖）…", _done())
        vn = ensure_venv(root=root)
        if not vn.get("ok"):
            return {"ok": False, "message": vn.get("message")}
        py = vn["python"]
        dp = ensure_deps(root=root, python=py)
        if not dp.get("ok"):
            return {"ok": False, "message": dp.get("message")}
        report.append({"stage": "deps", "ok": True, "message": dp.get("message", ""), "at": _now()})
        log(dp.get("message", "依赖已就绪"))

        _status_stage(root, "test", stages, "build", "从源码构建（scripts/build.py --frontend）…", _done())
        # app 已退出，可直接构建到程序目录（覆盖运行中的 exe 此时已无冲突）
        bd = run_build(root=root, frontend=frontend, python=py, output=root)
        if not bd.get("ok"):
            return {"ok": False, "message": bd.get("message")}
        report.append({"stage": "build", "ok": True, "message": "构建完成", "at": _now()})
        report.append({"stage": "install", "ok": True, "message": "构建产物已就位", "at": _now()})
        log(bd.get("message", "构建完成"))

    meta = source_meta(root)                       # 新源码的版本（迁移目标）
    if do_migrate and meta.get("schema_version"):
        _status_stage(root, "test", stages, "migrate", "迁移数据库结构…", _done())
        mg = run_migrations(root=root, apply=True, guard=False, to=meta["schema_version"])
        if not mg.get("ok"):
            return {"ok": False, "message": mg.get("message")}
        report.append({"stage": "migrate", "ok": True, "message": mg.get("message", ""), "at": _now()})
        log(mg.get("message", "数据库已就绪"))
    else:
        report.append({"stage": "migrate", "ok": True, "message": "跳过", "at": _now()})
    return {"ok": True, "to": meta.get("app_version") or version.APP_VERSION,
            "prefer_restart": "exe" if is_frozen() else "source"}


def _finish(root: Path, kind: str, frm: str, result: dict, stages: list,
            *, report: Optional[list] = None,
            frm_commit: str = "", to_commit: str = "") -> dict:
    ok = bool(result.get("ok"))
    to = result.get("to") or version.APP_VERSION
    msg = "更新完成" if ok else result.get("message", "更新失败")
    if ok:
        msg = f"已更新到 {to}"
    _write_status(root, {
        "phase": "done" if ok else "failed", "ok": ok, "kind": kind,
        "from": frm, "to": to,
        # 版本号（测试版两版常相同）之外再记提交哈希——界面上「上次更新」靠它区分测试版的变更。
        "from_commit": frm_commit, "to_commit": to_commit,
        "message": msg, "stages": stages,
        "report": report or [], "log": _latest_update_log(root),
        "finished_at": _now(),
    })
    _log.info("更新流程结束：ok=%s %s", ok, msg)
    return {**result, "ok": ok, "message": msg}


# ── 应用内入口：交棒 ──────────────────────────────────────

def _self_update_argv(root: Path, kind: str) -> list[str]:
    """「自更新」进程的命令：冻结 exe 用 ``--self-update``（exe 内跑 updater），
    源码运行用 ``python main.py --self-update``。两者都**不建窗口**，跑完即退。"""
    if is_frozen():
        return [sys.executable, "--self-update", kind]
    return [sys.executable, str(root / "main.py"), "--self-update", kind]


def _spawn_updater(root: Path, kind: str) -> int:
    """把「自更新」起成脱离进程（**两平台都不弹控制台窗口**，输出落 ``data/logs/update-*.log``）。

    早先 Windows 用 ``cmd /c start ... cmd /k`` 开一个 CMD 窗口；但准备阶段已把耗时步骤（下载 / 拉源码 /
    构建）挪进应用内，交棒只剩「替换/安装 + 迁移 + 重启」这点时间，**再弹黑框纯属打扰**。
    现在与 POSIX 一致：脱离进程 + 日志文件，进度看「上次更新」报告与日志。
    """
    argv = _self_update_argv(root, kind)
    env = proxy_env()          # 把更新代理注入子进程（git / pip / npm 据此出网）
    # `proxy_env()` 只造 dict、没动本进程环境，所以此刻 os.environ 还是「更新前」的原值 ——
    # 存档给交棒进程，供它重启新应用时还原（否则新应用会一直带着更新代理）。
    env[ENV_RESTORE_PROXY] = json.dumps(
        {key: os.environ.get(key, "") for key in _PROXY_ENV_KEYS})
    log = root / "data" / "logs" / f"update-{time.strftime('%Y%m%d-%H%M%S')}.log"
    return spawn_detached(argv, cwd=root, log=log, env=env)

def begin_handoff(kind: str = "formal", *, root: Optional[Any] = None, restart: bool = True,
                  terminal: str = "auto", quit_cb: Optional[Callable[[], None]] = None,
                  prepared: Optional[dict] = None) -> dict:
    """**交棒**：写请求文件（带 ``prepared`` 指针）→ 起脱离进程 → 延时关闭应用。

    **顺序不可颠倒**：先把自更新进程起起来，确认起来了，再退出应用。
    ``prepared`` 为准备产物清单（缺省读 ``prepared_file``）；交棒进程据它走快路径。
    """
    root = repo_root(root)
    kind = kind if kind in CHANNELS else "formal"
    if kind == "test" and not git_available():
        return {"ok": False, "message": "测试版需要从源码构建：本机未安装 git，无法拉取源码。"}
    if not isinstance(prepared, dict):
        prepared = read_prepared(root)

    req = {"kind": kind, "restart": bool(restart), "terminal": terminal or "auto",
           "requested_at": _now(), "relaunch": _relaunch_spec(root),
           "prepared": bool(prepared), "prepared_file": prepared_file(root).name}
    _write_json_atomic(request_file(root), req)

    try:
        pid = _spawn_updater(root, kind)
    except Exception as exc:  # noqa: BLE001 - 起不来就绝不退出应用
        _log.error("启动更新进程失败：%s", exc)
        return {"ok": False, "message": f"无法启动更新进程：{exc}"}

    _log.info("已启动更新进程 pid=%s（渠道=%s，准备产物=%s）", pid, kind, bool(prepared))
    _write_status(root, {"phase": "spawned", "ok": None, "kind": kind, "stages": _stages_for(kind),
                         "message": "已启动更新进程，等待应用退出…"})

    if quit_cb is not None:
        # 延迟一点再退出：保证本次 IPC 的返回值能先回到前端
        def _later() -> None:
            time.sleep(0.6)
            try:
                quit_cb()
            except Exception as e:  # noqa: BLE001
                _log.warning("退出应用失败：%s", e)

        threading.Thread(target=_later, daemon=True).start()

    if kind == "formal":
        hint = "将解压替换当前程序" if prepared else "将下载预编译包并替换当前程序"
    else:
        hint = "将安装新版本并重启" if prepared else "将从源码重新构建"
    return {"ok": True, "pid": pid,
            "message": f"已开始更新：程序将关闭，{hint}，完成后自动重启。"}


def start_update(kind: str = "formal", *, root: Optional[Any] = None,
                 restart: bool = True, terminal: str = "auto",
                 quit_cb: Optional[Callable[[], None]] = None) -> dict:
    """兼容入口：**旧语义**（不准备、直接交棒）。新流程用 ``prepare_update`` + ``begin_handoff``。"""
    return begin_handoff(kind, root=repo_root(root), restart=restart, terminal=terminal, quit_cb=quit_cb)
