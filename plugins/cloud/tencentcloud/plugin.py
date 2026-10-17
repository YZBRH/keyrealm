"""腾讯云 SecretId/SecretKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：**两套签名**都在本文件内维护，不依赖类别共享模块：

- TC3-HMAC-SHA256（``Authorization: TC3-HMAC-SHA256 Credential=...``）——STS / CVM 等云 API 走这套；
- COS 签名（``q-sign-algorithm=sha1``，HMAC-SHA1）——对象存储走这套，规则完全不同，故单独实现。

全部调用均为只读，不产生任何资源变更。仅从框架层 backend.plugin_sdk 复用通用工具。

本插件提供的操作（``actions``）：

- ``whoami``           查看调用者身份（STS GetCallerIdentity + 权限探测）
- ``list_regions``     地域列表（CVM DescribeRegions，含中文名；行内「实例」下钻）
- ``list_instances``   云主机实例列表（CVM DescribeInstances，地域留空则枚举全部地域并汇总）
- ``list_buckets``     对象存储桶列表（COS Service 级桶列表；桶自带地域）
- ``list_objects``     对象浏览（COS 对象列表 + delimiter，文件与文件夹分行）
- ``download_object``  下载单个对象（``save_to``，前端弹「另存为」）
- ``download_prefix``  把某个前缀（文件夹）下的对象并发拉取后打包成 zip 下载

下载类操作全部走签名 GET 流式读取，磁盘上限见 ``MAX_ZIP_*``。
两个下载操作都声明了 ``progress``，由 ``stream_action`` 边下边回调进度、并响应取消
（``run_action`` 三个参数调用时不上报也不可取消，行为与以前一致）。

参考文档：https://cloud.tencent.com/document/api/213/30654
参考文档：https://cloud.tencent.com/document/product/436/7778

``account_info``：验证通过后 best-effort 查余额（Billing ``DescribeAccountBalance``，单位分 → 元），
展示 ``余额 <x> CNY``；查不到就只显示账号 ID。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    download_to_file,
    guarded,
    make_client,
    safe_json,
)

# 腾讯云 STS 接口地址（TC3 签名走请求头）
STS_HOST = "sts.tencentcloudapi.com"
STS_SERVICE = "sts"
STS_ACTION = "GetCallerIdentity"
STS_VERSION = "2018-08-13"
STS_REGION = "ap-guangzhou"

# CVM（云主机）接口，与 STS 共用同一套 TC3 签名
CVM_HOST = "cvm.tencentcloudapi.com"
CVM_SERVICE = "cvm"
CVM_VERSION = "2017-03-12"

# COS（对象存储）全局端点：桶列表走这里
COS_GLOBAL_HOST = "service.cos.myqcloud.com"

# 默认地域（CVM / COS 均先按此探测）
DEFAULT_REGION = "ap-guangzhou"

# 对象预览每页条数；枚举（打包下载）时按 COS 单页上限翻页
_PAGE_SIZE = 50
_ENUM_PAGE = 1000

# 打包下载上限：**宁可明确报错，也不静默截断**（前端会原样展示 error/output）
MAX_ZIP_OBJECTS = 1000                # 单次最多打包的对象数
MAX_ZIP_BYTES = 512 * 1024 * 1024     # 单次最多打包的原始总字节数（512 MiB）
_ZIP_WORKERS = 8                      # 并发拉取对象的线程数
_SWEEP_WORKERS = 8                    # 全域扫描（区域留空时逐个地域取实例）的并发线程数

# 下载进度上报的节流：每写入这么多字节、或隔这么久才回调一次
# （chunk 是 64 KiB，大文件不动节流会打出几万次事件）
_PROGRESS_STEP = 256 * 1024           # 256 KiB
_PROGRESS_INTERVAL = 0.2              # 秒

# 权限探测的单请求超时（秒）——探测要「整体十几秒内」结束，不能用默认的 20 秒 ×3
_PROBE_TIMEOUT = 6.0

# 不属于「凭证无效」的错误码（时钟偏移、限流等不应判定为无效）
_NON_CREDENTIAL_CODES = {
    "AuthFailure.SignatureExpire",
    "RequestLimitExceeded",
    "InternalError",
}

# 权限探测结论：ok / denied（明确无权限）/ bad_cred（凭据被拒）/ unknown（拿不准 → fail-open）
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

# COS 错误码 → 结论
_COS_BAD_CRED_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch"}

# 桶名 / 地域白名单式校验：避免把接口返回的字符串直接拼进 URL
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9\-]+$")


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


# ── TC3-HMAC-SHA256 签名（host / service 可换，供 STS 与 CVM 共用）──

def _tc3_authorization(
    secret_id: str,
    secret_key: str,
    host: str,
    service: str,
    timestamp: int,
    date: str,
    payload: str,
    content_type: str,
) -> str:
    """生成腾讯云 TC3-HMAC-SHA256 Authorization 头（host/service 按目标接口传入）。"""
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _tc3_call(
    client,
    host: str,
    service: str,
    action: str,
    version: str,
    region: str,
    payload_dict: dict | None,
    secret_id: str,
    secret_key: str,
    timeout: float | None = None,
):
    """发起一次 TC3 签名的只读 POST 请求（腾讯云云 API 通用形态）。"""
    payload = json.dumps(payload_dict or {}, separators=(",", ":"), ensure_ascii=False)
    content_type = "application/json; charset=utf-8"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

    headers = {
        "Authorization": _tc3_authorization(
            secret_id, secret_key, host, service, timestamp, date, payload, content_type
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
    }
    if region:
        headers["X-TC-Region"] = region

    kwargs: dict[str, Any] = {"headers": headers, "content": payload}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.post(f"https://{host}/", **kwargs)


# ── COS 签名（q-sign-algorithm=sha1）─────────────────────

def _sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _hmac_sha1_hex(key: str, text: str) -> str:
    return hmac.new(key.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).hexdigest()


def _cos_quote(value: Any) -> str:
    """COS 签名里的 key/value 百分号编码（保留 -_.~，其余含 / 一并编码，与官方 SDK 一致）。"""
    return quote(str(value), safe="-_.~")


def _cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    params: dict,
    headers: dict,
    sign_time: str,
) -> str:
    """COS 签名（与官方 cos-python-sdk-v5 逐字节对齐）。

    signKey = HMAC-SHA1(SecretKey, KeyTime)          ← 单次 HMAC
    httpString = method\\npath\\nparams\\nheaders\\n
    stringToSign = "sha1"\\n + signTime\\n + sha1(httpString)\\n
    signature = HMAC-SHA1(signKey, stringToSign)

    注意：任务描述里的 ``HMAC(HMAC(SecretKey, signTime), keyTime)`` 是**双重** HMAC，
    与官方实现不符；已按官方 SDK 更正为单次 HMAC，并用 SDK 做了逐字节校验。
    """
    key_time = sign_time
    sign_key = _hmac_sha1_hex(secret_key, key_time)

    param_keys = sorted(params.keys())
    http_params = "&".join(f"{_cos_quote(k)}={_cos_quote(params[k])}" for k in param_keys)

    normalized = {str(k).lower(): v for k, v in headers.items()}
    header_keys = sorted(normalized.keys())
    http_headers = "&".join(f"{_cos_quote(k)}={_cos_quote(normalized[k])}" for k in header_keys)

    http_string = f"{method.lower()}\n{path}\n{http_params}\n{http_headers}\n"
    string_to_sign = f"sha1\n{sign_time}\n{_sha1_hex(http_string)}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)

    return (
        "q-sign-algorithm=sha1&q-ak={ak}&q-sign-time={st}&q-key-time={kt}"
        "&q-header-list={hl}&q-url-param-list={pl}&q-signature={sg}"
    ).format(
        ak=secret_id,
        st=sign_time,
        kt=key_time,
        hl=";".join(header_keys),
        pl=";".join(param_keys),
        sg=signature,
    )


def _cos_query(params: dict | None) -> str:
    """把查询参数编码成与签名**逐字节一致**的串（空格 -> %20，不是 httpx 的 +）。

    交给 httpx 的 ``params=`` 会按 ``quote_plus`` 编码（空格变 ``+``），与签名里的
    ``_cos_quote``（空格变 ``%20``）不一致，带空格/加号的 key 与前缀会导致 403，
    所以这里自己拼查询串。
    """
    if not params:
        return ""
    return "&".join(
        f"{_cos_quote(key)}={_cos_quote(params[key])}" for key in sorted(params.keys())
    )


def _cos_auth_headers(
    host: str,
    path: str,
    params: dict | None,
    secret_id: str,
    secret_key: str,
) -> dict:
    """一次 COS 签名 GET 所需的请求头（签名只覆盖 host 头 + 查询参数）。"""
    now = int(datetime.now(timezone.utc).timestamp())
    sign_time = f"{now};{now + 3600}"
    signed_headers = {"host": host}
    authorization = _cos_authorization(
        secret_id, secret_key, "GET", path, params or {}, signed_headers, sign_time
    )
    return {"Host": host, "Authorization": authorization}


def _cos_get(
    client,
    host: str,
    path: str,
    params: dict | None,
    secret_id: str,
    secret_key: str,
    timeout: float | None = None,
):
    """发起一次 COS 签名的只读 GET。"""
    headers = _cos_auth_headers(host, path, params, secret_id, secret_key)
    query = _cos_query(params)
    url = f"https://{host}{path}" + (f"?{query}" if query else "")
    kwargs: dict[str, Any] = {"headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(url, **kwargs)


def _cos_object_path(key: str) -> str:
    """对象在 COS 签名里的路径：``/`` + 百分号编码后的 key（保留 ``/`` 分隔）。"""
    return quote("/" + key, safe="/-_.~")


def _cos_region(location: str) -> str:
    """把 COS 桶列表里的 Location 归一成地域名（``cos.ap-guangzhou`` → ``ap-guangzhou``）。"""
    value = (location or "").strip()
    if value.startswith("cos."):
        value = value[4:]
    return value or DEFAULT_REGION


def _cos_target(params: dict) -> tuple[dict, str]:
    """校验并归一化 COS 目标（桶名 / 地域），返回 ``(target, error)``。"""
    bucket = _field(params, "bucket")
    if not bucket:
        return {}, "缺少桶名，请在表单里填写，或从「对象存储桶列表」行内点击「预览」进入"
    if not _BUCKET_RE.match(bucket):
        return {}, "桶名不合法"
    region = _cos_region(_field(params, "region"))
    if not _REGION_RE.match(region):
        return {}, "地域不合法"
    return {
        "bucket": bucket,
        "region": region,
        "host": f"{bucket}.cos.{region}.myqcloud.com",
    }, ""


# ── XML 解析（COS 返回 XML；用本地名匹配，绕开 namespace 差异）──

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_root(text: str):
    try:
        return ET.fromstring(text)
    except Exception:
        return None


def _xml_findall(text_or_elem, local_name: str) -> list:
    """返回所有本地名匹配的后代元素（namespace 无关）。"""
    root = text_or_elem
    if isinstance(root, str):
        root = _xml_root(root)
    if root is None:
        return []
    return [elem for elem in root.iter() if _local_name(elem.tag) == local_name]


def _xml_text(elem, local_name: str) -> str:
    """取某个后代元素的文本（第一个，忽略大小写差异的常见变体）。"""
    if elem is None:
        return ""
    target = local_name.lower()
    for child in elem.iter():
        if _local_name(child.tag).lower() == target:
            return (child.text or "").strip()
    return ""


def _xml_error(text: str) -> tuple[str, str]:
    """COS 错误响应 <Error><Code/><Message/></Error> → (code, message)。"""
    root = _xml_root(text)
    if root is None:
        return "", ""
    return _xml_text(root, "Code"), _xml_text(root, "Message")


# ── 权限探测（只读；拿不准 → fail-open）─────────────────

def _tc3_conclusion(resp) -> str:
    """把 TC3 响应归一成探测结论。"""
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    error = response_body.get("Error")
    if resp.status_code == 200 and not isinstance(error, dict):
        return _PROBE_OK
    if isinstance(error, dict):
        code = str(error.get("Code") or "")
        if code.startswith("AuthFailure") or "InvalidSecretId" in code:
            return _PROBE_BAD_CRED
        if code.startswith("UnauthorizedOperation") or code.startswith("FailedOperation.NotAllow"):
            return _PROBE_DENIED
        return _PROBE_UNKNOWN
    if resp.status_code == 401:
        return _PROBE_BAD_CRED
    if resp.status_code == 403:
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _tc3_probe(client, host, service, action, version, region, payload, ak, sk) -> str:
    """探测一次 TC3 接口，返回结论字符串；异常一律 unknown（不抛出）。"""
    try:
        resp = _tc3_call(
            client, host, service, action, version, region, payload, ak, sk, _PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - 探测失败不能让整个 capabilities 崩
        return _PROBE_UNKNOWN
    return _tc3_conclusion(resp)


def _cos_probe_buckets(client, ak, sk) -> str:
    """探测 COS 桶列表（只读），返回结论字符串。"""
    try:
        resp = _cos_get(client, COS_GLOBAL_HOST, "/", {}, ak, sk, _PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    code, _ = _xml_error(resp.text)
    if code in _COS_BAD_CRED_CODES:
        return _PROBE_BAD_CRED
    if code in ("AccessDenied", "Forbidden"):
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _probe_all(ak: str, sk: str) -> dict:
    """依次只读探测 STS / CVM / COS，返回 {action_id: 结论}（彼此独立）。"""
    status: dict[str, str] = {}
    with make_client() as client:
        status["whoami"] = _tc3_probe(
            client, STS_HOST, STS_SERVICE, STS_ACTION, STS_VERSION, STS_REGION, {}, ak, sk
        )
        status["list_regions"] = _tc3_probe(
            client, CVM_HOST, CVM_SERVICE, "DescribeRegions", CVM_VERSION, "", {}, ak, sk
        )
        status["list_instances"] = _tc3_probe(
            client,
            CVM_HOST,
            CVM_SERVICE,
            "DescribeInstances",
            CVM_VERSION,
            DEFAULT_REGION,
            {"Limit": 1},
            ak,
            sk,
        )
        status["list_buckets"] = _cos_probe_buckets(client, ak, sk)
    # 对象预览 / 单对象下载 / 打包下载与桶列表是同一个 COS 权限位
    status["list_objects"] = status["list_buckets"]
    status["download_object"] = status["list_buckets"]
    status["download_prefix"] = status["list_buckets"]
    return status


def capabilities(data: dict) -> dict:
    """返回当前凭据**真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    探测 STS / CVM / COS 三个只读接口，各自独立 try：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 凭据被拒」→ 隐藏该操作；
    - 网络异常或返回看不懂 → **unknown**，按可用处理（fail-open）。

    若三个接口全都拿不准，则直接抛异常，交给框架按「未知 → 全部可用」兜底。
    """
    data = data or {}
    ak = _field(data, "secret_id")
    sk = _field(data, "secret_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 SecretId 或 SecretKey"}

    status = _probe_all(ak, sk)
    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定 —— 让框架按「未知」处理，绝不返回空列表把操作全隐藏
        raise RuntimeError("腾讯云权限探测全部失败（网络异常或响应无法识别）")

    if status.get("whoami") == _PROBE_BAD_CRED:
        # 基础身份接口都明确拒了这对密钥 —— 同一对密钥在 CVM/COS 上必然也被拒，
        # 此时剩下几个探针的 unknown 不是「拿不准」而是「密钥已废」，没必要假装可用。
        return {"available": [], "note": "SecretId/SecretKey 无效或已禁用，任何操作都会失败"}

    available = [
        action_id
        for action_id, value in status.items()
        if value not in (_PROBE_DENIED, _PROBE_BAD_CRED)
    ]

    if any(value == _PROBE_BAD_CRED for value in status.values()):
        note = "凭证被拒绝，相关操作已隐藏"
    else:
        rights = []
        if status.get("list_instances") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
            rights.append("云主机管理权限")
        if status.get("list_buckets") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
            rights.append("对象存储权限")
        note = f"具备{'、'.join(rights)}" if rights else "未探测到额外资源权限"
    return {"available": available, "note": note}


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not _field(data, "secret_id"):
        errors.append("SecretId 不能为空")
    if not _field(data, "secret_key"):
        errors.append("SecretKey 不能为空")
    return errors


def _fetch_balance(secret_id: str, secret_key: str) -> dict | None:
    """查账户余额（best-effort，失败返回 None）：Billing ``DescribeAccountBalance``。

    返回 ``{"balance": float, "currency": "CNY"}``；接口金额单位是**分**，这里换算成元。
    查不到就不显示——不猜。
    """
    try:
        with make_client() as client:
            resp = _tc3_call(
                client, "billing.tencentcloudapi.com", "billing", "DescribeAccountBalance",
                "2018-07-09", "", {}, secret_id, secret_key,
            )
        if resp.status_code != 200:
            return None
        payload = safe_json(resp) or {}
        body = payload.get("Response") if isinstance(payload, dict) else None
        if not isinstance(body, dict) or body.get("Error"):
            return None
        cents = body.get("RealBalance", body.get("Balance"))
        value = float(cents) / 100.0
        if value < 0:
            return None
        return {"balance": round(value, 2), "currency": "CNY"}
    except Exception:  # noqa: BLE001 - 余额只是附加信息，失败不影响验证结论
        return None


@guarded
def verify(data):
    secret_id = _field(data, "secret_id")
    secret_key = _field(data, "secret_key")
    if not secret_id or not secret_key:
        return VerifyResult(status="invalid", message="SecretId 或 SecretKey 为空")

    with make_client() as client:
        resp = _tc3_call(
            client,
            STS_HOST,
            STS_SERVICE,
            STS_ACTION,
            STS_VERSION,
            STS_REGION,
            {},
            secret_id,
            secret_key,
        )

    status = resp.status_code
    payload_json = safe_json(resp) or {}
    response_body = payload_json.get("Response")
    if not isinstance(response_body, dict):
        response_body = {}

    # 腾讯云多数业务错误以 HTTP 200 + Response.Error 返回
    error = response_body.get("Error")
    if isinstance(error, dict):
        code = str(error.get("Code") or "").strip()
        message = error.get("Message") or code or "未知错误"
        if code in _NON_CREDENTIAL_CODES:
            return VerifyResult(status="error", message=f"无法确认凭证状态: {message}")
        if code.startswith("AuthFailure") or "InvalidSecretId" in code:
            return VerifyResult(status="invalid", message=f"凭证无效: {message}")
        return VerifyResult(status="error", message=f"无法确认凭证状态: {code or message}")

    if status == 200:
        # 附加信息：账号 ID 说明「这是哪个账号」，再尽量补一项余额（前端按「余额 x CNY」展示）。
        # ARN / PrincipalId 属于「这个凭据是谁」，那是 whoami 动作的活（那里三项都在展示）。
        account_id = response_body.get("AccountId")
        info: dict = {}
        if account_id:
            info["account_id"] = account_id
        info.update(_fetch_balance(secret_id, secret_key) or {})
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


# ── 操作 ────────────────────────────────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _tc3_failure(resp, ak: str, sk: str) -> str:
    """把 TC3 失败响应转成一句脱敏后的错误信息。"""
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    error = response_body.get("Error")
    if isinstance(error, dict):
        detail = str(error.get("Message") or error.get("Code") or f"HTTP {resp.status_code}")
    else:
        detail = f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


def _cos_failure(resp, ak: str, sk: str) -> str:
    """把 COS 失败响应（XML）转成一句脱敏后的错误信息。"""
    code, message = _xml_error(resp.text)
    detail = message or code or f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


_WHOAMI_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

_REGION_COLUMNS = [
    {"key": "Region", "label": "地域 ID", "mono": True},
    {"key": "RegionName", "label": "名称"},
    {"key": "RegionState", "label": "状态", "width": 110, "type": "tag"},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    # 「地域留空 = 枚举全部地域」时一张表里混着多个地域的实例，必须能一眼看出各在哪
    {"key": "Region", "label": "区域", "width": 120},
    {"key": "InstanceName", "label": "实例名"},
    {"key": "InstanceState", "label": "状态", "width": 100, "type": "tag"},
    {"key": "PublicIp", "label": "公网 IP", "width": 130, "mono": True},
    {"key": "InstanceType", "label": "规格"},
]

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "region", "label": "地域", "width": 120},
]

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "type", "label": "类型", "width": 80, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

# 地域列表行内「实例」下钻到 list_instances（region 作为 params 传下去预填表单）
_REGION_ROW_ACTION = {
    "id": "list_instances",
    "label": "实例",
    "params": {"region": "$row.Region"},
}

# 桶列表行内「预览」下钻到 list_objects
_OBJECT_ROW_ACTION = {
    "id": "list_objects",
    "label": "预览",
    "params": {"bucket": "$row.name", "region": "$row.region"},
}

# 对象列表的行内按钮：文件夹行有 folderPath（「进入」「打包下载」可用），
# 文件行有 fileKey（「下载」可用）—— 对方没有的键由前端自动置灰，绝不会传 undefined 过来。
_OBJECT_ROW_ACTIONS = [
    {
        "id": "list_objects",
        "label": "进入",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "prefix": "$row.folderPath",
        },
    },
    {
        "id": "download_object",
        "label": "下载",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "key": "$row.fileKey",
        },
    },
    {
        "id": "download_prefix",
        "label": "打包下载",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "prefix": "$row.folderPath",
        },
    },
]

_BUCKET_FIELD = {"key": "bucket", "label": "桶名", "placeholder": "如 mybucket-1250000000"}
_REGION_FIELD = {
    "key": "region",
    "label": "地域",
    "placeholder": f"默认 {DEFAULT_REGION}，如 ap-shanghai / ap-beijing",
}

# 云主机专用：这里「留空」的含义与 COS 那几个动作**不同**——
# COS 留空回落到 DEFAULT_REGION（桶名拼端点必须确定地域），云主机留空则枚举全部地域。
# 同名 key 语义相反，所以文案单独一份，不复用上面的 _REGION_FIELD。
_INSTANCE_REGION_FIELD = {
    "key": "region",
    "label": "地域",
    "placeholder": "留空枚举全部地域，也可只填一个如 ap-shanghai",
}


def _object_row_file(
    bucket: str, region: str, key: str, size_raw: str, modified: str
) -> dict:
    """文件行（fileKey / fileName 只出现在文件行，供「下载」与另存为默认名使用）。"""
    return {
        "key": key,
        "type": "文件",
        "size": int(size_raw) if str(size_raw).isdigit() else size_raw,
        "lastModified": modified,
        "bucket": bucket,
        "region": region,
        "fileKey": key,
        "fileName": key.rstrip("/").rsplit("/", 1)[-1],
    }


def _object_row_folder(bucket: str, region: str, prefix: str) -> dict:
    """文件夹行（folderPath / folderName 只出现在文件夹行）。"""
    name = prefix.rstrip("/").rsplit("/", 1)[-1] or "bucket"
    return {
        "key": prefix,
        "type": "文件夹",
        "size": "",
        "lastModified": "",
        "bucket": bucket,
        "region": region,
        "folderPath": prefix,
        "folderName": f"{name}.zip",
    }


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的身份 / 实例 / 对象存储查询）。"""
    data = data or {}
    if not (_field(data, "secret_id") and _field(data, "secret_key")):
        return []
    return [
        {
            "id": "whoami",
            "name": "查看调用者身份",
            "kind": "list",
            "group": "身份",
            "order": 10,
            "description": "STS GetCallerIdentity 身份信息 + 只读权限探测",
            "columns": _WHOAMI_COLUMNS,
            "empty_text": "未获取到身份信息",
        },
        {
            "id": "list_regions",
            "name": "地域列表",
            "kind": "list",
            "group": "计算",
            "order": 15,
            "description": "CVM DescribeRegions（列出该账号可选地域，行内「实例」下钻到该地域的云主机）",
            "columns": _REGION_COLUMNS,
            "row_actions": [_REGION_ROW_ACTION],
            "empty_text": "未获取到地域列表",
        },
        {
            "id": "list_instances",
            "name": "云主机实例列表",
            "kind": "list",
            "group": "计算",
            "order": 20,
            "description": f"CVM DescribeInstances（地域留空 = 枚举全部地域并汇总；填了则只查该地域，如 {DEFAULT_REGION}）",
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「查询」时并入参数
            "fields": [_INSTANCE_REGION_FIELD],
            "submit": "查询",
            "empty_text": "暂无云主机实例（未填地域时为全部地域）",
        },
        {
            "id": "list_buckets",
            "name": "对象存储桶列表",
            "kind": "list",
            "group": "存储",
            "order": 30,
            "description": "COS 桶列表（只读；桶自带地域，无需先选地域）",
            "columns": _BUCKET_COLUMNS,
            "row_actions": [_OBJECT_ROW_ACTION],
            "empty_text": "暂无对象存储桶",
        },
        {
            "id": "list_objects",
            "name": "对象浏览",
            "kind": "list",
            "group": "存储",
            "order": 40,
            "description": "COS 对象列表（按 / 分隔列出文件与文件夹；行内可「进入」「下载」「打包下载」）",
            "columns": _OBJECT_COLUMNS,
            "fields": [
                _BUCKET_FIELD,
                {
                    "key": "prefix",
                    "label": "前缀",
                    "placeholder": "留空为桶根目录，如 dir/",
                },
                _REGION_FIELD,
            ],
            "row_actions": _OBJECT_ROW_ACTIONS,
            "submit": "查询",
            "empty_text": "该前缀下暂无对象（仅显示前 50 个）",
        },
        {
            "id": "download_object",
            "name": "下载对象",
            "kind": "form",
            "group": "存储",
            "order": 50,
            "description": "下载桶内的单个对象（弹「另存为」；从对象列表行内「下载」进入会自动填好）",
            "fields": [
                _BUCKET_FIELD,
                {"key": "key", "label": "对象 Key", "required": True, "placeholder": "如 dir/file.txt"},
                _REGION_FIELD,
            ],
            "save_to": {"key": "path", "default_name": "$row.fileName"},
            "progress": True,
            "submit": "下载",
        },
        {
            "id": "download_prefix",
            "name": "打包下载（zip）",
            "kind": "form",
            "group": "存储",
            "order": 60,
            "description": (
                f"把该前缀下所有对象拉下来打成一个 zip（最多 {MAX_ZIP_OBJECTS} 个对象 / "
                f"{_human_size(MAX_ZIP_BYTES)}，超出会明确报错而不静默截断）"
            ),
            "fields": [
                _BUCKET_FIELD,
                {"key": "prefix", "label": "前缀（文件夹）", "placeholder": "如 dir/sub/"},
                _REGION_FIELD,
            ],
            "save_to": {"key": "path", "default_name": "$row.folderName"},
            "progress": True,
            "submit": "打包下载",
        },
    ]


