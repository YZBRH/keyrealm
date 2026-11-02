"""主机管理 类别共享模块：字段读取、代理 TCP 隧道与少量协议工具。

具体服务（Telnet / RDP / FTP 及 SSH）的连接与判定仍在各自 ``plugin.py``，
这里只放多个插件都会用到的底层工具。依赖方向：``plugin.py`` → ``remote_common``
→ ``backend.plugin_sdk``，只依赖标准库、PySocks 与 SDK。
"""
from __future__ import annotations

import ftplib
import re
import select
import socket
import time
from urllib.parse import unquote

from backend.plugin_sdk import proxy as sdk_proxy
from backend.plugin_sdk import ssl_context as sdk_ssl_context

DEFAULT_TIMEOUT = 15.0

try:
    import socks as _socks  # PySocks
except Exception:  # noqa: BLE001
    _socks = None

_PROXY_SCHEMES = {
    "socks5": "SOCKS5",
    "socks5h": "SOCKS5",
    "socks4": "SOCKS4",
    "socks4a": "SOCKS4",
    "http": "HTTP",
    "https": "HTTP",
}
_PROXY_PORTS = {"socks5": 1080, "socks5h": 1080, "socks4": 1080, "socks4a": 1080,
                "http": 8080, "https": 8080}


def text(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def port(data: dict, default: int) -> int:
    raw = text(data, "port") or str(default)
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 1 <= value <= 65535 else 0


def timeout(data: dict, default: float = DEFAULT_TIMEOUT) -> float:
    raw = text(data, "timeout")
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if 1.0 <= value <= 300.0 else default


def _proxy_parts(url: str):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    mapped = _PROXY_SCHEMES.get(scheme)
    if mapped is None:
        raise ValueError(f"不支持的代理协议：{scheme or '?'}")
    return (
        getattr(_socks, mapped),
        parts.hostname or "",
        parts.port or _PROXY_PORTS.get(scheme, 1080),
        unquote(parts.username) if parts.username else None,
        unquote(parts.password) if parts.password else None,
        scheme in ("socks5h", "socks4a"),
    )


def open_socket(host: str, port_number: int, timeout_seconds: float) -> socket.socket:
    """建立到目标的 TCP 连接；配了代理时穿过 SOCKS / HTTP CONNECT 隧道。"""
    proxy_url = sdk_proxy()
    if not proxy_url:
        return socket.create_connection((host, port_number), timeout=timeout_seconds)
    if _socks is None:
        raise ValueError("经代理连接需要 PySocks，但当前环境没有安装")
    proxy_type, addr, proxy_port, user, password, rdns = _proxy_parts(proxy_url)
    return _socks.create_connection(
        (host, port_number),
        timeout=timeout_seconds,
        proxy_type=proxy_type,
        proxy_addr=addr,
        proxy_port=proxy_port,
        proxy_rdns=rdns,
        proxy_username=user,
        proxy_password=password,
    )


# ── 代理感知的 FTP ────────────────────────────────────────
class TunneledFTP(ftplib.FTP):
    """只覆写建连处，登录 / 命令行为与标准库一致。"""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = open_socket(self.host, self.port, self.timeout)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class TunneledFTP_TLS(ftplib.FTP_TLS):
    """显式 FTPS（AUTH TLS）。"""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = open_socket(self.host, self.port, self.timeout)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def ftp_client(tls: bool, timeout_seconds: float):
    if tls:
        # 凭据验证不因自签 / 内网证书被误判；TLS 版本仍走框架的统一策略。
        context = sdk_ssl_context(verify=False)
        return TunneledFTP_TLS(timeout=timeout_seconds, context=context)
    return TunneledFTP(timeout=timeout_seconds)


# ── Telnet / RDP 的收包工具 ───────────────────────────────
_IAC = 0xFF
_IAC_VERB = {0xFB, 0xFC, 0xFD, 0xFE}
_ANSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_telnet(data: bytes) -> bytes:
    """去掉 Telnet IAC（选项协商）字节，便于展示与匹配提示符。"""
    out = bytearray()
    i = 0
    size = len(data)
    while i < size:
        byte = data[i]
        if byte != _IAC:
            out.append(byte)
            i += 1
            continue
        if i + 1 >= size:
            break
        code = data[i + 1]
        if code == _IAC:
            out.append(_IAC)
            i += 2
        elif code in _IAC_VERB:
            i += 3
        else:
            i += 2
    return bytes(out)


def readable_text(data: bytes) -> str:
    """IAC 与 ANSI 控制序列都去掉后的可读文本。"""
    return _ANSI_RE.sub(b"", strip_telnet(data)).decode("utf-8", "replace")


def recv_until(sock: socket.socket, pattern: re.Pattern, timeout_seconds: float,
               limit: int = 16384) -> tuple[bytes, bool]:
    """读 socket 直到匹配 pattern、超时或对端关闭。返回 ``(原始数据, 是否匹配)``。"""
    deadline = time.monotonic() + timeout_seconds
    buf = bytearray()
    while time.monotonic() < deadline:
        wait = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([sock], [], [], min(0.2, wait))
        if not ready:
            continue
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        if pattern.search(readable_text(bytes(buf))):
            return bytes(buf), True
        if len(buf) >= limit:
            break
    return bytes(buf), bool(pattern.search(readable_text(bytes(buf))))


def recv_idle(sock: socket.socket, seconds: float, limit: int = 16384) -> bytes:
    """读到连续 ``seconds`` 秒没有新数据或达到大小上限为止。"""
    deadline = time.monotonic() + seconds
    buf = bytearray()
    while time.monotonic() < deadline and len(buf) < limit:
        ready, _, _ = select.select([sock], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def printable(data: bytes, limit: int = 200) -> str:
    """把 banner 一类字节整理成单行可读文本。"""
    text_value = readable_text(data)
    text_value = "".join(ch if ch.isprintable() or ch in "\r\n\t" else " " for ch in text_value)
    lines = [line.strip() for line in text_value.replace("\r", "\n").split("\n") if line.strip()]
    joined = " / ".join(lines)
    return joined[:limit]
