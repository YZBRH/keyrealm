"""FTP / FTPS 凭据：登录验证。

支持普通 FTP 与显式 FTPS（AUTH TLS）。用户名留空时按匿名登录（anonymous）；
连接可走框架的全局代理 / 代理链。
"""
from __future__ import annotations

import ftplib
import socket
import ssl

from backend.plugin_sdk import VerifyResult, guarded, log, mask_secret
from remote_common import ftp_client, port, text, timeout, truthy

NAME = "FTP"
DEFAULT_PORT = 21
DEFAULT_TIMEOUT = 15.0


def _tls_mode(data: dict) -> str:
    value = text(data, "tls").lower()
    return value if value in ("none", "explicit") else "none"


def validate(data):
    errors = []
    if not text(data, "host"):
        errors.append("主机不能为空")
    if not port(data, DEFAULT_PORT):
        errors.append("端口必须是 1–65535 的整数")
    if text(data, "tls") and _tls_mode(data) != text(data, "tls").lower():
        errors.append("TLS 方式只能是 none 或 explicit")
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

    host = text(data, "host")
    port_number = port(data, DEFAULT_PORT)
    username = text(data, "username") or "anonymous"
    password = str(data.get("password") or "")
    tls = _tls_mode(data) == "explicit"
    timeout_seconds = timeout(data, DEFAULT_TIMEOUT)

    ftp = ftp_client(tls, timeout_seconds)
    welcome = ""
    try:
        welcome = ftp.connect(host, port_number)
        ftp.set_pasv(truthy(data.get("passive", True)))
        ftp.login(username, password)
        server = (welcome or "").splitlines()[0][:200] if welcome else "FTP"
        log(f"FTP 登录成功 user={mask_secret(username)} host={host}:{port_number} tls={tls}")
        return VerifyResult(
            status="valid",
            message=f"{'FTPS' if tls else 'FTP'} 登录成功",
            account_info={"user": username, "server": server},
        )
    except ftplib.error_perm as exc:
        message = str(exc)
        log(f"FTP 登录被拒绝 user={mask_secret(username)} host={host}:{port_number}")
        if message.startswith("530"):
            return VerifyResult(status="invalid", message="FTP 登录失败：用户名或密码错误")
        return VerifyResult(status="error", message=f"FTP 命令被拒绝：{message}")
    except ssl.SSLError as exc:
        return VerifyResult(status="error", message=f"TLS 握手失败：{exc}")
    except socket.timeout:
        return VerifyResult(status="error", message="连接超时")
    except (ftplib.Error, OSError, ValueError) as exc:
        return VerifyResult(status="error", message=f"连接失败：{exc or type(exc).__name__}")
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass


def get_display_summary(data):
    data = data or {}
    host = text(data, "host")
    if not host:
        return ""
    user = text(data, "username") or "anonymous"
    return f"{user}@{host}:{port(data, DEFAULT_PORT) or DEFAULT_PORT}"
