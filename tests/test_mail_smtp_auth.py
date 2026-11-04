"""SMTP 认证分支：服务器返回 535 后立即断开时仍能拿到认证失败原因。"""
import importlib.util
import smtplib
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PLUGIN_DIR = ROOT / "plugins" / "mail"
sys.path.insert(0, str(PLUGIN_DIR))
PLUGIN_PATH = PLUGIN_DIR / "smtp" / "plugin.py"

spec = importlib.util.spec_from_file_location("smtp_plugin_test", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeSMTP:
    def __init__(self, features, responses, quit_error=True):
        self.esmtp_features = features
        self._responses = list(responses)
        self._quit_error = quit_error

    def ehlo_or_helo_if_needed(self):
        return 250, b"ok"

    def docmd(self, cmd, arg=None):
        reply = self._responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def login(self, username, password):
        raise AssertionError("不应走 smtplib.login 回退")

    def quit(self):
        if self._quit_error:
            raise smtplib.SMTPServerDisconnected("closed")
        return 221, b"bye"


def test_auth_login_reads_535_before_disconnect():
    server = FakeSMTP(
        {"auth": "PLAIN LOGIN"},
        [(334, b"VXNlcm5hbWU6"), (334, b"UGFzc3dvcmQ6"),
         (535, b"Login fail. Account is abnormal")],
    )
    code, resp = plugin._auth_login(server, "user@example.com", "authcode")
    assert code == 535
    assert b"Login fail" in resp


def test_auth_login_success_and_fallback():
    server = FakeSMTP(
        {"auth": "LOGIN"},
        [(334, b"VXNlcm5hbWU6"), (334, b"UGFzc3dvcmQ6"), (235, b"Authentication succeeded")],
    )
    assert plugin._auth_login(server, "user", "pass")[0] == 235

    plain_only = FakeSMTP({"auth": "PLAIN"}, [])
    assert plugin._auth_login(plain_only, "user", "pass") is None


def test_verify_marks_535_as_invalid():
    server = FakeSMTP(
        {"auth": "LOGIN"},
        [(334, b"VXNlcm5hbWU6"), (334, b"UGFzc3dvcmQ6"), (535, b"Login fail")],
    )
    with patch.object(plugin, "_connect_smtp_ssl", lambda host, port: server):
        result = plugin._verify_smtp({
            "host": "smtp.example.com", "port": 465, "username": "user@example.com",
            "password": "authcode", "ssl": True,
        })
    assert result.status == "invalid"
    assert "Login fail" in result.message


def test_verify_disconnect_without_reply_is_error():
    server = FakeSMTP({"auth": "LOGIN"}, [smtplib.SMTPServerDisconnected("closed")])
    with patch.object(plugin, "_connect_smtp_ssl", lambda host, port: server):
        result = plugin._verify_smtp({
            "host": "smtp.example.com", "port": 465, "username": "user@example.com",
            "password": "authcode", "ssl": True,
        })
    assert result.status == "error"
    assert "认证阶段连接被服务器断开" in result.message


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
