"""百度 AI 类型改名的旧数据兼容：别名归一、状态文件、备份导入。"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import backup, data_upgrades, plugin_manager  # noqa: E402

OLD = "api_baidu_face"
NEW = "api_baidu_ai"


def test_canonical_type_and_default_auth_mode():
    assert data_upgrades.canonical_type(OLD) == NEW
    assert data_upgrades.canonical_type(NEW) == NEW
    assert data_upgrades.canonical_type("api_fofa") == "api_fofa"
    assert data_upgrades.upgrade_credential(NEW, {"api_key": "a"})["auth_mode"] == "ak_sk"
    same = {"auth_mode": "access_token", "access_token": "t"}
    assert data_upgrades.upgrade_credential(NEW, same) == same


def test_old_state_file_key_is_canonicalized():
    state = Path(tempfile.mkdtemp(prefix="kr-state-alias-")) / ".state.json"
    state.write_text(json.dumps({"disabled": [OLD, "api_fofa"]}), encoding="utf-8")
    old_file = plugin_manager.STATE_FILE
    old_cache, old_mtime = plugin_manager._state_cache, plugin_manager._state_mtime
    plugin_manager.STATE_FILE = state
    plugin_manager._state_cache, plugin_manager._state_mtime = None, None
    try:
        assert plugin_manager.is_plugin_enabled(NEW) is False
        assert plugin_manager.is_plugin_enabled("api_fofa") is False
        assert OLD not in plugin_manager.disabled_types()
    finally:
        plugin_manager.STATE_FILE = old_file
        plugin_manager._state_cache, plugin_manager._state_mtime = old_cache, old_mtime


def test_backup_disabled_types_are_canonicalized():
    captured = {}
    old_set = backup.plugin_manager.set_disabled
    backup.plugin_manager.set_disabled = lambda types: captured.setdefault("types", types)
    try:
        count = backup._import_plugins_disabled({"plugins_disabled": [OLD, "api_fofa"]})
    finally:
        backup.plugin_manager.set_disabled = old_set
    assert count == 2
    assert captured["types"] == [NEW, "api_fofa"]


def test_backup_schedule_type_is_canonicalized():
    captured = {}
    old_list = backup.db.list_schedules
    old_create = backup.db.create_schedule
    backup.db.list_schedules = lambda: []
    backup.db.create_schedule = lambda **kw: captured.update(kw) or {"id": 1}
    try:
        count = backup._import_schedules({"schedules": [{
            "name": "旧计划",
            "trigger_type": "interval",
            "trigger_args": {"hours": 1},
            "target_scope": "type",
            "target_id": OLD,
        }]})
    finally:
        backup.db.list_schedules = old_list
        backup.db.create_schedule = old_create
    assert count == 1
    assert captured["target_id"] == NEW


def test_old_backup_entry_gets_auth_mode_default():
    manifest = json.loads(
        (ROOT / "plugins" / "api" / "baidu_face" / "manifest.json").read_text())
    effective, errors = backup._prepare_entry(
        manifest["fields"],
        {"api_key": "a", "secret_key": "b", "base_url": "https://aip.baidubce.com"},
    )
    assert errors == []
    assert effective["auth_mode"] == "ak_sk"


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
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
