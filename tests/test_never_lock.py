"""「不锁定」契约测试（独立沙箱，不碰仓库真实数据）。

覆盖：
1. 会话密钥的本机绑定加密往返与清理；
2. 开启后解锁 / 重启恢复 / 手动锁定清除 / 关闭设置清除；
3. 「不锁定」开启时空闲锁定看门狗不触发；
4. 开机自启命令带 `--autostart`。

运行：.venv/bin/python tests/test_never_lock.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-neverlock-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, autostart, idle_lock, session_key  # noqa: E402

_original_save = session_key.save


def setup_module():
    # 测试统一走本机绑定加密，避免在开发机上写系统钥匙串 / DPAPI。
    session_key.save = lambda key, force="": _original_save(key, force="machine")


def teardown_module():
    session_key.save = _original_save
    try:
        session_key.clear()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)


def test_session_key_machine_round_trip():
    key = os.urandom(32)
    assert session_key.save(key, force="machine") is True
    assert session_key.exists() is True
    assert session_key.protection() == "machine"
    assert session_key.load() == key
    session_key.clear()
    assert session_key.exists() is False
    assert session_key.load() is None


def test_never_lock_restore_and_manual_lock():
    assert api.setup_master_password("TestPass!123")["ok"] is True
    assert api.get_never_lock()["enabled"] is False

    enabled = api.set_never_lock(True)
    assert enabled["ok"] is True and enabled["enabled"] is True
    assert enabled["persisted"] is True
    assert session_key.exists() is True

    # 模拟进程重启：内存密钥清空，启动恢复
    api._set_key(None)
    restored = api.restore_persisted_session()
    assert restored["ok"] is True and restored["restored"] is True
    assert api.is_unlocked()["unlocked"] is True

    # 手动锁定会删除持久化密钥，重启后保持锁定
    assert api.lock_vault()["ok"] is True
    assert session_key.exists() is False
    assert api.restore_persisted_session()["restored"] is False
    assert api.is_unlocked()["unlocked"] is False

    # 再次手动解锁后重新持久化
    assert api.unlock("TestPass!123", auto_check=False)["ok"] is True
    assert api.get_never_lock()["persisted"] is True

    # 关闭「不锁定」清除密钥
    disabled = api.set_never_lock(False)
    assert disabled["enabled"] is False
    assert session_key.exists() is False


def test_idle_lock_skips_when_never_lock_enabled():
    locked = []
    idle_lock.set_callbacks(lambda: locked.append(True), lambda: True)
    db.set_setting(idle_lock.SETTING_KEY, "1")
    idle_lock.touch()
    last = idle_lock.last_activity()

    db.set_setting(idle_lock.NEVER_LOCK_KEY, "1")
    assert idle_lock._tick(now=last + 3600) is False
    assert locked == []

    db.set_setting(idle_lock.NEVER_LOCK_KEY, "0")
    assert idle_lock._tick(now=last + 3600) is True
    assert locked == [True]


def test_autostart_argv_marks_autostart():
    argv = autostart.launch_argv()
    assert argv and argv[-1] == "--autostart"


def main():
    setup_module()
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