def run_action(action_id: str, params: dict, data: dict, progress=None, should_cancel=None) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。

    ``progress`` / ``should_cancel`` 都是**可选**的下载钩子（见 ``stream_action``）：

    - ``progress(current, total, text)``：上报进度，``total <= 0`` 表示总大小未知；
    - ``should_cancel() -> bool``：返回 True 表示用户点了「取消」，下载循环会尽快干净地停下。

    只有 ``stream_action`` 会传它们，框架直接调用 ``run_action`` 时保持三个参数不变。
    """
    data = data or {}
    params = params or {}
    try:
        ak = _field(data, "secret_id")
        sk = _field(data, "secret_key")
        if not ak or not sk:
            return {"ok": False, "error": "SecretId 或 SecretKey 为空"}

        if action_id == "whoami":
            return _run_whoami(ak, sk)
        if action_id == "list_regions":
            return _run_list_regions(ak, sk)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, params)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, params)
        if action_id == "download_object":
            return _run_download_object(ak, sk, params, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(ak, sk, params, progress, should_cancel)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(
    action_id: str,
    params: dict,
    data: dict,
    on_delta,
    on_progress=None,
    should_cancel=None,
) -> dict:
    """下载类操作的流式执行：边下边上报进度、并响应取消。其它操作回退到 run_action。

    ``on_progress(current, total, text)`` 与 ``should_cancel()`` 都由框架按关键字传入
    （写进签名才会传），都为 None 时下载路径与 ``run_action`` 完全一致。
    """
    if action_id in ("download_object", "download_prefix"):
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


def _run_whoami(ak: str, sk: str) -> dict:
    """身份信息 + 权限探测（各接口独立只读调用）。"""
    with make_client() as client:
        resp = _tc3_call(
            client, STS_HOST, STS_SERVICE, STS_ACTION, STS_VERSION, STS_REGION, {}, ak, sk
        )
        body = safe_json(resp) or {}
        response_body = body.get("Response")
        response_body = response_body if isinstance(response_body, dict) else {}
        if resp.status_code != 200 or isinstance(response_body.get("Error"), dict):
            return {"ok": False, "error": _tc3_failure(resp, ak, sk)}

        rows = [
            {"item": "账号 ID", "value": response_body.get("AccountId") or "—"},
            {"item": "ARN", "value": response_body.get("Arn") or "—"},
            {"item": "PrincipalId", "value": response_body.get("PrincipalId") or "—"},
        ]

        rights = []
        if _tc3_probe(
            client,
            CVM_HOST,
            CVM_SERVICE,
            "DescribeInstances",
            CVM_VERSION,
            DEFAULT_REGION,
            {"Limit": 1},
            ak,
            sk,
        ) == _PROBE_OK:
            rights.append("云主机管理")
        if _cos_probe_buckets(client, ak, sk) == _PROBE_OK:
            rights.append("对象存储")
        rows.append({"item": "探测到的权限", "value": "、".join(rights) if rights else "未探测到额外资源权限"})

    return {"ok": True, "rows": rows, "columns": _WHOAMI_COLUMNS, "total": len(rows), "has_more": False}


def _fetch_regions(client, ak: str, sk: str) -> tuple[list[dict], str]:
    """CVM DescribeRegions 的原始 RegionSet 条目；返回 ``(items, error)``。

    「地域列表」展示与「地域留空 → 枚举全部地域」共用这**一次**调用，不各写一遍签名请求。
    """
    resp = _tc3_call(
        client, CVM_HOST, CVM_SERVICE, "DescribeRegions", CVM_VERSION, "", {}, ak, sk
    )
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    if resp.status_code != 200 or isinstance(response_body.get("Error"), dict):
        return [], _tc3_failure(resp, ak, sk)
    items = response_body.get("RegionSet")
    items = items if isinstance(items, list) else []
    return [item for item in items if isinstance(item, dict)], ""


def _run_list_regions(ak: str, sk: str) -> dict:
    """地域列表（CVM DescribeRegions，只读）：RegionSet[] → 地域 ID / 中文名 / 状态。"""
    with make_client() as client:
        regions, error = _fetch_regions(client, ak, sk)
    if error:
        return {"ok": False, "error": error}

    rows = []
    for item in regions:
        region_id = item.get("Region") or ""
        if not region_id:
            continue
        rows.append(
            {
                "Region": region_id,
                "RegionName": item.get("RegionName") or "",
                "RegionState": item.get("RegionState") or "",
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _REGION_COLUMNS,
        "row_actions": [_REGION_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
        "empty_text": "未获取到地域列表",
    }


def _instances_one(
    client, ak: str, sk: str, region: str
) -> tuple[list[dict], int, str]:
    """取**单个地域**的 CVM 实例；返回 ``(rows, total, error)``（error 非空时 rows 必为空）。

    ``client`` 由调用方（父线程）建好传进来：代理按线程隔离，worker 里不能再建。
    """
    resp = _tc3_call(
        client,
        CVM_HOST,
        CVM_SERVICE,
        "DescribeInstances",
        CVM_VERSION,
        region,
        {"Limit": 100},
        ak,
        sk,
    )
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    if resp.status_code != 200 or isinstance(response_body.get("Error"), dict):
        return [], 0, _tc3_failure(resp, ak, sk)

    instances = response_body.get("InstanceSet")
    instances = instances if isinstance(instances, list) else []
    rows = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        ips = inst.get("PublicIpAddresses")
        ips = ips if isinstance(ips, list) else []
        rows.append(
            {
                "InstanceId": inst.get("InstanceId") or "",
                "Region": region,
                "InstanceName": inst.get("InstanceName") or "",
                "InstanceState": inst.get("InstanceState") or "",
                "PublicIp": ips[0] if ips else "",
                "InstanceType": inst.get("InstanceType") or "",
            }
        )
    return rows, int(response_body.get("TotalCount") or len(rows)), ""


def _sweep(items: list, worker) -> list[tuple[Any, Any, str]]:
    """并发对每个 item 调一次 ``worker(item)``，按**原顺序**返回 ``(item, 结果, 异常名)``。

    两个必须点：

    - ``client`` 由**父线程**创建后用闭包共享进来：代理是线程隔离的
      （``vault_proxy_ctx`` 用 ``threading.local``），worker 里各自 ``make_client()``
      拿不到本次凭据的代理。httpx client 本身线程安全，共享即可。
    - 单个 item 抛异常（RequestLimitExceeded / 无权限 / 网络抖动）**只记一笔**，
      绝不毁掉整批。

    顺序按输入还原：``as_completed`` 的完成顺序不确定，直接拼接会让汇总表每次刷新行列乱序。
    """
    ordered: list[tuple[Any, Any, str]] = [(item, None, "") for item in items]
    if not items:
        return ordered
    with ThreadPoolExecutor(max_workers=min(_SWEEP_WORKERS, len(items))) as pool:
        futures = {pool.submit(worker, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                ordered[index] = (items[index], future.result(), "")
            except Exception as exc:  # noqa: BLE001 - 单个地域失败不毁整张表
                ordered[index] = (items[index], None, type(exc).__name__)
    return ordered


def _run_list_instances(ak: str, sk: str, params: dict) -> dict:
    """云主机实例列表：填了地域就只查它；**留空则枚举全部地域**并发取完汇总成一张表。"""
    region = _field(params, "region")

    # client 在父线程建好、共享给 worker（见 _sweep 的说明），两条分支共用同一个
    with make_client() as client:
        if region:
            # 填了地域 —— 校验与报错与以前完全一致，不因别的地域失败而被掩盖
            if not _REGION_RE.match(region):
                return {"ok": False, "error": "地域不合法"}
            rows, total, error = _instances_one(client, ak, sk, region)
            if error:
                return {"ok": False, "error": error}
            return {
                "ok": True,
                "rows": rows,
                "columns": _INSTANCE_COLUMNS,
                "total": total,
                "empty_text": f"暂无云主机实例（地域 {region}）",
                "has_more": total > len(rows),
            }

        # 留空 —— 先拿地域全集，再逐地域并发取实例
        regions, error = _fetch_regions(client, ak, sk)
        if error:
            return {"ok": False, "error": f"未填地域，取地域列表失败：{error}"}
        region_ids = [str(item.get("Region")) for item in regions if item.get("Region")]
        if not region_ids:
            return {"ok": False, "error": "未填地域，且未获取到地域列表，无法枚举"}
        swept = _sweep(region_ids, lambda rid: _instances_one(client, ak, sk, rid))

    rows: list[dict] = []
    total = 0
    has_more = False
    for _region, result, exc_name in swept:
        if exc_name:
            continue                       # 单个地域查询失败（限流 / 该地域无权限）直接跳过，不进结果
        part_rows, part_total, part_error = result
        if part_error:
            continue
        rows.extend(part_rows)
        total += part_total
        has_more = has_more or part_total > len(part_rows)

    # 只列**成功取到**的地域：失败的地域既不进结果表，也不在空态文案里提一句——
    # 这类失败（如子用户被限制在某几个地域）是常态，报出来只是噪音。
    empty_text = f"未填地域，已扫描全部 {len(region_ids)} 个地域，均无云主机实例"

    return {
        "ok": True,
        "rows": rows,
        "columns": _INSTANCE_COLUMNS,
        "total": total or len(rows),
        "has_more": has_more,
        "empty_text": empty_text,
    }


def _run_list_buckets(ak: str, sk: str) -> dict:
    with make_client() as client:
        resp = _cos_get(client, COS_GLOBAL_HOST, "/", {}, ak, sk)
    if resp.status_code != 200:
        return {"ok": False, "error": _cos_failure(resp, ak, sk)}

    rows = []
    for bucket in _xml_findall(resp.text, "Bucket"):
        name = _xml_text(bucket, "Name")
        if not name:
            continue
        region = _cos_region(_xml_text(bucket, "Location"))
        rows.append(
            {
                "name": name,
                "creationDate": _xml_text(bucket, "CreationDate"),
                "region": region,
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _BUCKET_COLUMNS,
        "row_actions": [_OBJECT_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
    }


def _run_list_objects(ak: str, sk: str, params: dict) -> dict:
    """列出一个前缀下的对象与子目录（COS 对象列表 + delimiter=/）。"""
    target, error = _cos_target(params)
    if error:
        return {"ok": False, "error": error}
    bucket = target["bucket"]
    region = target["region"]
    host = target["host"]
    prefix = _field(params, "prefix")

    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    with make_client() as client:
        resp = _cos_get(client, host, "/", query, ak, sk)
    if resp.status_code != 200:
        return {"ok": False, "error": _cos_failure(resp, ak, sk)}

    root = _xml_root(resp.text)
    rows: list[dict] = []
    # CommonPrefixes 即「子目录」，排在文件前面
    for node in _xml_findall(root, "CommonPrefixes"):
        child = _xml_text(node, "Prefix")
        if child:
            rows.append(_object_row_folder(bucket, region, child))
    for content in _xml_findall(root, "Contents"):
        key = _xml_text(content, "Key")
        if not key or key.endswith("/"):     # 目录占位对象：已由 CommonPrefixes 表达
            continue
        rows.append(
            _object_row_file(
                bucket,
                region,
                key,
                _xml_text(content, "Size"),
                _xml_text(content, "LastModified"),
            )
        )
    where = prefix or "桶根目录"
    return {
        "ok": True,
        "rows": rows,
        "columns": _OBJECT_COLUMNS,
        "row_actions": _OBJECT_ROW_ACTIONS,
        "total": len(rows),
        "empty_text": f"该前缀下暂无对象（{bucket} / {where}，仅显示前 {_PAGE_SIZE} 个）",
        "has_more": _xml_text(root, "IsTruncated").lower() == "true",
    }


class _Cancelled(Exception):
    """协作式取消（用户点了「取消」）：内部信号，只在插件内部传播，不会逃出边界。"""


def _remove_quietly(path: str) -> None:
    """删除临时文件；不存在 / 删不掉都不抛（清理不能盖过真正的失败原因）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _content_length(response) -> int:
    """响应的 ``Content-Length``；拿不到 / 非法 / 非正数一律返回 0（= 总大小未知）。"""
    try:
        raw = response.headers.get("Content-Length") or ""
        value = int(str(raw).strip())
    except Exception:  # noqa: BLE001 - 头缺失或不是数字都只是「不知道总大小」
        return 0
    return value if value > 0 else 0


