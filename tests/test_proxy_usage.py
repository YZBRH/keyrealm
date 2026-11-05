"""删除代理：引用预览、确认后重置凭据/默认设置、代理链阻断。"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-proxy-usage-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, plugin_manager, preset_store, proxy_store  # noqa: E402


def _proxy(name):
    result = api.create_proxy(name, "socks5://127.0.0.1:1080")
    assert result.get("ok"), result
    return result["created"]["id"]


def _credential(name, proxy_id):
    return api.create_credential(
        name, "llm_openai",
        {"api_key": "sk-test", "base_url": "https://api.example.com/v1",
         "_proxy_mode": "proxy", "_proxy_id": proxy_id},
        auto_verify=False,
    )


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_usage_preview_and_reset_before_delete():
    pid = _proxy("测试代理 A")
    cred = _credential("引用该代理的凭据", pid)
    preset_store.set_scope_defaults("plugin", "llm_openai",
                                    {"_proxy_mode": "proxy", "_proxy_id": pid})
    preset_store.set_scope_defaults("category", "llm",
                                    {"_proxy_mode": "proxy", "_proxy_id": pid})
    api.set_global_proxy(pid)

    usage = api.get_proxy_usage(pid)
    assert usage["ok"] is True
    assert usage["total"] == 3
    assert [c["id"] for c in usage["credentials"]] == [cred["id"]]
    assert {p["scope"] for p in usage["presets"]} == {"plugin", "category"}
    assert usage["global_default"] is True

    refused = api.delete_proxy(pid)
    assert refused["ok"] is False
    assert refused["error"] == "proxy_in_use"
    assert db.get_proxy(pid) is not None

    result = api.delete_proxy(pid, reset_references=True)
    assert result["ok"] is True
    assert result["reset"] == {"credentials": 1, "presets": 2}
    assert db.get_proxy(pid) is None
    assert proxy_store.global_proxy_id() is None

    data = api.get_credential(cred["id"])["data"]
    assert "_proxy_mode" not in data and "_proxy_id" not in data
    assert preset_store.get_scope_defaults("plugin", "llm_openai") == {}
    assert preset_store.get_scope_defaults("category", "llm") == {}


def test_proxy_chain_still_blocks_deletion():
    pid = _proxy("链成员 1")
    other = _proxy("链成员 2")
    _credential("链阻断测试", pid)
    chain = api.create_proxy_chain("测试链", [pid, other])
    assert chain.get("ok"), chain

    result = api.delete_proxy(pid, reset_references=True)
    assert result["ok"] is False
    assert "代理链" in result["message"]
    assert db.get_proxy(pid) is not None
    # 代理链阻断发生在重置之前，凭据引用应保持原样
    assert api.get_proxy_usage(pid)["total"] == 1


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
