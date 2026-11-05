"""企业微信插件操作：部门 / 成员 / 客户联系 / 群机器人发送。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "wecom_plugin_test", ROOT / "plugins" / "im" / "wecom" / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, token_payload=None):
        self.token_payload = token_payload or {
            "errcode": 0, "access_token": "tok_secret_value", "expires_in": 7200,
        }

    def get(self, url, params=None):
        path = url.split("qyapi.weixin.qq.com", 1)[-1]
        if path == "/cgi-bin/gettoken":
            return FakeResponse(self.token_payload)
        if path == "/cgi-bin/department/list":
            return FakeResponse({"errcode": 0, "department": [
                {"id": 1, "name": "总部", "parentid": 0, "leader": "张三"},
                {"id": 80, "name": "门店", "parentid": 1, "leader": ""},
            ]})
        if path == "/cgi-bin/user/list":
            return FakeResponse({"errcode": 0, "userlist": [
                {"userid": "u1", "name": "李四", "department": [80],
                 "position": "店长", "status": 1},
            ]})
        if path == "/cgi-bin/externalcontact/get_follow_user_list":
            return FakeResponse({"errcode": 0, "follow_user": ["u1"]})
        if path == "/cgi-bin/externalcontact/list":
            return FakeResponse({"errcode": 84061, "errmsg": "not external contact"})
        return FakeResponse({"errcode": 40001, "errmsg": "bad"})

    def post(self, url, **kwargs):
        return FakeResponse({"errcode": 0, "errmsg": "ok"})


def test_actions_for_app_and_webhook():
    app = plugin.actions({"kind": "app", "corpid": "ww1", "corpsecret": "secret"})
    assert [a["id"] for a in app] == [
        "token", "departments", "users", "follow_users", "external_contacts"]
    webhook = plugin.actions({"kind": "webhook", "webhook_key": "key"})
    assert [a["id"] for a in webhook] == ["webhook_send"]
    assert webhook[0]["write"] is True


def test_token_is_masked():
    result = plugin._h_token(FakeClient(), "ww1", "secret", {})
    assert result["ok"] is True
    token_row = next(r for r in result["rows"] if r["item"] == "调用凭据")
    assert "tok_secret_value" not in token_row["value"]


def test_departments_and_users():
    client = FakeClient()
    dep = plugin._h_departments(client, "ww1", "secret", {})
    assert [r["id"] for r in dep["rows"]] == [1, 80]
    users = plugin._h_users(client, "ww1", "secret", {"department_id": "80"})
    assert users["rows"][0]["status"] == "已激活"
    assert users["rows"][0]["department"] == "80"


def test_follow_users_and_external_contacts_empty():
    client = FakeClient()
    follow = plugin._h_follow_users(client, "ww1", "secret", {})
    assert follow["rows"] == [{"userid": "u1"}]
    contacts = plugin._h_external_contacts(client, "ww1", "secret", {"userid": "u1"})
    assert contacts["ok"] is True
    assert contacts["rows"] == []
    assert "暂无外部联系人" in contacts["empty_text"]


def test_webhook_send():
    result = plugin._webhook_send(FakeClient(), "key", "测试消息")
    assert result == {"ok": True, "output": "文本消息已发送"}


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