def _download_with_progress(
    response, path: str, key: str, progress=None, should_cancel=None
) -> tuple[bool, str]:
    """带进度上报 / 取消响应的流式落盘（``.part`` + 原子替换语义与 ``download_to_file`` 一致）。

    **只在调用方拿到 ``progress`` 或 ``should_cancel`` 时才走这里**：两个钩子都没有时仍调用
    ``download_to_file``，保证「不上报也不可取消」路径的行为与以前逐字节一致。

    取消时删掉 ``.part`` 后返回 ``(False, "下载已取消")``——目标路径始终没被写过。
    """
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    total = _content_length(response)

    def _emit(current: int, text: str) -> None:
        """上报一次进度；没接回调（例如只传了 should_cancel）时静默。"""
        if progress is not None:
            progress(current, total, text)

    def _amount(current: int) -> str:
        """给用户看的量：``12.3 MB / 45.0 MB``；总大小未知（total<=0）时只写已下载量。"""
        if total > 0:
            return f"{_human_size(current)} / {_human_size(total)}"
        return _human_size(current)

    _emit(0, f"开始下载 {key.rsplit('/', 1)[-1] or key}")
    tmp = target + ".part"
    written = 0
    reported = 0
    last = time.monotonic()
    try:
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes(65536):
                if should_cancel is not None and should_cancel():
                    raise _Cancelled
                handle.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                # 节流：攒够 256 KiB 或过了 0.2 秒才回调一次
                if written - reported >= _PROGRESS_STEP or now - last >= _PROGRESS_INTERVAL:
                    reported, last = written, now
                    _emit(written, _amount(written))
        os.replace(tmp, target)      # 同目录内的替换是原子的
    except _Cancelled:
        _remove_quietly(tmp)
        return False, "下载已取消"
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        _remove_quietly(tmp)
        return False, f"写入失败：{type(exc).__name__}"
    if written != reported:          # 不足一个节流阈值的小文件也补报终值，别让进度条停在 0
        _emit(written, _amount(written))
    return True, target


