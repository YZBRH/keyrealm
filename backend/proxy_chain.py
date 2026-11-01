"""代理链：把多个已保存的代理按顺序串联，对外暴露成本机 SOCKS5 端口。

为什么需要本地转发器
--------------------
插件只拿到一个代理 URL（HTTP / SOCKS），无法表达「A → B → C」。所以这里为每条代理链
在本机 127.0.0.1 起一个很小的 SOCKS5 服务：客户端连它，它按成员顺序逐跳建立隧道，
再双向转发。插件侧完全无感，现有 httpx / PySocks / git 路径都能复用。

成员限制
--------
- 只接受已保存的 **HTTP / SOCKS4 / SOCKS5** 代理；HTTPS 代理（TLS 套 TLS）暂不支持。
- 至少 2 个成员，顺序即连接顺序；不允许重复、不允许嵌套代理链。
- 链配置存 ``settings.proxy_chains``（JSON，无表结构变化）；成员按代理 UUID 引用。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import select
import socket
import socketserver
import struct
import threading
import uuid
from typing import Optional
from urllib.parse import unquote, urlsplit

from . import database as db
from . import logger

_log = logger.get_logger(__name__)

SETTING_KEY = "proxy_chains"
CHAIN_PREFIX = "chain_"
SUPPORTED_SCHEMES = {"http", "socks4", "socks5", "socks5h"}
CONNECT_TIMEOUT = 15.0
MAX_HEADER = 16 * 1024

_runtimes: dict[str, "_Runtime"] = {}
_runtimes_lock = threading.Lock()


def is_chain_id(proxy_id) -> bool:
    return str(proxy_id or "").startswith(CHAIN_PREFIX)


# ── 配置读写 ──────────────────────────────────────────────

def _read() -> list[dict]:
    raw = db.get_setting(SETTING_KEY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write(items: list[dict]) -> None:
    db.set_setting(SETTING_KEY, json.dumps(items, ensure_ascii=False))


def _member_urls(member_ids: list[str]) -> tuple[list[dict], str]:
    """把成员 id 解析成代理记录；有缺失 / 类型不支持时返回错误。"""
    records = []
    for pid in member_ids:
        if is_chain_id(pid):
            return [], "代理链不能嵌套代理链"
        record = db.get_proxy(str(pid))
        if not record:
            return [], "成员代理不存在（可能已被删除）"
        scheme = (urlsplit(record.get("url", "")).scheme or "").lower()
        if scheme not in SUPPORTED_SCHEMES:
            return [], f"代理「{record.get('name')}」的协议 {scheme or '?'} 暂不支持用于代理链"
        records.append(record)
    return records, ""


def list_chains() -> list[dict]:
    """给界面用的代理链列表（成员附名称与地址）。"""
    items = []
    for chain in _read():
        members = chain.get("members") or []
        records, error = _member_urls(members)
        names = [r.get("name") or r.get("url") for r in records] if not error else []
        items.append({
            **chain,
            "kind": "chain",
            "members": members,
            "member_names": names,
            "member_urls": [r.get("url") for r in records],
            "display": " → ".join(names) if names else "（成员缺失）",
            "valid": not error,
            "error": error,
        })
    return items


def get_chain(chain_id: str) -> Optional[dict]:
    cid = str(chain_id or "")
    for chain in _read():
        if str(chain.get("id")) == cid:
            return chain
    return None


def _validate(name: str, members: list[str]) -> tuple[str, list[str], str]:
    name = (name or "").strip()
    if not name:
        return "", [], "代理链名称不能为空"
    cleaned = [str(item or "").strip() for item in members if str(item or "").strip()]
    if len(cleaned) < 2:
        return "", [], "代理链至少需要选择 2 个代理"
    if len(set(cleaned)) != len(cleaned):
        return "", [], "同一个代理不能重复加入代理链"
    records, error = _member_urls(cleaned)
    if error:
        return "", [], error
    return name, cleaned, ""


def create_chain(name: str, member_ids: list[str]) -> dict:
    name, members, error = _validate(name, member_ids)
    if error:
        return {"ok": False, "message": error}
    chain = {
        "id": CHAIN_PREFIX + uuid.uuid4().hex,
        "name": name,
        "members": members,
    }
    items = _read()
    items.append(chain)
    _write(items)
    _log.info("已创建代理链「%s」（%s 跳）", name, len(members))
    return {"ok": True, **chain}


def update_chain(chain_id: str, name: str, member_ids: list[str]) -> dict:
    cid = str(chain_id or "")
    name, members, error = _validate(name, member_ids)
    if error:
        return {"ok": False, "message": error}
    items = _read()
    for index, chain in enumerate(items):
        if str(chain.get("id")) == cid:
            items[index] = {**chain, "name": name, "members": members}
            _write(items)
            close_chain(cid)  # 成员顺序/内容变了，旧转发器立即失效
            _log.info("已更新代理链「%s」（%s 跳）", name, len(members))
            return {"ok": True, "id": cid, "name": name, "members": members}
    return {"ok": False, "message": "代理链不存在"}


def delete_chain(chain_id: str) -> dict:
    cid = str(chain_id or "")
    items = _read()
    remaining = [chain for chain in items if str(chain.get("id")) != cid]
    if len(remaining) == len(items):
        return {"ok": False, "message": "代理链不存在"}
    _write(remaining)
    close_chain(cid)
    _log.info("已删除代理链 %s", cid)
    return {"ok": True, "id": cid}


def chains_using(proxy_id: str) -> list[str]:
    """引用了该代理的代理链名称（删除代理前检查）。"""
    pid = str(proxy_id or "")
    names = []
    for chain in _read():
        if pid in {str(item) for item in chain.get("members") or []}:
            names.append(str(chain.get("name") or chain.get("id")))
    return names


# ── 单跳协议 ──────────────────────────────────────────────

class ChainError(RuntimeError):
    pass


def _parse_proxy(url: str) -> dict:
    text = str(url or "").strip()
    if "://" not in text:
        raise ChainError("代理地址缺少协议")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        raise ChainError("代理链暂不支持 HTTPS 代理")
    if scheme not in SUPPORTED_SCHEMES:
        raise ChainError(f"不支持的代理协议：{scheme or '?'}")
    host = parts.hostname or ""
    if not host:
        raise ChainError("代理地址缺少主机")
    try:
        port = parts.port or (1080 if scheme.startswith("socks") else 8080)
    except ValueError as exc:
        raise ChainError("代理端口不合法") from exc
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": unquote(parts.username) if parts.username else "",
        "password": unquote(parts.password) if parts.password else "",
    }


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ChainError("代理提前关闭连接")
        data += chunk
    return data


def _socks5_connect(sock: socket.socket, host: str, port: int, user: str, password: str) -> None:
    methods = b"\x00" + (b"\x02" if user else b"")
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    ver, method = _recv_exact(sock, 2)
    if ver != 5 or method == 0xFF:
        raise ChainError("SOCKS5 代理拒绝认证方式")
    if method == 2:
        if not user:
            raise ChainError("SOCKS5 代理要求用户名密码")
        u = user.encode("utf-8")
        p = password.encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise ChainError("SOCKS5 用户名或密码过长")
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exact(sock, 2)[1] != 0:
            raise ChainError("SOCKS5 认证失败")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        addr = b"\x03" + bytes([len(host.encode("utf-8"))]) + host.encode("utf-8")
    else:
        packed = ip.packed
        addr = (b"\x01" if ip.version == 4 else b"\x04") + packed
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    ver, rep, _, atyp = _recv_exact(sock, 4)
    if ver != 5 or rep != 0:
        raise ChainError(f"SOCKS5 CONNECT 失败（代码 {rep}）")
    if atyp == 1:
        _recv_exact(sock, 4 + 2)
    elif atyp == 4:
        _recv_exact(sock, 16 + 2)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length + 2)
    else:
        raise ChainError("SOCKS5 返回了未知地址类型")


def _socks4_connect(sock: socket.socket, host: str, port: int, user: str) -> None:
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ChainError(f"SOCKS4 需要 IPv4 目标：{host}") from exc
    ip = infos[0][4][0]
    sock.sendall(
        struct.pack(">BBH", 4, 1, port)
        + socket.inet_aton(ip)
        + user.encode("utf-8")
        + b"\x00"
    )
    reply = _recv_exact(sock, 8)
    if reply[1] != 90:
        raise ChainError(f"SOCKS4 CONNECT 失败（代码 {reply[1]}）")


def _http_connect(sock: socket.socket, host: str, port: int, user: str, password: str) -> None:
    host_header = f"[{host}]" if ":" in host else host
    target = f"{host_header}:{port}"
    headers = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", "Proxy-Connection: keep-alive"]
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ChainError("HTTP 代理提前关闭连接")
        buf += chunk
        if len(buf) > MAX_HEADER:
            raise ChainError("HTTP 代理响应头过长")
    first = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = first.split()
    if len(parts) < 2 or parts[1] != "200":
        raise ChainError(f"HTTP 代理 CONNECT 失败：{first}")


def _connect_through(urls: list[str], host: str, port: int) -> socket.socket:
    if not urls:
        raise ChainError("代理链没有成员")
    hops = [_parse_proxy(url) for url in urls]
    sock = socket.create_connection((hops[0]["host"], hops[0]["port"]), timeout=CONNECT_TIMEOUT)
    try:
        sock.settimeout(CONNECT_TIMEOUT)
        for index, hop in enumerate(hops):
            if index + 1 < len(hops):
                dest_host, dest_port = hops[index + 1]["host"], hops[index + 1]["port"]
            else:
                dest_host, dest_port = host, port
            scheme = hop["scheme"]
            if scheme == "http":
                _http_connect(sock, dest_host, dest_port, hop["username"], hop["password"])
            elif scheme in ("socks5", "socks5h"):
                _socks5_connect(sock, dest_host, dest_port, hop["username"], hop["password"])
            elif scheme == "socks4":
                _socks4_connect(sock, dest_host, dest_port, hop["username"])
            else:  # pragma: no cover - _parse_proxy 已拦截
                raise ChainError(f"不支持的代理协议：{scheme}")
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


# ── 本地 SOCKS5 转发器 ────────────────────────────────────

def _relay(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([a, b], [], [], 60)
            if not readable:
                continue
            for src in readable:
                data = src.recv(65536)
                if not data:
                    return
                (b if src is a else a).sendall(data)
    except OSError:
        pass
    finally:
        for sock in (a, b):
            try:
                sock.close()
            except OSError:
                pass


class _SocksHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(CONNECT_TIMEOUT)
        try:
            greeting = _recv_exact(client, 2)
            if greeting[0] != 5:
                return
            methods = _recv_exact(client, greeting[1])
            client.sendall(b"\x05\x00" if 0 in methods else b"\x05\xff")
            if 0 not in methods:
                return

            header = _recv_exact(client, 4)
            if header[0] != 5 or header[1] != 1:
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                return
            atyp = header[3]
            if atyp == 1:
                host = socket.inet_ntoa(_recv_exact(client, 4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
            elif atyp == 3:
                length = _recv_exact(client, 1)[0]
                host = _recv_exact(client, length).decode("utf-8", "replace")
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            port = struct.unpack(">H", _recv_exact(client, 2))[0]

            try:
                upstream = _connect_through(self.server.chain_urls, host, port)
            except Exception as exc:  # noqa: BLE001 - 统一回失败码
                _log.warning("代理链连接失败（%s:%s）：%s", host, port, exc)
                client.sendall(b"\x05\x01\x00\x01" + b"\x00" * 6)
                return
            client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            client.settimeout(None)
            _relay(client, upstream)
        except (OSError, ChainError):
            return
        finally:
            try:
                client.close()
            except OSError:
                pass


class _ChainServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Runtime:
    def __init__(self, chain_id: str, urls: list[str]):
        self.chain_id = chain_id
        self.fingerprint = tuple(urls)
        self.server = _ChainServer(("127.0.0.1", 0), _SocksHandler)
        self.server.chain_urls = list(urls)  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, name=f"proxy-chain-{chain_id}", daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


def _member_urls_for(chain: dict) -> tuple[list[str], str]:
    records, error = _member_urls(list(chain.get("members") or []))
    if error:
        return [], error
    return [str(record.get("url") or "") for record in records], ""


def ensure_url(chain_id: str) -> Optional[str]:
    """确保该代理链的本地转发器在运行，返回 ``socks5://127.0.0.1:<port>``。"""
    cid = str(chain_id or "")
    chain = get_chain(cid)
    if not chain:
        return None
    urls, error = _member_urls_for(chain)
    if error or not urls:
        _log.warning("代理链不可用（%s）：%s", cid, error)
        return None
    with _runtimes_lock:
        runtime = _runtimes.get(cid)
        if runtime is not None and runtime.fingerprint == tuple(urls):
            return runtime.url
        if runtime is not None:
            runtime.stop()
            _runtimes.pop(cid, None)
        try:
            runtime = _Runtime(cid, urls)
        except OSError as exc:
            _log.warning("代理链转发器启动失败（%s）：%s", cid, exc)
            return None
        _runtimes[cid] = runtime
        _log.info("代理链「%s」已启动：%s", chain.get("name") or cid, runtime.url)
        return runtime.url


def close_chain(chain_id: str) -> None:
    with _runtimes_lock:
        runtime = _runtimes.pop(str(chain_id or ""), None)
    if runtime is not None:
        runtime.stop()


def close_all() -> None:
    with _runtimes_lock:
        runtimes = list(_runtimes.values())
        _runtimes.clear()
    for runtime in runtimes:
        runtime.stop()
