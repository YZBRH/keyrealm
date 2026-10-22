"""
Keyrealm — 入口
pywebview 主窗口 + 系统托盘 + MCP Server + 调度器
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# 确保项目根目录在 sys.path
BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ── Linux：兜住「GPU/GL 路径不可靠」的环境（虚拟机 / 无 GPU / WSL / NVIDIA 专有驱动）──
# 同一个根因（这些环境里 GL 路径不完整）会以两种症状冒出来：
#   · WebKitGTK：**dmabuf 渲染器**会让窗口完全黑屏——不崩溃、日志无痕，用户只看到一片黑。
#     回退办法是关掉 dmabuf 渲染器。
#   · Qt（QtWebEngine）：Qt 的 GLX 探测失败会**直接 abort**——
#     `qt.glx: qglx_findConfig: Failed to finding matching FBConfig` → `Could not initialize
#     GLX` → `IOT instruction`，窗口根本出不来。回退办法是软件渲染（`QT_OPENGL=software`）
#     + 让内嵌 Chromium 不碰 GPU（`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`）——
#     **两个必须同时**，实测只设其一仍崩。
#
# **不在所有机器上一刀切**：健康环境什么都不动，保留完整 GPU 加速；只有**能明确判断有风险**
# 时才回退。取舍：宁可**误判**（代价只是走软件渲染，界面依旧可用），也不要**漏判**（代价是
# 纯黑窗口或直接崩溃、且毫无线索）。有真 GPU 想强制走硬件：Qt 设 `QT_OPENGL=desktop`，
# GTK 设 `WEBKIT_DISABLE_DMABUF_RENDERER=0`。
_GPU_RISK_REASON = ""


def _risky_gpu_reason() -> str:
    """Linux 上「GPU/GL 路径不可靠」的环境判据；返回原因，空串 = 健康环境。

    判据都取「有明确证据」的那几类：NVIDIA 专有驱动、WSL、没有 DRM 渲染节点、虚拟机。
    任何一步探测失败都不抛异常，按「没命中」处理。
    """
    try:
        if os.path.exists("/proc/driver/nvidia/version"):
            return "NVIDIA 专有驱动"
    except OSError:
        pass
    # WSL / WSLg：图形走 /dev/dxg + d3d12，**没有**标准 DRM 渲染节点，Qt 的 GLX 探测会失败。
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return "WSL"
    try:
        if "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower():
            return "WSL（内核标识）"
    except OSError:
        pass
    try:
        dri = Path("/dev/dri")
        if not dri.is_dir() or not any(dri.glob("renderD*")):
            return "没有 DRM 渲染节点（无可用 GPU）"
    except OSError:
        pass
    dmi = ""
    for path in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/sys_vendor"):
        try:
            dmi += Path(path).read_text(encoding="utf-8", errors="replace").strip().lower() + " "
        except OSError:
            pass
    for marker in ("virtualbox", "vmware", "qemu", "kvm", "innotek",
                   "microsoft corporation", "parallels", "xen", "bochs", "virtual machine"):
        if marker in dmi:
            return f"虚拟机（{marker}）"
    return ""


if sys.platform.startswith("linux"):
    _GPU_RISK_REASON = _risky_gpu_reason()
    if _GPU_RISK_REASON:
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        # Qt 三件套（实测**缺一不可**，少任何一条主窗口都渲染不出来）：
        #   · QT_QUICK_BACKEND=software —— Qt 6 的 WebEngine 内部走 Qt Quick 渲染栈，
        #     RHI 建不出 GL context 时报 `QQuickWidget: Attempted to render scene with no rhi`，
        #     主窗口空白或不出现；这一条让它改走 CPU 软件渲染。**最关键的一条。**
        #   · QT_OPENGL=software —— Qt 自身的 GL 也退回软件。
        #   · QTWEBENGINE_CHROMIUM_FLAGS 追加 --disable-gpu —— 内嵌 Chromium 别碰 GPU。
        # 一律 setdefault / 追加，不覆盖用户显式设置。
        # xcb（X11）平台：**必须禁掉 GLX 集成**。某些 X server（VMware 未开 3D 加速、
        # 远程 X、纯 X11 转发）的 GLX 探测失败会让 Qt **直接 abort**
        # （`qt.glx: … Failed to finding matching FBConfig` → `Could not initialize GLX`
        # → `IOT instruction`），窗口根本出不来；禁掉后 Qt 走 Qt Quick 的软件后端即可正常渲染。
        # wayland 平台忽略这个变量，无副作用。
        os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
        os.environ.setdefault("QT_OPENGL", "software")
        _qtweb_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if "--disable-gpu" not in _qtweb_flags:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{_qtweb_flags} --disable-gpu".strip()

import webview  # pywebview

from backend import api, autostart, database as db, idle_lock, logger, mcp_server, plugin_manager, proxy as proxy_config, scheduler, single_instance, tls_store, tray, verify_options, version
from backend import vault_proxy_ctx, vault_tls_ctx
from backend.paths import app_dir, is_frozen, resource_dir

# 把「按调用生效的代理 / TLS 版本上下文」以**固定别名**注册进 sys.modules：插件侧
# proxy() / tls_version() 靠这两个名字懒查（插件不直接 import backend 内部模块，只允许
# import backend.plugin_sdk，见 PLUGIN_GUIDE §3）。必须在任何插件调用之前完成，所以放模块级。
sys.modules.setdefault("vault_proxy_ctx", vault_proxy_ctx)
sys.modules.setdefault("vault_tls_ctx", vault_tls_ctx)


class VaultApi:
    """
    pywebview JS-API 暴露类
    所有方法都是同步的（pywebview 在线程池调用）
    """

    # ── Auth ──────────────────────────────────────────────
    def is_initialized(self):
        return api.is_initialized()

    def is_unlocked(self):
        return api.is_unlocked()

    def setup_master_password(self, password: str):
        return api.setup_master_password(password)

    def unlock(self, password: str):
        return api.unlock(password)

    def lock_vault(self):
        return api.lock_vault()

    def change_master_password(self, old_password: str, new_password: str):
        return api.change_master_password(old_password, new_password)

    # ── Credentials ───────────────────────────────────────
    def list_credentials(self, group_id=None, credential_type=None, status=None,
                         search=None, credential_types=None):
        return api.list_credentials(group_id, credential_type, status, search, credential_types)

    def get_credential(self, credential_id: int):
        return api.get_credential(credential_id)

    def create_credential(self, name: str, credential_type: str, data: dict,
                          group_id=None, auto_verify: bool = True):
        return api.create_credential(name, credential_type, data, group_id, auto_verify)

    def update_credential(self, credential_id: int, name=None, group_id=None,
                          data=None, auto_verify: bool = False):
        return api.update_credential(credential_id, name, group_id, data, auto_verify)

    def delete_credential(self, credential_id: int):
        return api.delete_credential(credential_id)

    def delete_credentials(self, credential_ids):
        return api.delete_credentials(credential_ids)

    def verify_credential(self, credential_id: int):
        return api.verify_credential(credential_id)

    def batch_verify(self, scope: str = "all", group_id=None, credential_type=None,
                     credential_ids=None, concurrency=None, timeout=None):
        return api.batch_verify(scope, group_id, credential_type, credential_ids,
                                concurrency, timeout)

    def get_batch_options(self):
        return api.get_batch_options()

    def check_duplicate_credential(self, credential_type: str, data: dict):
        return api.check_duplicate_credential(credential_type, data)

    def find_duplicate_credentials(self, credential_ids=None, group_id=None,
                                   credential_type=None, credential_types=None, search=None):
        return api.find_duplicate_credentials(
            credential_ids, group_id, credential_type, credential_types, search,
        )

    def resolve_duplicate_group(self, keep_id: int, remove_ids: list, merge_notes: bool = False):
        return api.resolve_duplicate_group(keep_id, remove_ids, merge_notes)

    # ── Groups ────────────────────────────────────────────
    def list_groups(self):
        return api.list_groups()

    def create_group(self, name: str, description: str = "", color: str = "#409EFF"):
        return api.create_group(name, description, color)

    def update_group(self, group_id: int, name=None, description=None, color=None):
        return api.update_group(group_id, name, description, color)

    def delete_group(self, group_id: int):
        return api.delete_group(group_id)

    # ── Plugins ───────────────────────────────────────────
    def list_plugins(self):
        return api.list_plugins()

    def set_plugin_enabled(self, credential_type: str, enabled: bool):
        return api.set_plugin_enabled(credential_type, enabled)

    def list_credential_actions(self, credential_id: int):
        return api.list_credential_actions(credential_id)

    def credential_capabilities(self, credential_id: int):
        return api.credential_capabilities(credential_id)

    def pick_save_path(self, default_name: str = "", file_types=None):
        return api.pick_save_path(default_name, file_types)

    def run_credential_action(self, credential_id: int, action_id: str, params=None):
        return api.run_credential_action(credential_id, action_id, params)

    def list_credential_models(self, credential_id: int, force: bool = False):
        return api.list_credential_models(credential_id, force)

    def get_version(self):
        return api.get_version()

    def list_draft_models(self, credential_type: str, data=None, force: bool = False):
        return api.list_draft_models(credential_type, data, force)

    def stream_credential_action(self, credential_id: int, action_id: str, params=None, title: str = ""):
        return api.stream_credential_action(credential_id, action_id, params, title)

    # ── 后台任务 ──
    def list_tasks(self):
        return api.list_tasks()

    def cancel_task(self, task_id: str):
        return api.cancel_task(task_id)

    def clear_finished_tasks(self):
        return api.clear_finished_tasks()

    # ── MCP ───────────────────────────────────────────────
    def get_mcp_status(self):
        return api.get_mcp_status()

    def start_mcp(self):
        return api.start_mcp()

    def stop_mcp(self):
        return api.stop_mcp()

    def get_mcp_options(self):
        return api.get_mcp_options()

    def set_mcp_options(self, host: str, port):
        return api.set_mcp_options(host, port)

    def list_mcp_permissions(self):
        return api.list_mcp_permissions()

    def set_mcp_permission(self, tool_name: str, enabled: bool):
        return api.set_mcp_permission(tool_name, enabled)

    def get_mcp_token(self):
        return api.get_mcp_token()

    def rotate_mcp_token(self):
        return api.rotate_mcp_token()

    # ── Schedules ─────────────────────────────────────────
    def list_schedules(self):
        return api.list_schedules()

    def create_schedule(self, name: str, trigger_type: str, trigger_args: dict,
                        target_scope: str = "all", target_id=None, credential_ids=None):
        return api.create_schedule(name, trigger_type, trigger_args, target_scope, target_id,
                                   credential_ids)

    def update_schedule(self, schedule_id: int, payload=None):
        # 与 api.update_schedule(schedule_id, payload) 对齐：pywebview 桥只走**位置参数**，
        # 前端是 call('update_schedule', id, payload) —— 这里若写成 **kwargs 就永远接不到第二参，
        # 定时任务的启停/编辑会直接 TypeError。
        return api.update_schedule(schedule_id, payload)

    def delete_schedule(self, schedule_id: int):
        return api.delete_schedule(schedule_id)

    # ── Tray ──────────────────────────────────────────────
    def minimize_to_tray(self):
        return api.minimize_to_tray()

    def log_frontend_error(self, message=""):
        """前端把用户看不见的失败回传到后端日志（见 api.log_frontend_error）。"""
        return api.log_frontend_error(message)

    def get_tray_status(self):
        return api.get_tray_status_api()

    # ── 关闭窗口行为 ──────────────────────────────────────
    def get_close_action(self):
        return api.get_close_action()

    def set_close_action(self, action: str):
        return api.set_close_action(action)

    def confirm_close_action(self, action: str, remember: bool = False):
        return api.confirm_close_action(action, remember)

    # ── 空闲自动锁定 ──────────────────────────────────────
    def get_auto_lock(self):
        return api.get_auto_lock()

    def set_auto_lock(self, minutes):
        return api.set_auto_lock(minutes)

    def touch_activity(self):
        return api.touch_activity()

    # ── 轻量模式 ──────────────────────────────────────────
    def get_light_mode(self):
        return api.get_light_mode()

    def set_light_mode(self, enabled: bool):
        return api.set_light_mode(enabled)

    # ── 开机自启动 ────────────────────────────────────────
    def get_autostart(self):
        return api.get_autostart()

    def set_autostart(self, enabled: bool):
        return api.set_autostart(enabled)

    # ── 凭据目录树顺序 ────────────────────────────────────
    def get_tree_order(self):
        return api.get_tree_order()

    def set_tree_order(self, categories=None, plugins=None):
        return api.set_tree_order(categories, plugins)

    # ── 验证代理 ──────────────────────────────────────────
    # ── 验证代理（注册表） ─────────────────────────────────
    def list_proxies(self):
        return api.list_proxies()

    def create_proxy(self, name: str, url: str):
        return api.create_proxy(name, url)

    def update_proxy(self, proxy_id: int, name=None, url=None):
        return api.update_proxy(proxy_id, name, url)

    def delete_proxy(self, proxy_id: int):
        return api.delete_proxy(proxy_id)

    def set_global_proxy(self, proxy_id=None):
        return api.set_global_proxy(proxy_id)

    def test_proxy_id(self, proxy_id=None):
        return api.test_proxy_id(proxy_id)

    # ── TLS 版本策略 ──────────────────────────────────────
    def get_tls_options(self):
        return api.get_tls_options()

    def set_tls_version(self, value):
        return api.set_tls_version(value)

    # ── 类别 / 插件默认设置 ────────────────────────────────
    def get_credential_defaults(self, credential_type: str):
        return api.get_credential_defaults(credential_type)

    def get_scope_options(self, scope: str, scope_key: str, credential_type: str = ""):
        return api.get_scope_options(scope, scope_key, credential_type)

    def set_scope_options(self, scope: str, scope_key: str, values: dict, credential_type: str = ""):
        return api.set_scope_options(scope, scope_key, values, credential_type)

    def clear_scope_options(self, scope: str, scope_key: str):
        return api.clear_scope_options(scope, scope_key)

    # ── 备份 / 恢复 ───────────────────────────────────────
    def export_backup(self, scope: str = "all", encrypted: bool = True, credential_ids=None):
        return api.export_backup(scope, encrypted, credential_ids)

    def import_backup(self, orig_password: str = None, path: str = None):
        return api.import_backup(orig_password, path)

    # ── Logging ───────────────────────────────────────────
    def get_log_config(self):
        return api.get_log_config()

    def set_log_level(self, level: str):
        return api.set_log_level(level)

    def get_logs(self, limit: int = 200, level: str = "", search: str = ""):
        return api.get_logs(limit, level, search)

    def clear_logs(self):
        return api.clear_logs()

    def open_log_file(self, name: str):
        return api.open_log_file(name)

    # ── 程序内更新 ────────────────────────────────────────
    def update_check(self, kind: str = "formal"):
        return api.update_check(kind)

    def update_status(self):
        return api.update_status()

    def db_migration_status(self):
        return api.db_migration_status()

    def get_update_channel(self):
        return api.get_update_channel()

    def set_update_channel(self, channel: str):
        return api.set_update_channel(channel)

    def get_update_proxy(self):
        return api.get_update_proxy()

    def set_update_proxy(self, mode: str, proxy_id: str = ""):
        return api.set_update_proxy(mode, proxy_id)

    def get_update_token(self):
        return api.get_update_token()

    def set_update_token(self, token: str):
        return api.set_update_token(token)

    def get_update_auto_check(self):
        return api.get_update_auto_check()

    def set_update_auto_check(self, enabled: bool):
        return api.set_update_auto_check(enabled)

    def take_update_notice(self):
        return api.take_auto_update_notice()

    def start_update_check(self):
        return api.start_update_check()

    def get_update_check_result(self):
        return api.get_update_check_result()

    def start_update(self, kind: str = "formal", restart: bool = True, terminal: str = "auto"):
        return api.start_update(kind, restart, terminal)

    def apply_prepared_update(self, restart: bool = True, terminal: str = "auto"):
        return api.apply_prepared_update(restart, terminal)


# ── 窗口与托盘 ────────────────────────────────────────────

_window: webview.Window | None = None
_quitting = False

# 窗口状态跟踪（最大化 / 最小化）。托盘唤出时必须知道「隐藏前是什么状态」：
# pywebview 的 `show()` 不保证从最小化恢复，而 `restore()` 在 Windows 上等价于
# `WindowState = Normal`——对最大化窗口一并还原，于是出现「全屏后缩到托盘、再唤出
# 时窗口缩回去」。状态由 pywebview 的窗口事件维护，唤出时只做必要的恢复。
_window_state = {"maximized": False, "minimized": False}

# 轻量模式：界面文档已被释放（换成了占位页）时置 True。见 _release_page / _restore_page。
_page_released = False
# 释放页面要**延后**这么多秒（见 _hide_to_tray）：得让「最小化到托盘」这个 JS 调用的返回值
# 先写回页面，否则 pywebview 会往一个已经被换掉的文档里写回调，抛 JavascriptException。
_RELEASE_DELAY_SEC = 0.8
_page_released_lock = threading.Lock()

# 启动时实际交给 pywebview 的前端 URL。轻量模式恢复时**原样**重载它：
# 恢复必须回到与首次加载完全相同的 URL，否则 origin 一变，localStorage 里的
# 主题、列顺序、目录树展开状态就全丢了。
_app_url = ""


def _get_frontend_path() -> str:
    """返回前端 dist/index.html 路径。

    frontend/dist 随包分发（只读），冻结后从 _MEIPASS 取；开发时从项目根取。
    """
    root = resource_dir()
    dist = root / "frontend" / "dist" / "index.html"
    if dist.exists():
        return str(dist)
    # 没有构建过前端：`frontend/dist` 是 gitignore 的，源码树里**天然没有**。下面会退到 Vite 的
    # **开发**入口（只有 `npm run dev` 能服务它），直接加载就是一片空白——所以要明确说一句，
    # 否则用户只看到一个黑/白窗口，毫无线索可循。
    logger.get_logger("main").warning(
        "未找到前端构建产物 %s —— 窗口将是空白的。请先构建前端："
        "cd frontend && npm ci && npm run build", dist)
    return str(root / "frontend" / "index.html")


def _to_url(path_or_url: str) -> str:
    """把窗口入口转成可以安全交给 Window.load_url 的 URL。

    ⚠ 绝不能把本地路径裸传给 load_url。pywebview 把"非 http(s)/file 的字符串"
    都当作需要内置 HTTP 服务的目标（webview/util.py:is_local_url），会现起一个
    http://127.0.0.1:<随机端口> 来托管它——origin 就从 file:// 变成了 http://，
    与首次加载不是同一个源，localStorage 当场清零（主题、列顺序全没）。
    转成 file:// 后 is_local_url 返回 False，load_url 会原样导航过去，origin 不变。
    """
    path = Path(path_or_url)
    if path.exists():
        return path.as_uri()
    return path_or_url  # 已是 http(s):// 的地址原样使用


def _shutdown_services():
    """停止后台服务并标记为正在退出（供"直接退出"路径共用的清理）。"""
    global _quitting
    logger.get_logger("main").info("用户请求退出应用")
    _quitting = True
    scheduler.stop_scheduler()
    idle_lock.stop()
    mcp_server.stop_server()
    tray.stop_tray()


def _hide_to_tray():
    """隐藏窗口到系统托盘（后台运行）。**不弹任何提示**——缩到托盘是常规操作，弹一次反而吵。

    轻量模式开启时顺带释放界面文档（见 _release_page）：托盘常驻期间前端不再占内存。

    **托盘没挂上时直接不隐藏**（`tray.available()` 为假）：否则窗口藏起来却没有托盘可以
    叫回，用户只能去杀进程。
    """
    if _window is None or not tray.available():
        return
    try:
        _window.hide()
    except Exception as e:
        # 隐藏失败还去释放页面的话，用户会看到占位页却不知道为什么——这里直接放弃释放。
        logger.get_logger("main").warning("隐藏窗口失败，跳过界面释放：%s", e)
        return
    if api.read_light_mode():
        # 页面一换，当前文档就没了；而此刻 pywebview 很可能**还在把本次 JS 调用的返回值**
        # 写回页面（「最小化到托盘」就是从界面按钮过来的一个 JS 调用）——换早了，这趟写回就
        # 落到一个已不存在的回调上，抛 JavascriptException（Linux 上稳定复现）。
        # 延后一拍：让返回值先落地，再换页。
        timer = threading.Timer(_RELEASE_DELAY_SEC, _release_page)
        timer.daemon = True
        timer.start()


# 轻量模式释放后显示的那一页。正常流程下用户看不到它（窗口是隐藏的），
# 只有在"界面已被释放、窗口却被外部直接拉起"（例如重复启动 exe 触发
# single_instance.focus_existing 的 ShowWindow）时才会露面，所以必须写明怎么恢复。
_LIGHT_PLACEHOLDER = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Keyrealm</title>
<style>
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; align-items: center; justify-content: center;
    background: #09090b; color: #a1a1aa;
    font: 14px/1.8 "Microsoft YaHei", system-ui, sans-serif;
  }
  main { text-align: center; padding: 0 24px; }
  h1 { margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #e4e4e7; }
  p { margin: 0; }
</style>
</head>
<body>
<main>
  <h1>轻量模式：界面已释放</h1>
  <p>窗口最小化期间前端不占用内存。</p>
  <p>从托盘菜单选择「打开窗口」即可恢复界面。</p>
</main>
</body>
</html>
"""


