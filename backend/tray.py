"""
系统托盘
pystray。**Windows 用后台守护线程；macOS / Linux 必须在主线程并入 pywebview 的主循环**
（见 `start_tray`：这两个平台的后端会去跑各自的主事件循环——Cocoa / GLib——丢进线程
就会把循环抢走，让 `webview.start()` 立刻返回、应用秒退）。
窗口关闭 → 隐藏到托盘
图标颜色：🟢运行+活跃Token 🔵运行空闲 🔴停止/错误 ⚪锁定
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageChops, ImageDraw, ImageOps

from . import logger
from .paths import resource_dir

_log = logger.get_logger(__name__)

_tray_icon: Optional[pystray.Icon] = None
_tray_thread: Optional[threading.Thread] = None
_show_window_cb: Optional[Callable] = None
_lock_vault_cb: Optional[Callable] = None
_quit_cb: Optional[Callable] = None
_get_status_cb: Optional[Callable] = None

# 托盘图标：新品牌图按状态色着色。状态色必须占满整块底色，
# 才能在 16~24px 的系统托盘里一眼分辨（角标小圆点在这个尺寸下会糊成噪点）。
_ICON_SIZE = 64
_TRAY_COLORS = {
    "green": (34, 197, 94),
    "blue": (59, 130, 246),
    "red": (239, 68, 68),
    "gray": (156, 163, 175),
}
_base_icon: Optional[Image.Image] = None
_base_icon_loaded = False
_icon_cache: dict[str, Image.Image] = {}


def _load_base_icon() -> Optional[Image.Image]:
    """读取随包品牌图（assets/icon.png），缺失时返回 None 走兜底绘制。"""
    global _base_icon, _base_icon_loaded
    if not _base_icon_loaded:
        _base_icon_loaded = True
        path = resource_dir() / "assets" / "icon.png"
        try:
            _base_icon = Image.open(path).convert("RGBA")
        except OSError:
            _log.warning("托盘图标母版不可用，改用兜底图形：%s", path)
    return _base_icon


def _tint_icon(base: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    """背景按亮度映射为状态色，白笔画与星光保持白色（三通道均 ≥246 视为白）。"""
    rgb = base.convert("RGB")
    tinted = ImageOps.colorize(
        rgb.convert("L"), black=(12, 12, 20), white=color
    ).convert("RGB")

    def _white(channel: Image.Image) -> Image.Image:
        return channel.point(lambda v: 255 if v >= 246 else 0)

    r, g, b = rgb.split()
    white_mask = ImageChops.multiply(ImageChops.multiply(_white(r), _white(g)), _white(b))
    tinted = Image.composite(
        Image.new("RGB", rgb.size, (255, 255, 255)), tinted, white_mask
    )

    out = tinted.convert("RGBA")
    out.putalpha(base.getchannel("A"))
    return out.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)


def _fallback_icon(color: tuple[int, int, int]) -> Image.Image:
    """母版缺失时绘制兜底图标：状态色圆角方块 + 白色保险库门/钥匙孔。"""
    s = _ICON_SIZE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c = (*color, 255)

    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=c)

    cx = cy = s / 2
    ring_r = s * 0.34
    draw.ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        outline=(255, 255, 255, 235),
        width=max(2, int(s * 0.055)),
    )
    head_r = s * 0.10
    head_cy = cy - s * 0.05
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(255, 255, 255, 255),
    )
    stem_top_y = head_cy + head_r * 0.35
    stem_bot_y = cy + s * 0.19
    top_half = head_r * 0.62
    bot_half = head_r * 1.05
    draw.polygon(
        [
            (cx - top_half, stem_top_y),
            (cx + top_half, stem_top_y),
            (cx + bot_half, stem_bot_y),
            (cx - bot_half, stem_bot_y),
        ],
        fill=(255, 255, 255, 255),
    )
    return img


def _make_icon_image(color: str) -> Image.Image:
    """生成 64x64 托盘图标：新品牌图按状态色着色，一眼判别服务状态。"""
    key = color if color in _TRAY_COLORS else "gray"
    if key not in _icon_cache:
        base = _load_base_icon()
        _icon_cache[key] = (
            _tint_icon(base, _TRAY_COLORS[key])
            if base is not None
            else _fallback_icon(_TRAY_COLORS[key])
        )
    return _icon_cache[key]


def _get_icon_color() -> str:
    if _get_status_cb:
        try:
            status = _get_status_cb()
            if status.get("is_locked"):
                return "gray"
            if not status.get("mcp_running"):
                return "red"
            if status.get("active_tokens", 0) > 0:
                return "green"
            return "blue"
        except Exception:
            pass
    return "gray"


def _update_icon():
    if not _tray_icon:
        return
    # 托盘是辅助功能：X11 下 pystray 用 latin-1 写 WM_NAME，中文标题会抛
    # UnicodeEncodeError；托盘未挂载时更新也会失败。任何异常都不能冒泡到
    # unlock / 验证等主流程（否则 WSLg 这类没有系统托盘的桌面会连解锁都失败）。
    try:
        color = _get_icon_color()
        _tray_icon.icon = _make_icon_image(color)
        status = {}
        if _get_status_cb:
            try:
                status = _get_status_cb()
            except Exception:
                pass
        # 标题保持 ASCII：X11 的 pystray 后端按 latin-1 编码 WM_NAME
        mcp = "running" if status.get("mcp_running") else "stopped"
        port = status.get("mcp_port", 8765)
        tokens = status.get("active_tokens", 0)
        locked = "locked" if status.get("is_locked") else "unlocked"
        _tray_icon.title = f"Keyrealm | MCP: {mcp} :{port} | Tokens: {tokens} | {locked}"
    except Exception as exc:  # noqa: BLE001 - 托盘刷新失败不影响主流程
        _log.debug("刷新托盘状态失败：%s", exc)


def _on_show(icon, item):
    if _show_window_cb:
        _show_window_cb()


def _on_lock(icon, item):
    if _lock_vault_cb:
        _lock_vault_cb()
    _update_icon()


def _on_quit(icon, item):
    if _quit_cb:
        _quit_cb()
    icon.stop()


def _build_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("显示主窗口", _on_show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("锁定保险库", _on_lock),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _on_quit),
    )


def start_tray(
    show_window_cb: Callable,
    lock_vault_cb: Callable,
    quit_cb: Callable,
    get_status_cb: Callable,
):
    """启动系统托盘。**Windows 用后台守护线程；macOS / Linux 必须挂到 pywebview 的主循环上**（见下）。"""
    global _tray_icon, _tray_thread
    global _show_window_cb, _lock_vault_cb, _quit_cb, _get_status_cb

    _show_window_cb = show_window_cb
    _lock_vault_cb = lock_vault_cb
    _quit_cb = quit_cb
    _get_status_cb = get_status_cb

    if _tray_icon:
        return

    # 逃生阀：排障用——怀疑托盘导致启动异常时，一次运行就能把它排除。
    if os.environ.get("KEYREALM_NO_TRAY"):
        _log.info("已设置 KEYREALM_NO_TRAY：跳过系统托盘")
        return

    # macOS 的 NSApplication 要在**构造 Icon 时**传进去（pystray 的平台专属选项走构造函数，
    # 不是 `run_detached()` 的参数）。不同版本对这个选项的支持不一样，故先备好再建图标。
    icon_kwargs: dict = {}
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplication          # pyobjc，随 pywebview 一并装上
            icon_kwargs["darwin_nsapplication"] = NSApplication.sharedApplication()
        except Exception as exc:  # noqa: BLE001 - 拿不到就干脆不显示托盘，别冒险
            _log.warning("macOS 上拿不到 NSApplication，跳过系统托盘（应用照常运行）：%s", exc)
            return

    try:
        icon = pystray.Icon(
            name="Keyrealm",
            icon=_make_icon_image(_get_icon_color()),
            title="Keyrealm",
            menu=_build_menu(),
            **icon_kwargs,
        )
    except TypeError as exc:
        # 旧版 pystray 不认平台专属构造选项（如 macOS 的 `darwin_nsapplication`）；
        # 而旧版 `run_detached()` 的语义是「另起线程跑 run()」——正是把主循环抢走、
        # 让应用秒退的那个坑。**宁可没有托盘**，也不能再赌一次。
        _log.warning("当前 pystray 不支持托盘集成，跳过系统托盘（应用照常运行）：%s", exc)
        return

    if os.name != "nt":
        # **macOS / Linux 都不能把 pystray 放进后台线程** —— 这两个平台的后端会去跑各自
        # 的主事件循环，丢进线程 = 把循环抢走，随后主线程的 `webview.start()` 拿不到控制权、
        # 立刻返回，表现为「应用启动一两秒后自己退出」：
        #   * macOS：darwin 后端 `_run()` 里直接是 `NSApplication.sharedApplication().run()`；
        #   * Linux：AppIndicator / GTK 后端要 GLib 主上下文，抢不到就报
        #     ``g_application_run() cannot acquire the default main context because it is
        #     already acquired by another thread!``
        # 正解是 pystray 为这种情形提供的 `run_detached()`（非阻塞）：**在主线程、进入
        # pywebview 主循环之前**把托盘挂上去，由 pywebview 随后启动的循环来驱动它。
        # macOS 还需在构造时传入那个共享的 NSApplication（见上）。
        try:
            icon.run_detached()
            _tray_icon = icon
        except Exception as exc:  # noqa: BLE001 - 托盘起不来不该拖垮应用
            _log.warning("托盘启动失败（%s），应用照常运行但没有托盘图标：%s", sys.platform, exc)
        return

    _tray_icon = icon
    _tray_thread = threading.Thread(target=_tray_icon.run, daemon=True)
    _tray_thread.start()


def available() -> bool:
    """托盘是否真的挂上了。**为 False 时不能「隐藏到托盘」**——窗口藏了就没办法叫回来。"""
    return _tray_icon is not None


def update_tray():
    """刷新托盘图标状态"""
    _update_icon()


def stop_tray():
    global _tray_icon
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass
        _tray_icon = None
