"""手动设置凭据状态：API 契约、事件、失败分支（独立沙箱）。"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-manual-status-"))

from backend import api, database as db, plugin_manager  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()
plugin_manager.sync_plugins()
assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def _cred():
    return api.create_credential(
        "manual-status", "api_fofa", {"api_key": "fofa-test-key"}, auto_verify=False,
    )


def test_set_status_writes_manual_detail():
    cred = _cred()
    events = []
    old_push = api.push_event
    api.push_event = lambda name, detail=None: events.append((name, detail))
    try:
        result = api.set_credential_status(cred["id"], "invalid", "已人工确认失效")
    finally:
        api.push_event = old_push

    assert result["ok"] is True
    row = result["credential"]
    assert row["status"] == "invalid"
    detail = row["status_detail"]
    assert detail["manual"] is True
    assert detail["message"] == "已人工确认失效"
    assert detail["manual_at"]
    assert "fofa-test-key" not in str(detail)
    assert any(name == "vault:credential_updated" for name, _ in events)
    assert db.get_credential(cred["id"])["status"] == "invalid"


def test_default_message_and_unknown_status():
    cred = _cred()
    assert api.set_credential_status(cred["id"], "unknown")["credential"]["status_detail"][
        "message"] == "手动标记为未验证"
    assert api.set_credential_status(cred["id"], "bogus")["ok"] is False
    assert api.set_credential_status(999999, "valid")["ok"] is False


def test_status_does_not_touch_verification_timestamps():
    cred = _cred()
    before = db.get_credential(cred["id"])
    api.set_credential_status(cred["id"], "valid", "人工放行")
    after = db.get_credential(cred["id"])
    assert after["last_verified_at"] == before["last_verified_at"]
    assert after["last_valid_at"] == before["last_valid_at"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    teardown_module()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
