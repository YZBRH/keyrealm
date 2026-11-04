"""通用 SMTP 邮箱验证插件（plugins/mail/ 类别下的独立子插件）。

本插件自带 SMTP 连接与登录的**全部实现**；mail_common 只提供通用工具。
新增邮箱类插件时参照本目录（含 _safe_quit / 建连分类 / 登录判定）即可，
无需改动 mail_common.py。

常见服务商参数（均需使用「授权码 / 应用专用密码」，而非网页登录密码）：
  - Gmail    : smtp.gmail.com   465 SSL
  - QQ 邮箱   : smtp.qq.com      465 SSL
  - 网易 163 : smtp.163.com     465 SSL
  - 网易 126 : smtp.126.com     465 SSL
非 SSL 端口通常为 587（STARTTLS），25 端口为明文（不推荐）。

**TLS 版本**：交给框架的「TLS 版本」策略统一决定（凭据 > 插件预设 > 类别预设 > 全局默认），
本插件用 `plugin_sdk.ssl_context()` 复用同一套（见 `backend/tls_store.py`）。默认 `auto` =
最低 **TLS 1.2**、**不降级**。部分老邮件服务器只开 **TLS 1.1**（已被 RFC 8996 弃用），会被服务端回
`TLSV1_ALERT_PROTOCOL_VERSION`，表现为「SSL/TLS 握手失败」——看着像凭据问题、其实是协议谈不拢，
此时把该凭据的「TLS 版本」固定成 **1.1**（或 1.0）再验证。降级会让授权码走在更弱的信道上，
须确认服务器可信后再改。

验证方式：按 ssl 开关选择加密通道后调用 login()。
  登录成功            → valid
  认证失败(授权码错)  → invalid
  连接/超时/其它异常  → error

可选操作（凭据含 host/username/password 时可见，均只读、绝不回显授权码）：
  - ``inbox``     (list) 经 IMAP 列出最近若干封（时间 / 发件人 / 主题 / 是否带附件）
  - ``read_mail`` (list) 「查看收件箱」的行内下钻，读取单封正文与基本头部
IMAP 主机默认由 ``smtp.*`` 推导为 ``imap.*``，端口默认 993；非 993 端口先试
STARTTLS 再回落 SSL。全程用 EXAMINE / ``BODY.PEEK``，不置「已读」标记、不改动邮箱。
网易 163/126 额外要求登录后先按 RFC 2971 发 ``ID`` 声明客户端身份，否则 EXAMINE 会
直接回 ``Unsafe Login. Please contact kefu@188.com``（见 ``_send_imap_id``）。

参考文档：https://datatracker.ietf.org/doc/html/rfc5321（RFC 5321，SMTP）
"""
from __future__ import annotations

import base64
import imaplib
import re
import smtplib
import socket
import ssl
from typing import Any, Optional

from backend.plugin_sdk import VerifyResult, guarded, ssl_context
from mail_common import TunneledIMAP4, TunneledIMAP4_SSL, TunneledSMTP, TunneledSMTP_SSL, timeout

SERVICE_NAME = "通用 SMTP 邮箱"

# SSL 与非 SSL 的默认端口
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


# ── 取值辅助（兼容前端 checkbox→bool、number→int、text→str）──

def _as_text(value: Any) -> str:
    """统一转字符串并去除首尾空白（避免密码/账号带入多余空格）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    """checkbox 可能为 bool / 数字 / 字符串，逐一兼容。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_port(value: Any, default: int) -> int:
    """端口兼容 int / 数字字符串；非法或越界时回落默认值。"""
    if value is None or value == "":
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _safe_quit(server: Optional[smtplib.SMTP]) -> None:
    """尽力关闭连接：先 quit() 正常登出，失败再 close() 兜底，绝不抛异常。"""
    if server is None:
        return
    try:
        server.quit()
        return
    except Exception:  # noqa: BLE001 - 关闭阶段无需隔离具体异常
        pass
    try:
        server.close()
    except Exception:  # noqa: BLE001
        pass


