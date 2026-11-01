"""敏感值预览开关契约测试（独立沙箱，不碰仓库真实数据）。

覆盖：
1. 默认开启，短值按「前 4 位…」展示；
2. 关闭后凭据列表与去重结果的预览位置统一为 ******；
3. 重新开启后恢复掩码。

运行：.venv/bin/python tests/test_secret_preview.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-preview-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, plugin_manager  # noqa: E402


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def _create(name: str, api_key: str) -> dict:
    return api.create_credential(
        name=name,
        credential_type="llm_openai",
        data={"base_url": "https://api.openai.com/v1", "api_key": api_key, "notes": ""},
        auto_verify=False,
    )


def test_preview_enabled_by_default_and_short_mask():
    assert api.get_secret_preview()["enabled"] is True
    _create("preview-a", "sk-12345")
    row = next(item for item in api.list_credentials() if item["name"] == "preview-a")
    assert row["data_masked"]["api_key"] == "sk-1…"


def test_preview_disabled_replaces_list_and_dedupe():
    _create("preview-b", "dup-secret")
    _create("preview-c", "dup-secret")

    assert api.set_secret_preview(False)["enabled"] is False
    rows = api.list_credentials()
    assert rows
    assert all(
        value == "******"
        for row in rows
        for value in (row.get("data_masked") or {}).values()
    )

    groups = api.find_duplicate_credentials()["groups"]
    assert groups
    assert groups[0]["match"][0]["masked"] == "******"

    assert api.set_secret_preview(True)["enabled"] is True
    assert api.get_secret_preview()["enabled"] is True


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
