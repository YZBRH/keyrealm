"""百度 AI 开放平台插件：清单、认证分流、操作与权限探测（不联网）。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLUGIN_PATH = ROOT / "plugins" / "api" / "baidu_ai" / "plugin.py"
_spec = importlib.util.spec_from_file_location("baidu_ai_plugin_test", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

TOKEN = "24.test-token"

FACE_GROUP = plugin._FACE_GROUP_PATH
FACE_USERS = plugin._FACE_USERS_PATH


def _patched(**values):
    """临时替换插件模块属性，返回 restore 函数。"""
    old = {name: getattr(plugin, name) for name in values}
    for name, value in values.items():
        setattr(plugin, name, value)

    def restore():
        for name, value in old.items():
            setattr(plugin, name, value)
    return restore


def _call_map(mapping):
    def fake(base, token, path, body):
        value = mapping.get(path)
        if isinstance(value, tuple):
            return value
        if value is None:
            return {"error_code": 6, "error_msg": "No permission to access data"}, 200, None
        return value, 200, None
    return fake


def test_manifest_identity_and_fields():
    manifest = json.loads((PLUGIN_PATH.parent / "manifest.json").read_text())
    assert manifest["name"] == "百度 AI 开放平台"
    assert manifest["credential_type"] == "api_baidu_ai"
    assert manifest["version"] == "1.1.0"
    auth = next(f for f in manifest["fields"] if f["key"] == "auth_mode")
    assert auth["default"] == "ak_sk"
    assert [o["value"] for o in auth["options"]] == ["ak_sk", "access_token"]
    sensitive = {f["key"] for f in manifest["fields"] if f.get("sensitive")}
    assert sensitive == {"api_key", "secret_key", "access_token"}
    shown = {f["key"]: f.get("show_if") for f in manifest["fields"]}
    assert shown["api_key"] == {"auth_mode": "ak_sk"}
    assert shown["access_token"] == {"auth_mode": "access_token"}
    # 直填 token 模式不暴露任何探针配置字段
    assert "probe_path" not in shown and "probe_body" not in shown


def test_mode_defaults_to_ak_sk():
    assert plugin._mode({}) == "ak_sk"
    assert plugin._mode({"auth_mode": "bogus"}) == "ak_sk"
    assert plugin._mode({"auth_mode": "access_token"}) == "access_token"


def test_validate_branches():
    assert plugin.validate({}) == ["API Key 不能为空", "Secret Key 不能为空"]
    assert plugin.validate({"auth_mode": "access_token"}) == [
        "密钥种类为 access_token 时必须填写 access_token"]
    assert plugin.validate({"auth_mode": "access_token", "access_token": TOKEN}) == []
    # 旧的探针字段不再要求填写；插件校验忽略它们（数据升级会清理）
    assert plugin.validate({
        "auth_mode": "access_token", "access_token": TOKEN,
        "probe_path": "https://old.example/x", "probe_body": "not-json",
    }) == []
    assert plugin.validate({"api_key": "ak", "secret_key": "sk", "base_url": "ftp://x"}) == [
        "Base URL 必须是 http(s) 地址"]


def test_classify_codes():
    expected = {
        0: plugin._PROBE_OK,
        6: plugin._PROBE_DENIED,
        14: plugin._PROBE_DENIED,
        110: plugin._PROBE_BAD_CRED,
        111: plugin._PROBE_BAD_CRED,
        4: plugin._PROBE_QUOTA,
        17: plugin._PROBE_QUOTA,
        18: plugin._PROBE_QUOTA,
        19: plugin._PROBE_QUOTA,
        216: plugin._PROBE_BUSINESS,
    }
    for code, state in expected.items():
        assert plugin._classify({"error_code": code}, 200) == state
    assert plugin._classify(None, 200) == plugin._PROBE_UNKNOWN


def test_verify_ak_sk_exchange_only():
    restore = _patched(_get_access_token=lambda base, ak, sk: ("tok", "", False, 2592000))
    try:
        result = plugin.verify({"api_key": "ak", "secret_key": "sk"})
        assert result.status == "valid"
        assert result.account_info == {"auth_mode": "ak_sk",
                                       "access_token_expires_in": 2592000}

        plugin._get_access_token = lambda base, ak, sk: (None, "API Key 不正确或已被禁用", True, 0)
        assert plugin.verify({"api_key": "bad", "secret_key": "sk"}).status == "invalid"

        plugin._get_access_token = lambda base, ak, sk: (None, "请求超时（6 秒）", False, 0)
        assert plugin.verify({"api_key": "ak", "secret_key": "sk"}).status == "error"
    finally:
        restore()


def test_verify_token_mode_states():
    data = {"auth_mode": "access_token", "access_token": TOKEN}

    cases = [
        ("ok", ({"error_code": 0, "error_msg": "SUCCESS"}, 200, None), "valid"),
        ("denied", ({"error_code": 6, "error_msg": "No permission"}, 200, None), "valid"),
        ("quota", ({"error_code": 17, "error_msg": "limit"}, 200, None), "valid"),
        ("business", ({"error_code": 216201, "error_msg": "bad image"}, 200, None), "valid"),
        ("bad", ({"error_code": 110, "error_msg": "invalid token"}, 200, None), "invalid"),
        ("network", (None, 0, "网络请求失败（ConnectError）"), "error"),
    ]
    for name, response, expected in cases:
        restore = _patched(_call_api=lambda base, token, path, body, _r=response: _r)
        try:
            result = plugin.verify(dict(data))
            assert result.status == expected, (name, result.status, result.message)
            assert TOKEN not in result.message
        finally:
            restore()


def test_actions_by_mode():
    aksk = [a["id"] for a in plugin.actions({"api_key": "ak", "secret_key": "sk"})]
    token = [a["id"] for a in plugin.actions(
        {"auth_mode": "access_token", "access_token": TOKEN})]
    assert aksk == ["probe_permissions", "list_face_groups", "list_face_users",
                    "refresh_access_token"]
    assert token == ["probe_permissions", "list_face_groups", "list_face_users"]
    assert plugin.actions({}) == []


def test_probe_permissions_rows_align_columns():
    data = {"auth_mode": "access_token", "access_token": TOKEN}
    restore = _patched(
        _token_for=lambda d: (TOKEN, "", False, 0),
        _call_api=_call_map({FACE_GROUP: {"error_code": 0, "error_msg": "SUCCESS"}}),
    )
    try:
        result = plugin.run_action("probe_permissions", {}, data)
    finally:
        restore()
    assert result["ok"] is True
    keys = {c["key"] for c in plugin._PROBE_COLUMNS}
    assert all(set(row) == keys for row in result["rows"])
    states = {row["service"]: row["status"] for row in result["rows"]}
    assert states["百度人脸"] == "可用"
    assert states["文字识别 OCR"] == "无权限"


def test_face_actions_parse_nested_result():
    data = {"auth_mode": "access_token", "access_token": TOKEN}
    fake = _call_map({
        FACE_GROUP: {"error_code": 0, "result": {"group_id_list": ["g1", "g2"]}},
        FACE_USERS: {"error_code": 0, "result": {"user_id_list": ["u1"]}},
    })
    restore = _patched(_token_for=lambda d: (TOKEN, "", False, 0), _call_api=fake)
    try:
        groups = plugin.run_action("list_face_groups", {"start": 0, "length": 10}, data)
        users = plugin.run_action("list_face_users", {"group_id": "g1"}, data)
    finally:
        restore()
    assert groups == {
        "ok": True,
        "rows": [{"group_id": "g1"}, {"group_id": "g2"}],
        "columns": plugin._GROUP_COLUMNS,
        "total": 2,
        "empty_text": "该应用下没有人脸库分组",
    }
    assert users["rows"] == [{"user_id": "u1", "group_id": "g1"}]
    assert users["ok"] is True


def test_refresh_access_token_writes_updated_data_without_echo():
    data = {"api_key": "ak", "secret_key": "sk"}
    restore = _patched(_get_access_token=lambda base, ak, sk: ("new-secret-token", "", False, 2592000))
    try:
        result = plugin.run_action("refresh_access_token", {}, data)
    finally:
        restore()
    assert result["ok"] is True
    assert result["updated_data"] == {"access_token": "new-secret-token"}
    assert "new-secret-token" not in result["output"]
    assert plugin.run_action("refresh_access_token", {},
                             {"auth_mode": "access_token", "access_token": TOKEN})["ok"] is False


def test_capabilities_fail_open_and_hide():
    data = {"api_key": "ak", "secret_key": "sk"}

    restore = _patched(
        _token_for=lambda d: (TOKEN, "", False, 0),
        _call_api=_call_map({FACE_GROUP: {"error_code": 0}}),
    )
    try:
        cap = plugin.capabilities(data)
    finally:
        restore()
    assert cap["available"] == ["probe_permissions", "refresh_access_token",
                                "list_face_groups", "list_face_users"]
    assert cap["note"] == ""

    restore = _patched(
        _token_for=lambda d: (TOKEN, "", False, 0),
        _call_api=_call_map({FACE_GROUP: {"error_code": 6, "error_msg": "no permission"}}),
    )
    try:
        cap = plugin.capabilities(data)
    finally:
        restore()
    assert cap["available"] == ["probe_permissions", "refresh_access_token"]
    assert "人脸" in cap["note"]

    restore = _patched(
        _token_for=lambda d: (TOKEN, "", False, 0),
        _call_api=_call_map({FACE_GROUP: (None, 0, "网络请求失败（ConnectError）")}),
    )
    try:
        assert plugin.capabilities(data)["available"] is None
    finally:
        restore()

    restore = _patched(
        _token_for=lambda d: (TOKEN, "", False, 0),
        _call_api=_call_map({FACE_GROUP: {"error_code": 110}}),
    )
    try:
        assert plugin.capabilities(data)["available"] == []
    finally:
        restore()


def test_capabilities_token_invalid_returns_empty():
    data = {"auth_mode": "access_token", "access_token": TOKEN}
    restore = _patched(_token_for=lambda d: (None, "access_token 被拒绝", True, 0))
    try:
        assert plugin._has_credentials(data)
        cap = plugin.capabilities(data)
    finally:
        restore()
    assert cap["available"] == []


def test_old_credential_without_auth_mode_still_validates():
    assert plugin.validate({"api_key": "ak", "secret_key": "sk"}) == []
    assert "refresh_access_token" in [a["id"] for a in plugin.actions(
        {"api_key": "ak", "secret_key": "sk"})]


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