# ── TLS 上下文：复用框架的「TLS 版本」策略 ──────────────────
# 本插件**不自己造 TLS 开关**：邮箱走标准库 smtplib / imaplib 的裸 TCP，用不了
# make_client()，但可以拿 `plugin_sdk.ssl_context()` 复用框架同一套策略
# （凭据 > 插件预设 > 类别预设 > 全局默认，见 backend/tls_store.py）。
# 要连只开 TLS 1.0/1.1 的老服务器，把该凭据的「TLS 版本」固定成 1.1 / 1.0 即可——
# 框架在固定旧版时会自动放宽 OpenSSL 安全级别，旧套件才协商得出来。
#
# 证书校验：与 smtplib / imaplib 默认**行为一致（不校验）**——改成校验会让大量自签名 /
# 内网邮箱服务器从「能用」变成「验证失败」。故统一 ssl_context(verify=False)。
_LEGACY_TLS_MARKERS = (
    "TLSV1_ALERT_PROTOCOL_VERSION",   # 服务端明确回「协议版本不行」
    "UNSUPPORTED_PROTOCOL",           # 本地 OpenSSL 直接拒发
    "WRONG_VERSION_NUMBER",
    "HANDSHAKE_FAILURE",              # 版本谈成了但套件谈不拢（老服务器常见；跨构建告警不同）
)


def _looks_like_legacy_tls(exc: Exception) -> bool:
    """是不是「对面是只开旧版 TLS/旧套件的老服务器」——据此才给得出可操作的提示。

    **刻意把 `HANDSHAKE_FAILURE` 也算进来**：同一台只开 TLS 1.1 的服务器，在不同 OpenSSL
    构建上抛的告警不一样（Linux 上是 `TLSV1_ALERT_PROTOCOL_VERSION`，Windows 构建上是
    `SSLV3_ALERT_HANDSHAKE_FAILURE`）。只认前者会导致 Windows 上**永远给不出提示**。
    """
    text = str(exc).upper()
    return any(marker in text for marker in _LEGACY_TLS_MARKERS)


def _tls_hint() -> str:
    """旧版 TLS 谈不拢时的统一提示：指路到框架的「TLS 版本」。"""
    return ("该服务器多半只开了已被 RFC 8996 弃用的 TLS 1.0/1.1。把凭据（或所属插件 / "
            "类别 / 全局设置）的「TLS 版本」固定成 1.1（或 1.0）后再验证——降级会让授权码 "
            "走在更弱的信道上，确认服务器可信再改。")


def _connect_smtp_ssl(host: str, port: int):
    """隐式 SSL 建连；TLS 版本按框架策略（``ssl_context()``）。"""
    return TunneledSMTP_SSL(host, port, timeout=timeout(),
                            context=ssl_context(verify=False))


def _auth_login(server: smtplib.SMTP, username: str, password: str):
    """优先手工走 AUTH LOGIN，返回 ``(code, resp)``；服务器未提供 LOGIN 时返回 ``None``。

    QQ 邮箱在返回 535 后会立刻断开连接，``smtplib.login()`` 只能看到
    ``SMTPServerDisconnected``，把「授权码错误 / 账号异常」误报成网络断连。
    手工发 AUTH LOGIN 可以在断连前拿到服务端真正的返回码与原因。
    """
    server.ehlo_or_helo_if_needed()
    mechanisms = str(server.esmtp_features.get("auth") or "").upper().split()
    if "LOGIN" not in mechanisms:
        return None

    code, resp = server.docmd("AUTH", "LOGIN")
    if code != 334:
        # 个别服务器虽然宣告 LOGIN，实际又拒绝该机制：交回 smtplib.login() 走 PLAIN 等。
        return None if code in (500, 502, 504) else (code, resp)

    code, resp = server.docmd(base64.b64encode(username.encode("utf-8")).decode("ascii"))
    if code != 334:
        return code, resp
    return server.docmd(base64.b64encode(password.encode("utf-8")).decode("ascii"))


