"""MCP 操作审计：落库、参数脱敏、工具包装。"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-mcp-audit-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import mcp_audit, mcp_server, plugin_manager  # noqa: E402


def setup_module():
    plugin_manager.sync_plugins()


def teardown_module():
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_record_redacts_sensitive_values():
    db.clear_mcp_operation_logs()
    mcp_audit.record(
        "vault_create_credential",
        {
            "name": "测试",
            "credential_type": "llm_openai",
            "data": {"api_key": "sk-super-secret-value", "base_url": "https://x"},
        },
        result={"ok": True, "credential": {"id": 1, "api_key": "sk-super-secret-value"}},
        duration_ms=12,
        client_ip="10.0.0.5",
    )
    rows = db.list_mcp_operation_logs()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "vault_create_credential"
    assert row["operation"] == "创建凭据"
    assert row["client_ip"] == "10.0.0.5"
    assert "sk-super-secret-value" not in row["params_preview"]
    assert "sk-super-secret-value" not in row["result_preview"]
    assert '"***"' in row["params_preview"]
    assert '"***"' in row["result_preview"]
    assert row["status"] == "ok"
    assert row["duration_ms"] == 12


def test_get_secret_result_is_redacted():
    """vault_get_secret 的键名可能是 value/data，返回值必须整体当密文处理。"""
    db.clear_mcp_operation_logs()
    mcp_audit.record(
        "vault_get_secret",
        {"credential_id": 1, "field": "value"},
        result={"field": "value", "value": "sk-direct-secret"},
        client_ip="127.0.0.1",
    )
    row = db.list_mcp_operation_logs()[0]
    assert "sk-direct-secret" not in row["result_preview"]
    assert '"***"' in row["result_preview"]

    # 短密文同样要打码（MIN_SECRET_LEN 不适用于 get_secret 的返回值）
    db.clear_mcp_operation_logs()
    mcp_audit.record("vault_get_secret", {"credential_id": 1},
                     result={"data": {"pin": "1234", "hint": "x"}}, client_ip="127.0.0.1")
    row = db.list_mcp_operation_logs()[0]
    assert "1234" not in row["result_preview"]
    assert '"pin": "***"' in row["result_preview"]


def test_url_userinfo_is_redacted():
    db.clear_mcp_operation_logs()
    mcp_audit.record(
        "vault_create_proxy",
        {"name": "x", "url": "socks5://alice:pa55word@127.0.0.1:1080"},
        result={"ok": True, "proxy": {"url": "socks5://alice:pa55word@127.0.0.1:1080"}},
        client_ip="127.0.0.1",
    )
    row = db.list_mcp_operation_logs()[0]
    assert "pa55word" not in row["params_preview"] and "alice" not in row["params_preview"]
    assert "pa55word" not in row["result_preview"] and "alice" not in row["result_preview"]
    assert "socks5://***@127.0.0.1:1080" in row["params_preview"]


def test_error_message_is_redacted():
    db.clear_mcp_operation_logs()
    mcp_audit.record(
        "vault_verify_credential",
        {"credential_id": 1, "api_key": "sk-another-secret"},
        result={"error": "bad key sk-another-secret"},
        duration_ms=9,
    )
    row = db.list_mcp_operation_logs()[0]
    assert row["status"] == "error"
    assert "sk-another-secret" not in row["message"]
    assert "sk-another-secret" not in row["params_preview"]


def test_tool_wrapper_writes_audit_row():
    db.clear_mcp_operation_logs()
    db.set_mcp_permission("vault_list_credentials", True)
    app = mcp_server._build_mcp_app()

    async def call():
        token = mcp_server._authenticated_var.set(True)
        ip_token = mcp_audit.client_ip_var.set("192.0.2.10")
        try:
            return await app.call_tool("vault_list_credentials", {})
        finally:
            mcp_audit.client_ip_var.reset(ip_token)
            mcp_server._authenticated_var.reset(token)

    result = asyncio.run(call())
    assert result is not None
    rows = db.list_mcp_operation_logs()
    assert any(row["tool"] == "vault_list_credentials"
               and row["operation"] == "查询凭据列表"
               and row["client_ip"] == "192.0.2.10"
               and row["result_preview"]
               for row in rows), rows


def test_client_ip_extraction():
    from starlette.datastructures import Headers

    scope = {"client": ("10.1.2.3", 4567)}
    assert mcp_server._client_ip(scope, Headers({})) == "10.1.2.3"
    assert (mcp_server._client_ip(scope, Headers({"x-forwarded-for": "8.8.8.8, 10.0.0.1"}))
            == "10.1.2.3 (xff: 8.8.8.8)")
    assert mcp_server._client_ip({"client": None}, Headers({})) == ""


def test_list_returns_previews_and_detail_returns_full():
    db.clear_mcp_operation_logs()
    long_text = "x" * 400
    mcp_audit.record("vault_list_credentials", {"search": long_text},
                     result={"note": long_text}, duration_ms=1)
    row = db.list_mcp_operation_logs()[0]
    assert len(row["params_preview"]) <= 200
    assert len(row["result_preview"]) <= 200
    full = db.get_mcp_operation_log(row["id"])
    assert full and long_text in full["params"] and long_text in full["result"]
    assert db.get_mcp_operation_log(999999) is None


def test_retention_keeps_latest_rows():
    db.clear_mcp_operation_logs()
    old_max = db._MCP_LOG_MAX
    db._MCP_LOG_MAX = 3
    try:
        for i in range(5):
            mcp_audit.record("vault_list_groups", {"i": i}, result=[], duration_ms=1)
        rows = db.list_mcp_operation_logs(limit=10)
        assert len(rows) == 3
        assert [row["params_preview"] for row in rows] == [
            '{"i": 4}', '{"i": 3}', '{"i": 2}',
        ]
    finally:
        db._MCP_LOG_MAX = old_max
        db.clear_mcp_operation_logs()


def test_search_and_clear():
    db.clear_mcp_operation_logs()
    mcp_audit.record("vault_list_groups", {}, result=[], duration_ms=1, client_ip="192.0.2.10")
    mcp_audit.record("vault_list_plugins", {}, result=[], duration_ms=1)
    assert len(db.list_mcp_operation_logs(search="分组")) == 1
    assert len(db.list_mcp_operation_logs(search="192.0.2.10")) == 1
    assert db.clear_mcp_operation_logs() == 2
    assert db.list_mcp_operation_logs() == []


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
