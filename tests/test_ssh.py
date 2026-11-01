"""SSH 凭据插件与会话层契约测试（不依赖外部 SSH 服务）。

用 paramiko 起一个本地最小 SSH 服务器：
- 认证：只接受密码 `testpass`；
- shell：收到初始化标记回复 READY；收到命令脚本时回复固定输出、`/tmp` 与结束标记。

覆盖：字段校验、操作声明、会话建立 / 命令往返 / 断开、错误密码。

运行：.venv/bin/python tests/test_ssh.py
"""
import importlib.util
import io
import re
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import ssh_session  # noqa: E402

PLUGIN_PATH = ROOT / "plugins" / "remote" / "ssh" / "plugin.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("keyrealm_ssh_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


# ── 最小 SSH 服务器 ────────────────────────────────────────

def _make_server_class(password: str, state: dict):
    import paramiko

    class MockServer(paramiko.ServerInterface):
        def check_auth_password(self, username, password_given):
            if username == "test" and password_given == password:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password,publickey"

        def check_auth_publickey(self, username, key):
            if username == "test":
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def check_channel_request(self, kind, chanid):
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_shell_request(self, channel):
            return True

        def check_channel_exec_request(self, channel, command):
            state["exec_command"] = (
                command.decode("utf-8", "replace") if isinstance(command, bytes) else str(command)
            )
            return True

        def check_channel_pty_request(self, channel, term, width, height,
                                      pixelwidth, pixelheight, modes):
            return True

    return MockServer


def _start_mock_server(password="testpass"):
    import paramiko

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(15)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    state = {"transport": None, "channel": None, "stop": False, "received": b"", "exec_command": ""}
    server_cls = _make_server_class(password, state)

    def serve():
        try:
            client_sock, _ = listener.accept()
            transport = paramiko.Transport(client_sock)
            state["transport"] = transport
            transport.add_server_key(host_key)
            transport.start_server(server=server_cls())
            channel = transport.accept(10)
            state["channel"] = channel
            if channel is None:
                return
            while not state["stop"]:
                try:
                    data = channel.recv(8192)
                except Exception:  # noqa: BLE001
                    break
                if not data:
                    break
                state["received"] += data
                ready = re.search(rb"__KR_READY_([0-9a-f]+)__", data)
                if ready:
                    channel.send(b"\n__KR_READY_" + ready.group(1) + b"__\n")
                    continue
                if b"__KR_READY_%s__" in data:
                    tag_match = re.search(rb"'([0-9a-f]{12})'", data)
                    if tag_match:
                        channel.send(b"\n__KR_READY_" + tag_match.group(1) + b"__\n")
                    continue
                if b"__KR_PWD_%s__" in data:
                    match = re.search(rb"'([0-9a-f]{12})'", data)
                    if not match:
                        continue
                    tag = match.group(1)
                    rc = b"1" if b"false" in data else b"0"
                    channel.send(
                        b"__KR_START_" + tag + b"__\n"
                        b"mock-output\n"
                        b"__KR_PWD_" + tag + b"__\n/tmp\n"
                        b"__KR_RC_" + tag + b"_" + rc + b"__\n"
                        b"__KR_END_" + tag + b"__\n"
                    )
        except Exception:  # noqa: BLE001 - 测试服务器失败不阻塞用例
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def stop():
        state["stop"] = True
        for obj in (state.get("channel"), state.get("transport"), listener):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        thread.join(timeout=3)

    return port, stop, state


def _data(port, password="testpass"):
    return {
        "host": "127.0.0.1",
        "port": str(port),
        "username": "test",
        "auth_type": "password",
        "password": password,
        "timeout": "5",
    }


# ── 插件校验 / 操作 ────────────────────────────────────────

def test_validate_accepts_password_credentials():
    data = {
        "host": "example.com", "port": "22", "username": "root",
        "auth_type": "password", "password": "secret",
    }
    assert plugin.validate(data) == []


