"""写入校验：所有写凭据的路径都必须拒绝非法字段值（独立沙箱）。

覆盖：
1. 创建 / 更新按 manifest 的 select 选项校验（SSH auth_type 只能 password / key）；
2. 未知字段、错误类型、非法数字被拒绝；
3. 名称为空 / 超长被拒绝；
4. MCP 的 vault_update_credential 走同一套 api 校验（不再直接写库）；
5. 插件被禁用时仍允许编辑已有凭据（只跳过插件级 validate）。

运行：.venv/bin/python tests/test_validation.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-validation-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import api, mcp_server, plugin_manager  # noqa: E402


def _ssh(**overrides):
    data = {
        "host": "192.0.2.1", "port": "22", "username": "root",
        "auth_type": "password", "password": "secret",
    }
    data.update(overrides)
    return data


def _create_ssh(name="ssh-test"):
    return api.create_credential(name, "remote_ssh", _ssh(), auto_verify=False)


def setup_module():
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True


def teardown_module():
    api.lock_vault()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_create_rejects_invalid_select_value():
    res = api.create_credential(
        "bad-openai", "llm_openai",
        {"auth_mode": "private_key", "api_key": "sk-x"}, auto_verify=False,
    )
    assert res.get("error") == "validation_failed"
    assert any("取值不合法" in item for item in res.get("details", []))


def test_update_rejects_invalid_select_and_keeps_old_value():
    cred = _create_ssh()
    res = api.update_credential(cred["id"], data=_ssh(auth_type="private_key"))
    assert res.get("error") == "validation_failed"
    assert any("取值不合法" in item for item in res.get("details", []))

    stored = api.get_credential(cred["id"])["data"]
    assert stored.get("auth_type") == "password", "非法更新不应落库"


def test_unknown_field_and_bad_scalar_type_rejected():
    cred = _create_ssh()
    res = api.update_credential(cred["id"], data={**_ssh(), "bogus": 1})
    assert res.get("error") == "validation_failed"
    assert any("未声明字段" in item for item in res.get("details", []))

    res = api.update_credential(cred["id"], data={**_ssh(), "host": {"nested": True}})
    assert res.get("error") == "validation_failed"
    assert any("值类型不合法" in item for item in res.get("details", []))


def test_number_field_rejects_non_numeric():
    errors = plugin_manager.validate_field_values("mail_smtp", {"port": "not-a-number"})
    assert any("必须是数字" in item for item in errors)


def test_name_validation():
    empty = api.create_credential("", "remote_ssh", _ssh(), auto_verify=False)
    assert empty.get("error") == "validation_failed"
    cred = _create_ssh("valid-name")
    too_long = api.update_credential(cred["id"], name="x" * 101)
    assert too_long.get("error") == "validation_failed"


def test_disabled_plugin_still_allows_editing_existing_credential():
    cred = _create_ssh("disabled-edit")
    original = plugin_manager.get_module

    def _missing(credential_type):
        if credential_type == "remote_ssh":
            return None
        return original(credential_type)

    plugin_manager.get_module = _missing
    try:
        res = api.update_credential(cred["id"], data=_ssh(notes="still editable"))
    finally:
        plugin_manager.get_module = original
    assert res.get("id") == cred["id"], res
    assert api.get_credential(cred["id"])["data"].get("notes") == "still editable"


def test_mcp_update_uses_same_validation():
    cred = _create_ssh("mcp-edit")
    db.set_mcp_permission("vault_update_credential", True)
    app = mcp_server._build_mcp_app()

    async def call():
        token = mcp_server._authenticated_var.set("test")
        try:
            return await app.call_tool("vault_update_credential", {
                "credential_id": cred["id"],
                "data": _ssh(auth_type="private_key"),
            })
        finally:
            mcp_server._authenticated_var.reset(token)

    result = asyncio.run(call())
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "validation_failed" in text
    assert "取值不合法" in text
    assert api.get_credential(cred["id"])["data"].get("auth_type") == "password"


def test_group_name_validation():
    assert api.create_group("")["error"] == "validation_failed"
    assert api.create_group("x" * 41)["error"] == "validation_failed"
    group = api.create_group("  ok-group  ")
    assert group.get("name") == "ok-group", group
    res = api.update_group(group["id"], name="   ")
    assert res.get("error") == "validation_failed"


def test_mcp_create_group_uses_same_validation():
    db.set_mcp_permission("vault_create_group", True)
    app = mcp_server._build_mcp_app()

    async def call(name):
        token = mcp_server._authenticated_var.set("test")
        try:
            return await app.call_tool("vault_create_group", {"name": name})
        finally:
            mcp_server._authenticated_var.reset(token)

    bad = asyncio.run(call(""))
    text = "".join(getattr(block, "text", "") for block in bad.content)
    assert "validation_failed" in text, text

    good = asyncio.run(call("mcp-group"))
    text = "".join(getattr(block, "text", "") for block in good.content)
    assert "mcp-group" in text, text


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