def _release_page():
    """轻量模式：把界面换成占位页，释放前端文档（DOM / JS 堆 / 各组件实例）。

    调用前窗口必须已经隐藏（_hide_to_tray 负责），否则用户会看到换页过程。
    只换文档、不销毁窗口：销毁最后一个 pywebview 窗口会直接结束 GUI 主循环，
    整个程序就退出了（winforms 后端的 Application.Exit），托盘常驻无从谈起。
    """
    global _page_released
    with _page_released_lock:
        if _page_released or _window is None:
            return
        if not _app_url.startswith(("file://", "http://", "https://")):
            # 拿不准怎么退回来就别释放：宁可不省这点内存，也不能把用户关在一个
            # 恢复不回来的占位页上。见 _to_url。
            logger.get_logger("main").warning(
                "轻量模式：前端入口 %r 不是 file:// 或 http(s)://，本次不释放界面", _app_url
            )
            return
        try:
            _window.load_html(_LIGHT_PLACEHOLDER)
        except Exception:
            logger.get_logger("main").exception("轻量模式释放界面失败，保持原页面")
            return
        _page_released = True
        api.mark_page_released(True)


def _restore_page():
    """轻量模式：重新加载前端入口。必须在窗口已显示之后再调用（load_url 需要窗口可见）。

    用 _app_url（file:// 形式）而不是裸路径，原因见 _to_url 的注释。
    """
    global _page_released
    with _page_released_lock:
        if not _page_released or _window is None:
            return
        try:
            _window.load_url(_app_url)
        except Exception:
            # 失败就保持 _page_released=True：占位页上已写明"从托盘打开窗口"作为兜底，
            # 下次再从托盘点一次会重试。
            logger.get_logger("main").exception("轻量模式恢复界面失败")
            return
        _page_released = False
        api.mark_page_released(False)


