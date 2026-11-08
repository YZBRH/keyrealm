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


def test_proxy_keys_are_validated_on_write():
    """创建 / 修改 / 导入都会校验代理保留键：非法模式、缺 id、悬空 id 一律拒绝。"""
    pid = _proxy("校验用代理")
    base = {"api_key": "sk-test", "base_url": "https://api.example.com/v1"}

    for label, extra in (
        ("global", {"_proxy_mode": "global"}),
        ("direct", {"_proxy_mode": "direct"}),
        ("proxy", {"_proxy_mode": "proxy", "_proxy_id": pid}),
    ):
        result = api.create_credential(
            f"代理校验-{label}", "llm_openai", {**base, **extra}, auto_verify=False)
        assert "error" not in result, (label, result)

    cases = [
        ("bad-mode", {"_proxy_mode": "bogus"}, "代理模式"),
        ("missing-id", {"_proxy_mode": "proxy"}, "_proxy_id"),
        ("ghost-id", {"_proxy_mode": "proxy",
                      "_proxy_id": "0b0b0b0b-0000-0000-0000-000000000000"}, "不存在"),
    ]
    for label, extra, keyword in cases:
        result = api.create_credential(
            f"代理校验-{label}", "llm_openai", {**base, **extra}, auto_verify=False)
        assert result.get("error") == "validation_failed", (label, result)
        assert any(keyword in item for item in result.get("details", [])), (label, result)


def test_import_proxy_override():
    """导入时可给整批凭据统一指定出口；非法代理整体拒绝，不会导一半。"""
    pid = _proxy("导入统一代理")
    doc = {
        "format": "credential-vault-backup", "encrypted": False,
        "scope": "credentials", "groups": [],
        "credentials": [
            {"name": "导入-无代理", "credential_type": "llm_openai", "group": "",
             "data": {"api_key": "sk-imp-1", "base_url": "https://api.example.com/v1"}},
            {"name": "导入-自带全局", "credential_type": "llm_openai", "group": "",
             "data": {"api_key": "sk-imp-2", "base_url": "https://api.example.com/v1",
                      "_proxy_mode": "global"}},
        ],
    }
    result = api.apply_backup_doc(
        doc, proxy_override={"_proxy_mode": "proxy", "_proxy_id": pid})
    assert result.get("ok"), result
    assert result.get("imported") == 2, result
    imported = [c for c in db.list_credentials() if str(c["name"]).startswith("导入-")]
    assert len(imported) == 2
    for cred in imported:
        data = api.get_credential(cred["id"])["data"]
        assert data.get("_proxy_mode") == "proxy", (cred["name"], data)
        assert data.get("_proxy_id") == pid, (cred["name"], data)

    bad = api.apply_backup_doc(
        doc, proxy_override={"_proxy_mode": "proxy", "_proxy_id": "no-such-proxy"})
    assert bad.get("ok") is False
    assert "代理设置不合法" in str(bad.get("message") or "")


def test_mcp_proxy_args_merge():
    """MCP 的 proxy_mode / proxy_id 参数合并规则。"""
    from backend.mcp_server import _with_proxy_args

    pid = "3f2a0000-0000-0000-0000-000000000001"
    data = {"api_key": "sk-test", "_proxy_mode": "direct", "_proxy_id": "old"}

    # 两个参数都没传 → data 原样保留
    assert _with_proxy_args(data, None, None) == data
    # 只给 id → 按 proxy 处理
    assert _with_proxy_args({}, None, pid) == {"_proxy_mode": "proxy", "_proxy_id": pid}
    # global / direct → 丢掉残留 id
    assert _with_proxy_args(data, "global", "") == {"api_key": "sk-test", "_proxy_mode": "global"}
    assert _with_proxy_args(data, "direct", pid) == {"api_key": "sk-test", "_proxy_mode": "direct"}
    # 显式空串 → 清空代理设置（回到跟随默认）
    assert _with_proxy_args(data, "", "") == {"api_key": "sk-test"}


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
