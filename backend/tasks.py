"""后台任务注册表 —— 长操作（下载 / 打包等）跑在后台线程里，这里记录它们的状态。

为什么需要它
------------
「点一下就开始、关掉弹窗还在跑」这类操作，用户需要三件事：**看得见**（列表 + 进度）、
**能终止**（取消正在跑的下载）、**不丢结果**（关掉弹窗后回来还能看到成败）。光靠
`vault:action_stream` 那条一次性事件流做不到——它绑在某个弹窗上，弹窗一关就没人接了。

职责划分
--------
- **本模块只管状态**：任务的增删改查 + 取消标志。不认识 webview、不认识数据库。
- **推送由 `backend/api.py` 注入**（`set_notifier`）：任何变更都回调一次，由它 `push_event`
  通知前端。这样本模块能脱离应用单独测试，也不会和 api 形成循环依赖。

持久化
------
任务记录与日志一样落**普通文件**（`<data_dir>/tasks.json`），进程重启后仍在，用户回来能翻到
「上次那个下载到底成没成」。之所以不进数据库：任务是运行期状态、不参与查询与关联，
进库就白白要一次 schema 变动 + 迁移脚本，且锁库时反而读不到。文件与日志同放数据目录，
默认随程序目录走，也可经配置/命令行改到别处。

进程重启时**所有 `running` 的任务一律改判 `interrupted`**：线程随进程没了，任务不可能还在跑，
继续显示「进行中」是骗人；但也没失败（用户没做错什么），故单列一档，与 `canceled` 区分。

写盘时机：状态**变迁**（新建 / 结束 / 取消 / 清理）一律立刻落盘；`progress()` 是高频回调
（每个 chunk 一次），按 `_SAVE_INTERVAL` 节流，最多每 2 秒写一次——重进度任务的进度条
本来就不需要毫秒级精度，重启后看到的是最后一次节流点。

已结束的只保留最近 `_MAX_FINISHED` 条，免得文件长时间运行越攒越大。

取消是怎么生效的
----------------
`cancel()` 只置一个 `threading.Event`；真正停下来要靠**插件**在下载循环里查
`should_cancel`（框架把这个回调按关键字传给 `stream_action`）。所以取消是**协作式**的：
做不到立刻杀掉线程，但会在下一个检查点（每个 chunk / 每个对象）停下并清理半成品。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .paths import data_dir

# 任务状态
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"
# 应用退出时仍在跑的任务：线程已随进程消失，既不是失败（用户没做错）也不是用户取消
INTERRUPTED = "interrupted"

# 已结束的任务只留最近这么多条
_MAX_FINISHED = 50

# progress() 的落盘节流间隔（秒）
_SAVE_INTERVAL = 2.0

_STORE = data_dir() / "tasks.json"

_lock = threading.Lock()
_tasks: dict[str, dict] = {}          # task_id → 快照
_cancel_events: dict[str, threading.Event] = {}
_notifier: Optional[Callable[[dict], None]] = None
_last_save = 0.0
_loaded = False


def set_notifier(fn: Optional[Callable[[dict], None]]) -> None:
    """注入「任务变更」回调（api 用它推事件）。传 None 可关闭。"""
    global _notifier
    _notifier = fn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notify(snapshot: dict) -> None:
    fn = _notifier
    if fn is None:
        return
    try:
        fn(snapshot)
    except Exception:  # noqa: BLE001 - 推送失败绝不能影响任务本身
        pass


def _snapshot(task: dict) -> dict:
    """给外部看的副本：去掉内部字段（取消事件），带上是否已请求取消。"""
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    event = _cancel_events.get(task["id"])
    out["cancel_requested"] = bool(event is not None and event.is_set())
    return out


# ── 持久化 ────────────────────────────────────────────────

def _save_locked(force: bool = False) -> None:
    """把任务表写回文件（调用方需持有锁）。

    写盘失败（磁盘满 / 权限）只吞掉：任务状态本来就在内存里，界面照常，
    不能因为存不下历史记录就让正在跑的任务崩掉。
    """
    global _last_save
    stamp = time.monotonic()
    if not force and stamp - _last_save < _SAVE_INTERVAL:
        return
    try:
        payload = [_snapshot(t) for t in _tasks.values()]
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：避免写到一半被强杀，留下半个 JSON 让下次启动解析失败
        tmp = _STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STORE)
        _last_save = stamp
    except Exception:  # noqa: BLE001 - 见 docstring
        pass


def load() -> int:
    """从文件恢复历史任务（应用启动时调用一次），返回恢复的条数。

    仍在 `running` 的一律改判 `interrupted`——进程重启意味着线程没了。
    文件缺失 / 损坏都按「没有历史」处理，不影响启动。重复调用只生效一次。
    """
    global _loaded
    if _loaded:
        return 0
    _loaded = True
    try:
        raw = _STORE.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError:
        return 0
    except Exception:  # noqa: BLE001 - 损坏的历史记录不值得拦住启动
        return 0
    if not isinstance(payload, list):
        return 0

    restored = 0
    with _lock:
        for item in payload:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "").strip()
            if not task_id or task_id in _tasks:
                continue
            item.pop("cancel_requested", None)
            item["id"] = task_id
            if item.get("status") == RUNNING:
                item["status"] = INTERRUPTED
                item["text"] = "应用已退出，任务中断"
                item["finished_at"] = item.get("finished_at") or _now()
            _tasks[task_id] = item
            restored += 1
        _prune_locked()
    return restored


def _prune_locked() -> None:
    """只保留最近 `_MAX_FINISHED` 条已结束的任务（调用方需持有锁）。"""
    finished = [t for t in _tasks.values() if t["status"] not in (RUNNING,)]
    if len(finished) <= _MAX_FINISHED:
        return
    finished.sort(key=lambda t: t.get("finished_at") or "")
    for task in finished[: len(finished) - _MAX_FINISHED]:
        _tasks.pop(task["id"], None)
        _cancel_events.pop(task["id"], None)


def create(kind: str, title: str, *, credential_id: Optional[int] = None,
           credential_type: str = "", action_id: str = "") -> dict:
    """登记一个新任务（状态 running），返回它的快照。"""
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "kind": kind,
        "title": title,
        "credential_id": credential_id,
        "credential_type": credential_type,
        "action_id": action_id,
        "status": RUNNING,
        "current": 0,
        "total": 0,
        "text": "",
        "output": "",
        "error": "",
        "created_at": _now(),
        "finished_at": None,
    }
    with _lock:
        _tasks[task_id] = task
        _cancel_events[task_id] = threading.Event()
        snapshot = _snapshot(task)
        _save_locked(force=True)
    _notify(snapshot)
    return snapshot


def get(task_id: str) -> Optional[dict]:
    with _lock:
        task = _tasks.get(str(task_id or ""))
        return _snapshot(task) if task else None


def list_tasks() -> list[dict]:
    """全部任务，**进行中在前、其内按创建时间倒序**（前端直接照序渲染）。

    两次 sort 是刻意的：Python 的排序稳定，先按时间倒序、再按「是否进行中」分组，
    分组内的时间倒序就被保留下来了。
    """
    with _lock:
        items = [_snapshot(t) for t in _tasks.values()]
    items.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    items.sort(key=lambda t: t["status"] != RUNNING)
    return items


def progress(task_id: str, current: Any, total: Any, text: str = "") -> None:
    """更新进度（**尽力而为**：非法数值一律按 0 处理，缺失的任务直接忽略）。"""
    try:
        cur = max(0, int(current))
    except (TypeError, ValueError):
        cur = 0
    try:
        tot = max(0, int(total or 0))
    except (TypeError, ValueError):
        tot = 0
    with _lock:
        task = _tasks.get(str(task_id or ""))
        if task is None or task["status"] != RUNNING:
            return
        task["current"] = cur
        task["total"] = tot
        task["text"] = str(text or "")
        snapshot = _snapshot(task)
        _save_locked()   # 高频回调，内部按 _SAVE_INTERVAL 节流
    _notify(snapshot)


def finish(task_id: str, *, ok: bool = True, output: str = "", error: str = "",
           canceled: bool = False) -> Optional[dict]:
    """收尾。

    ``canceled=True`` 表示**确实是用户取消导致的结束**（由调用方判断——框架持有取消标志，
    不必让插件去猜错误文案），此时即使插件返回 ok=False 也标成「已取消」而不是「失败」。
    """
    with _lock:
        task = _tasks.get(str(task_id or ""))
        if task is None:
            return None
        if task["status"] != RUNNING:
            return _snapshot(task)
        if canceled:
            task["status"] = CANCELED
        else:
            task["status"] = DONE if ok else FAILED
        task["output"] = str(output or "")
        task["error"] = str(error or "")
        task["finished_at"] = _now()
        if task["status"] == CANCELED:
            task["text"] = "已取消"
        snapshot = _snapshot(task)
        _prune_locked()
        _save_locked(force=True)
    _notify(snapshot)
    return snapshot


def cancel(task_id: str) -> bool:
    """请求取消：置标志位，等插件在下一个检查点停下。任务不存在或已结束返回 False。"""
    with _lock:
        task = _tasks.get(str(task_id or ""))
        event = _cancel_events.get(str(task_id or ""))
        if task is None or event is None or task["status"] != RUNNING:
            return False
        event.set()
        task["text"] = task["text"] or "正在取消…"
        snapshot = _snapshot(task)
        _save_locked(force=True)
    _notify(snapshot)
    return True


def should_cancel(task_id: str) -> Callable[[], bool]:
    """给插件用的取消查询：返回一个无参函数，True 表示该停了。"""
    event = _cancel_events.get(str(task_id or ""))

    def _check() -> bool:
        return bool(event is not None and event.is_set())

    return _check


def clear_finished() -> int:
    """清掉所有已结束的任务，返回清掉的条数（进行中的不动）。"""
    with _lock:
        ids = [tid for tid, t in _tasks.items() if t["status"] != RUNNING]
        for tid in ids:
            _tasks.pop(tid, None)
            _cancel_events.pop(tid, None)
        if ids:
            _save_locked(force=True)
    return len(ids)


def running_count() -> int:
    with _lock:
        return sum(1 for t in _tasks.values() if t["status"] == RUNNING)

