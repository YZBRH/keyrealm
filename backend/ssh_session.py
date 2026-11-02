"""SSH 交互会话（paramiko）：为「SSH 控制台」维护短期会话。

- 一个会话 = 一条已认证的 SSH 连接 + 一个持久 shell：默认非交互 ``sh -s``（不申请 PTY）；
  显式开启交互模式时优先 PTY（加载登录 shell 的 rc）；服务器禁止 exec 时自动换另一条路，
  都不可用则退化为逐条 exec。
- 连接成功后首先关闭目标端 shell 历史记录，并让命令输出不含提示符 / 输入回显，减少目标侧痕迹。
- 命令通过唯一标记分隔：``START`` → ``command`` → ``pwd`` → ``RC`` → ``END``，
  据此切出输出、当前目录与退出码；脚本只用 POSIX 语法（``printf`` / ``pwd`` / ``$?``），
  兼容 bash / zsh / sh。
- 会话只存在内存中，关闭窗口 / 手动断开 / 空闲 30 分钟 / 程序退出时关闭。
- 不向日志写入命令内容或任何凭据字段。
"""
from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import posixpath
import re
import socket
import stat
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

# SFTP 文件操作上限：预览只看一小段，下载 / 上传走 JS 桥会有 base64 膨胀，限制在 20MB。
SFTP_PREVIEW_MAX = 256 * 1024
SFTP_DOWNLOAD_MAX = 20 * 1024 * 1024
SFTP_UPLOAD_MAX = 20 * 1024 * 1024
SFTP_DIR_MAX = 2000

# 连接成功后首先执行：关闭本次会话的历史记录，减少在目标主机留下的痕迹。
HISTORY_OFF = (
    "unset HISTORY HISTFILE HISTSAVE HISTZONE HISTORY HISTLOG; "
    "export HISTFILE=/dev/null; export HISTSIZE=0; export HISTFILESIZE=0"
)

# 首选非交互 `sh -s`：不申请 PTY、无提示符、无回显，也不写入 shell 历史。
PIPE_SHELL = "sh -s"

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


def _looks_text(data: bytes, mime: str) -> bool:
    """粗判文件能否按文本预览（NUL 字节直接判二进制；UTF-8 / GB18030 可解码则算文本）。"""
    if mime:
        if mime.startswith("text/"):
            return True
        if mime in ("application/json", "application/xml", "application/javascript",
                    "application/x-sh", "application/x-yaml"):
            return True
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    for encoding in ("utf-8", "gb18030"):
        try:
            sample.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


_PROTOCOL_MARKERS = ("__KR_READY_%s", "__KR_START_%s", "__KR_PWD_%s", "__KR_RC_%s", "__KR_END_%s")
_PROTOCOL_LINES = {"__kr_rc=$?", "pwd 2>/dev/null", "stty -echo 2>/dev/null"}


