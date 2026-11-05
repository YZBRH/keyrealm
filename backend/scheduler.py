"""
定时任务调度器
APScheduler BackgroundScheduler + ThreadPoolExecutor
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import database as db
from . import logger
from . import plugin_manager
from . import tasks
from . import verify_options

_log = logger.get_logger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
_executor = ThreadPoolExecutor(max_workers=10)

# 由 api.py 注入，用于推送事件到前端
_event_callback: Optional[Callable] = None
# 由 api.py 注入，用于获取解密密钥
_get_key_callback: Optional[Callable[[], Optional[bytes]]] = None


def set_callbacks(event_cb: Callable, get_key_cb: Callable[[], Optional[bytes]]):
    global _event_callback, _get_key_callback
    _event_callback = event_cb
    _get_key_callback = get_key_cb


def _push_event(event_name: str, detail: dict):
    if _event_callback:
        try:
            _event_callback(event_name, detail)
        except Exception as e:
            _log.warning("事件推送失败: %s", e)


def _get_key() -> Optional[bytes]:
    if _get_key_callback:
        return _get_key_callback()
    return None


# 正在验证中的凭据 id。同一凭据并发验证没有意义——两次结果会互相覆盖，网络请求还翻倍；
# 前端连点、或前一次还没回来就点了别的条目又点回来时，靠这里挡掉。
_inflight: set = set()
_inflight_lock = threading.Lock()


def verify_single_credential(cred_id: int, trigger_type: str = "schedule") -> dict:
    """验证单条凭据，返回结果 dict。同一凭据已在验证中时直接挡回。"""
    with _inflight_lock:
        if cred_id in _inflight:
            return {"status": "unknown", "message": "该凭据正在验证中，请稍候"}
        _inflight.add(cred_id)
    try:
        return _verify_single_credential(cred_id, trigger_type)
    finally:
        with _inflight_lock:
            _inflight.discard(cred_id)


def _verify_single_credential(cred_id: int, trigger_type: str = "schedule") -> dict:
    """验证单条凭据的实际实现（调用方保证同一凭据不会并发进入）。"""
    from .crypto import decrypt_dict

    key = _get_key()
    if key is None:
        return {"status": "error", "message": "保险库已锁定，无法验证"}

    cred = db.get_credential(cred_id)
    if not cred:
        return {"status": "error", "message": "凭据不存在"}

    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception as e:
        # 解密失败通常是主密码变更或数据损坏，必须留痕而非只体现在界面上
        _log.error("凭据 id=%s 解密失败: %s", cred_id, e)
        return {"status": "error", "message": f"解密失败: {e}"}

    start = time.monotonic()
    result = plugin_manager.verify_credential(cred["credential_type"], data)
    duration_ms = int((time.monotonic() - start) * 1000)

    status_detail = result.to_dict() if hasattr(result, "to_dict") else {
        "status": result.status,
        "message": result.message,
        "account_info": getattr(result, "account_info", None),
    }
    status_detail["duration_ms"] = duration_ms

    # 验证结果落日志：插件内部的 print 在打包后不可见，这里是唯一的追责/排查线索
    label = f"{cred['credential_type']}/{cred.get('name', '')} (id={cred_id})"
    if result.status == "valid":
        _log.info("验证通过 %s，耗时 %dms（触发: %s）", label, duration_ms, trigger_type)
    elif result.status == "invalid":
        _log.warning("验证无效 %s：%s（耗时 %dms，触发: %s）",
                     label, result.message, duration_ms, trigger_type)
    else:
        _log.error("验证异常 %s：%s（耗时 %dms，触发: %s）",
                   label, result.message, duration_ms, trigger_type)

    now = datetime.now(timezone.utc).isoformat()
    updates = {
        "status": result.status,
        "status_detail": status_detail,
        "last_verified_at": now,
    }
    # 仅当验证结果为有效时刷新「最后有效时间」
    if result.status == "valid":
        updates["last_valid_at"] = now
    db.update_credential(cred_id, **updates)

    updated = db.get_credential(cred_id)
    if updated:
        _push_event("vault:credential_updated", updated)

    return status_detail


def batch_verify(
    scope: str = "all",
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    trigger_type: str = "manual",
    credential_ids: Optional[list] = None,
    concurrency: Optional[int] = None,
):
    """批量验证，后台线程池**并发**执行。

    - 显式传入 `credential_ids` 时只验证这些凭据（前端手动勾选 / 定时计划勾选）；
      否则按 scope/group/type 取。注意 `credential_ids` 为**空列表/None 时等同于「不带筛选」**，
      会验证全部凭据 —— 调用方要「一条也不验」必须自己先拦住（`_run_schedule` 就是这么做的）。
    - 禁用批量验证的插件（manifest 中 batch_verify=false）会被排除：这类插件无法
      自动化验证或有写副作用，只能手动单个验证，计入「跳过」。
    - **被禁用的插件**（plugins/.state.json）同样排除：它没有可加载的模块，逐条打起来只会
      得到一串「插件已禁用」，白白占用并发与接口配额。
    - `concurrency` 为本次并发线程数（前端在批量验证对话框里设置）；不传则读已保存的设置。
      定时触发的批量验证走同一份设置——见 verify_options。

    **返回本次实际要验证的条数**（已排除跳过项）。调用方（`api.batch_verify`）拿它当
    返回给前端的 ``total``，而不是自己另查一遍 —— 否则前后端会看到两个口径。
    """
    if credential_ids:
        wanted = set(int(i) for i in credential_ids)
        all_creds = [c for c in db.list_credentials() if c.get("id") in wanted]
    else:
        all_creds = db.list_credentials(
            group_id=group_id if scope == "group" else None,
            credential_type=credential_type if scope == "type" else None,
        )
    batchable = plugin_manager.batchable_types()
    creds = [c for c in all_creds if c.get("credential_type") in batchable]
    skipped = len(all_creds) - len(creds)

    total = len(creds)
    if total == 0:
        _push_event("vault:batch_verify_done",
                    {"total": 0, "valid": 0, "invalid": 0, "error": 0, "skipped": skipped})
        return 0

    # 批量验证登记成一条**后台任务**：界面上只弹一条横幅提示，进度在「任务」页看。
    # 不再用「不可关闭的模态弹窗」把用户按在前台（那是「卡住」的来源）。
    task = tasks.create(
        "batch_verify",
        "批量验证（定时）" if trigger_type == "schedule" else "批量验证",
    )
    task_id = task["id"]
    should_cancel = tasks.should_cancel(task_id)
    # 前端据此把对应条目的「验证」按钮置成 loading，避免重复点
    # trigger 用于区分「手动发起」与「定时触发」：手动才整页互斥（用户自己点的，知道在跑），
    # 定时是后台自己触发的，只提示 + 禁用本次涉及的验证按钮，不能把用户按在界面前。
    _push_event("vault:batch_verify_started", {
        "total": total,
        "skipped": skipped,
        "trigger": trigger_type,
        "task_id": task_id,
        "credentials": [c["id"] for c in creds],
    })

    def _run():
        valid = invalid = error = 0
        done = 0
        canceled = False
        workers = min(verify_options.concurrency(concurrency), total)
        # 每个凭据一个任务并发执行；结果在 as_completed 的消费线程里单线程汇总
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            fut_cred = {
                pool.submit(verify_single_credential, c["id"], trigger_type): c
                for c in creds
            }
            for fut in as_completed(fut_cred):
                if should_cancel():
                    canceled = True          # 用户点了「取消」：不再收新的结果
                    break
                cred = fut_cred[fut]
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001 - 单条异常不影响整体
                    result = {"status": "error", "message": f"执行异常: {exc}"}
                status = result.get("status", "error")
                if status == "valid":
                    valid += 1
                elif status == "invalid":
                    invalid += 1
                else:
                    error += 1
                done += 1
                tasks.progress(task_id, done, total, cred.get("name", ""))
                _push_event("vault:batch_verify_progress", {
                    "current": done,
                    "total": total,
                    "credential_name": cred.get("name", ""),
                    "status": status,
                })
        finally:
            # 取消时不再等队列里那些（正在跑的几个让它自己跑完，不强杀线程）
            pool.shutdown(wait=not canceled, cancel_futures=canceled)

        if canceled:
            message = f"已取消（已处理 {done}/{total}）"
            tasks.finish(task_id, ok=False, output=message, canceled=True)
            _push_event("vault:batch_verify_done", {
                "total": total, "valid": valid, "invalid": invalid, "error": error,
                "skipped": skipped, "canceled": True, "done": done,
            })
            _log.info("批量验证已取消（触发: %s）：已处理 %d/%d", trigger_type, done, total)
            return

        summary = (f"共 {total} 条：有效 {valid}，无效 {invalid}，异常 {error}"
                   + (f"，跳过 {skipped}" if skipped else ""))
        tasks.finish(task_id, ok=True, output=summary)
        _push_event("vault:batch_verify_done", {
            "total": total, "valid": valid, "invalid": invalid, "error": error, "skipped": skipped,
        })
        _log.info("批量验证完成（触发: %s）：共 %d 条，有效 %d，无效 %d，异常 %d，跳过 %d",
                  trigger_type, total, valid, invalid, error, skipped)

    _executor.submit(_run)
    return total


def _run_schedule(schedule: dict):
    """执行定时任务"""
    # 数据库锁定 → 整轮跳过（早退，连线程池都不进）。
    # 不拦的话每条凭据都会各自进 _verify_single_credential，在 `_get_key() is None` 那一步
    # 返回「保险库已锁定，无法验证」，最后汇总成一行「异常 N」—— 日志里分不清是「锁着」
    # 还是「这 N 条真出错了」，而且白占并发。锁定是全局前置条件，在整轮入口判一次即可。
    # （回调未注入时 _get_key() 也为 None，但那种情况只出现在调度器脱离 api 单独启动时，
    #  拿不准就别拦，让逐条验证各自报错。界面提示见 Tasks.vue「数据库锁定期间…」。）
    if _get_key_callback is not None and _get_key() is None:
        _log.info("定时计划 #%s「%s」跳过：数据库已锁定，本轮不执行验证",
                  schedule.get("id"), schedule.get("name", ""))
        _mark_ran(schedule)
        return

    scope = schedule.get("target_scope", "all")
    target_id = schedule.get("target_id")
    if scope == "credentials":
        # 勾选的**具体凭据**（前端目录树，与备份页同一个组件）。
        # 空选择必须在这里拦住：batch_verify 的 `if credential_ids:` 对空列表为假，会掉进
        # 「不带筛选」的分支去验证**全库** —— 与「一条也不验」正好相反，是危险的反向默认。
        ids = schedule.get("credential_ids") or []
        if not ids:
            _log.warning("定时计划 #%s「%s」未选中任何凭据，本次跳过",
                         schedule.get("id"), schedule.get("name", ""))
        else:
            batch_verify(credential_ids=ids, trigger_type="schedule")
    else:
        # target_id 是多态字段：scope=="group" 时为分组 ID（int），
        # scope=="type" 时为凭据类型名（str，前端直接存入 target_id）。
        batch_verify(
            scope=scope,
            group_id=target_id if scope == "group" else None,
            credential_type=target_id if scope == "type" else None,
            trigger_type="schedule",
        )
    _mark_ran(schedule)


def _mark_ran(schedule: dict):
    """记一次「本计划到点跑过了」。

    跳过也算「运行过」：否则界面上会一直停在「从未运行」，而 next_run_at 却在正常往前走。
    字段叫 last_run_at 不是 last_verified_at —— 触发器确实响了，只是这一轮没验证。
    """
    db.update_schedule(schedule["id"], last_run_at=datetime.now(timezone.utc).isoformat())


def _build_trigger(trigger_type: str, trigger_args: dict):
    if trigger_type == "interval":
        return IntervalTrigger(**trigger_args)
    elif trigger_type == "cron":
        return CronTrigger(**trigger_args)
    raise ValueError(f"不支持的触发器类型: {trigger_type}")


def start_scheduler():
    """启动调度器，加载所有已保存的计划"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(executor=_executor)
    _scheduler.start()

    for sched in db.list_schedules():
        if not sched.get("enabled"):
            continue
        try:
            trigger = _build_trigger(sched["trigger_type"], sched["trigger_args"])
            job = _scheduler.add_job(
                _run_schedule,
                trigger=trigger,
                args=[sched],
                id=f"schedule_{sched['id']}",
                replace_existing=True,
            )
            if job and job.next_run_time:
                db.update_schedule(sched["id"], next_run_at=job.next_run_time.isoformat())
        except Exception as e:
            _log.error("加载计划失败 id=%s: %s", sched["id"], e)


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def reload_schedules():
    """重新加载所有计划（增删改后调用）"""
    if not _scheduler:
        return
    # 移除所有旧 job
    for job in _scheduler.get_jobs():
        job.remove()
    # 重新添加
    for sched in db.list_schedules():
        if not sched.get("enabled"):
            continue
        try:
            trigger = _build_trigger(sched["trigger_type"], sched["trigger_args"])
            job = _scheduler.add_job(
                _run_schedule,
                trigger=trigger,
                args=[sched],
                id=f"schedule_{sched['id']}",
                replace_existing=True,
            )
            if job and job.next_run_time:
                db.update_schedule(sched["id"], next_run_at=job.next_run_time.isoformat())
        except Exception as e:
            _log.error("重载计划失败 id=%s: %s", sched["id"], e)
