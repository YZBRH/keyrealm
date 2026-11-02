"""Telnet / RDP / FTP 插件验证契约测试（本地最小服务器）。

运行：.venv/bin/python tests/test_remote_services.py
"""
import socketserver
import ssl
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.plugin_base import load_plugin_module  # noqa: E402

telnet_plugin = load_plugin_module(ROOT / "plugins" / "remote" / "telnet", "remote_telnet")
rdp_plugin = load_plugin_module(ROOT / "plugins" / "remote" / "rdp", "remote_rdp")
ftp_plugin = load_plugin_module(ROOT / "plugins" / "remote" / "ftp", "remote_ftp")


class _Server:
    def __init__(self, handler):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _self_signed_context(tmp: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


class _TlsServer:
    """隐式 FTPS：accept 后立即 TLS 握手，再交给 FTP handler。"""

    def __init__(self, handler):
        self.tmp = tempfile.TemporaryDirectory(prefix="kr-ftps-")
        context = _self_signed_context(Path(self.tmp.name))

        class TlsServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

            def get_request(inner):
                sock, addr = super().get_request()
                return context.wrap_socket(sock, server_side=True), addr

        self.server = TlsServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()


class TelnetHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"Mock Telnet\r\nlogin: ")
        self.wfile.flush()
        username = self.rfile.readline().decode("utf-8", "replace").strip()
        if username != "admin":
            self.wfile.write(b"Login incorrect\r\nlogin: ")
            self.wfile.flush()
            return
        self.wfile.write(b"Password: ")
        self.wfile.flush()
        password = self.rfile.readline().decode("utf-8", "replace").strip()
        if password == "secret":
            self.wfile.write(b"\r\nWelcome to mock\r\n$ ")
        else:
            self.wfile.write(b"Login incorrect\r\nlogin: ")
        self.wfile.flush()


class RdpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(64)
        # TPKT + X.224 Connection Confirm + RDP Negotiation Response (CredSSP/NLA)
        self.request.sendall(bytes.fromhex("030000130ed000000000000200080002000000"))


class FtpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 Mock FTP ready\r\n")
        self.wfile.flush()
        user = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb == "USER":
                user = command.split(" ", 1)[1] if " " in command else ""
                self.wfile.write(b"331 Password required\r\n")
            elif verb == "PASS":
                password = command.split(" ", 1)[1] if " " in command else ""
                if user == "admin" and password == "secret":
                    self.wfile.write(b"230 Logged in\r\n")
                else:
                    self.wfile.write(b"530 Login incorrect\r\n")
            elif verb == "PWD":
                self.wfile.write(b'257 "/" is current directory\r\n')
            elif verb == "QUIT":
                self.wfile.write(b"221 Bye\r\n")
                self.wfile.flush()
                return
            else:
                self.wfile.write(b"200 OK\r\n")
            self.wfile.flush()


def _run(handler, plugin, data):
    server = _Server(handler)
    try:
        return plugin.verify({**data, "host": "127.0.0.1", "port": str(server.port), "timeout": "5"})
    finally:
        server.stop()


def test_telnet_requires_credentials():
    result = telnet_plugin.verify({"host": "127.0.0.1", "port": "23"})
    assert result.status == "invalid", result
    assert "用户名" in result.message and "密码" in result.message


def test_telnet_login_success_and_failure():
    ok = _run(TelnetHandler, telnet_plugin, {"username": "admin", "password": "secret"})
    assert ok.status == "valid", ok
    assert "登录成功" in ok.message

    bad = _run(TelnetHandler, telnet_plugin, {"username": "admin", "password": "wrong"})
    assert bad.status == "invalid", bad
    assert "登录失败" in bad.message


def test_rdp_requires_full_authentication():
    # 只完成 X.224 协商、没有 CredSSP/TLS 的服务器不能被判为凭据有效
    result = _run(RdpHandler, rdp_plugin, {"username": "admin", "password": "secret"})
    assert result.status == "error", result
    assert result.status != "valid"
    assert rdp_plugin._looks_auth_failure(Exception("STATUS_LOGON_FAILURE")) is True


def test_rdp_auth_failure_maps_to_invalid():
    original = rdp_plugin._attempt

    async def fake_attempt(target, credential, iosettings, timeout, auth_only):
        return False, Exception("STATUS_LOGON_FAILURE")

    rdp_plugin._attempt = fake_attempt
    try:
        result = rdp_plugin.verify({
            "host": "127.0.0.1", "port": "3389", "username": "admin",
            "password": "wrong", "timeout": "5",
        })
    finally:
        rdp_plugin._attempt = original
    assert result.status == "invalid", result
    assert "凭据无效" in result.message


def test_rdp_does_not_fall_back_by_default():
    original = rdp_plugin._attempt
    calls = []

    async def fake_attempt(target, credential, iosettings, timeout, auth_only):
        calls.append(auth_only)
        if auth_only:
            return False, RuntimeError("auth_only requires CredSSP/NLA")
        return True, None

    rdp_plugin._attempt = fake_attempt
    try:
        result = rdp_plugin.verify({
            "host": "127.0.0.1", "port": "3389", "username": "admin",
            "password": "secret", "timeout": "5",
        })
    finally:
        rdp_plugin._attempt = original
    assert result.status == "error", result
    assert calls == [True]
    assert "未回退" in result.message


def test_rdp_falls_back_to_full_login_when_allowed():
    original = rdp_plugin._attempt
    calls = []

    async def fake_attempt(target, credential, iosettings, timeout, auth_only):
        calls.append(auth_only)
        if auth_only:
            return False, RuntimeError("auth_only requires CredSSP/NLA")
        return True, None

    rdp_plugin._attempt = fake_attempt
    try:
        result = rdp_plugin.verify({
            "host": "127.0.0.1", "port": "3389", "username": "admin",
            "password": "secret", "timeout": "5",
            "allow_full_login": True,
        })
    finally:
        rdp_plugin._attempt = original
    assert result.status == "valid", result
    assert calls == [True, False]


def test_ftp_login_success_and_failure():
    ok = _run(FtpHandler, ftp_plugin, {"username": "admin", "password": "secret"})
    assert ok.status == "valid", ok
    assert "登录成功" in ok.message

    bad = _run(FtpHandler, ftp_plugin, {"username": "admin", "password": "wrong"})
    assert bad.status == "invalid", bad
    assert "用户名或密码错误" in bad.message


def test_implicit_ftps_login_success_and_failure():
    server = _TlsServer(FtpHandler)
    try:
        ok = ftp_plugin.verify({
            "host": "127.0.0.1", "port": str(server.port), "timeout": "5",
            "username": "admin", "password": "secret", "tls": "implicit",
        })
        assert ok.status == "valid", ok
        assert "隐式" in ok.message

        bad = ftp_plugin.verify({
            "host": "127.0.0.1", "port": str(server.port), "timeout": "5",
            "username": "admin", "password": "wrong", "tls": "implicit",
        })
        assert bad.status == "invalid", bad
    finally:
        server.stop()


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
