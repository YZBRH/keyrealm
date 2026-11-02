"""RDP 凭据：使用 aardwolf 完成 CredSSP/NLA 登录校验。

- 优先 ``auth_only=True``：只做 CredSSP/NLA 认证，不建立桌面会话。
- 服务器不支持 NLA 时回退一次完整 RDP 登录（随后立即断开），以校验证书 / 标准 RDP 安全下的凭据。
- 密码错误、账户被拒等认证失败返回 ``invalid``；网络 / TLS / 协议错误返回 ``error``。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import unquote, urlsplit

from backend.plugin_sdk import VerifyResult, guarded, log, mask_secret, proxy as sdk_proxy
from backend.plugin_sdk import ssl_context as sdk_ssl_context
from remote_common import port, text, timeout, truthy

NAME = "RDP"
DEFAULT_PORT = 3389
DEFAULT_TIMEOUT = 15.0

_AUTH_FAILURE_HINTS = (
    "logon failure",
    "logon_failure",
    "status_logon",
    "authentication failed",
    "auth failed",
    "invalid credentials",
    "invalid username",
    "invalid password",
    "access denied",
    "account restriction",
    "password expired",
    "user name or password",
    "username or password",
    "status_account",
    "status_password",
    "status_acct",
    "status_no_logon",
    "status_smartcard",
    "status_access",
    "status_wrong",
)


def _looks_auth_failure(error) -> bool:
    message = f"{type(error).__name__}: {error}".lower()
    return any(hint in message for hint in _AUTH_FAILURE_HINTS)


def _aardwolf_proxies(timeout_seconds: float):
    """把框架解析出的代理 URL 转成 asysocks 的代理链（空列表 = 直连）。"""
    url = sdk_proxy()
    if not url:
        return []
    from asysocks.common.credentials import SocksCredential
    from asysocks.unicomm.common.proxy import UniProxyProto, UniProxyTarget

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    protocols = {
        "socks5": UniProxyProto.CLIENT_SOCKS5_TCP,
        "socks5h": UniProxyProto.CLIENT_SOCKS5_TCP,
        "socks4": UniProxyProto.CLIENT_SOCKS4,
        "socks4a": UniProxyProto.CLIENT_SOCKS4,
        "http": UniProxyProto.CLIENT_HTTP,
        "https": UniProxyProto.CLIENT_SSL_HTTP,
    }
    protocol = protocols.get(scheme)
    if protocol is None:
        raise ValueError(f"不支持的代理协议：{scheme or '?'}")
    default_ports = {"socks5": 1080, "socks5h": 1080, "socks4": 1080, "socks4a": 1080,
                     "http": 8080, "https": 8443}
    proxy = UniProxyTarget()
    proxy.server_ip = parts.hostname or ""
    proxy.server_port = parts.port or default_ports.get(scheme, 1080)
    proxy.protocol = protocol
    proxy.timeout = max(1, int(timeout_seconds))
    if scheme == "https":
        proxy.ssl_ctx = sdk_ssl_context(verify=False)
    if parts.username is not None or parts.password is not None:
        credential = SocksCredential()
        credential.username = unquote(parts.username or "")
        credential.password = unquote(parts.password or "")
        proxy.credential = credential
    return [proxy]


def _rdp_target(host: str, port_number: int, domain: str, timeout_seconds: float,
                proxies):
    from aardwolf.commons.target import RDPTarget

    try:
        ipaddress.ip_address(host)
        ip = host
        hostname = host
    except ValueError:
        ip = None
        hostname = host
    return RDPTarget(
        ip=ip,
        port=port_number,
        hostname=hostname,
        timeout=max(1, int(timeout_seconds)),
        domain=domain or None,
        unsafe_ssl=True,
        proxies=proxies,
    )


def _iosettings():
    from aardwolf.commons.iosettings import RDPIOSettings

    settings = RDPIOSettings()
    settings.clipboard_use_pyperclip = False
    settings.channels = []
    settings.vchannels = {}
    settings.drives = []
    settings.video_width = 640
    settings.video_height = 480
    return settings


async def _attempt(target, credential, iosettings, timeout_seconds: float,
                   auth_only: bool):
    from aardwolf.connection import RDPConnection

    connection = RDPConnection(target, credential, iosettings)
    try:
        try:
            ok, error = await asyncio.wait_for(
                connection.connect(auth_only=auth_only),
                timeout=timeout_seconds + 10,
            )
        except asyncio.TimeoutError:
            return False, RuntimeError("RDP 登录超时")
        return bool(ok), error
    finally:
        try:
            await asyncio.wait_for(connection.terminate(), timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _verify_rdp(data: dict) -> VerifyResult:
    try:
        from aardwolf.connection import RDPConnection  # noqa: F401
        from asyauth.common.credentials import UniCredential
        from asyauth.common.constants import asyauthProtocol, asyauthSecret
    except ImportError as exc:
        return VerifyResult(
            status="error",
            message=f"RDP 完整校验组件未安装或加载失败：{exc}",
        )

    host = text(data, "host")
    port_number = port(data, DEFAULT_PORT)
    username = text(data, "username")
    password = str(data.get("password") or "")
    domain = text(data, "domain")
    if "\\" in username and not domain:
        domain, username = username.split("\\", 1)
    timeout_seconds = timeout(data, DEFAULT_TIMEOUT)

    try:
        proxies = _aardwolf_proxies(timeout_seconds)
        target = _rdp_target(host, port_number, domain, timeout_seconds, proxies)
        credential = UniCredential(
            secret=password,
            username=username,
            domain=domain or None,
            stype=asyauthSecret.PASSWORD,
            protocol=asyauthProtocol.NTLM,
        )
        iosettings = _iosettings()
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(status="error", message=f"RDP 配置错误：{exc}")

    async def run():
        ok, error = await _attempt(target, credential, iosettings,
                                   timeout_seconds, auth_only=True)
        if ok:
            return VerifyResult(
                status="valid",
                message="RDP 登录成功（CredSSP/NLA）",
                account_info={"user": f"{domain}\\{username}" if domain else username,
                              "server": "RDP / CredSSP"},
            )
        if error is not None and _looks_auth_failure(error):
            return VerifyResult(
                status="invalid",
                message="RDP 凭据无效：用户名、密码或域不正确",
            )

        # 默认**不**回退完整日志登录：NLA 失败通常意味着服务器未启用 CredSSP，
        # 完整 RDP 登录会创建桌面会话并留下更完整的登录记录。
        if not truthy(data.get("allow_full_login")):
            detail = f"{type(error).__name__}: {error}" if error is not None else "未知错误"
            return VerifyResult(
                status="error",
                message=("服务器未完成 NLA/CredSSP 认证；为避免产生完整登录记录，"
                         "未回退完整 RDP 登录（可在凭据中开启「允许回退完整登录」）。"
                         f"原因：{detail[:200]}"),
            )

        # 用户显式允许时才回退一次完整 RDP 登录。
        ok, error = await _attempt(target, credential, iosettings,
                                   timeout_seconds, auth_only=False)
        if ok:
            return VerifyResult(
                status="valid",
                message="RDP 登录成功（完整 RDP 会话已建立后断开）",
                account_info={"user": f"{domain}\\{username}" if domain else username,
                              "server": "RDP"},
            )
        if error is not None and _looks_auth_failure(error):
            return VerifyResult(
                status="invalid",
                message="RDP 凭据无效：用户名、密码或域不正确",
            )
        detail = f"{type(error).__name__}: {error}" if error is not None else "未知错误"
        return VerifyResult(status="error", message=f"RDP 登录失败：{detail[:300]}")

    try:
        return asyncio.run(run())
    except RuntimeError as exc:
        # 极端情况下在已有事件循环的线程里调用：换一个线程跑，保持插件契约。
        if "running event loop" not in str(exc):
            return VerifyResult(status="error", message=f"RDP 登录失败：{exc}")
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(run()))
            try:
                return future.result(timeout=timeout_seconds * 2 + 20)
            except Exception as inner:  # noqa: BLE001
                return VerifyResult(status="error",
                                    message=f"RDP 登录失败：{type(inner).__name__}")


def validate(data):
    errors = []
    if not text(data, "host"):
        errors.append("主机不能为空")
    if not port(data, DEFAULT_PORT):
        errors.append("端口必须是 1–65535 的整数")
    if not text(data, "username"):
        errors.append("用户名不能为空")
    if not str((data or {}).get("password") or ""):
        errors.append("密码不能为空")
    raw_timeout = text(data, "timeout")
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
    result = _verify_rdp(data)
    if result.status == "invalid":
        log(f"RDP 认证失败 user={mask_secret(text(data, 'username'))} "
            f"host={text(data, 'host')}:{port(data, DEFAULT_PORT)}")
    return result


def get_display_summary(data):
    data = data or {}
    host = text(data, "host")
    if not host:
        return ""
    user = text(data, "username")
    prefix = f"{user}@" if user else ""
    return f"{prefix}{host}:{port(data, DEFAULT_PORT) or DEFAULT_PORT}"
