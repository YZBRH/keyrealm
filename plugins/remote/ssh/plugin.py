"""SSH 凭据：连接验证 + 交互式控制台入口。

认证支持密码与私钥（可选私钥口令）。私钥验证默认使用 PK_OK 查询（不完成登录，
通常不产生 Accepted 记录），服务器不支持时可回退完整认证；验证只做 TCP/SSH 握手与认证，
不执行远端命令。控制台由 ``backend/ssh_session.py`` 提供（插件只负责声明操作，避免插件越过 SDK 边界）。
"""
from __future__ import annotations

import io
import os
import socket
import threading
import time

from backend.plugin_sdk import VerifyResult, guarded, log, mask_secret, proxy as sdk_proxy

NAME = "SSH"
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 15.0


def _text(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _port(data: dict) -> int:
    raw = _text(data, "port") or str(DEFAULT_PORT)
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 1 <= value <= 65535 else 0


def _timeout(data: dict) -> float:
    raw = _text(data, "timeout") or str(int(DEFAULT_TIMEOUT))
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if 1.0 <= value <= 300.0 else DEFAULT_TIMEOUT


def _load_key(data: dict):
    """解析私钥；失败时抛 ValueError（消息可直接给用户看）。"""
    import paramiko  # noqa: PLC0415 - 仅私钥认证路径需要

    key_text = _text(data, "private_key")
    if not key_text:
        raise ValueError("使用私钥认证时必须填写私钥")
    passphrase = _text(data, "passphrase") or None
    last_error = None
    # paramiko 5 基类 PKey.from_private_key 不再按内容分派，逐个具体类型尝试。
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_text), password=passphrase)
        except paramiko.PasswordRequiredException:
            if not passphrase:
                raise ValueError("私钥已加密，请填写私钥口令") from None
            last_error = "私钥口令错误"
        except paramiko.SSHException as exc:
            last_error = str(exc) or "格式不受支持"
    raise ValueError(f"私钥解析失败：{last_error or '格式不受支持'}")


def _fingerprint(client) -> str:
    transport = client.get_transport()
    key = transport.get_remote_server_key() if transport else None
    return _fingerprint_key(key)


def _fingerprint_key(key) -> str:
    if key is None:
        return ""
    import base64
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _silent_key_check(data: dict) -> bool:
    """未显式关闭时默认启用（兼容旧凭据没有该字段的情况）。"""
    value = (data or {}).get("silent_key_check")
    return True if value is None else _truthy(value)


def _proxy_socket(host: str, port: int, timeout: float):
    """按框架解析出的代理（含代理链）建立到目标的 socket；直连返回 None。"""
    url = sdk_proxy()
    if not url:
        return None
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        raise ValueError("SSH 经 HTTPS 代理连接暂不支持")
    try:
        import socks  # PySocks
    except ImportError:
        raise ValueError("经代理连接 SSH 需要 PySocks") from None
    types = {
        "socks5": socks.SOCKS5, "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4, "http": socks.HTTP,
    }
    if scheme not in types:
        raise ValueError(f"不支持的代理协议：{scheme or '?'}")
    return socks.create_connection(
        (host, port),
        timeout=timeout,
        proxy_type=types[scheme],
        proxy_addr=parts.hostname or "",
        proxy_port=parts.port or (1080 if scheme.startswith("socks") else 8080),
        proxy_username=unquote(parts.username) if parts.username else None,
        proxy_password=unquote(parts.password) if parts.password else None,
    )


def _connect(data: dict):
    """建立并认证 SSH 连接，返回 (client, 主机密钥指纹)。"""
    import paramiko  # noqa: PLC0415 - 保持应用启动不被 paramiko 拖慢

    host = _text(data, "host")
    port = _port(data)
    username = _text(data, "username")
    timeout = _timeout(data)
    auth_type = (_text(data, "auth_type") or "password").lower()

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
    if auth_type == "key":
        kwargs["pkey"] = _load_key(data)
    else:
        kwargs["password"] = _text(data, "password")
    sock = _proxy_socket(host, port, timeout)
    if sock is not None:
        kwargs["sock"] = sock
    try:
        client.connect(**kwargs)
    except Exception:
        client.close()
        if sock is not None:
            sock.close()
        raise
    return client, _fingerprint(client)


def _known_hosts_paths() -> list[str]:
    return [
        os.path.expanduser("~/.ssh/known_hosts"),
        "/etc/ssh/ssh_known_hosts",
        "/etc/ssh/known_hosts",
    ]


