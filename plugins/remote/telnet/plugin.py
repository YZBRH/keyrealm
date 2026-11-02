"""Telnet 凭据：TCP 连通性 + 尽力而为的登录验证。

Telnet 没有统一认证协议，提示符与成功标志因设备而异。本插件只在填写了用户名 / 密码时
尝试一次文本登录：匹配常见的 login / password 提示，检测明确的失败关键字；结果仅为
「尽力而为」，不保证覆盖所有网络设备。
"""
from __future__ import annotations

import re
import socket

from backend.plugin_sdk import VerifyResult, guarded, log, mask_secret
from remote_common import open_socket, port, printable, recv_idle, recv_until, text, timeout

NAME = "Telnet"
DEFAULT_PORT = 23
DEFAULT_TIMEOUT = 10.0

_LOGIN_RE = re.compile(r"(?i)(?:login|username|user\s*name|账号|用户名)\s*[:：]\s*")
_PASSWORD_RE = re.compile(r"(?i)(?:password|passwd|口令|密码)\s*[:：]\s*")
_FAIL_RE = re.compile(
    r"(?i)(incorrect|invalid|failed|denied|bad password|authentication failed|"
    r"登录失败|密码错误|认证失败|拒绝)"
)
_PROMPT_RE = re.compile(r"(?m)[$#>%]\s*$")
_WELCOME_RE = re.compile(r"(?i)(welcome|last login|success|登录成功)")


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

    host = text(data, "host")
    port_number = port(data, DEFAULT_PORT)
    username = text(data, "username")
    password = str(data.get("password") or "")
    timeout_seconds = timeout(data, DEFAULT_TIMEOUT)
    sock = None
    try:
        sock = open_socket(host, port_number, timeout_seconds)
        sock.settimeout(timeout_seconds)
        initial, login_prompted = recv_until(
            sock, _LOGIN_RE, min(3.0, timeout_seconds),
        )
        banner = printable(initial)

        if not login_prompted:
            # 部分设备先要求按回车（Press any key）才显示登录提示。
            try:
                sock.sendall(b"\r\n")
            except OSError:
                pass
            extra, login_prompted = recv_until(sock, _LOGIN_RE, timeout_seconds)
            initial += extra
            banner = printable(initial)
        if not login_prompted:
            return VerifyResult(
                status="error",
                message="已连接，但未检测到登录提示，无法验证凭据",
                account_info={"user": username, "server": banner or "Telnet"},
            )

        sock.sendall((username + "\r\n").encode("utf-8", "replace"))
        pwd_raw, pwd_prompted = recv_until(sock, _PASSWORD_RE, timeout_seconds)
        if not pwd_prompted:
            shown = printable(pwd_raw)
            if _FAIL_RE.search(shown):
                return VerifyResult(status="invalid", message="Telnet 登录失败")
            return VerifyResult(
                status="error",
                message="已提交用户名，但未检测到密码提示，无法判断登录结果",
                account_info={"user": username, "server": banner or "Telnet"},
            )

        sock.sendall((password + "\r\n").encode("utf-8", "replace"))
        after = recv_idle(sock, min(1.0, timeout_seconds))
        shown = printable(after)
        if _FAIL_RE.search(shown):
            log(f"Telnet 登录失败 user={mask_secret(username)} host={host}:{port_number}")
            return VerifyResult(status="invalid", message="Telnet 登录失败（服务端拒绝凭据）")
        if _PROMPT_RE.search(shown) or _WELCOME_RE.search(shown):
            return VerifyResult(
                status="valid",
                message="Telnet 登录成功",
                account_info={"user": username, "server": banner or "Telnet"},
            )
        return VerifyResult(
            status="error",
            message="已提交凭据，但服务端未返回可判定的登录结果",
            account_info={"user": username, "server": banner or "Telnet"},
        )
    except socket.timeout:
        return VerifyResult(status="error", message="连接超时")
    except (OSError, ValueError) as exc:
        return VerifyResult(status="error", message=f"连接失败：{exc or type(exc).__name__}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def get_display_summary(data):
    data = data or {}
    host = text(data, "host")
    if not host:
        return ""
    user = text(data, "username")
    prefix = f"{user}@" if user else ""
    return f"{prefix}{host}:{port(data, DEFAULT_PORT) or DEFAULT_PORT}"
