"""SSH 凭据：连接验证 + 交互式控制台入口。

认证支持密码与私钥（可选私钥口令）。验证只做 TCP/SSH 握手与认证，不执行远端命令；
控制台由 ``backend/ssh_session.py`` 提供（插件只负责声明操作，避免插件越过 SDK 边界）。
"""
from __future__ import annotations

import io
import socket

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
    if key is None:
        return ""
    import base64
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


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
    try:
        import paramiko  # noqa: PLC0415

        client, fingerprint = _connect(data)
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
            account_info={
                "host": host,
                "port": port,
                "user": username,
                "server": server,
                "host_key": fingerprint,
            },
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
    }]