# ── GUI 后端缺失（Linux）──────────────────────────────────────────────────
# pywebview 在 Linux 上要么用 GTK、要么用 Qt，两者都没有时它抛 WebViewException。
# 把那段 traceback 原样甩给用户毫无用处（尤其对预编译包：**运行期再装系统包也没用**），
# 所以拦下来，换成能照着做的指引。
_GUI_BACKEND_HINT_SOURCE = """\
Keyrealm 需要一个 WebView 后端才能显示界面，但当前环境里 GTK 与 Qt 都没有。

【首选 · Qt】纯 pip，装进**当前这个** Python 环境即可，不依赖任何系统包：
      pip install "pywebview[pyside6]"
      ⚠️ glibc 较老的机器（如 CentOS/RHEL 8 = glibc 2.28）要钉版本——PySide6 6.10 起要求
         glibc >= 2.34，装不上；改用 6.9.3（其 wheel 是 manylinux_2_28）：
             pip install "pywebview[pyside6]" "PySide6==6.9.3"

【或者 · GTK】用系统自带的 GTK/WebKit（体积小），但**只在你这个 Python 与系统 Python
是同一个版本时**才可行——系统装的 gi 是按系统 Python 编的，版本对不上就导不进来。
（典型：CentOS 8 的系统 Python 是 3.6，而你跑的是 3.12 → 用不了 GTK，请走上面的 Qt。）

  Debian / Ubuntu / Kali
      sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
  Fedora
      sudo dnf install python3-gobject gtk3 webkit2gtk4.1
  CentOS / RHEL 8
      sudo dnf install python3-gobject gtk3 webkit2gtk3
      （CentOS 8 官方源已 EOL：先把 yum 源指向 vault.centos.org 才装得上，见 README）
  Arch / Manjaro
      sudo pacman -S python-gobject gtk3 webkit2gtk-4.1

走 GTK 时请用**系统 Python** 运行（python3 main.py）；要留在 venv 就在建 venv 时加
--system-site-packages，且解释器版本必须与系统一致。
"""

