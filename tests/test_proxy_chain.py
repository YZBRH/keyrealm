"""代理链契约测试（独立沙箱 + 本地 mock SOCKS5 代理）。

验证：代理链配置、两级 SOCKS5 串联后的真实数据往返、成员删除保护。
不访问外网。

运行：.venv/bin/python tests/test_proxy_chain.py
"""
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-proxychain-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import proxy_chain, proxy_store  # noqa: E402


# ── 最小 SOCKS5 代理（无认证）──────────────────────────────

def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("closed")
        data += chunk
    return data


def _relay(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass


def _start_socks5():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def handle(conn):
        try:
            ver, count = _recv_exact(conn, 2)
            _recv_exact(conn, count)
            conn.sendall(b"\x05\x00")
            ver, cmd, _, atyp = _recv_exact(conn, 4)
            if atyp == 1:
                host = socket.inet_ntoa(_recv_exact(conn, 4))
            elif atyp == 3:
                host = _recv_exact(conn, _recv_exact(conn, 1)[0]).decode()
            else:
                conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            target_port = int.from_bytes(_recv_exact(conn, 2), "big")
            upstream = socket.create_connection((host, target_port), timeout=5)
            conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            t = threading.Thread(target=_relay, args=(upstream, conn), daemon=True)
            t.start()
            _relay(conn, upstream)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def close():
        stop.set()
        try:
            listener.close()
        except OSError:
            pass

    return port, close


def _start_http_connect():
    """最小 HTTP CONNECT 代理（无认证）。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def handle(conn):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            first = buf.split(b"\r\n", 1)[0].decode()
            target = first.split()[1]
            host, target_port = target.rsplit(":", 1)
            host = host.strip("[]")
            upstream = socket.create_connection((host, int(target_port)), timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t = threading.Thread(target=_relay, args=(upstream, conn), daemon=True)
            t.start()
            _relay(conn, upstream)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def close():
        stop.set()
        try:
            listener.close()
        except OSError:
            pass

    return port, close


def _start_echo():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def handle(conn):
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return port, lambda: listener.close()


# ── 用例 ──────────────────────────────────────────────────

def test_chain_round_trip_through_two_socks5():
    import socks  # PySocks

    p1, stop1 = _start_socks5()
    p2, stop2 = _start_socks5()
    echo_port, stop_echo = _start_echo()
    try:
        a = proxy_store.create_proxy("hop-1", f"socks5://127.0.0.1:{p1}")
        b = proxy_store.create_proxy("hop-2", f"socks5://127.0.0.1:{p2}")
        assert a.get("id") and b.get("id"), (a, b)

        chain = proxy_chain.create_chain("two-hops", [a["id"], b["id"]])
        assert chain.get("ok") is True, chain
        chain_id = chain["id"]

        items = proxy_store.list_proxies()
        chain_item = next((item for item in items if item["id"] == chain_id), None)
        assert chain_item and chain_item["kind"] == "chain"
        assert "hop-1" in chain_item["url"] and "hop-2" in chain_item["url"]

        local = proxy_store.get_proxy_url(chain_id)
        assert local and local.startswith("socks5://127.0.0.1:"), local
        local_port = int(local.rsplit(":", 1)[1])

        # 凭据级选择代理链时，解析结果同样是本机转发器
        resolved = proxy_store.resolve_credential_proxy(
            {"_proxy_mode": "proxy", "_proxy_id": chain_id}, "remote_ssh"
        )
        assert str(resolved).startswith("socks5://127.0.0.1:"), resolved

        # 设为全局默认也能解析
        assert proxy_store.set_global_proxy(chain_id).get("ok") is True
        assert str(proxy_store.global_url()).startswith("socks5://127.0.0.1:")
        proxy_store.set_global_proxy(None)

        conn = socks.create_connection(
            ("127.0.0.1", echo_port),
            proxy_type=socks.SOCKS5,
            proxy_addr="127.0.0.1",
            proxy_port=local_port,
            timeout=5,
        )
        try:
            conn.sendall(b"hello-through-chain")
            assert conn.recv(64) == b"hello-through-chain"
        finally:
            conn.close()

        # 被代理链引用的成员不能直接删除
        refused = proxy_store.delete_proxy(a["id"])
        assert refused.get("ok") is False
        assert "代理链" in refused.get("message", "")
    finally:
        proxy_chain.close_all()
        stop1()
        stop2()
        stop_echo()


def test_http_and_socks5_mixed_chain():
    import socks  # PySocks

    p_http, stop_http = _start_http_connect()
    p_socks, stop_socks = _start_socks5()
    echo_port, stop_echo = _start_echo()
    try:
        first = proxy_store.create_proxy("http-hop", f"http://127.0.0.1:{p_http}")
        second = proxy_store.create_proxy("socks-hop", f"socks5://127.0.0.1:{p_socks}")
        chain = proxy_chain.create_chain("mixed", [first["id"], second["id"]])
        assert chain.get("ok") is True, chain
        local = proxy_store.get_proxy_url(chain["id"])
        local_port = int(local.rsplit(":", 1)[1])
        conn = socks.create_connection(
            ("127.0.0.1", echo_port),
            proxy_type=socks.SOCKS5,
            proxy_addr="127.0.0.1",
            proxy_port=local_port,
            timeout=5,
        )
        try:
            conn.sendall(b"http-then-socks")
            assert conn.recv(64) == b"http-then-socks"
        finally:
            conn.close()
    finally:
        proxy_chain.close_all()
        stop_http()
        stop_socks()
        stop_echo()


def test_chain_validation():
    p1, stop1 = _start_socks5()
    try:
        a = proxy_store.create_proxy("only-hop", f"socks5://127.0.0.1:{p1}")
        bad = proxy_chain.create_chain("too-short", [a["id"]])
        assert bad.get("ok") is False
        dup = proxy_chain.create_chain("dup", [a["id"], a["id"]])
        assert dup.get("ok") is False
    finally:
        stop1()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
        proxy_chain.close_all()
        shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
