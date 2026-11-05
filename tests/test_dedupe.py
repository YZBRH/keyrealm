"""去重判定：同一敏感值指向不同目标时不算重复（独立沙箱）。

运行：.venv/bin/python tests/test_dedupe.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-dedupe-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, backup, plugin_manager  # noqa: E402


def _ssh(host, password, username="root", port="22"):
    return {
        "host": host, "port": port, "username": username,
        "auth_type": "password", "password": password,
    }


def _create(name, data, credential_type="remote_ssh"):
    return api.create_credential(name, credential_type, data, auto_verify=False)


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_same_secret_different_host_not_duplicate():
    _create("host-a", _ssh("192.0.2.10", "same-secret-1"))
    assert api.check_duplicate_credential(
        "remote_ssh", _ssh("192.0.2.11", "same-secret-1")) == []


def test_same_secret_different_username_not_duplicate():
    _create("user-a", _ssh("192.0.2.20", "same-secret-2", username="alice"))
    assert api.check_duplicate_credential(
        "remote_ssh", _ssh("192.0.2.20", "same-secret-2", username="bob")) == []


def test_same_target_same_secret_is_duplicate():
    first = _create("dup-a", _ssh("192.0.2.30", "same-secret-3"))
    matches = api.check_duplicate_credential(
        "remote_ssh", _ssh("192.0.2.30", "same-secret-3"))
    assert [item["id"] for item in matches] == [first["id"]]

    second = _create("dup-b", _ssh("192.0.2.30", "same-secret-3"))
    res = api.find_duplicate_credentials()
    groups = [g for g in res["groups"]
              if any(item["id"] == first["id"] for item in g["items"])]
    assert len(groups) == 1, res
    assert {item["id"] for item in groups[0]["items"]} == {first["id"], second["id"]}
    assert groups[0]["target"] == [
        {"key": "host", "label": "主机", "value": "192.0.2.30"},
        {"key": "port", "label": "端口", "value": "22"},
        {"key": "username", "label": "用户名", "value": "root"},
    ]


def test_baidu_token_mode_duplicate():
    data = {"auth_mode": "access_token", "access_token": "baidu-token-123456"}
    first = _create("baidu-a", data, "api_baidu_ai")
    matches = api.check_duplicate_credential("api_baidu_ai", dict(data))
    assert [item["id"] for item in matches] == [first["id"]]


def test_baidu_old_data_without_auth_mode_still_dedupes():
    data = {"api_key": "ak-123456", "secret_key": "sk-123456"}
    first = _create("baidu-old-a", data, "api_baidu_ai")
    matches = api.check_duplicate_credential("api_baidu_ai", dict(data))
    assert [item["id"] for item in matches] == [first["id"]]


def test_scan_does_not_group_by_secret_alone():
    a = _create("scan-a", _ssh("192.0.2.40", "same-secret-4"))
    b = _create("scan-b", _ssh("192.0.2.41", "same-secret-4"))
    res = api.find_duplicate_credentials()
    grouped_ids = {item["id"] for g in res["groups"] for item in g["items"]}
    assert a["id"] not in grouped_ids
    assert b["id"] not in grouped_ids


def test_url_normalization_and_different_base_url():
    data_a = {"auth_mode": "api_key", "base_url": "https://api.example.com/v1/",
              "api_key": "sk-same-5"}
    data_b = {"auth_mode": "api_key", "base_url": "https://api.example.com/v1",
              "api_key": "sk-same-5"}
    data_c = {"auth_mode": "api_key", "base_url": "https://other.example.com/v1",
              "api_key": "sk-same-5"}
    first = _create("url-a", data_a, "llm_openai")
    assert api.check_duplicate_credential("llm_openai", data_b)
    assert api.check_duplicate_credential("llm_openai", data_c) == []
    assert first["id"]


def test_missing_show_if_controller_uses_manifest_default():
    # API / CLI 创建时可能不带 auth_mode；默认值 api_key 下 api_key 字段仍然可见，
    # 不能因为 show_if 控制器缺省就跳过敏感值、导致重复检测失效。
    data = {"base_url": "https://api.example.com/v1", "api_key": "sk-same-7"}
    first = _create("showif-a", data, "llm_openai")
    second = _create("showif-b", dict(data), "llm_openai")
    groups = api.find_duplicate_credentials()["groups"]
    ids = {item["id"] for group in groups for item in group["items"]}
    assert {first["id"], second["id"]} <= ids


def test_backup_dedupe_key_includes_target():
    a = _ssh("192.0.2.50", "same-secret-6")
    b = _ssh("192.0.2.51", "same-secret-6")
    same = dict(a)
    assert backup._dedupe_key("remote_ssh", a) != backup._dedupe_key("remote_ssh", b)
    assert backup._dedupe_key("remote_ssh", a) == backup._dedupe_key("remote_ssh", same)


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