_GUI_BACKEND_HINT_FROZEN = """\
这个预编译包里没有内嵌 Linux 的 WebView 后端（GTK / Qt），所以起不了窗口。

**运行期再装系统包没有用**——PyInstaller 打出来的程序只认打包时收进去的东西。
请改从源码运行（见 README 的「Linux」一节），或换一个带了 GUI 后端的构建。
"""


# Qt 6.5 起，xcb（X11）平台插件需要系统里的 **libxcb-cursor**；缺了它窗口根本起不来，
# 报错是「Could not load the Qt platform plugin "xcb"」。它**不是**「GTK/Qt 都没装」——
# 得单独给指引，否则会误导用户去装后端（装了也没用）。
_LIBXCB_CURSOR_HINT = """\
Qt 的 xcb 平台插件起不来 —— 从 Qt 6.5 起它需要系统里的 **libxcb-cursor** 这个库。

  Debian / Ubuntu / Kali   sudo apt install libxcb-cursor0
  Fedora / CentOS / RHEL   sudo dnf install xcb-util-cursor
  Arch / Manjaro           sudo pacman -S xcb-util-cursor

装完重新启动即可。注意它是**系统库**、打不进预编译包，所以预编译包也要在系统里装一次。
"""


def _is_xcb_cursor_error(exc: BaseException) -> bool:
    """是不是「Qt 的 xcb 平台插件因缺 libxcb-cursor 加载失败」。

    判据取 Qt 自己打的那几句（``Could not load the Qt platform plugin "xcb"`` /
    ``xcb-cursor0 or libxcb-cursor0 is needed``）。**必须在 `_is_missing_gui_backend` 之前判断** ——
    这类报错文本里也带 "Qt"，否则会被误当成「后端都没装」而给错指引。
    """
    text = str(exc).lower()
    if "xcb" not in text:
        return False
    return "platform plugin" in text or "libxcb-cursor" in text or "xcb-cursor0" in text


