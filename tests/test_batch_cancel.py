"""批量验证取消：started 事件带 task_id，取消后任务以 canceled 结束。"""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-batch-cancel-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, plugin_manager, scheduler, tasks  # noqa: E402


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_cancel_marks_batch_canceled_and_reports_task_id():
    cred = api.create_credential(
        "批量取消测试", "llm_openai",
        {"api_key": "sk-test", "base_url": "https://api.example.com/v1"},
        auto_verify=False,
    )

    events = []
    started = threading.Event()
    release = threading.Event()
    done = threading.Event()

    original_push = scheduler._push_event
    original_verify = scheduler.verify_single_credential

    def fake_push(name, detail=None):
        events.append((name, detail or {}))
        if name == "vault:batch_verify_started":
            started.set()
        elif name == "vault:batch_verify_done":
            done.set()

    def fake_verify(credential_id, trigger_type="manual"):
        release.wait(10)
        return {"status": "valid", "message": "ok"}

    scheduler._push_event = fake_push
    scheduler.verify_single_credential = fake_verify
    try:
        total = scheduler.batch_verify(credential_ids=[cred["id"]],
                                       trigger_type="manual", concurrency=1)
        assert total == 1
        assert started.wait(5), "没有收到 batch_verify_started"

        started_event = next(e for e in events if e[0] == "vault:batch_verify_started")[1]
        task_id = started_event.get("task_id")
        assert task_id, "started 事件缺少 task_id"

        assert tasks.cancel(task_id) is True
        release.set()
        assert done.wait(10), "没有收到 batch_verify_done"

        done_event = next(e for e in events if e[0] == "vault:batch_verify_done")[1]
        assert done_event.get("canceled") is True
        assert done_event.get("done") in (0, 1)
    finally:
        release.set()
        scheduler._push_event = original_push
        scheduler.verify_single_credential = original_verify


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    setup_module()
    passed = failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  PASS  {test.__name__}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {test.__name__}: {exc}")
                failed += 1
    finally:
        teardown_module()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
