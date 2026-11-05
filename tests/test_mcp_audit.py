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
        result={"ok": True},
        duration_ms=12,
    )
    rows = db.list_mcp_operation_logs()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "vault_create_credential"
    assert row["operation"] == "创建凭据"
    assert "sk-super-secret-value" not in row["params"]
    assert '"***"' in row["params"]
    assert row["status"] == "ok"
    assert row["duration_ms"] == 12


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
    assert "sk-another-secret" not in row["params"]


def test_tool_wrapper_writes_audit_row():
    db.clear_mcp_operation_logs()
    db.set_mcp_permission("vault_list_credentials", True)
    app = mcp_server._build_mcp_app()

    async def call():
        token = mcp_server._authenticated_var.set(True)
        try:
            return await app.call_tool("vault_list_credentials", {})
        finally:
            mcp_server._authenticated_var.reset(token)

    result = asyncio.run(call())
    assert result is not None
    rows = db.list_mcp_operation_logs()
    assert any(row["tool"] == "vault_list_credentials" and row["operation"] == "查询凭据列表"
               for row in rows), rows


def test_search_and_clear():
    db.clear_mcp_operation_logs()
    mcp_audit.record("vault_list_groups", {}, result=[], duration_ms=1)
    mcp_audit.record("vault_list_plugins", {}, result=[], duration_ms=1)
    assert len(db.list_mcp_operation_logs(search="分组")) == 1
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
