"""空闲自动锁定：一段时间无操作后清除内存中的解密密钥。

「一段时间未操作后自动锁定数据库」——由用户可在「系统设置 → 安全」里配置，设置项
``auto_lock_minutes`` 存 ``settings`` 表（0 = 关闭，见 ``backend/api.py`` 的 get/set）。

为什么计时放在后端
------------------
前端当然也能挂个 ``setTimeout``，但本应用常驻系统托盘运行（``close_action=background``），
窗口隐藏时 WebView 会把定时器节流，托盘场景下并不可靠；而「有后台任务在跑就顺延」这条
规则的依据 ``tasks.running_count()`` 本来就在后端。所以看门狗线程放在这里，前端只负责
**有用户操作时上报活动**（``api.touch_activity``）与**收到 ``vault:locked`` 事件后切到锁定态**。

顺延规则
--------
到点时若仍有 ``status == RUNNING`` 的后台任务（批量验证 / 定时验证 / 长操作），**不锁**，
等任务结束且**仍无操作**再锁 —— 否则批次里还没轮到的条目会全部命中 `scheduler` 的
「保险库已锁定」守卫，被打成一堆 ``error``，用户的验证结果白跑。

回调注入（与 ``scheduler`` 同款，避免 import 循环）
--------------------------------------------------
本模块不 import ``api``；由 ``api.init_callbacks()`` 注入：
``lock_cb``（真正执行锁定）与 ``is_unlocked_cb``（判断当前是否已解锁）。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import database as db
from . import logger
from . import tasks

_log = logger.get_logger(__name__)

# ── 设置项（与 backend/api.py 的 get/set 共用同一份常量）────────────
SETTING_KEY = "auto_lock_minutes"
# 「不锁定」：开启后空闲锁定整体失效（手动锁定仍可用）。
NEVER_LOCK_KEY = "never_lock"
DEFAULT_MINUTES = 0
ALLOWED_MINUTES = (0, 1, 5, 10, 15, 30, 60)
OPTIONS = [
    {"value": 0, "label": "关闭"},
    {"value": 1, "label": "1 分钟"},
    {"value": 5, "label": "5 分钟"},
    {"value": 10, "label": "10 分钟"},
    {"value": 15, "label": "15 分钟"},
    {"value": 30, "label": "30 分钟"},
    {"value": 60, "label": "60 分钟"},
]

# 看门狗轮询间隔（秒）。取 10s：相对分钟级的超时足够准，又不至于频繁读库。
WATCH_INTERVAL = 10.0

_lock = threading.Lock()
_last_activity: float = time.monotonic()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None

# 由 api.init_callbacks() 注入
_lock_cb: Optional[Callable[[], None]] = None
_is_unlocked_cb: Optional[Callable[[], bool]] = None


def read_minutes() -> int:
    """读取当前设置（分钟）；非法/缺失回落默认值。"""
    raw = (db.get_setting(SETTING_KEY, "") or "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        return DEFAULT_MINUTES
    return minutes if minutes in ALLOWED_MINUTES else DEFAULT_MINUTES


def read_never_lock() -> bool:
    """读「不锁定」开关；值异常时按关闭处理。"""
    return (db.get_setting(NEVER_LOCK_KEY, "0") or "").strip() == "1"


def touch() -> None:
    """记一次用户活动，重置空闲计时。"""
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def last_activity() -> float:
    """最近一次活动时间（``time.monotonic()`` 基准），供测试/诊断读取。"""
    with _lock:
        return _last_activity


def should_lock(now: float, last: float, minutes: int, running_count: int) -> bool:
    """纯判定：是否该锁。关掉（0）不锁；有任务在跑顺延；否则看空闲是否到点。"""
    if minutes <= 0:
        return False
    if running_count > 0:
        return False
    return (now - last) >= minutes * 60


def set_callbacks(lock_cb: Callable[[], None], is_unlocked_cb: Callable[[], bool]) -> None:
    """注入回调（由 api.init_callbacks 调用）。"""
    global _lock_cb, _is_unlocked_cb
    _lock_cb = lock_cb
    _is_unlocked_cb = is_unlocked_cb


def _tick(now: Optional[float] = None) -> bool:
    """单次检查；真锁了就返回 True。抽出来便于离线打桩测试。"""
    if read_never_lock():
        return False
    minutes = read_minutes()
    running = tasks.running_count()
    now = time.monotonic() if now is None else now
    if not should_lock(now, last_activity(), minutes, running):
        return False
    if _is_unlocked_cb is None or not _is_unlocked_cb():
        return False
    if _lock_cb is not None:
        _log.info("空闲超过 %s 分钟，自动锁定保险库", minutes)
        _lock_cb()
    return True


def _loop() -> None:
    while not _stop.wait(WATCH_INTERVAL):
        try:
            _tick()
        except Exception as e:  # 看门狗绝不能因一次异常而退出
            _log.error("空闲锁定检查异常: %s", e)


def start() -> None:
    """启动看门狗守护线程（幂等）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    touch()  # 启动即视为刚有活动，别一开机就锁
    _thread = threading.Thread(target=_loop, name="idle-lock", daemon=True)
    _thread.start()


def stop() -> None:
    """停止看门狗线程。"""
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