def _check_host_key(transport, host: str, port: int) -> None:
    """查询模式下复刻 SSHClient 的 known_hosts 校验（仅 verify_host_key 开启时）。"""
    import paramiko

    keys = paramiko.HostKeys()
    for path in _known_hosts_paths():
        try:
            keys.load(path)
        except (IOError, OSError):
            continue
    name = host if port == 22 else f"[{host}]:{port}"
    server_key = transport.get_remote_server_key()
    record = keys.lookup(name) or {}
    if any(item.asbytes() == server_key.asbytes() for item in record.values()):
        return
    if not record:
        raise paramiko.SSHException(f"主机 {name} 不在 known_hosts 中，已拒绝连接")
    raise paramiko.BadHostKeyException(host, server_key, next(iter(record.values())))


def _ensure_auth_service(transport, timeout: float) -> None:
    """请求 ssh-userauth 服务并等待接受（带超时，避免服务端直接断开时卡死）。"""
    import paramiko
    from paramiko.common import cMSG_SERVICE_REQUEST
    from paramiko.message import Message

    if transport._service_userauth_accepted:
        transport.auth_handler = transport.get_auth_handler()
        return
    message = Message()
    message.add_byte(cMSG_SERVICE_REQUEST)
    message.add_string("ssh-userauth")
    with transport.lock:
        transport._send_message(message)
    deadline = time.monotonic() + timeout
    while not transport._service_userauth_accepted:
        if not transport.is_active():
            raise paramiko.SSHException("SSH 认证服务不可用")
        if time.monotonic() >= deadline:
            raise paramiko.SSHException("等待 SSH 认证服务超时")
        time.sleep(0.05)
    transport.auth_handler = transport.get_auth_handler()


def _pk_ok_query(transport, username: str, key, timeout: float):
    """发送不带签名的 publickey 请求，返回 True / False / None（无法判定）。"""
    from paramiko.auth_handler import AuthOnlyHandler
    from paramiko.common import MSG_USERAUTH_PK_OK, cMSG_USERAUTH_REQUEST
    from paramiko.message import Message
    from paramiko.ssh_exception import AuthenticationException

    class _QueryHandler(AuthOnlyHandler):
        def __init__(self, owner):
            super().__init__(owner)
            self.answered = False
            self.pk_ok = False

        @property
        def _client_handler_table(self):
            table = super()._client_handler_table.copy()
            table[MSG_USERAUTH_PK_OK] = self._parse_pk_ok
            return table

        def _parse_pk_ok(self, message):
            self.pk_ok = True
            self.answered = True
            if self.auth_event is not None:
                self.auth_event.set()

        def _parse_userauth_failure(self, message):
            self.answered = True
            super()._parse_userauth_failure(message)

        def _parse_userauth_success(self, message):
            self.pk_ok = True
            self.answered = True
            super()._parse_userauth_success(message)

    _ensure_auth_service(transport, timeout)
    handler = _QueryHandler(transport)
    transport.auth_handler = handler
    key_type, bits = handler._get_key_type_and_bits(key)
    algorithm = handler._finalize_pubkey_algorithm(key_type)

    request = Message()
    request.add_byte(cMSG_USERAUTH_REQUEST)
    request.add_string(username)
    request.add_string("ssh-connection")
    request.add_string("publickey")
    request.add_boolean(False)  # 不带签名：仅询问该公钥是否被接受
    request.add_string(algorithm)
    request.add_string(bits)

    handler.auth_method = "publickey"
    handler.username = username
    # 先建事件再发送，避免服务器应答过快时事件尚未创建。
    handler.auth_event = threading.Event()
    with transport.lock:
        transport._send_message(request)

    try:
        handler.wait_for_response(handler.auth_event)
    except AuthenticationException:
        # PK_OK 不是完整认证，wait_for_response 会抛异常；结果看 handler 标志。
        pass
    except Exception:  # noqa: BLE001 - 协议不兼容时交由调用方回退
        return None

    if handler.pk_ok:
        return True
    if handler.answered:
        return False
    return None