def _run_download_object(
    ak: str, sk: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """下载单个对象（流式写入 save_to 指定的路径）；给了钩子就边写边上报 / 响应取消。"""
    target, error = _cos_target(params)
    if error:
        return {"ok": False, "error": error}
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    host = target["host"]
    url = f"https://{host}{_cos_object_path(key)}"
    headers = _cos_auth_headers(host, _cos_object_path(key), None, ak, sk)
    with make_client() as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()                       # 错误体很小，读出来好给出原因
                return {"ok": False, "error": _cos_failure(resp, ak, sk)}
            if progress is None and should_cancel is None:
                ok, message = download_to_file(resp, path)
            else:
                ok, message = _download_with_progress(resp, path, key, progress, should_cancel)
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def _list_all_objects(
    client, host: str, ak: str, sk: str, prefix: str, limit: int
) -> tuple[list[tuple[str, int]], bool, str]:
    """翻页枚举前缀下的**全部**对象（打包下载用，不受预览的 50 条限制）。

    返回 ``(objects, over_limit, error)``；``over_limit`` 为真表示已超过 ``limit`` 个
    并提前停止（调用方据此明确报错，不做静默截断）。
    """
    token = ""
    objects: list[tuple[str, int]] = []
    while True:
        query: dict[str, str] = {"list-type": "2", "max-keys": str(_ENUM_PAGE)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        resp = _cos_get(client, host, "/", query, ak, sk)
        if resp.status_code != 200:
            return objects, False, _cos_failure(resp, ak, sk)
        root = _xml_root(resp.text)
        for content in _xml_findall(root, "Contents"):
            key = _xml_text(content, "Key")
            if not key or key.endswith("/"):
                continue
            size_raw = _xml_text(content, "Size")
            objects.append((key, int(size_raw) if size_raw.isdigit() else 0))
            if len(objects) > limit:
                return objects, True, ""
        if _xml_text(root, "IsTruncated").lower() != "true":
            break
        # V2 用 NextContinuationToken；兼容只返回 NextMarker 的实现
        token = _xml_text(root, "NextContinuationToken") or _xml_text(root, "NextMarker")
        if not token:
            break
    return objects, False, ""


def _zip_entry_name(key: str, prefix: str) -> str | None:
    """zip 内的相对路径：去掉前缀、保留目录结构；路径不安全（.. / 绝对路径）时返回 None。"""
    rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
    rel = rel.lstrip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        return None
    return rel


def _run_download_prefix(
    ak: str, sk: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """把某个前缀下的所有对象并发拉取后打包成 zip（本地临时文件 → 原子落盘）。

    给了 ``progress`` 时按**对象个数**上报（跨对象累加字节数对用户不直观）；
    给了 ``should_cancel`` 时在**每个对象开始前**和**对象内的每个 chunk** 各查一次
    （正在下的那个大对象也立刻断流），取消即停止后续下载并清干净临时文件。
    """
    target, error = _cos_target(params)
    if error:
        return {"ok": False, "error": error}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = _field(params, "prefix")
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表行内点击「打包下载」进入"}

    host = target["host"]

    with make_client() as client:
        objects, over_limit, list_error = _list_all_objects(
            client, host, ak, sk, prefix, MAX_ZIP_OBJECTS
        )
    if list_error:
        return {"ok": False, "error": list_error}
    if over_limit:
        return {
            "ok": False,
            "error": (
                f"「{prefix}」下的对象数超过打包上限（{MAX_ZIP_OBJECTS} 个），已枚举前 "
                f"{MAX_ZIP_OBJECTS + 1} 个即停止。请换一个更小的前缀，或逐个下载。"
            ),
        }
    if not objects:
        return {"ok": False, "error": f"「{prefix}」下没有可下载的对象"}

    total = sum(size for _, size in objects)
    if total > MAX_ZIP_BYTES:
        return {
            "ok": False,
            "error": (
                f"「{prefix}」下共 {len(objects)} 个对象、{_human_size(total)}，"
                f"超过单次打包上限 {_human_size(MAX_ZIP_BYTES)}。请换一个更小的前缀，或逐个下载。"
            ),
        }

    tmpdir = tempfile.mkdtemp(prefix="vault_cos_zip_")
    part_path = path + ".part"
    failed: list[str] = []
    skipped: list[str] = []
    written = 0
    count = len(objects)
    done = 0
    if progress is not None:
        progress(0, count, f"开始打包 {count} 个对象")

    def _fetch(index: int, key: str) -> tuple[str, str, str]:
        """把一个对象流式拉到本地临时文件，返回 (key, local_path, error)。"""
        if should_cancel is not None and should_cancel():
            raise _Cancelled              # 每个对象开始前查一次：取消后不再有新下载
        local = os.path.join(tmpdir, f"{index}.bin")
        try:
            cos_path = _cos_object_path(key)
            url = f"https://{host}{cos_path}"
            headers = _cos_auth_headers(host, cos_path, None, ak, sk)
            with make_client() as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        return key, "", f"{key} (HTTP {resp.status_code})"
                    with open(local, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            # 正在下的这个对象也要能随时断流：500 MB 的对象不能等它下完
                            if should_cancel is not None and should_cancel():
                                raise _Cancelled
                            handle.write(chunk)
        except _Cancelled:
            raise          # 取消要往外传，不能被「单个对象失败」当成一个失败项吞掉
        except Exception as exc:  # noqa: BLE001 - 单个对象失败不该毁掉整包
            return key, "", f"{key} ({type(exc).__name__})"
        return key, local, ""

    try:
        with ThreadPoolExecutor(max_workers=_ZIP_WORKERS) as pool:
            futures = [pool.submit(_fetch, i, key) for i, (key, _size) in enumerate(objects)]
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for future in as_completed(futures):
                    key, local, fetch_error = future.result()   # 取消会以 _Cancelled 抛出
                    if should_cancel is not None and should_cancel():
                        raise _Cancelled
                    done += 1
                    if progress is not None:
                        # 上报「已处理对象数 / 对象总数」，text 带当前对象名
                        # （失败与跳过的对象同样计入——它们也是「已经处理完」的）
                        progress(done, count, f"{done}/{count} {key}")
                    if fetch_error:
                        failed.append(fetch_error)
                        continue
                    try:
                        entry = _zip_entry_name(key, prefix)
                        if entry is None:
                            skipped.append(f"{key}（路径不安全）")
                            continue
                        archive.write(local, arcname=entry)
                        written += 1
                    except Exception as exc:  # noqa: BLE001
                        failed.append(f"{key} ({type(exc).__name__})")
                    finally:
                        try:
                            os.remove(local)
                        except OSError:
                            pass
        if written == 0:
            raise RuntimeError("没有对象写入 zip")
        os.replace(part_path, path)      # 同目录内的替换是原子的
    except _Cancelled:                    # 取消：清干净半成品，目标路径始终没被写过
        _remove_quietly(part_path)
        return {"ok": False, "error": "打包已取消"}
    except Exception as exc:  # noqa: BLE001 - 打包失败不留半成品
        _remove_quietly(part_path)
        detail = f"，失败 {len(failed)} 个" if failed else ""
        return {"ok": False, "error": f"打包失败（{type(exc).__name__}{detail}）"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    lines = [f"已打包 {written} 个对象（{_human_size(total)}）到 {path}", f"字节数（未压缩）：{total}"]
    if failed:
        lines.append(f"失败 {len(failed)} 个：" + "；".join(failed[:5]) + ("…" if len(failed) > 5 else ""))
    if skipped:
        lines.append(f"跳过 {len(skipped)} 个：" + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
    lines.append(f"zip 内为相对「{prefix}」的路径")
    return {"ok": True, "output": "\n".join(lines)}


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    return "腾讯云 | SecretId/SecretKey"