def test_validate_rejects_missing_and_invalid_fields():
    errors = plugin.validate({})
    assert any("主机" in item for item in errors)
    assert any("用户名" in item for item in errors)

    bad_port = plugin.validate({
        "host": "h", "port": "70000", "username": "u", "auth_type": "password",
        "password": "x",
    })
    assert any("端口" in item for item in bad_port)

    assert any("私钥" in item for item in plugin.validate({
        "host": "h", "port": "22", "username": "u", "auth_type": "key",
    }))


def test_actions_declares_console():
    data = {"host": "example.com", "username": "root"}
    actions = plugin.actions(data)
    assert actions and actions[0]["id"] == "console"
    assert actions[0]["kind"] == "console"


# ── 会话层 ────────────────────────────────────────────────

def test_shell_session_round_trip():
    port, stop, state = _start_mock_server()
    try:
        opened = ssh_session.open_session(1, _data(port))
        assert opened.get("ok") is True, opened
        sid = opened["session_id"]
        assert opened["mode"] == "shell"
        assert opened["shell_kind"] == "pipe"
        assert opened["history_off"] is True
        assert opened["host_key"].startswith("SHA256:")
        # 首条命令必须是关闭目标端历史记录的那条
        assert state["exec_command"] == ssh_session.PIPE_SHELL
        assert ssh_session.HISTORY_OFF.encode() in state["received"]

        result = ssh_session.run_command(sid, "echo hi")
        assert result.get("ok") is True, result
        assert "mock-output" in result["output"]
        assert result["cwd"] == "/tmp"
        assert result["exit_code"] == 0

        failed = ssh_session.run_command(sid, "false")
        assert failed.get("ok") is True, failed
        assert failed["exit_code"] == 1

        assert ssh_session.close_session(sid)["ok"] is True
        missing = ssh_session.run_command(sid, "echo hi")
        assert missing.get("ok") is False
    finally:
        stop()


def test_interactive_session_prefers_pty():
    port, stop, state = _start_mock_server()
    try:
        opened = ssh_session.open_session(1, _data(port), interactive=True)
        assert opened.get("ok") is True, opened
        assert opened["mode"] == "shell"
        assert opened["shell_kind"] == "pty"
        assert opened["interactive"] is True
        assert opened["history_off"] is True
        # 交互模式直接开 shell，不应再走 exec 通道
        assert state["exec_command"] == ""
        assert ssh_session.HISTORY_OFF.encode() in state["received"]

        result = ssh_session.run_command(opened["session_id"], "echo hi")
        assert result.get("ok") is True, result
        assert "mock-output" in result["output"]
        assert result["cwd"] == "/tmp"
        ssh_session.close_session(opened["session_id"])
    finally:
        stop()


def test_wrong_password_reports_error():
    port, stop, _state = _start_mock_server(password="testpass")
    try:
        opened = ssh_session.open_session(1, _data(port, password="wrong"))
        assert opened.get("ok") is False
        assert "认证失败" in opened.get("error", "")
    finally:
        stop()


def test_private_key_auth_and_passphrase():
    import paramiko

    key = paramiko.RSAKey.generate(2048)
    raw = io.StringIO()
    key.write_private_key(raw)
    encrypted = io.StringIO()
    key.write_private_key(encrypted, password="phrase")

    base = {
        "host": "127.0.0.1", "username": "test", "auth_type": "key", "timeout": "5",
    }
    assert plugin.validate({**base, "port": "22", "private_key": raw.getvalue()}) == []

    port, stop, state = _start_mock_server()
    try:
        opened = ssh_session.open_session(
            1, {**base, "port": str(port), "private_key": raw.getvalue()}
        )
        assert opened.get("ok") is True, opened
        ssh_session.close_session(opened["session_id"])
    finally:
        stop()

    port, stop, state = _start_mock_server()
    try:
        encrypted_opened = ssh_session.open_session(
            1, {
                **base, "port": str(port), "private_key": encrypted.getvalue(),
                "passphrase": "phrase",
            }
        )
        assert encrypted_opened.get("ok") is True, encrypted_opened
        ssh_session.close_session(encrypted_opened["session_id"])
    finally:
        stop()


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
    ssh_session.close_all()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
