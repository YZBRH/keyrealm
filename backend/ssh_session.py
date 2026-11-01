"""SSH 交互会话（paramiko）：为「SSH 控制台」维护短期会话。

- 一个会话 = 一条已认证的 SSH 连接 + 一个交互 shell（服务器不支持 shell 时退化为逐条 exec）。
- 命令通过唯一标记分隔：``command`` → ``pwd`` → ``END``，据此切出输出与当前目录；
  不依赖 ``$?`` 等 shell 特有语法，尽量兼容 bash / zsh / fish / sh。
- 会话只存在内存中，关闭窗口 / 手动断开 / 空闲 30 分钟 / 程序退出时关闭。
- 不向日志写入命令内容或任何凭据字段。
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import socket
import threading
import time
import uuid
from typing import Optional

from . import logger

_log = logger.get_logger(__name__)

MAX_SESSIONS = 4
IDLE_SECONDS = 30 * 60
INIT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 30.0
MAX_OUTPUT = 512 * 1024
RECV_CHUNK = 65536

_ANSI_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))"
)


class SshError(RuntimeError):
    """带用户可读消息的会话错误。"""


def _text(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _port(data: dict) -> int:
    try:
        value = int(_text(data, "port") or "22")
    except ValueError:
        return 0
    return value if 1 <= value <= 65535 else 0


def _timeout(data: dict) -> float:
    try:
        value = float(_text(data, "timeout") or "15")
    except ValueError:
        return 15.0
    return value if 1 <= value <= 300 else 15.0


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _proxy_url(data: dict) -> Optional[str]:
    """该凭据实际生效的代理 URL（含代理链的本机转发器）；直连返回 None。"""
    from . import proxy_store
    from . import vault_proxy_ctx as ctx

    resolved = proxy_store.resolve_credential_proxy(data, "remote_ssh")
    if resolved is ctx.DIRECT:
        return None
    if resolved is ctx.UNSET:
        for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
            value = os.environ.get(key)
            if value:
                return value
        return None
    return str(resolved)


def _open_transport_socket(host: str, port: int, timeout: float, data: dict) -> socket.socket:
    """建立到目标的 TCP 连接；有代理时经 PySocks 建隧道（支持 http / socks4 / socks5）。"""
    url = _proxy_url(data)
    if not url:
        return socket.create_connection((host, port), timeout=timeout)
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        raise SshError("SSH 经 HTTPS 代理连接暂不支持")
    try:
        import socks  # PySocks
    except ImportError as exc:
        raise SshError("经代理连接 SSH 需要 PySocks") from exc
    types = {
        "socks5": socks.SOCKS5, "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4, "http": socks.HTTP,
    }
    if scheme not in types:
        raise SshError(f"不支持的代理协议：{scheme or '?'}")
    return socks.create_connection(
        (host, port),
        timeout=timeout,
        proxy_type=types[scheme],
        proxy_addr=parts.hostname or "",
        proxy_port=parts.port or (1080 if scheme.startswith("socks") else 8080),
        proxy_username=unquote(parts.username) if parts.username else None,
        proxy_password=unquote(parts.password) if parts.password else None,
    )


def _load_key(data: dict):
    import paramiko  # noqa: PLC0415

    key_text = _text(data, "private_key")
    if not key_text:
        raise SshError("使用私钥认证时必须填写私钥")
    passphrase = _text(data, "passphrase") or None
    last_error = None
    # paramiko 5 基类 PKey.from_private_key 不再按内容分派，逐个具体类型尝试。
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_text), password=passphrase)
        except paramiko.PasswordRequiredException:
            if not passphrase:
                raise SshError("私钥已加密，请填写私钥口令") from None
            last_error = "私钥口令错误"
        except paramiko.SSHException as exc:
            last_error = str(exc) or "格式不受支持"
    raise SshError(f"私钥解析失败：{last_error or '格式不受支持'}")


def _fingerprint(client) -> str:
    transport = client.get_transport()
    key = transport.get_remote_server_key() if transport else None
    if key is None:
        return ""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _Session:
    def __init__(self, session_id: str, client, channel, data: dict):
        self.id = session_id
        self.client = client
        self.channel = channel
        self.summary = f"{_text(data, 'username')}@{_text(data, 'host')}"
        self.mode = "shell" if channel is not None else "exec"
        self.created = time.monotonic()
        self.last_used = self.created
        self.lock = threading.Lock()
        self.closed = False

    # ── 底层读写 ──────────────────────────────────────

    def _recv_until(self, token: bytes, timeout: float) -> tuple[bytes, bool]:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.channel is None:
                return bytes(buf), False
            try:
                chunk = self.channel.recv(RECV_CHUNK)
            except socket.timeout:
                continue
            except (OSError, EOFError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if token in buf:
                return bytes(buf), True
            if len(buf) > MAX_OUTPUT:
                break
        return bytes(buf), token in buf

    def _drain(self, timeout: float = 0.25) -> None:
        """丢弃残留输出（例如超时后 Ctrl+C 的回显）。"""
        if self.channel is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self.channel.recv_ready():
                    time.sleep(0.02)
                    continue
                if not self.channel.recv(RECV_CHUNK):
                    return
            except (socket.timeout, OSError, EOFError):
                return

    def _init_shell(self) -> tuple[bool, str]:
        """初始化交互 shell，返回 ``(是否就绪, 登录横幅)``。"""
        marker = b"__KR_READY__"
        setup = (
            "export PS1='' PS2='' PS3=''; unset PROMPT_COMMAND; "
            "stty -echo 2>/dev/null; printf '\\n__KR_READY__\\n'\n"
        )
        try:
            self.channel.send(setup.encode("utf-8"))
        except (OSError, EOFError):
            return False, ""
        raw, ok = self._recv_until(marker, INIT_TIMEOUT)
        text = _clean(raw.decode("utf-8", "replace"))
        if not ok:
            return False, ""
        banner = text.split("__KR_READY__", 1)[0].strip("\n")[:2000]
        return True, banner

    # ── 对外操作 ──────────────────────────────────────

    def run(self, command: str) -> dict:
        command = (command or "").rstrip("\n")
        if not command.strip():
            return {"ok": True, "output": "", "cwd": "", "exit_code": None}
        with self.lock:
            if self.closed:
                raise SshError("会话已断开，请重新连接")
            self.last_used = time.monotonic()
            if self.mode == "shell":
                return self._run_shell(command)
            return self._run_exec(command)

    def _run_shell(self, command: str) -> dict:
        tag = uuid.uuid4().hex[:12]
        pwd_mark = f"__KR_PWD_{tag}__"
        end_mark = f"__KR_END_{tag}__"
        script = (
            f"{command}\n"
            f"printf '\\n{pwd_mark}\\n'\n"
            "pwd 2>/dev/null\n"
            f"printf '{end_mark}\\n'\n"
        )
        try:
            self.channel.send(script.encode("utf-8"))
        except (OSError, EOFError) as exc:
            self.closed = True
            raise SshError("会话已断开，请重新连接") from exc

        raw, ok = self._recv_until(end_mark.encode(), COMMAND_TIMEOUT)
        text = _clean(raw.decode("utf-8", "replace"))
        if not ok:
            # 命令可能在等待输入（交互式程序）：发 Ctrl+C 并断开，避免污染下一条命令
            try:
                self.channel.send(b"\x03")
            except OSError:
                pass
            self._drain()
            self.close()
            partial = text.split(pwd_mark, 1)[0].strip("\n")
            raise SshError(
                ("命令超时或会话已断开" + (f"\n已收到部分输出：\n{partial[-4000:]}" if partial else ""))
            )

        before, _, rest = text.partition(pwd_mark)
        cwd_block, _, _ = rest.partition(end_mark)
        cwd = ""
        for line in reversed(cwd_block.strip("\n").splitlines()):
            if line.strip():
                cwd = line.strip()
                break
        output = before.strip("\n")
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n…（输出过长已截断）"
        return {"ok": True, "output": output, "cwd": cwd, "exit_code": None}

    def _run_exec(self, command: str) -> dict:
        """服务器不支持交互 shell（常见于 Windows）时逐条执行，不保留 cwd / 环境。"""
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=COMMAND_TIMEOUT)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
        except Exception as exc:  # noqa: BLE001 - 连接断开 / 超时
            self.closed = True
            raise SshError(f"命令执行失败：{type(exc).__name__}") from exc
        text = _clean(out + (("\n" + err) if err else ""))
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n…（输出过长已截断）"
        return {"ok": True, "output": text, "cwd": "", "exit_code": code}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.channel is not None:
                self.channel.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()
_cleaner: Optional[threading.Thread] = None


def _clean_loop() -> None:
    while True:
        time.sleep(60)
        with _sessions_lock:
            stale = _prune_locked()
        _close_many(stale)


def _ensure_cleaner() -> None:
    global _cleaner
    if _cleaner is not None and _cleaner.is_alive():
        return
    _cleaner = threading.Thread(target=_clean_loop, name="ssh-session-cleaner", daemon=True)
    _cleaner.start()


def _prune_locked() -> list[_Session]:
    now = time.monotonic()
    stale = [s for s in _sessions.values() if now - s.last_used > IDLE_SECONDS]
    for s in stale:
        _sessions.pop(s.id, None)
    return stale


def _close_many(sessions: list[_Session]) -> None:
    for session in sessions:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def open_session(credential_id: int, data: dict) -> dict:
    """建立连接与会话；返回前端需要的摘要信息。"""
    try:
        import paramiko  # noqa: PLC0415
    except ImportError:
        return {"ok": False, "error": "SSH 组件未安装（缺少 paramiko）"}
    _ensure_cleaner()

    host = _text(data, "host")
    port = _port(data)
    username = _text(data, "username")
    timeout = _timeout(data)
    auth_type = (_text(data, "auth_type") or "password").lower()
    if not host or not port or not username:
        return {"ok": False, "error": "凭据缺少主机 / 端口 / 用户名"}

    client = paramiko.SSHClient()
    if _truthy(data.get("verify_host_key")):
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = dict(
        hostname=host,
        port=port,
        username=username,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        channel_timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    sock = None
    try:
        sock = _open_transport_socket(host, port, timeout, data)
        kwargs["sock"] = sock
        if auth_type == "key":
            kwargs["pkey"] = _load_key(data)
        else:
            kwargs["password"] = _text(data, "password")
        client.connect(**kwargs)
    except paramiko.AuthenticationException:
        client.close()
        if sock is not None:
            sock.close()
        return {"ok": False, "error": "认证失败：用户名/密码或私钥不正确"}
    except paramiko.BadHostKeyException:
        client.close()
        if sock is not None:
            sock.close()
        return {"ok": False, "error": "主机密钥与 known_hosts 不一致，已拒绝连接"}
    except SshError as exc:
        client.close()
        if sock is not None:
            sock.close()
        return {"ok": False, "error": str(exc)}
    except (paramiko.SSHException, socket.timeout, OSError) as exc:
        client.close()
        if sock is not None:
            sock.close()
        return {"ok": False, "error": f"连接失败：{exc}"}

    fingerprint = _fingerprint(client)
    channel = None
    banner = ""
    try:
        channel = client.invoke_shell(width=120, height=40, term="xterm-256color")
        channel.settimeout(0.2)
    except Exception:  # noqa: BLE001 - 服务器禁用 shell：退化为逐条 exec
        channel = None

    session_id = uuid.uuid4().hex
    session = _Session(session_id, client, channel, data)
    if channel is not None:
        try:
            ready, banner = session._init_shell()
        except Exception:  # noqa: BLE001 - 初始化失败也允许 exec 模式
            ready, banner = False, ""
        if not ready:
            # 没拿到 READY（Windows / 非 POSIX shell）：关闭 shell，改走 exec
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass
            session.channel = None
            session.mode = "exec"

    with _sessions_lock:
        stale = _prune_locked()
        while len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda s: s.last_used)
            _sessions.pop(oldest.id, None)
            stale.append(oldest)
        _sessions[session_id] = session
    _close_many(stale)
    _log.info("SSH 会话已建立：%s:%s（模式=%s）", host, port, session.mode)
    return {
        "ok": True,
        "session_id": session_id,
        "summary": session.summary,
        "mode": session.mode,
        "host_key": fingerprint,
        "banner": banner,
        "credential_id": credential_id,
    }


def run_command(session_id: str, command: str) -> dict:
    with _sessions_lock:
        session = _sessions.get(str(session_id))
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    try:
        result = session.run(command)
    except SshError as exc:
        _drop(session_id)
        return {"ok": False, "error": str(exc), "closed": True}
    if session.closed:
        _drop(session_id)
    return result


def _drop(session_id: str) -> None:
    with _sessions_lock:
        session = _sessions.pop(str(session_id), None)
    if session is not None:
        session.close()


def close_session(session_id: str) -> dict:
    _drop(session_id)
    return {"ok": True}


def close_all() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    _close_many(sessions)