def _report_missing_libxcb_cursor(exc: BaseException) -> None:
    """缺 libxcb-cursor：打印可照做的指引，并记进日志。"""
    print("\n" + _LIBXCB_CURSOR_HINT, file=sys.stderr, flush=True)
    try:
        logger.get_logger("main").error("Qt 的 xcb 平台插件加载失败（多半缺 libxcb-cursor）：%s", exc)
    except Exception:  # noqa: BLE001 - 日志失败不该盖过这条提示
        pass


def _is_missing_gui_backend(exc: BaseException) -> bool:
    """是不是「GTK / Qt 都不在」这一类错误。

    只认 pywebview 针对「没有后端」抛的那一种，别的 WebViewException（例如「窗口已创建」）
    照旧抛出去——那是真 bug，不该被伪装成环境问题。xcb 插件那类**另行判断**（见上）。
    """
    if type(exc).__name__ != "WebViewException":
        return False
    if _is_xcb_cursor_error(exc):
        return False
    text = str(exc)
    return "QT" in text or "GTK" in text


#: 界面后端的显式选择：``qt`` / ``gtk``（空 = 自动）。源码运行与打包都认这个变量。
GUI_ENV_KEY = "KEYREALM_GUI"


def _baked_gui() -> str:
    """冻结包**构建时**选定的界面后端（``build.py --gui`` 写进 ``_build_info.GUI``）；无则空串。"""
    try:
        from backend import _build_info          # 由 scripts/build.py 生成，不入库
        return str(getattr(_build_info, "GUI", "") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 源码运行 / 从未构建过
        return ""


def _preferred_gui() -> str | None:
    """优先用哪个 WebView 后端；``None`` = 交给 pywebview 自己挑（即平台默认）。

    取值优先级：
      1. 环境变量 ``KEYREALM_GUI``（``qt`` / ``gtk``）—— 显式指定，源码运行与打包都认；
      2. 冻结包**构建时**选定的那个（``build.py --gui`` 写进 ``_build_info.GUI``）；
      3. Linux 的默认：有 Qt 就 Qt，否则返回 ``None`` 交回 pywebview（会退到 GTK）；
         Windows / macOS 一律 ``None``，继续用系统自带的 WebView2 / WebKit。

    Linux 默认偏好 Qt 的缘由见 README 的 Linux 一节：PyInstaller 能把 PySide6 整包收进去 →
    冻出来的程序**自包含**；而 GTK 走的是宿主 typelib + 一整棵 WebKit 依赖树，冻结后仍依赖
    宿主机（实测会死在 `gi.overrides` 的版本错配上）。想改用 GTK：``KEYREALM_GUI=gtk``。
    """
    explicit = (os.environ.get(GUI_ENV_KEY) or "").strip().lower()
    if explicit in ("qt", "gtk"):
        return explicit
    if not sys.platform.startswith("linux"):
        return None
    baked = _baked_gui()
    if baked in ("qt", "gtk"):
        return baked
    try:
        import qtpy  # noqa: F401  —— pywebview 的 Qt 后端入口；装了才说明 Qt 可用
        return "qt"
    except Exception:  # noqa: BLE001
        return None


def _report_missing_gui_backend(exc: BaseException) -> None:
    """GUI 后端缺失：打印可照做的指引（而不是 traceback），并记进日志。"""
    hint = _GUI_BACKEND_HINT_FROZEN if is_frozen() else _GUI_BACKEND_HINT_SOURCE
    print("\n" + hint, file=sys.stderr, flush=True)
    try:
        logger.get_logger("main").error("无可用 GUI 后端（pywebview）：%s", exc)
    except Exception:  # noqa: BLE001 - 日志失败不该盖过这条提示
        pass


def _exit_cleanly() -> None:
    """收尾：若仍有**非守护线程**活着，直接结束进程——否则会「日志说退了、进程却挂着」。

    这类线程不在我们的管控里，也不吃 `daemon=True`，常见来源：
      * 托盘后端（pystray 的 AppIndicator / GTK 路径）内部起的 GLib 主循环线程；
      * GTK / GLib 自己创建的线程；
      * `ThreadPoolExecutor` 的 worker —— **Python 会在 atexit 里 join 它们**（且不设超时），
        只要有一个卡住，解释器就永久停在这儿（`backend/scheduler.py` 有个模块级
        `ThreadPoolExecutor`，pywebview 的 JS 桥也用自己的池）。
    所以退出前点名记一笔，然后强制退出；日志里那串名字就是将来要收口的目标。
    """
    leftovers = [
        t for t in threading.enumerate()
        if t is not threading.main_thread() and t.is_alive() and not t.daemon
    ]
    if not leftovers:
        return
    logger.get_logger("main").warning(
        "退出时仍有非守护线程存活（进程本会卡住），已强制退出。线程：%s",
        [(t.name, t.ident) for t in leftovers],
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - 收尾阶段，刷不动就算了
            pass
    os._exit(0)      # 跳过 atexit（正是它卡住的），直接退


def _on_closing():
    """窗口关闭事件：按用户设置的关闭行为处理（询问/直接退出/后台运行）。"""
    if _quitting:
        return True  # 正在退出，允许关闭

    action = db.get_setting("close_action", "ask").strip().lower()
    if action not in ("ask", "quit", "background"):
        action = "ask"

    if action == "quit":
        _shutdown_services()
        return True  # 允许关闭，应用随之退出
    if action == "background":
        if not tray.available():
            # 托盘没起来（如 macOS 上启动失败 / KEYREALM_NO_TRAY）：不能「隐藏」——
            # 窗口藏了就没法叫回来，直接退出更安全。
            _shutdown_services()
            return True
        _hide_to_tray()
        return False  # 阻止关闭，驻留托盘
    # ask：先阻止本次关闭，通知前端弹窗让用户选择。
    # 注意：closing 事件运行在 GUI 线程，而 evaluate_js 需要 GUI 线程执行 JS
    # 后才能返回——同步调用会互相等待导致窗口卡死，因此放到守护线程异步推送。
    threading.Thread(
        target=api.push_event, args=("vault:close_confirm",), daemon=True
    ).start()
    return False


def _on_window_maximized():
    """窗口进入最大化（含 pywebview 的全屏模式，它内部也是 Maximized）。"""
    _window_state["maximized"] = True
    _window_state["minimized"] = False


def _on_window_minimized():
    _window_state["minimized"] = True


def _on_window_restored():
    """窗口回到 Normal（从最大化或最小化恢复）。"""
    _window_state["maximized"] = False
    _window_state["minimized"] = False


def _show_window():
    """把窗口从托盘调出来（托盘菜单 / 关闭后再次打开）。

    **恢复顺序有讲究**：`restore()` 在 Windows 上是 `WindowState = Normal`，
    对最大化窗口会把状态一起清掉。所以只有确实最小化时才调用它；最大化状态用
    `maximize()` 还原，普通窗口靠 `show()` 即可保持原样。
    """
    if _window is None:
        return
    # 先取快照：`show()` 自身可能触发 resize/restored 事件，别让事件把手里的状态改掉。
    was_maximized = _window_state["maximized"]
    was_minimized = _window_state["minimized"]
    # 顺序：先显示、后恢复页面。load_url 要求窗口已显示；万一重载失败，
    # 用户至少能看到窗口与占位页上的提示，而不是一个"看起来没反应"的程序。
    _window.show()
    if was_maximized:
        _window.maximize()
    elif was_minimized:
        _window.restore()
    _restore_page()


def _lock_vault():
    api.lock_vault()


def _quit_app():
    _shutdown_services()
    if _window:
        _window.destroy()


def main():
    global _window, _app_url

    # 单实例闸门：已经有实例在跑时，把它的窗口拉到前台然后自己退出，而不是再起一套。
    # 放在最前面（日志都还没配），因为重复启动不该产生任何副作用（连日志都不写）。
    if not single_instance.acquire():
        single_instance.focus_existing()
        sys.exit(0)

    # 尽早初始化日志（默认级别），待数据库就绪后再按用户持久化的级别调整
    logger.setup_logging()
    log = logger.get_logger("main")

    # 初始化数据库
    db.init_db()

    # 应用持久化的日志级别（settings 表）
    persisted = db.get_setting("log_level", "").strip().upper()
    if persisted in logger.LEVELS:
        logger.set_level(persisted)

    # 应用持久化的验证代理（写入 VAULT_PROXY 供插件读取；外部同名变量优先）
    proxy_config.apply_proxy(db.get_setting("verify_proxy", ""))

    # 应用持久化的验证超时（写入 VAULT_TIMEOUT 供插件读取；外部同名变量优先）
    verify_options.apply_timeout()

    # 应用持久化的 TLS 版本策略（写入 VAULT_TLS 供插件读取；外部同名变量优先）
    tls_store.apply_global()

    log.info("应用启动（版本=%s，frozen=%s，日志级别=%s，数据目录=%s）",
             version.version_string(), is_frozen(), logger.get_level(), app_dir())

    # 上面那段 Linux 图形回退是**模块级**做的（必须早于 `import webview`），那时日志还没配好，
    # 所以原因在这里补记一笔——否则用户想查「为什么我的 GPU 加速没生效」会毫无线索。
    if _GPU_RISK_REASON:
        log.info("图形回退：已退回软件渲染（原因：%s）——Qt 用 QT_XCB_GL_INTEGRATION=none + "
                 "QT_QUICK_BACKEND/QT_OPENGL=software + Chromium --disable-gpu，GTK 关 dmabuf "
                 "渲染器；有真 GPU 想强制走硬件可自行覆盖", _GPU_RISK_REASON)

    # 迁移改名（Credential Vault → Keyrealm）留在系统里的旧自启条目：不清掉的话，
    # 开关会显示「已关闭」而系统里仍有一条指向旧 exe 的条目，开机弹「找不到文件」。
    # 幂等：没有旧条目时什么都不做（见 backend/autostart.py 的 migrate_legacy）。
    if autostart.migrate_legacy():
        log.info("已把开机自启条目迁移到新名字（Keyrealm）")

    # 扫描 plugins/ 装载插件
    plugin_manager.sync_plugins()

    # 注入回调
    api.init_callbacks()

    # 启动调度器
    scheduler.start_scheduler()

    # 启动空闲自动锁定看门狗（是否真的锁由 settings.auto_lock_minutes 决定，默认关闭）
    idle_lock.start()

    # MCP：按上次退出前的开关状态恢复（上次开着就自动开）。端口被占用时只记日志，不改开关。
    api.autostart_mcp()

    # 创建窗口。窗口入口的 file:// 形式另存一份：轻量模式恢复界面时用它重载（见 _restore_page）。
    _frontend = _get_frontend_path()
    _app_url = _to_url(_frontend)
    _window = webview.create_window(
        title="Keyrealm",
        url=_frontend,
        js_api=VaultApi(),
        width=1200,
        height=800,
        min_size=(900, 600),
        resizable=True,
        confirm_close=False,
    )

    # 注入 window 引用与关闭行为回调到 api 模块
    api.set_window(_window)
    api.set_close_callbacks(quit_cb=_quit_app, hide_cb=_hide_to_tray)

    # 启动托盘
    tray.start_tray(
        show_window_cb=_show_window,
        lock_vault_cb=_lock_vault,
        quit_cb=_quit_app,
        get_status_cb=api.get_tray_status,
    )

    # 启动时自动检测一次更新（默认开启；非阻塞，发现新版本会通知界面）。
    # 此刻多半还没解锁、访问令牌尚未注入——unlock() 里会再补跑一次。
    api.start_auto_update_check()

    # 绑定关闭事件
    _window.events.closing += _on_closing

    # 跟踪最大化 / 最小化状态：托盘唤出时据此还原（见 _window_state / _show_window）
    _window.events.maximized += _on_window_maximized
    _window.events.minimized += _on_window_minimized
    _window.events.restored += _on_window_restored

    # 启动 pywebview（阻塞主线程）。
    # private_mode=False + 固定 storage_path：持久化 localStorage（列顺序、
    # 目录树状态等用户偏好），否则每次启动 WebView2 使用临时配置全部丢失。
    #
    # 图标：显式传入 .ico。不传时 pywebview 会从 sys.executable 提取 exe 内嵌图标，
    # 但提取到的通常是 16×16 小图，在高 DPI 任务栏/标题栏下模糊甚至回退为默认图标。
    # 图标随包在 resource_dir()/assets/（见 Keyrealm.spec）。
    webview_dir = db.DATA_DIR / "webview"
    webview_dir.mkdir(parents=True, exist_ok=True)
    _icon_file = resource_dir() / "assets" / "icon.ico"
    _icon = str(_icon_file) if _icon_file.is_file() else None
    try:
        webview.start(
            debug=False, private_mode=False, storage_path=str(webview_dir), icon=_icon,
            gui=_preferred_gui(),
        )
    except Exception as exc:  # noqa: BLE001
        # 缺 libxcb-cursor、或没有 GUI 后端：先把服务收干净，再给一段能照着做的指引、
        # 干净退出——不甩 traceback（对用户毫无用处，尤其对预编译包）。
        report = None
        if _is_xcb_cursor_error(exc):
            report = _report_missing_libxcb_cursor
        elif _is_missing_gui_backend(exc):
            report = _report_missing_gui_backend
        if report is None:
            raise       # 别的异常照旧抛：那是真 bug，不该被当成环境问题糊过去
        try:
            _shutdown_services()
        except Exception:  # noqa: BLE001
            pass
        report(exc)
        sys.exit(1)
    log.info("应用退出")
    _exit_cleanly()


if __name__ == "__main__":
    # 调试/构建校验：打印冻结后解析到的各关键路径后退出（不创建窗口）。
    # 用于确认打包产物中 frontend/dist 随包、data/plugins 落在 exe 同级。
    if "--print-paths" in sys.argv:
        print(f"frozen           : {is_frozen()}")
        print(f"resource_dir     : {resource_dir()}")
        print(f"app_dir          : {app_dir()}")
        print(f"frontend_index   : {_get_frontend_path()}")
        print(f"db_path          : {db.DB_PATH}")
        print(f"plugins_dir      : {plugin_manager.PLUGINS_DIR}")
        sys.exit(0)

    # 自更新入口：由 backend/updater.start_update 起成脱离进程；**不建窗口**，跑完即退。
    # 冻结 exe 与源码运行都用这条路径（后者是 `python main.py --self-update <kind>`）。
    if "--self-update" in sys.argv:
        from backend import updater

        def _say(msg: str) -> None:
            """往本进程的 stdout 写一行。**打不出来也绝不能让更新挂掉**
            （窗口化进程里 stdout 可能是 None；正常路径已被 spawn 重定向到 update-*.log）。"""
            try:
                print(msg, flush=True)
            except Exception:  # noqa: BLE001
                pass

        # Windows 控制台默认非 UTF-8，自更新全程要打中文——先钉成 UTF-8，否则一开打就 UnicodeEncodeError。
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
        _idx = sys.argv.index("--self-update")
        _kind = sys.argv[_idx + 1] if len(sys.argv) > _idx + 1 else "formal"
        _say(f"=== Keyrealm 自更新进程启动（渠道={_kind}，pid={os.getpid()}）===")
        _ok = updater.apply_update(_kind).get("ok")
        _say(f"=== 自更新进程结束：{'成功' if _ok else '失败'} ===")
        sys.exit(0 if _ok else 1)

    main()