def _verify_smtp(data: dict) -> VerifyResult:
    """通用 SMTP 邮箱验证：连接 → （可选 STARTTLS）→ login()。"""
    host = _as_text(data.get("host"))
    username = _as_text(data.get("username"))
    password = _as_text(data.get("password"))
    use_ssl = _as_bool(data.get("ssl"), True)
    port = _as_port(data.get("port"), DEFAULT_SSL_PORT if use_ssl else DEFAULT_STARTTLS_PORT)

    if not host:
        return VerifyResult(status="invalid", message="SMTP 服务器地址为空")
    if not username:
        return VerifyResult(status="invalid", message="邮箱账号为空")
    if not password:
        return VerifyResult(status="invalid", message="授权码 / 应用专用密码为空")

    server: Optional[smtplib.SMTP] = None
    try:
        # 建连阶段：区分 SMTP 协议错误、SSL 错误、网络/超时错误
        try:
            if use_ssl:
                server = _connect_smtp_ssl(host, port)
            else:
                server = TunneledSMTP(host, port, timeout=timeout())
                server.ehlo()
                server.starttls(context=ssl_context(verify=False))
                server.ehlo()
        except TimeoutError:
            return VerifyResult(status="error", message=f"连接超时（{int(timeout())} 秒）")
        except smtplib.SMTPConnectError as exc:
            return VerifyResult(status="error", message=f"无法连接 SMTP 服务器: {exc}")
        except smtplib.SMTPException as exc:
            return VerifyResult(status="error", message=f"SMTP 连接失败: {exc}")
        except ssl.SSLError as exc:
            if _looks_like_legacy_tls(exc):
                return VerifyResult(status="error",
                                    message=f"SSL/TLS 握手失败：{_tls_hint()}")
            return VerifyResult(status="error", message=f"SSL/TLS 握手失败: {exc}")
        except socket.gaierror as exc:
            return VerifyResult(status="error", message=f"无法解析服务器地址: {exc}")
        except RuntimeError as exc:
            # 代理配置有问题（协议不支持 / 缺 PySocks）——原样透出中文处理办法
            return VerifyResult(status="error", message=str(exc))
        except OSError as exc:
            return VerifyResult(status="error", message=f"网络连接失败: {exc}")

        # 认证阶段：认证失败明确判 invalid，其余 SMTP 错误判 error
        try:
            auth = _auth_login(server, username, password)
            if auth is None:
                server.login(username, password)
            else:
                code, resp = auth
                detail = _redact(_err_text(resp), password)
                if code == 535:
                    return VerifyResult(
                        status="invalid",
                        message=f"认证失败：{detail or '账号或授权码错误'}",
                    )
                if code != 235:
                    return VerifyResult(
                        status="error",
                        message=f"SMTP 认证异常（{code}）：{detail or '服务器未返回原因'}",
                    )
        except smtplib.SMTPAuthenticationError as exc:
            detail = _redact(_err_text(getattr(exc, "smtp_error", b"")), password)
            return VerifyResult(status="invalid", message=f"认证失败：{detail or '账号或授权码错误'}")
        except smtplib.SMTPServerDisconnected:
            return VerifyResult(
                status="error",
                message="SMTP 认证阶段连接被服务器断开：常见于授权码错误、"
                        "账号未开启 SMTP 服务或登录频率限制",
            )
        except smtplib.SMTPException as exc:
            return VerifyResult(status="error", message=f"SMTP 认证异常: {exc}")

        return VerifyResult(
            status="valid",
            message="凭证有效：SMTP 登录成功",
            # 附加信息只放「邮箱账号」与「SMTP 服务器」——端口是配置细节，不占附加信息的位置
            account_info={"smtp_account": username, "smtp_server": host},
        )
    finally:
        _safe_quit(server)


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    if not _as_text(data.get("host")):
        errors.append("SMTP 服务器地址不能为空")

    port_raw = data.get("port")
    if port_raw is None or port_raw == "":
        errors.append("端口不能为空")
    else:
        try:
            port = int(port_raw)
            if not 1 <= port <= 65535:
                errors.append("端口需在 1-65535 之间")
        except (TypeError, ValueError):
            errors.append("端口必须是数字")

    if not _as_text(data.get("username")):
        errors.append("邮箱账号不能为空")
    if not _as_text(data.get("password")):
        errors.append("授权码 / 应用专用密码不能为空")

    return errors


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 SMTP 凭据是否有效。保证不抛出异常（guarded 兜底）。"""
    return _verify_smtp(data or {})


# ── 可选操作：查看收件箱 / 看正文（IMAP，只读）──────────────

IMAP_DEFAULT_PORT = 993
# IMAP 一次操作要 select + search + 逐封 fetch，往返次数远多于 SMTP 的一次 login，
# 沿用 SMTP 默认的 15 秒容易在邮箱稍大或链路稍慢时误判超时，故单独兜底到 20 秒起。
IMAP_MIN_TIMEOUT = 20.0


def _imap_wait() -> float:
    """IMAP 连接超时：至少 ``IMAP_MIN_TIMEOUT`` 秒（理由见上方常量注释）。"""
    return max(timeout(), IMAP_MIN_TIMEOUT)


def _imap_host(smtp_host: str) -> str:
    """由 SMTP 主机推导 IMAP 主机：smtp.X → imap.X，否则原样返回。"""
    host = (smtp_host or "").strip()
    return "imap." + host[len("smtp."):] if host.startswith("smtp.") else host


def _decode_header(value: Optional[str]) -> str:
    from email.header import decode_header, make_header

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - 头部编码异常时按原文返回
        return str(value)


def _redact(text: Any, password: str) -> str:
    """把错误文本里的授权码替换成 ***（异常信息也绝不回显授权码）。"""
    out = str(text or "")
    if password:
        out = out.replace(password, "***")
    return out


def _err_text(value: Any) -> str:
    """把 IMAP 返回体（bytes / str / list）转成一行可读文本。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(_err_text(item) for item in value if item not in (None, b"", ""))
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def _truncate(text: str, limit: int = 80) -> str:
    """压平换行并截断到 limit 字符（过长加省略号），用于列表展示。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _imap_utf7(text: str) -> str:
    """把文件夹名编码为 IMAP modified UTF-7（RFC 3501）。

    Python 标准库没有现成的编解码器（imaplib 只接受已编码的字节），自行实现：
    可打印 ASCII（0x20-0x7E）原样保留，``&`` 转义为 ``&-``，其余字符按 UTF-16BE
    做 modified BASE64（``+`` 保持、``/`` 换成 ``,``、去掉 ``=`` 填充）并用
    ``&...-`` 包裹。选它而非「遇异常再提示换 ASCII 名」是因为编码是确定性的，
    不需要用户迁就服务器。
    """
    import base64

    out: list[str] = []
    buf = bytearray()

    def flush() -> None:
        if buf:
            b64 = base64.b64encode(bytes(buf)).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf.clear()

    for ch in str(text):
        code = ord(ch)
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= code <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.extend(ch.encode("utf-16-be"))
    flush()
    return "".join(out)


def _safe_imap_close(conn: Optional[imaplib.IMAP4]) -> None:
    """尽力关闭 IMAP 连接（先 logout 再 shutdown），绝不抛异常。"""
    if conn is None:
        return
    try:
        conn.logout()
        return
    except Exception:  # noqa: BLE001 - 关闭阶段无需隔离具体异常
        pass
    try:
        conn.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _connect_imap(host: str, port: int, password: str, wait: float):
    """建立 IMAP 连接，返回 ``(conn, errors)``。

    TLS 版本按框架策略（``ssl_context()``，与 SMTP 同一套「TLS 版本」设置）。

    993 是 IMAP over SSL 的惯用端口，直接走 ``IMAP4_SSL``；其它端口常见为 STARTTLS
    （143 等），先试 ``IMAP4`` + ``starttls()``，不行再回落 ``IMAP4_SSL``（也有服务器
    把 SSL 配在非标准端口）。两条路径都失败时返回 ``(None, 原因列表)``，由调用方把
    两条原因一起报出来——只说「连接失败」看不出是证书、端口还是认证问题。
    """
    errors: list[str] = []

    def note(label: str, exc: Exception) -> None:
        reason = _redact(_err_text(exc) or type(exc).__name__, password)
        if _looks_like_legacy_tls(exc):
            reason += "（该服务器多半只开旧版 TLS，可在「TLS 版本」里固定成 1.1 / 1.0 后重试）"
        errors.append(f"{label}：{reason}")

    if port == IMAP_DEFAULT_PORT:
        try:
            return TunneledIMAP4_SSL(host, port, timeout=wait, ssl_context=ssl_context(verify=False)), errors
        except Exception as exc:  # noqa: BLE001
            note(f"SSL {port}", exc)
            return None, errors

    try:
        conn = TunneledIMAP4(host, port, timeout=wait)
    except Exception as exc:  # noqa: BLE001
        note(f"明文 {port}", exc)
    else:
        try:
            conn.starttls(ssl_context=ssl_context(verify=False))
            return conn, errors
        except Exception as exc:  # noqa: BLE001
            note(f"STARTTLS {port}", exc)
            _safe_imap_close(conn)

    try:
        return imaplib.IMAP4_SSL(host, port, timeout=wait, ssl_context=ssl_context(verify=False)), errors
    except Exception as exc:  # noqa: BLE001
        note(f"SSL {port}", exc)
    return None, errors


def _imap_target(params: dict, data: dict):
    """解析 IMAP 目标：参数优先，其次由 smtp.* → imap.* 推导，端口默认 993。

    返回 ``(host, port, error)``；无法确定主机时 error 为可直接返回的 dict。
    """
    host = _as_text(params.get("imap_host")) or _imap_host(_as_text(data.get("host")))
    if not host:
        return "", 0, {"ok": False, "error": "无法确定 IMAP 主机，请填写 imap_host"}
    port = _as_port(params.get("imap_port"), IMAP_DEFAULT_PORT)
    return host, port, None


def _send_imap_id(conn: imaplib.IMAP4) -> None:
    """向服务器声明客户端身份（RFC 2971）。

    网易 163/126 要求客户端在**登录之后、EXAMINE/SELECT 之前**先发一条 ``ID``，
    否则 ``select`` 会失败并回 ``Unsafe Login. Please contact kefu@188.com``。

    用 ``conn.xatom("ID", ...)`` 而非 ``_simple_command``：CPython 的
    ``imaplib.Commands`` 里没有 ``ID``，直接发会在 AUTH 态抛
    ``command ID illegal in state AUTH``；``xatom`` 会先把 ``ID`` 登记为
    *当前状态*合法，再发出去并消费 ``* ID (...)`` 未标记响应。
    （网上常见的 ``imaplib.Commands["ID"] = ("NONAUTH",)`` 是给登录前用的，
    这里的调用点已是 AUTH 态，那样写反而会报错。）

    **尽力而为、失败放过**：服务器 CAPABILITY 里没有 ``ID``（Gmail、QQ 等不要求）
    就直接跳过；发送异常也一律吞掉——不该让声明身份这一步成为新的失败点。
    真出问题时，紧随其后的 ``select`` 会给出与改动前一致的原始原因，不会掩盖。
    """
    try:
        caps = getattr(conn, "capabilities", ()) or ()
        if "ID" not in caps:  # imaplib 的 capabilities 是大写字符串元组
            return
        conn.xatom("ID", '("name" "Keyrealm" "version" "1.0.0")')
    except Exception:  # noqa: BLE001 - 见上：放过，交由 select 报真正的原因
        pass


def _select_folder(conn: imaplib.IMAP4, folder: str, password: str):
    """选中文件夹（只读）并检查返回值，返回 ``(ok, error)``。

    非 ASCII 名先做 modified UTF-7 编码；``select`` 返回非 ``OK``（名字不对、
    权限不足等）时立刻带出服务器给出的原因，避免延后成莫名的「搜索失败或无权限」。
    """
    # 两个调用方（_run_inbox / _run_read_mail）都是先 login 再进本函数，
    # 所以这里正好是「登录后、EXAMINE 前」，是网易要求的 ID 声明时机；
    # _connect_imap 的两条建连路径最终都汇聚到这里，故只需发这一处。
    _send_imap_id(conn)
    try:
        encoded = _imap_utf7(folder)
    except Exception:  # noqa: BLE001 - 编码异常时退回原名，让服务器报错更直白
        encoded = folder
    try:
        typ, detail = conn.select(encoded, readonly=True)
    except imaplib.IMAP4.error as exc:
        return False, f"无法打开文件夹「{folder}」：{_redact(_err_text(exc), password)}"
    if typ != "OK":
        reason = _redact(_err_text(detail) or "服务器未给出原因", password)
        return False, f"无法打开文件夹「{folder}」：{reason}"
    return True, ""


def _extract_header_bytes(fetched: Any) -> Optional[bytes]:
    """从 imaplib ``fetch`` 的返回里取出邮件原始字节。

    服务器返回形状并不统一：``[(b'1 (BODY[HEADER] {n}', b'..')]``、``[b'..']``、
    ``[(None, b'..')]``，末尾还可能挂一个 ``b')'`` 结束标记。这里优先取元组里的
    内容（第二个元素即字面量），无元组时取第一个非 ``')'`` 的裸 bytes；拿不到返回
    ``None``——单封取不到应由调用方跳过，而不是让整体失败。
    """
    if not fetched:
        return None
    for item in fetched:
        if isinstance(item, (tuple, list)):
            for sub in reversed(item):
                if isinstance(sub, (bytes, bytearray)) and sub.strip():
                    return bytes(sub)
    for item in fetched:
        if isinstance(item, (bytes, bytearray)) and item.strip() and item.strip() != b")":
            return bytes(item)
    return None


def _fetch_blob(fetched: Any) -> bytes:
    """把 fetch 结果里所有 bytes 片段拼起来（用于搜 BODYSTRUCTURE 关键字）。"""
    blob = bytearray()
    for item in fetched or []:
        if isinstance(item, (bytes, bytearray)):
            blob += bytes(item)
        elif isinstance(item, (tuple, list)):
            for sub in item:
                if isinstance(sub, (bytes, bytearray)):
                    blob += bytes(sub)
    return bytes(blob)


def _detect_attachment(fetched: Any, msg) -> str:
    """判断是否带附件，返回「有」或「—」。

    同一次 fetch 里带回了 ``BODYSTRUCTURE`` 时以它为准：结构里出现 ``"ATTACHMENT"``
    处置或 ``"FILENAME"`` 名参数即视为有附件（含内联图片）。部分服务器不返回结构，
    此时回落看顶层 ``Content-Type`` 是否为 ``multipart/mixed``。两者都拿不准就显示
    ``—``，不臆造。
    """
    blob = _fetch_blob(fetched)
    idx = blob.find(b"BODYSTRUCTURE")
    if idx >= 0:
        upper = blob[idx:].upper()
        return "有" if (b'"ATTACHMENT"' in upper or b'"FILENAME"' in upper) else "—"
    ctype = _as_text(msg.get("Content-Type")).lower()
    return "有" if ctype.startswith("multipart/mixed") else "—"


def _strip_html(html: str) -> str:
    """粗暴去标签：先剔除 script/style 段，再去标签、还原实体。"""
    import html as html_mod

    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    return html_mod.unescape(text)


def _normalize_ws(text: str) -> str:
    """压掉多余空行与连续空白，便于在只读列表里显示。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_body_text(msg) -> str:
    """取正文：优先 ``text/plain``，没有再用 ``text/html`` 去标签。"""
    part = None
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - 结构异常时回落到手工遍历
        part = None
    if part is not None:
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001 - 取不到内容就当空正文
            content = ""
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode(part.get_content_charset() or "utf-8", "replace")
        text = str(content)
        if (part.get_content_subtype() or "").lower() == "html":
            text = _strip_html(text)
        return _normalize_ws(text)

    for sub in msg.walk():  # 回落：逐段找第一个 text/*
        if (sub.get_content_maintype() or "") == "text":
            try:
                text = str(sub.get_content())
            except Exception:  # noqa: BLE001
                continue
            if (sub.get_content_subtype() or "").lower() == "html":
                text = _strip_html(text)
            return _normalize_ws(text)
    return ""


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 host/username/password 时才提供收件箱查看）。"""
    data = data or {}
    if not (
        _as_text(data.get("host"))
        and _as_text(data.get("username"))
        and _as_text(data.get("password"))
    ):
        return []
    return [
        {
            "id": "inbox",
            "name": "查看收件箱",
            "kind": "list",
            "group": "邮箱",
            "order": 10,
            "submit": "拉取",
            "columns": [
                {"key": "date", "label": "时间", "width": 160},
                {"key": "from", "label": "发件人"},
                {"key": "subject", "label": "主题"},
                {"key": "attach", "label": "附件", "width": 60, "type": "tag"},
            ],
            "row_actions": [
                {
                    "id": "read_mail",
                    "label": "看正文",
                    # 顺带把本次实际用的连接参数带下去，行内动作才能连到同一个服务器
                    "params": {
                        "uid": "$row.uid",
                        "folder": "$row.folder",
                        "imap_host": "$row.imap_host",
                        "imap_port": "$row.imap_port",
                    },
                },
            ],
            "empty_text": "收件箱是空的",
            "fields": [
                {"key": "count", "label": "拉取封数", "type": "text",
                 "default": "20", "placeholder": "1-50"},
                {"key": "folder", "label": "文件夹", "type": "text", "default": "INBOX"},
                {"key": "imap_host", "label": "IMAP 主机", "type": "text",
                 "default": "", "placeholder": "留空则按 smtp.* → imap.* 推导"},
                {"key": "imap_port", "label": "IMAP 端口", "type": "text", "default": "993"},
            ],
        },
        {
            "id": "read_mail",
            "name": "看正文",
            "kind": "list",
            "group": "邮箱",
            "order": 20,
            "description": "读取单封邮件正文（请从「查看收件箱」行内「看正文」进入）",
            "columns": [
                {"key": "field", "label": "字段", "width": 90},
                {"key": "value", "label": "内容"},
            ],
            "empty_text": "没有可显示的正文",
            "fields": [],
        },
    ]


def _run_inbox(params: dict, data: dict) -> dict:
    """列出最近若干封邮件（EXAMINE + BODY.PEEK，全程只读）。"""
    from email import policy
    from email.parser import BytesParser

    host, port, error = _imap_target(params, data)
    if error:
        return error
    username = _as_text(data.get("username"))
    password = _as_text(data.get("password"))
    try:
        count = max(1, min(int(_as_text(params.get("count")) or "20"), 50))
    except ValueError:
        count = 20
    folder = _as_text(params.get("folder")) or "INBOX"

    conn, errors = _connect_imap(host, port, password, _imap_wait())
    if conn is None:
        return {"ok": False, "error": "连接 IMAP 失败：" + "；".join(errors)}
    try:
        try:
            conn.login(username, password)
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "error": f"登录失败：{_redact(_err_text(exc), password)}"}

        ok, why = _select_folder(conn, folder, password)
        if not ok:
            return {"ok": False, "error": why}

        try:
            typ, found = conn.search(None, "ALL")
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "error": f"搜索邮件失败：{_redact(_err_text(exc), password)}"}
        if typ != "OK":
            return {"ok": False, "error": f"搜索邮件失败：{_redact(_err_text(found), password)}"}

        ids = found[0].split() if found and found[0] else []
        recent = ids[-count:][::-1]

        rows: list[dict] = []
        for seq in recent:
            try:
                typ, fetched = conn.fetch(seq, "(BODY.PEEK[HEADER] BODYSTRUCTURE)")
            except imaplib.IMAP4.error:  # 单封失败跳过，不影响其它邮件
                continue
            if typ != "OK":
                continue
            raw = _extract_header_bytes(fetched)
            if not raw:
                continue
            try:
                msg = BytesParser(policy=policy.default).parsebytes(raw)
            except Exception:  # noqa: BLE001 - 单封解析失败跳过
                continue
            rows.append({
                # uid 用消息序号（search 返回的 seq），read_mail 拿它去 fetch
                "uid": _err_text(seq),
                "folder": folder,
                "imap_host": host,
                "imap_port": str(port),
                "date": _truncate(_decode_header(msg.get("Date")), 40),
                "from": _truncate(_decode_header(msg.get("From"))),
                "subject": _truncate(_decode_header(msg.get("Subject"))) or "(无主题)",
                "attach": _detect_attachment(fetched, msg),
            })
        return {"ok": True, "rows": rows, "total": len(ids), "has_more": len(ids) > len(rows)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {
            "ok": False,
            "error": f"读取邮件失败：{_redact(_err_text(exc) or type(exc).__name__, password)}",
        }
    finally:
        _safe_imap_close(conn)


def _run_read_mail(params: dict, data: dict) -> dict:
    """读取单封邮件正文（BODY.PEEK[]，只读取到本地解析，不改动已读状态）。"""
    from email import policy
    from email.parser import BytesParser

    host, port, error = _imap_target(params, data)
    if error:
        return error
    username = _as_text(data.get("username"))
    password = _as_text(data.get("password"))
    folder = _as_text(params.get("folder")) or "INBOX"
    uid = _as_text(params.get("uid"))
    if not uid:
        return {"ok": False, "error": "缺少邮件序号，请从「查看收件箱」行内「看正文」进入"}

    conn, errors = _connect_imap(host, port, password, _imap_wait())
    if conn is None:
        return {"ok": False, "error": "连接 IMAP 失败：" + "；".join(errors)}
    try:
        try:
            conn.login(username, password)
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "error": f"登录失败：{_redact(_err_text(exc), password)}"}

        ok, why = _select_folder(conn, folder, password)
        if not ok:
            return {"ok": False, "error": why}

        try:
            # PEEK[] 取整封原始邮件（含 BODYSTRUCTURE 供附件判断），且不置 \Seen
            typ, fetched = conn.fetch(uid, "(BODY.PEEK[] BODYSTRUCTURE)")
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "error": f"读取邮件失败：{_redact(_err_text(exc), password)}"}
        if typ != "OK":
            return {"ok": False, "error": f"读取邮件失败：{_redact(_err_text(fetched), password)}"}

        raw = _extract_header_bytes(fetched)
        if not raw:
            return {"ok": False, "error": "邮件内容为空或无法读取"}
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"解析邮件失败：{_redact(_err_text(exc) or type(exc).__name__, password)}",
            }

        body = _extract_body_text(msg)
        if len(body) > 2000:
            body = body[:2000] + "…"
        rows = [
            {"field": "时间", "value": _decode_header(msg.get("Date")) or "—"},
            {"field": "发件人", "value": _decode_header(msg.get("From")) or "—"},
            {"field": "收件人", "value": _decode_header(msg.get("To")) or "—"},
            {"field": "主题", "value": _decode_header(msg.get("Subject")) or "—"},
            {"field": "附件", "value": _detect_attachment(fetched, msg)},
            {"field": "正文", "value": body or "（无正文）"},
        ]
        return {"ok": True, "rows": rows, "total": len(rows)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {
            "ok": False,
            "error": f"读取邮件失败：{_redact(_err_text(exc) or type(exc).__name__, password)}",
        }
    finally:
        _safe_imap_close(conn)


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：经 IMAP 只读查看收件箱 / 邮件正文（不改动邮箱状态）。"""
    if action_id == "inbox":
        return _run_inbox(params or {}, data or {})
    if action_id == "read_mail":
        return _run_read_mail(params or {}, data or {})
    return {"ok": False, "error": "未知操作"}


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（绝不回显密码）。"""
    data = data or {}
    host = _as_text(data.get("host"))
    username = _as_text(data.get("username"))
    use_ssl = _as_bool(data.get("ssl"), True)
    port = _as_port(data.get("port"), DEFAULT_SSL_PORT if use_ssl else DEFAULT_STARTTLS_PORT)
    target = f"{host}:{port}" if host else "-"
    # 账号通常已是完整邮箱地址，避免再拼 "@" 造成 "a@qq.com@smtp.qq.com" 的歧义
    return f"{SERVICE_NAME} | {username} → {target}" if username else f"{SERVICE_NAME} | {target}"