def _verify_publickey_query(data: dict):
    """静默校验公钥：只做 PK_OK 查询，不完成登录。

    返回 ``VerifyResult``；``None`` 表示查询不可判定，调用方回退完整认证。
    """
    try:
        import paramiko
        from paramiko.transport import ServiceRequestingTransport
    except ImportError:
        return None

    host = _text(data, "host")
    port = _port(data)
    username = _text(data, "username")
    timeout = _timeout(data)
    try:
        key = _load_key(data)
    except ValueError as exc:
        return VerifyResult(status="invalid", message=str(exc))

    sock = None
    transport = None
    try:
        sock = _proxy_socket(host, port, timeout)
        if sock is None:
            sock = socket.create_connection((host, port), timeout=timeout)
        transport = ServiceRequestingTransport(sock)
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        transport.start_client(timeout=timeout)
        if _truthy(data.get("verify_host_key")):
            _check_host_key(transport, host, port)
        server = transport.remote_version or ""
        outcome = _pk_ok_query(transport, username, key, timeout)
    except paramiko.BadHostKeyException:
        return VerifyResult(status="invalid", message="主机密钥与 known_hosts 不一致，已拒绝连接")
    except (paramiko.SSHException, socket.timeout, OSError):
        return None
    except Exception:  # noqa: BLE001 - 查询只是可选优化
        return None
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    if outcome is None:
        return None
    if not outcome:
        return VerifyResult(status="invalid", message="公钥未被服务器接受")
    return VerifyResult(
        status="valid",
        message=f"公钥已被服务器接受（静默校验，未完成登录；{server or 'SSH'}）",
        account_info={"user": username, "server": server},
    )


def validate(data):
    data = data or {}
    errors = []
    if not _text(data, "host"):
        errors.append("主机不能为空")
    if not _port(data):
        errors.append("端口必须是 1–65535 的整数")
    if not _text(data, "username"):
        errors.append("用户名不能为空")
    auth_type = (_text(data, "auth_type") or "password").lower()
    if auth_type not in ("password", "key"):
        errors.append("认证方式只能是密码或私钥")
    elif auth_type == "password":
        if not _text(data, "password"):
            errors.append("使用密码认证时必须填写密码")
    else:
        try:
            _load_key(data)
        except ValueError as exc:
            errors.append(str(exc))
        except Exception:  # noqa: BLE001 - paramiko 不可用时不要拖垮整个表单
            errors.append("SSH 组件不可用，无法校验私钥")
    raw_timeout = _text(data, "timeout")
    if raw_timeout:
        try:
            value = float(raw_timeout)
        except ValueError:
            errors.append("超时必须是数字")
        else:
            if not (1 <= value <= 300):
                errors.append("超时需在 1–300 秒之间")
    return errors


@guarded
def verify(data):
    data = data or {}
    errors = validate(data)
    if errors:
        return VerifyResult(status="invalid", message="；".join(errors))

    host = _text(data, "host")
    port = _port(data)
    username = _text(data, "username")
    auth_type = (_text(data, "auth_type") or "password").lower()

    # 私钥默认走 PK_OK 静默校验：只确认公钥是否被接受，不产生登录会话。
    # 服务器不支持查询 / 结果不可判定时回退到完整认证。
    if auth_type == "key" and _silent_key_check(data):
        silent = _verify_publickey_query(data)
        if silent is not None:
            return silent

    try:
        import paramiko  # noqa: PLC0415

        client, _ = _connect(data)
    except ImportError:
        return VerifyResult(status="error", message="SSH 组件未安装（缺少 paramiko）")
    except paramiko.AuthenticationException:
        log(f"SSH 认证失败 user={mask_secret(username)} host={host}:{port}")
        return VerifyResult(status="invalid", message="认证失败：用户名/密码或私钥不正确")
    except paramiko.BadHostKeyException:
        return VerifyResult(status="invalid", message="主机密钥与 known_hosts 不一致，已拒绝连接")
    except ValueError as exc:
        return VerifyResult(status="error", message=str(exc))
    except socket.timeout:
        return VerifyResult(status="error", message="连接超时")
    except (paramiko.SSHException, OSError) as exc:
        message = str(exc) or type(exc).__name__
        return VerifyResult(status="error", message=f"连接失败：{message}")
    else:
        transport = client.get_transport()
        server = transport.remote_version if transport else ""
        client.close()
        return VerifyResult(
            status="valid",
            message=f"连接成功（{server or 'SSH'}）",
            account_info={"user": username, "server": server},
        )


def get_display_summary(data):
    data = data or {}
    host = _text(data, "host")
    if not host:
        return ""
    return f"{_text(data, 'username')}@{host}:{_port(data) or DEFAULT_PORT}"


def actions(data):
    data = data or {}
    if not _text(data, "host") or not _text(data, "username"):
        return []
    return [{
        "id": "console",
        "name": "SSH 控制台",
        "kind": "console",
        "description": "打开交互式命令控制台（关闭窗口即断开）",
    }, {
        "id": "files",
        "name": "文件管理",
        "kind": "files",
        "description": "通过 SFTP 浏览、预览、下载与上传远程文件",
    }]