def _strip_protocol_noise(text: str) -> str:
    """去掉协议脚本自身被 PTY 回显出来的内容（真实输出不受影响）。"""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in _PROTOCOL_LINES:
            continue
        if any(marker in line for marker in _PROTOCOL_MARKERS):
            continue
        if "unset HISTORY HISTFILE" in line:
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def _strip_pty_echo(text: str, command: str) -> str:
    """PTY 模式下服务器会回显刚发送的脚本；去掉命令回显与协议行。"""
    text = text.lstrip("\n")
    if command and text.startswith(command):
        text = text[len(command):]
    return _strip_protocol_noise(text)


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
    def __init__(self, session_id: str, client, channel, data: dict,
                 proxied: bool = False):
        self.id = session_id
        self.client = client
        self.channel = channel
        self.summary = f"{_text(data, 'username')}@{_text(data, 'host')}"
        self.mode = "shell" if channel is not None else "exec"
        self.shell_kind = ""          # pipe / pty
        self.proxied = proxied
        self.banner = ""
        self.created = time.monotonic()
        self.last_used = self.created
        self.lock = threading.Lock()
        self.sftp = None
        self.sftp_lock = threading.Lock()
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

    def _init_pipe(self) -> bool:
        """初始化非交互 ``sh -s``，成功返回 True。

        首条命令固定关闭目标侧历史记录；没有 PTY，因此没有提示符与输入回显。
        """
        tag = uuid.uuid4().hex[:12]
        marker = f"__KR_READY_{tag}__"
        setup = f"{HISTORY_OFF}\nprintf '\\n{marker}\\n'\n"
        try:
            self.channel.send(setup.encode("utf-8"))
        except (OSError, EOFError):
            return False
        raw, ok = self._recv_until(marker.encode(), INIT_TIMEOUT)
        if not ok:
            return False
        text = _clean(raw.decode("utf-8", "replace")).replace(marker, "")
        self.banner = _strip_protocol_noise(text)[:2000]
        return True

    def _init_shell(self) -> bool:
        """初始化 PTY 交互 shell（交互模式），成功返回 True。"""
        tag = uuid.uuid4().hex[:12]
        marker = f"__KR_READY_{tag}__"
        # 标记由 printf 在目标端拼出：脚本里只有 %s，即使输入被回显也不会误判就绪。
        setup = (
            f"{HISTORY_OFF}; export PS1='' PS2='' PS3=''; unset PROMPT_COMMAND; "
            f"stty -echo 2>/dev/null; printf '\\n__KR_READY_%s__\\n' '{tag}'\n"
        )
        try:
            self.channel.send(setup.encode("utf-8"))
        except (OSError, EOFError):
            return False
        raw, ok = self._recv_until(marker.encode(), INIT_TIMEOUT)
        text = _clean(raw.decode("utf-8", "replace"))
        if not ok:
            return False
        self.banner = _strip_protocol_noise(text.split(marker, 1)[0])[:2000]
        return True

    def _drop_channel(self) -> None:
        """放弃当前通道，回到 exec 模式前先关闭它。"""
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:  # noqa: BLE001
                pass
        self.channel = None
        self.mode = "exec"
        self.shell_kind = ""

    def _try_pipe(self) -> bool:
        """尝试非交互 ``sh -s``（静默模式）。"""
        try:
            channel = self.client.get_transport().open_session()
            channel.set_combine_stderr(True)
            channel.settimeout(0.2)
            channel.exec_command(PIPE_SHELL)
        except Exception:  # noqa: BLE001 - 服务器拒绝 exec
            return False
        self.channel = channel
        self.mode = "shell"
        self.shell_kind = "pipe"
        try:
            ready = self._init_pipe()
        except Exception:  # noqa: BLE001
            ready = False
        if not ready:
            self._drop_channel()
        return ready

    def _try_pty(self) -> bool:
        """尝试 PTY 交互 shell（交互模式；也会加载登录 shell 的 rc）。"""
        try:
            channel = self.client.invoke_shell(width=120, height=40, term="xterm-256color")
            channel.settimeout(0.2)
        except Exception:  # noqa: BLE001 - 服务器禁用 shell
            return False
        self.channel = channel
        self.mode = "shell"
        self.shell_kind = "pty"
        try:
            ready = self._init_shell()
        except Exception:  # noqa: BLE001
            ready = False
        if not ready:
            self._drop_channel()
        return ready

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
        start_mark = f"__KR_START_{tag}__"
        pwd_mark = f"__KR_PWD_{tag}__"
        rc_mark = f"__KR_RC_{tag}_"
        end_mark = f"__KR_END_{tag}__"
        # 标记由目标端 printf 拼出（脚本里只有 %s 占位）。这样即使 PTY 回显了
        # 刚发送的脚本，回显文本也不会被当成真实标记，输出不会被提前截断。
        # START 标记之后才是真正的命令输出，可据此丢掉提示符 / 命令回显。
        script = (
            f"printf '\\n__KR_START_%s__\\n' '{tag}'\n"
            f"{command}\n"
            "__kr_rc=$?\n"
            f"printf '\\n__KR_PWD_%s__\\n' '{tag}'\n"
            "pwd 2>/dev/null\n"
            f"printf '__KR_RC_%s_%s__\\n' '{tag}' \"$__kr_rc\"\n"
            f"printf '__KR_END_%s__\\n' '{tag}'\n"
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

        # 只保留 START 之后的真实输出（PTY 回显与提示符都在它之前 / 已过滤）。
        if start_mark in text:
            text = text.split(start_mark, 1)[1]
        before, _, rest = text.partition(pwd_mark)
        cwd_block, _, tail = rest.partition(rc_mark)
        rc_text, _, _ = tail.partition("__")
        exit_code = int(rc_text) if rc_text.isdigit() else None
        cwd = ""
        for line in reversed(_strip_protocol_noise(cwd_block).strip("\n").splitlines()):
            if line.strip():
                cwd = line.strip()
                break
        output = before
        if self.shell_kind == "pty":
            output = _strip_pty_echo(output, command)
        output = output.strip("\n")
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n…（输出过长已截断）"
        return {"ok": True, "output": output, "cwd": cwd, "exit_code": exit_code}

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

    # ── SFTP 文件操作 ─────────────────────────────────

    def _sftp_locked(self):
        """获取（懒创建）SFTP 客户端；调用方必须持有 ``sftp_lock``。"""
        if self.closed:
            raise SshError("会话已断开，请重新连接")
        if self.sftp is not None:
            return self.sftp
        try:
            sftp = self.client.open_sftp()
            channel = sftp.get_channel()
            if channel is not None:
                channel.settimeout(COMMAND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 服务器未启用 SFTP
            raise SshError(f"无法启动 SFTP：{exc or type(exc).__name__}") from exc
        self.sftp = sftp
        return sftp

    def list_dir(self, path: str) -> dict:
        with self.sftp_lock:
            self.last_used = time.monotonic()
            sftp = self._sftp_locked()
            try:
                base = sftp.normalize(path or ".")
                home = sftp.normalize(".")
                attrs = sftp.listdir_attr(base)
            except (IOError, OSError) as exc:
                raise SshError(str(exc) or "无法读取目录") from exc

            entries = []
            for attr in attrs[:SFTP_DIR_MAX]:
                mode = attr.st_mode or 0
                is_link = stat.S_ISLNK(mode)
                is_dir = stat.S_ISDIR(mode)
                entry_path = posixpath.join(base, attr.filename)
                if is_link:
                    try:
                        target = sftp.stat(entry_path)
                        is_dir = stat.S_ISDIR(target.st_mode or 0)
                    except (IOError, OSError):
                        pass
                entries.append({
                    "name": attr.filename,
                    "path": entry_path,
                    "is_dir": bool(is_dir),
                    "is_link": bool(is_link),
                    "size": int(attr.st_size or 0),
                    "mtime": int(attr.st_mtime or 0),
                })
            entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
            parent = posixpath.dirname(base.rstrip("/")) or "/"
            return {
                "ok": True,
                "path": base,
                "home": home,
                "parent": parent,
                "entries": entries,
                "truncated": len(attrs) > SFTP_DIR_MAX,
            }

    def read_file(self, path: str, max_bytes: Optional[int] = None) -> dict:
        limit = int(max_bytes or SFTP_PREVIEW_MAX)
        limit = max(1, min(limit, SFTP_DOWNLOAD_MAX))
        with self.sftp_lock:
            self.last_used = time.monotonic()
            sftp = self._sftp_locked()
            try:
                attr = sftp.stat(path)
                if stat.S_ISDIR(attr.st_mode or 0):
                    raise SshError("不能读取目录")
                with sftp.open(path, "rb") as handle:
                    data = handle.read(limit + 1)
            except SshError:
                raise
            except (IOError, OSError) as exc:
                raise SshError(str(exc) or "无法读取文件") from exc

        truncated = len(data) > limit
        data = data[:limit]
        name = posixpath.basename(path)
        mime, _ = mimetypes.guess_type(name)
        if mime and mime.startswith("image/"):
            kind = "image"
        elif _looks_text(data, mime or ""):
            kind = "text"
        else:
            kind = "binary"
        return {
            "ok": True,
            "name": name,
            "path": path,
            "size": int(attr.st_size or 0),
            "mime": mime or "application/octet-stream",
            "kind": kind,
            "truncated": truncated,
            "content_b64": base64.b64encode(data).decode("ascii"),
        }

    def download_to(self, remote_path: str, local_path: str) -> dict:
        """把远端文件流式写到本地路径（桌面端「另存为」后使用）。"""
        if not local_path:
            raise SshError("缺少本地保存路径")
        with self.sftp_lock:
            self.last_used = time.monotonic()
            sftp = self._sftp_locked()
            try:
                attr = sftp.stat(remote_path)
                if stat.S_ISDIR(attr.st_mode or 0):
                    raise SshError("不能下载目录")
                size = int(attr.st_size or 0)
                if size > SFTP_DOWNLOAD_MAX:
                    raise SshError(f"文件超过下载上限（{SFTP_DOWNLOAD_MAX // 1024 // 1024}MB）")
                written = 0
                with sftp.open(remote_path, "rb") as source:
                    with open(local_path, "wb") as target:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > SFTP_DOWNLOAD_MAX:
                                raise SshError(
                                    f"文件超过下载上限（{SFTP_DOWNLOAD_MAX // 1024 // 1024}MB）"
                                )
                            target.write(chunk)
            except SshError:
                raise
            except (IOError, OSError) as exc:
                raise SshError(str(exc) or "下载失败") from exc
        return {"ok": True, "path": local_path, "size": written}

    def write_file(self, directory: str, name: str, content_b64: str) -> dict:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise SshError("文件名不合法")
        try:
            data = base64.b64decode(content_b64 or "", validate=True)
        except Exception as exc:  # noqa: BLE001 - binascii.Error
            raise SshError("上传内容不是合法的 base64") from exc
        if len(data) > SFTP_UPLOAD_MAX:
            raise SshError(f"文件超过上传上限（{SFTP_UPLOAD_MAX // 1024 // 1024}MB）")
        with self.sftp_lock:
            self.last_used = time.monotonic()
            sftp = self._sftp_locked()
            try:
                base = sftp.normalize(directory or ".")
                target = posixpath.join(base, name)
                with sftp.open(target, "wb") as handle:
                    handle.write(data)
            except (IOError, OSError) as exc:
                raise SshError(str(exc) or "无法写入文件") from exc
        return {"ok": True, "path": target, "size": len(data)}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.sftp is not None:
                self.sftp.close()
        except Exception:  # noqa: BLE001
            pass
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


def open_session(credential_id: int, data: dict, interactive: bool = False,
                 shell: bool = True) -> dict:
    """建立连接与会话；返回前端需要的摘要信息。

    ``interactive=True`` 时优先申请 PTY（加载登录 shell 的 rc、满足需要 TTY 的程序），
    否则优先使用不申请 PTY 的静默 ``sh -s`` 会话。
    ``shell=False`` 用于 SFTP 文件管理：只认证，不启动 shell / 不申请 PTY。
    """
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

    from . import anonymous
    proxy_url = _proxy_url(data)
    if anonymous.enabled() and not proxy_url:
        return {"ok": False, "error": "匿名模式已开启：该凭据未配置可用代理，已拒绝直连请求"}

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
    session_id = uuid.uuid4().hex
    session = _Session(session_id, client, None, data, proxied=bool(proxy_url))

    # 静默模式优先非交互 `sh -s`（无 PTY、无提示符/回显）；交互模式优先 PTY，
    # 以加载登录 shell 的 rc 并满足需要 TTY 的程序。另一条路径作为退路。
    if shell:
        if interactive:
            ready = session._try_pty() or session._try_pipe()
        else:
            ready = session._try_pipe() or session._try_pty()
        if not ready:
            session.mode = "exec"
            session.shell_kind = ""
    else:
        # SFTP 文件会话：只认证，不启动 shell。
        session.mode = "files"
        session.shell_kind = ""

    banner = session.banner

    with _sessions_lock:
        stale = _prune_locked()
        while len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda s: s.last_used)
            _sessions.pop(oldest.id, None)
            stale.append(oldest)
        _sessions[session_id] = session
    _close_many(stale)
    _log.info("SSH 会话已建立：%s:%s（模式=%s%s）", host, port, session.mode,
              f"/{session.shell_kind}" if session.shell_kind else "")
    return {
        "ok": True,
        "session_id": session_id,
        "summary": session.summary,
        "mode": session.mode,
        "shell_kind": session.shell_kind,
        "interactive": session.shell_kind == "pty",
        "history_off": session.shell_kind in ("pipe", "pty"),
        "host_key": fingerprint,
        "banner": banner,
        "credential_id": credential_id,
    }


def run_command(session_id: str, command: str) -> dict:
    with _sessions_lock:
        session = _sessions.get(str(session_id))
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    denied = _anon_denied(session)
    if denied:
        return {"ok": False, "error": denied}
    try:
        result = session.run(command)
    except SshError as exc:
        _drop(session_id)
        return {"ok": False, "error": str(exc), "closed": True}
    if session.closed:
        _drop(session_id)
    return result


def _anon_denied(session: _Session) -> str:
    """匿名模式下拒绝使用「建立时是直连」的旧会话继续操作。"""
    if session.proxied:
        return ""
    from . import anonymous
    if anonymous.enabled():
        return "匿名模式已开启：该会话建立时未走代理，已拒绝继续操作"
    return ""


def _get(session_id: str) -> Optional[_Session]:
    with _sessions_lock:
        return _sessions.get(str(session_id))


def list_files(session_id: str, path: str = "") -> dict:
    session = _get(session_id)
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    denied = _anon_denied(session)
    if denied:
        return {"ok": False, "error": denied}
    try:
        return session.list_dir(path)
    except SshError as exc:
        if session.closed:
            _drop(session_id)
        return {"ok": False, "error": str(exc)}


def read_file(session_id: str, path: str, max_bytes: Optional[int] = None) -> dict:
    session = _get(session_id)
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    denied = _anon_denied(session)
    if denied:
        return {"ok": False, "error": denied}
    try:
        return session.read_file(path, max_bytes)
    except SshError as exc:
        if session.closed:
            _drop(session_id)
        return {"ok": False, "error": str(exc)}


def write_file(session_id: str, directory: str, name: str, content_b64: str) -> dict:
    session = _get(session_id)
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    denied = _anon_denied(session)
    if denied:
        return {"ok": False, "error": denied}
    try:
        return session.write_file(directory, name, content_b64)
    except SshError as exc:
        if session.closed:
            _drop(session_id)
        return {"ok": False, "error": str(exc)}


def download_to(session_id: str, remote_path: str, local_path: str) -> dict:
    session = _get(session_id)
    if session is None:
        return {"ok": False, "error": "会话不存在或已断开，请重新连接"}
    denied = _anon_denied(session)
    if denied:
        return {"ok": False, "error": denied}
    try:
        return session.download_to(remote_path, local_path)
    except SshError as exc:
        if session.closed:
            _drop(session_id)
        return {"ok": False, "error": str(exc)}


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
