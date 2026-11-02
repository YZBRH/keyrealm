"""AWS AccessKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

只读调用 STS ``GetCallerIdentity``，用返回的 ARN 证明密钥有效、并给出它代表谁
（root / IAM 用户 / 角色）。用它而非列 S3 桶：``sts:GetCallerIdentity`` **不需要任何 IAM
权限**，拿不到 ARN 才是真无效（列桶会把「有效但无权限」误判成无效）；端点全局、签名固定
``us-east-1``，用户不必填地域。

自带完整实现（AWS SigV4 只在这里维护，不依赖类别共享模块）。三套调用形态：STS / EC2 走
Query API（POST 表单）；S3 走 GET，且**空体的 SHA256 必须放进 ``x-amz-content-sha256``
一起签**，规范查询串按 RFC3986 自拼（不能交给 httpx 的 ``params=``——实发串会与签名串不一致）。

操作（``actions``，id 与阿里云插件对齐）：``whoami`` · ``list_regions`` · ``list_instances`` ·
``list_buckets`` · ``list_objects`` · ``download_object`` · ``download_prefix``。
下载类走签名 GET 流式读取（上限见 ``MAX_ZIP_*``），声明了 ``progress``、支持取消。

全部调用只读，不产生任何资源变更。仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_aws-signing.html
参考文档：https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    download_to_file,
    guarded,
    log,
    make_client,
    mask_secret,
)

NAME = "AWS"
# 全局 STS 端点：签名区域固定 us-east-1，与账号实际所处地域无关
HOST = "sts.amazonaws.com"
REGION = "us-east-1"
SERVICE = "sts"
ALGORITHM = "AWS4-HMAC-SHA256"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"

# EC2 Query API：区域化端点 ec2.{region}.amazonaws.com，版本号固定
EC2_VERSION = "2016-11-15"

# S3 全局端点：ListBuckets 走这里（签名区域 us-east-1，与桶所在区域无关）
S3_GLOBAL_HOST = "s3.amazonaws.com"

# 空体的 SHA256：S3 的每个请求都必须带 x-amz-content-sha256，GET 空体就是这个常量
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# 对象预览每页条数；枚举（打包下载）时按 S3 单页上限翻页
_PAGE_SIZE = 50
_ENUM_PAGE = 1000
# 云主机单区域单页条数（EC2 MaxResults 允许 5~1000）
_INSTANCE_PAGE = 100
# 区域留空时并发扫描的线程数：AWS 会限流，8 个已足够快又不至于整片 Throttling
_SWEEP_WORKERS = 8

# 打包下载上限：**宁可明确报错，也不静默截断**（前端会原样展示 error/output）
MAX_ZIP_OBJECTS = 1000                # 单次最多打包的对象数
MAX_ZIP_BYTES = 512 * 1024 * 1024     # 单次最多打包的原始总字节数（512 MiB）
_ZIP_WORKERS = 8                      # 并发拉取对象的线程数

# 下载进度上报的节流：每写入这么多字节、或隔这么久才回调一次
# （chunk 是 64 KiB，大文件不动节流会打出几万次事件）
_PROGRESS_STEP = 256 * 1024           # 256 KiB
_PROGRESS_INTERVAL = 0.2              # 秒

# 权限探测的单请求超时（秒）——探测要「整体十几秒内」结束，不能用默认的 20 秒 ×N
_PROBE_TIMEOUT = 6.0

# 拿到了明确「密钥不成立」的判定才判无效：
#   InvalidClientTokenId / UnrecognizedClientException → AK 不存在或已被吊销
#   ExpiredToken                                       → 临时凭据已过期
#   SignatureDoesNotMatch / InvalidAccessKeyId         → SK 不对
INVALID_CODES = {
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "ExpiredToken",
    "ExpiredTokenException",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
    "AuthFailure",
}

# 权限探测的四种结论。**只有明确判定才敢下结论**，其余一律 unknown：
# 探测本身抖一下就把能用的操作全隐藏掉，比不探测还糟（框架对 unknown 是 fail-open）。
_PROBE_OK = "ok"                # 明确成功 → 该操作可用
_PROBE_DENIED = "denied"        # 身份没问题，但没这个权限 → 隐藏该操作
_PROBE_BAD_CRED = "bad_cred"    # 密钥本身被拒 → 所有操作都不可能成功
_PROBE_UNKNOWN = "unknown"      # 网络异常 / 响应看不懂 → 按可用处理

# 「身份有效但缺权限」的错误码。AWS 各族接口给的码不一样：
#   EC2 Query API  → UnauthorizedOperation / Client.UnauthorizedOperation
#   STS / S3       → AccessDenied（S3 还会给 AccessDenied 的 403 空码分支）
# 不放进 INVALID_CODES，因为它们是「没权限」而不是「密钥无效」。
_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "Client.UnauthorizedOperation",
    "AllAccessDisabled",
    "Forbidden",
}

# 探测实例权限用的区域：us-east-1 最便宜（几乎所有账号都开通，且不产生跨区流量）
_PROBE_REGION = REGION

# 桶名 / 区域名的白名单式校验：避免把用户/接口返回的字符串直接拼进 URL 与 Host
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,31}$")

REQUIRED = (("access_key_id", "Access Key ID"), ("secret_access_key", "Secret Access Key"))
DISPLAY_FIELD = "access_key_id"

# 桶 → 区域 的进程内缓存：ListBuckets 不给 BucketRegion 时要靠 ?location 逐个补问，
# 而「桶在哪个区域」是全局事实，同一进程里问一次就够（key 是全局唯一的桶名）。
_BUCKET_REGIONS: dict[str, str] = {}


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ── AWS Signature Version 4 ─────────────────────────────

def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    """HMAC 逐级派生：AWS4+SK → 日期 → 区域 → 服务 → aws4_request。"""
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _uri_encode(value: Any) -> str:
    """SigV4 要求的 RFC3986 编码：``-_.~`` 不编码，其余（含斜杠、空格）一律百分号编码。"""
    return quote(str(value), safe="-_.~")


def _canonical_query(params: dict | None) -> str:
    """规范查询串：按 key 升序 + RFC3986 编码。

    **必须自己拼**：httpx 的 ``params=`` 会按自己的规则排序与编码，拼出来的
    查询串未必与这里一致，签名就会对不上。S3 的请求 URL 因此也由本函数的
    结果直接拼成，保证「发出去的」与「签出来的」逐字节相同。
    """
    if not params:
        return ""
    return "&".join(
        f"{_uri_encode(key)}={_uri_encode(value)}"
        for key, value in sorted(params.items(), key=lambda item: str(item[0]))
    )


def _form_body(params: dict) -> str:
    """Query API（STS / EC2）的 form 请求体：按 key 升序。

    排序不是 AWS 的硬性要求，但 STS 的历史实现用的是 ``Action=…&Version=…``
    这个字节序，保持一致才能保证那条路径的签名逐字节不变。
    """
    return _canonical_query(params)


def _canonical_request(
    method: str, path: str, query: str, headers: list[tuple[str, str]], payload_hash: str
) -> tuple[str, str]:
    """构造规范请求与待签串，返回 ``(canonical_request, signed_headers)``。

    头必须**按名排序**且与 signed_headers 一致；``path`` / ``query`` 必须是
    已经编码好的规范形式（调用方负责）。
    """
    items = sorted(headers, key=lambda item: item[0])
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in items)
    signed_headers = ";".join(name for name, _ in items)
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_headers, payload_hash]
    )
    return canonical_request, signed_headers


def _signed_headers(
    access_key: str,
    secret_key: str,
    token: str,
    now: datetime.datetime,
    *,
    method: str,
    host: str,
    path: str,
    query: str = "",
    payload: bytes = b"",
    headers: tuple = (),
    region: str = REGION,
    service: str = SERVICE,
) -> dict:
    """生成本次请求的完整头（含 Authorization）。

    ``headers`` 是「除了 host / x-amz-date / x-amz-security-token 之外还要**参与签名**
    的头」——form 请求的 content-type、S3 的 x-amz-content-sha256 都从这里进；
    它们会被原样放进返回值（也就是原样发出去），不必再单独设一遍。
    """
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    to_sign: list[tuple[str, str]] = [("host", host), ("x-amz-date", amzdate)]
    to_sign.extend((str(name).lower(), str(value)) for name, value in headers)
    if token:
        to_sign.append(("x-amz-security-token", token))

    canonical_request, signed_headers = _canonical_request(
        method, path, query, to_sign, payload_hash
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    out: dict[str, str] = {str(name): str(value) for name, value in headers}
    out["X-Amz-Date"] = amzdate
    out["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    if token:
        out["X-Amz-Security-Token"] = token
    return out


def _authorization(access_key: str, secret_key: str, token: str, now: datetime.datetime) -> dict:
    """STS GetCallerIdentity 的请求头（保持历史行为：POST + form body，签名区域 us-east-1）。

    这是参数化签名的一份「历史取值」——与改造前逐字节等价：待签头仍是
    content-type / host / x-amz-date（+ x-amz-security-token），规范 URI 仍是 "/"，
    规范查询串仍为空，请求体仍是模块常量 ``BODY``。
    """
    return _signed_headers(
        access_key,
        secret_key,
        token,
        now,
        method="POST",
        host=HOST,
        path="/",
        payload=BODY.encode("utf-8"),
        headers=(("content-type", _FORM_CONTENT_TYPE),),
        region=REGION,
        service=SERVICE,
    )


# ── XML 解析（STS / EC2 / S3 都回 XML；用本地名匹配，绕开 namespace 差异）──

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
    """取某个后代元素的文本（第一个）。"""
    if elem is None:
        return ""
    for child in elem.iter():
        if _local_name(child.tag) == local_name:
            return (child.text or "").strip()
    return ""


def _xml_child(elem, local_name: str):
    """取**直接子元素**里的第一个同名元素（比全树搜索精确，不会命中嵌套里的同名字段）。"""
    if elem is None:
        return None
    for child in elem:
        if _local_name(child.tag) == local_name:
            return child
    return None


def _xml_children(elem, local_name: str) -> list:
    """取全部同名直接子元素（EC2 的各种 ``xxxSet/item`` 都是这个形状）。"""
    if elem is None:
        return []
    return [child for child in elem if _local_name(child.tag) == local_name]


def _child_text(elem, local_name: str) -> str:
    """取直接子元素的文本（EC2 的字段都在 item 的直接子层）。"""
    child = _xml_child(elem, local_name)
    return (child.text or "").strip() if child is not None else ""


def _xml_error(text: str) -> tuple[str, str]:
    """错误响应 ``<Error><Code/><Message/></Error>`` → (code, message)。

    EC2 是 ``<Response><Errors><Error>…``，S3 是顶层 ``<Error>``，两者的
    Code / Message 位置一致，同一个函数都能取到。
    """
    root = _xml_root(text)
    if root is None:
        return "", ""
    return _xml_text(root, "Code"), _xml_text(root, "Message")


def _xml_fields(text: str) -> dict:
    """把 STS 的 XML 回包拍平成一层的 ``{标签: 文本}``（自动剥掉命名空间）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    flat: dict[str, str] = {}
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if node.text and node.text.strip():
            flat.setdefault(tag, node.text.strip())
    return flat


# ── 通用请求封装 ─────────────────────────────────────────

def _ec2_call(
    client,
    region: str,
    action: str,
    extra_params: dict | None,
    access_key: str,
    secret_key: str,
    token: str,
    timeout: float | None = None,
):
    """发起一次 EC2 Query API 调用（与 STS 同形：POST + form body，签名服务 ec2）。"""
    body = _form_body({"Action": action, "Version": EC2_VERSION, **(extra_params or {})})
    host = f"ec2.{region}.amazonaws.com"
    headers = _signed_headers(
        access_key,
        secret_key,
        token,
        _utcnow(),
        method="POST",
        host=host,
        path="/",
        payload=body.encode("utf-8"),
        headers=(("content-type", _FORM_CONTENT_TYPE),),
        region=region,
        service="ec2",
    )
    kwargs: dict[str, Any] = {"headers": headers, "content": body.encode("utf-8")}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.post(f"https://{host}/", **kwargs)


def _s3_sign(
    host: str,
    path: str,
    params: dict | None,
    access_key: str,
    secret_key: str,
    token: str,
    region: str,
    now: datetime.datetime | None = None,
) -> tuple[str, dict]:
    """返回 S3 请求的 ``(完整 URL, 已签名的请求头)``。

    ``path`` 必须是**已编码**的规范 URI（调用方用 ``_uri_encode`` 拼）；
    查询串在这里拼成规范形式并直接进 URL，不做二次编码。

    ``x-amz-content-sha256`` 是 S3 特有的要求：即使 body 为空也必须带上并参与签名，
    否则报 ``SignatureDoesNotMatch``。
    """
    query = _canonical_query(params)
    headers = _signed_headers(
        access_key,
        secret_key,
        token,
        now or _utcnow(),
        method="GET",
        host=host,
        path=path,
        query=query,
        payload=b"",
        headers=(("x-amz-content-sha256", _EMPTY_SHA256),),
        region=region,
        service="s3",
    )
    url = f"https://{host}{path}"
    if query:
        url = f"{url}?{query}"
    return url, headers


def _s3_get(
    client,
    host: str,
    path: str,
    params: dict | None,
    access_key: str,
    secret_key: str,
    token: str,
    region: str,
    timeout: float | None = None,
):
    """发起一次 S3 SigV4 的只读 GET。"""
    url, headers = _s3_sign(host, path, params, access_key, secret_key, token, region)
    kwargs: dict[str, Any] = {"headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(url, **kwargs)


def _s3_stream(client, host: str, path: str, access_key: str, secret_key: str, token: str, region: str):
    """发起一次 S3 流式 GET（对象下载用），返回可 ``with`` 的上下文管理器。"""
    url, headers = _s3_sign(host, path, {}, access_key, secret_key, token, region)
    return client.stream("GET", url, headers=headers)


# ── 错误信息（任何路径都不回显凭据）─────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _http_failure(resp, access_key: str, secret_key: str, token: str) -> str:
    """把失败的响应转成一句脱敏后的错误信息（XML 与 JSON 都能给出 Code/Message）。"""
    code, message = _xml_error(resp.text)
    detail = message or code or body_snippet(resp) or f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, access_key, secret_key, token)}"


def _error_code(resp) -> str:
    """失败响应的短错误码（并发扫描时用来汇总，不展开长文案）。"""
    code, _ = _xml_error(resp.text)
    return code or f"HTTP {resp.status_code}"


# ── 权限探测（只读；拿不准 → fail-open）─────────────────
#
# 这一段是给 capabilities() 用的：点「操作」时先探一遍，只列出这对密钥真正能用的动作。
# 两条铁律：
#   1) 探针只读、串行、每个都自带 try —— 探测失败绝不能把整个 capabilities 拖崩；
#   2) **只有拿到明确判定才下结论**，网络抖动 / 响应看不懂一律 unknown（按可用处理）。
# 反过来做的代价更大：误判一次 denied 就会把用户本来能用的操作藏起来。

def _probe_verdict(status_code: int, code: str) -> str:
    """把一次探测响应的 ``(HTTP 状态, 错误码)`` 归纳成四种结论之一。"""
    if status_code == 200 and not code:
        return _PROBE_OK
    # 密钥本身不成立 —— 优先级最高，这不是「没权限」而是「密钥废了」
    if code in INVALID_CODES:
        return _PROBE_BAD_CRED
    if code in _DENIED_CODES or code.startswith("NoPermission") or code.startswith("Forbidden"):
        return _PROBE_DENIED
    # 403 但一个 Code 都没给（S3 某些出错分支就是空体）→ 只能是「签名过了但没权限」
    if status_code == 403:
        return _PROBE_DENIED
    # 5xx / Throttling / 看不懂的码：宁可当可用，也不误隐藏
    return _PROBE_UNKNOWN


def _probe_sts(client, ak: str, sk: str, token: str) -> tuple[str, str]:
    """探测 GetCallerIdentity，返回 ``(结论, 原因)``。

    走的还是 ``_authorization`` + ``BODY`` 这条老路（见 ``_run_whoami``），
    判据也一样：200 且带 Arn 才算成功 —— 这样 whoami 的可用性跟验证结果永远一致。
    """
    try:
        resp = client.post(
            f"https://{HOST}/",
            headers=_authorization(ak, sk, token, _utcnow()),
            content=BODY,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - 单个探针失败不能让整个 capabilities 崩
        return _PROBE_UNKNOWN, "网络异常"
    fields = _xml_fields(resp.text)
    if resp.status_code == 200 and fields.get("Arn"):
        return _PROBE_OK, ""
    code = fields.get("Code") or ""
    return _probe_verdict(resp.status_code, code), code or f"HTTP {resp.status_code}"


def _probe_query(
    client, region: str, action: str, extra: dict | None, ak: str, sk: str, token: str
) -> tuple[str, str]:
    """探测一次 EC2 Query 只读接口，返回 ``(结论, 原因)``。"""
    try:
        resp = _ec2_call(client, region, action, extra, ak, sk, token, timeout=_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN, "网络异常"
    if resp.status_code == 200:
        return _PROBE_OK, ""
    code, _message = _xml_error(resp.text)
    return _probe_verdict(resp.status_code, code), code or f"HTTP {resp.status_code}"


def _probe_s3_buckets(client, ak: str, sk: str, token: str) -> tuple[str, str]:
    """探测 S3 ListBuckets，返回 ``(结论, 原因)``。

    ListBuckets 是**全局**接口，签名区域必须写死 us-east-1 —— 用别的区域签会拿到
    301/400 而不是真实权限，那就成了「网络异常」以外的假 unknown。
    """
    try:
        resp = _s3_get(
            client, S3_GLOBAL_HOST, "/", {}, ak, sk, token, REGION, timeout=_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN, "网络异常"
    if resp.status_code == 200:
        return _PROBE_OK, ""
    code, _message = _xml_error(resp.text)
    return _probe_verdict(resp.status_code, code), code or f"HTTP {resp.status_code}"


def _probe_all(ak: str, sk: str, token: str) -> tuple[dict, dict]:
    """串行只读探测，返回 ``(结论表, 原因表)``，键都是 action id。

    **整个探测只开一个 client**：代理是按线程绑定的（``vault_proxy_ctx`` 用 threading.local），
    串行场景下多开 client 只会多付 TLS 握手，没有并发收益。
    """
    status: dict[str, str] = {}
    reasons: dict[str, str] = {}
    with make_client() as client:
        status["whoami"], reasons["whoami"] = _probe_sts(client, ak, sk, token)
        # DescribeRegions 不查资源、最轻，先探它：它过了说明 EC2 这一族至少连通
        status["list_regions"], reasons["list_regions"] = _probe_query(
            client, REGION, "DescribeRegions", {}, ak, sk, token
        )
        # MaxResults=5：只验证「能不能调」，别顺手把整个账号的实例都拉回来
        status["list_instances"], reasons["list_instances"] = _probe_query(
            client, _PROBE_REGION, "DescribeInstances", {"MaxResults": "5"}, ak, sk, token
        )
        status["list_buckets"], reasons["list_buckets"] = _probe_s3_buckets(client, ak, sk, token)
    # 对象浏览 / 单对象下载 / 打包下载和桶列表共用 S3 这一族的权限位，直接沿用结论。
    # 局限：S3 的 s3:ListAllMyBuckets 与 s3:ListBucket/s3:GetObject 是三个独立的动作，
    # 只给对象级权限、不给列桶权限的密钥会在这里被低估 —— 但宁可少显示也不误报能用。
    for action_id in ("list_objects", "download_object", "download_prefix"):
        status[action_id] = status["list_buckets"]
        reasons[action_id] = reasons["list_buckets"]
    return status, reasons


# ── 验证 ────────────────────────────────────────────────

def _verify(access_key: str, secret_key: str, token: str) -> VerifyResult:
    headers = _authorization(access_key, secret_key, token, _utcnow())
    with make_client() as client:
        resp = client.post(f"https://{HOST}/", headers=headers, content=BODY)

    fields = _xml_fields(resp.text)
    arn = fields.get("Arn")
    if resp.status_code == 200 and arn:
        # 附加信息只留「账号」一项：Arn / UserId 挪进 whoami 动作里展示
        return VerifyResult(
            status="valid",
            message=f"密钥有效：{arn}",
            account_info={"account_id": fields.get("Account")},
        )

    code = fields.get("Code") or ""
    detail = fields.get("Message") or body_snippet(resp)
    if code in INVALID_CODES:
        return VerifyResult(
            status="invalid",
            message=f"密钥无效（{code}）：{detail}" if detail else f"密钥无效（{code}）",
        )
    # 签名已被接受、只是被 IAM 策略拒绝：密钥本身是真的
    if code in ("AccessDenied", "AccessDeniedException"):
        return VerifyResult(
            status="valid",
            message=f"密钥有效，但被 IAM 策略拒绝调用 STS：{detail}" if detail else "密钥有效，但被 IAM 策略拒绝",
        )
    if resp.status_code == 403 and not code:
        return VerifyResult(status="invalid", message="密钥无效（HTTP 403）")
    return VerifyResult(
        status="error",
        message=f"无法确认密钥状态 (HTTP {resp.status_code})：{detail}" if detail
        else f"无法确认密钥状态 (HTTP {resp.status_code})",
    )


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    access_key = str(data.get("access_key_id") or "").strip().upper()
    if access_key.startswith("ASIA") and not str(data.get("session_token") or "").strip():
        errors.append("临时凭据（ASIA… 开头）必须填写 Session Token")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    access_key = str(data["access_key_id"]).strip()
    secret_key = str(data["secret_access_key"]).strip()
    token = str(data.get("session_token") or "").strip()

    if access_key.upper().startswith("ASIA") and not token:
        return VerifyResult(
            status="error",
            message="以 ASIA 开头的 Access Key ID 属临时凭据，缺少 Session Token 无法签名验证",
        )
    log(f"校验 AWS 凭据 {mask_secret(access_key)}")
    return _verify(access_key, secret_key, token)


def capabilities(data: dict) -> dict:
    """返回当前凭据**真正能用**的操作 id（只读探测；拿不准时抛异常 → fail-open）。

    探测四个只读入口，各自独立 try：

    - STS GetCallerIdentity → ``whoami``
    - EC2 DescribeRegions   → ``list_regions``
    - EC2 DescribeInstances → ``list_instances``
    - S3 ListBuckets        → ``list_buckets``（并沿用给 ``list_objects`` /
      ``download_object`` / ``download_prefix``）

    三种结论的处置：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 密钥被拒」→ 从 ``available`` 里去掉，并把错误码汇总进 ``note``；
    - 网络异常或响应看不懂 → **unknown**，按可用处理。

    绝不返回 ``[]`` 来表示「拿不准」：``[]`` 在框架里等于「一个操作都没有」，
    会把用户的操作全藏掉。真的一个探针都没结论时直接抛异常，交给框架按未知兜底。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 Access Key ID 或 Secret Access Key"}

    status, reasons = _probe_all(ak, sk, _field(data, "session_token"))

    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定（网络不通 / 响应认不出来）—— 让框架按「未知 → 全部可用」处理
        raise RuntimeError("AWS 权限探测全部失败（网络异常或响应无法识别）")

    if status.get("whoami") == _PROBE_BAD_CRED:
        # 连身份接口都明确拒了这对密钥，别的接口必然也拒；此时其余探针的 unknown
        # 不是「拿不准」而是「密钥已废」，没必要假装可用
        return {"available": [], "note": "Access Key 无效或已禁用，任何操作都会失败"}

    available = [
        action_id
        for action_id, value in status.items()
        if value not in (_PROBE_DENIED, _PROBE_BAD_CRED)
    ]

    # 动作 id → 中文名，直接问 actions() 要，避免两处各写一份标题日后走样
    labels = {
        item.get("id"): item.get("name") or item.get("id")
        for item in actions({"access_key_id": ak, "secret_access_key": sk})
    }

    parts: list[str] = []
    if any(value == _PROBE_BAD_CRED for value in status.values()):
        parts.append("部分接口判定该密钥无效，相关操作已隐藏")
    rights = []
    if status.get("list_instances") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
        rights.append("云主机管理权限")
    if status.get("list_buckets") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
        rights.append("对象存储权限")
    parts.append(f"具备{'、'.join(rights)}" if rights else "未探测到额外资源权限")

    # 失败原因汇总：把「为什么藏了这个操作」直接说清楚，别让用户以为插件坏了
    denied = [aid for aid, value in status.items() if value == _PROBE_DENIED]
    if denied:
        detail = "、".join(
            f"{labels.get(aid, aid)}（{reasons.get(aid) or '无权限'}）" for aid in denied
        )
        parts.append(f"已隐藏无权限的操作：{detail}")

    return {"available": available, "note": "；".join(parts)}


# ── 列 / 行内动作常量 ───────────────────────────────────

_WHOAMI_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

_REGION_COLUMNS = [
    {"key": "RegionName", "label": "区域", "width": 150, "mono": True},
    {"key": "RegionEndpoint", "label": "接入点", "width": 240, "mono": True},
    {"key": "OptInStatus", "label": "开通状态", "width": 170},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    {"key": "Name", "label": "名称"},
    {"key": "State", "label": "状态", "width": 100, "type": "tag"},
    {"key": "InstanceType", "label": "规格", "width": 130},
    {"key": "PublicIp", "label": "公网 IP", "width": 130, "mono": True},
    {"key": "PrivateIp", "label": "内网 IP", "width": 130, "mono": True},
    {"key": "Region", "label": "区域", "width": 130, "mono": True},
]

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "region", "label": "区域", "width": 130},
]

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "type", "label": "类型", "width": 80, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

# 区域列表行内「实例」下钻到 list_instances（region 作为 params 传下去预填表单）
_REGION_ROW_ACTION = {
    "id": "list_instances",
    "label": "实例",
    "params": {"region": "$row.RegionName"},
}

# 桶列表双击「预览」下钻到 list_objects
_OBJECT_ROW_ACTION = {
    "id": "list_objects",
    "label": "预览",
    "params": {"bucket": "$row.name", "region": "$row.region"},
}

# 对象列表的行内按钮：文件夹行有 folderPath（「进入」「打包下载」可用），
# 文件行有 fileKey（「下载」可用）——对方没有的键由前端自动置灰，绝不会传 undefined 过来。
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

_BUCKET_FIELD = {"key": "bucket", "label": "桶名", "placeholder": "如 my-bucket"}
_REGION_FIELD = {
    "key": "region",
    "label": "区域",
    "placeholder": f"默认 {REGION}，如 ap-northeast-1",
}


def _object_row_file(bucket: str, region: str, key: str, size_raw: str, modified: str) -> dict:
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
    if not (_field(data, "access_key_id") and _field(data, "secret_access_key")):
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
            "name": "区域列表",
            "kind": "list",
            "group": "计算",
            "order": 15,
            "description": "EC2 DescribeRegions（列出该账号可用的区域，行内「实例」下钻到该区域的云主机）",
            "columns": _REGION_COLUMNS,
            "row_actions": [_REGION_ROW_ACTION],
            "empty_text": "未获取到区域列表",
        },
        {
            "id": "list_instances",
            "name": "云主机实例列表",
            "kind": "list",
            "group": "计算",
            "order": 20,
            "description": (
                f"EC2 DescribeInstances（区域默认 {REGION}；**留空则并发扫描全部区域**并汇总，"
                "会自动带上「区域」列；也可从「区域列表」行内进入）"
            ),
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「查询」时并入参数
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "empty_text": f"暂无云主机实例（默认区域 {REGION}）",
        },
        {
            "id": "list_buckets",
            "name": "对象存储桶列表",
            "kind": "list",
            "group": "存储",
            "order": 30,
            "description": "S3 ListBuckets（只读；桶自带区域，无需先选区域）",
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
            "description": "S3 ListObjectsV2（按 / 分隔列出文件与文件夹；双击可「进入」，右键可「下载」「打包下载」）",
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
            "empty_text": f"该前缀下暂无对象（仅显示前 {_PAGE_SIZE} 个）",
        },
        {
            "id": "download_object",
            "name": "下载对象",
            "kind": "form",
            "group": "存储",
            "order": 50,
            "description": "下载桶内的单个对象（弹「另存为」；从对象列表右键「下载」进入会自动填好）",
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
        ak = _field(data, "access_key_id")
        sk = _field(data, "secret_access_key")
        token = _field(data, "session_token")
        if not ak or not sk:
            return {"ok": False, "error": "Access Key ID 或 Secret Access Key 为空"}

        if action_id == "whoami":
            return _run_whoami(ak, sk, token)
        if action_id == "list_regions":
            return _run_list_regions(ak, sk, token)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, token, params)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk, token)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, token, params)
        if action_id == "download_object":
            return _run_download_object(ak, sk, token, params, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(ak, sk, token, params, progress, should_cancel)
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


def _run_whoami(ak: str, sk: str, token: str) -> dict:
    """身份信息 + 只读权限探测（各接口独立调用，探测失败只影响那一行）。"""
    with make_client() as client:
        headers = _authorization(ak, sk, token, _utcnow())
        resp = client.post(f"https://{HOST}/", headers=headers, content=BODY, timeout=_PROBE_TIMEOUT)
        fields = _xml_fields(resp.text)
        if resp.status_code != 200 or not fields.get("Arn"):
            return {"ok": False, "error": _http_failure(resp, ak, sk, token)}

        rows = [
            {"item": "账号 ID", "value": fields.get("Account") or "—"},
            {"item": "ARN", "value": fields.get("Arn") or "—"},
            {"item": "UserId", "value": fields.get("UserId") or "—"},
        ]

        # 只读探测：能不能列区域（EC2）、能不能列桶（S3）——很短超时，失败只影响这一行
        rights = []
        try:
            probe = _describe_regions(client, ak, sk, token, timeout=_PROBE_TIMEOUT)
            if probe[1] == "" and probe[0]:
                rights.append("云主机管理")
        except Exception:  # noqa: BLE001 - 探测失败不能毁掉身份信息
            pass
        try:
            probe = _s3_get(
                client, S3_GLOBAL_HOST, "/", {}, ak, sk, token, REGION, timeout=_PROBE_TIMEOUT
            )
            if probe.status_code == 200:
                rights.append("对象存储")
        except Exception:  # noqa: BLE001
            pass
        rows.append({"item": "探测到的权限", "value": "、".join(rights) if rights else "未探测到额外资源权限"})

    return {"ok": True, "rows": rows, "columns": _WHOAMI_COLUMNS, "total": len(rows), "has_more": False}


# ── EC2 ─────────────────────────────────────────────────

def _describe_regions(
    client, ak: str, sk: str, token: str, timeout: float | None = None
) -> tuple[list[dict], str, str]:
    """DescribeRegions（固定用 us-east-1 端点调用），返回 ``(regions, code, detail)``。

    只用默认的 ``AllRegions=false``：拿到的就是该账号**当前可用**的区域，
    正是并发扫描想要的那一份（未开通的区域扫了也只会报错）。
    """
    resp = _ec2_call(client, REGION, "DescribeRegions", {}, ak, sk, token, timeout=timeout)
    if resp.status_code != 200:
        code, message = _xml_error(resp.text)
        return [], code or f"HTTP {resp.status_code}", _redact(message, ak, sk, token)

    regions: list[dict] = []
    # 字段在 regionInfo/item 这一层（regionInfo 本身只是容器，直接子元素就是一个个 item）
    for info in _xml_findall(resp.text, "regionInfo"):
        for item in _xml_children(info, "item"):
            name = _child_text(item, "regionName")
            if not name:
                continue
            regions.append(
                {
                    "name": name,
                    "endpoint": _child_text(item, "regionEndpoint"),
                    "opt_in": _child_text(item, "optInStatus"),
                }
            )
    return regions, "", ""


def _instances_one(
    client, region: str, ak: str, sk: str, token: str
) -> tuple[list[dict], bool, str]:
    """单个区域的 DescribeInstances（MaxResults=100），返回 ``(rows, has_more, code)``。

    ``code`` 非空表示该区域查询失败（Throttling / 未授权 / 未开通……）——
    并发扫描时单区域失败只记账，不影响别的区域。
    """
    resp = _ec2_call(client, region, "DescribeInstances", {"MaxResults": str(_INSTANCE_PAGE)}, ak, sk, token)
    if resp.status_code != 200:
        return [], False, _error_code(resp)

    rows: list[dict] = []
    for reservation in _xml_findall(resp.text, "reservationSet"):
        for entry in _xml_children(reservation, "item"):
            for inst in _xml_children(_xml_child(entry, "instancesSet"), "item"):
                rows.append(_instance_row(inst, region))
    # nextToken 在 <DescribeInstancesResponse> 的直接子节点上，注意 _xml_text 收的是 Element 不是文本
    has_more = bool(_xml_text(_xml_root(resp.text), "nextToken"))
    return rows, has_more, ""


def _instance_row(inst, region: str) -> dict:
    """一条实例记录。公网 IP 在 EC2 的 XML 里叫 ``ipAddress``（不是 publicIpAddress）。"""
    name = ""
    for tag in _xml_children(_xml_child(inst, "tagSet"), "item"):
        if _child_text(tag, "key") == "Name":
            name = _child_text(tag, "value")
            break
    return {
        "InstanceId": _child_text(inst, "instanceId"),
        "Name": name,
        "State": _child_text(_xml_child(inst, "instanceState"), "name"),
        "InstanceType": _child_text(inst, "instanceType"),
        "PublicIp": _child_text(inst, "ipAddress") or _child_text(inst, "publicIpAddress"),
        "PrivateIp": _child_text(inst, "privateIpAddress"),
        "Region": region,
    }


def _instances_result(rows: list[dict], has_more: bool, empty_text: str) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": _INSTANCE_COLUMNS,
        "total": len(rows),
        "has_more": has_more,
        "empty_text": empty_text,
    }


def _run_list_regions(ak: str, sk: str, token: str) -> dict:
    """区域列表（EC2 DescribeRegions，只读）。"""
    with make_client() as client:
        regions, code, detail = _describe_regions(client, ak, sk, token)
    if code:
        return {"ok": False, "error": f"请求失败：{detail or code}"}

    rows = [
        {
            "RegionName": item["name"],
            "RegionEndpoint": item["endpoint"],
            "OptInStatus": item["opt_in"],
        }
        for item in regions
    ]
    return {
        "ok": True,
        "rows": rows,
        "columns": _REGION_COLUMNS,
        "row_actions": [_REGION_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
        "empty_text": "未获取到区域列表",
    }


def _run_list_instances(ak: str, sk: str, token: str, params: dict) -> dict:
    """云主机实例列表：给了区域就查那一个，**留空则并发扫描全部区域**后汇总。"""
    region = _field(params, "region")
    with make_client() as client:
        if region:
            rows, has_more, code = _instances_one(client, region, ak, sk, token)
            if code:
                return {"ok": False, "error": f"请求失败（区域 {region}）：{code}"}
            return _instances_result(rows, has_more, f"暂无云主机实例（区域 {region}）")

        regions, code, detail = _describe_regions(client, ak, sk, token)
        if code:
            return {"ok": False, "error": f"无法获取区域列表：{detail or code}"}
        names = [item["name"] for item in regions]
        if not names:
            return {"ok": False, "error": "未获取到任何可用区域，请在表单里指定区域"}

        rows: list[dict] = []
        has_more = False
        # 并发要点：client 在**父线程**创建、共享给 worker。
        # 代理是按线程隔离的（vault_proxy_ctx 用 threading.local），worker 里各自
        # make_client() 会丢掉本次凭据指定的代理；httpx.Client 本身线程安全。
        with ThreadPoolExecutor(max_workers=_SWEEP_WORKERS) as pool:
            futures = {
                pool.submit(_instances_one, client, name, ak, sk, token): name for name in names
            }
            for future in as_completed(futures):
                try:
                    part, more, _err = future.result()
                except Exception:  # noqa: BLE001 - 单区域炸掉不能毁整张表
                    continue
                rows.extend(part)
                has_more = has_more or more

    # 只列**成功取到**的区域：查询失败的区域（限流 / 未授权 / 未开通）既不进结果表，
    # 也不在空态文案里提一句。这类失败（如子用户被限制在某几个区域）是常态，报出来只是噪音。
    return _instances_result(rows, has_more, f"已扫描全部 {len(names)} 个区域，暂无云主机实例")


# ── S3 ──────────────────────────────────────────────────

def _s3_target(params: dict) -> tuple[dict, str]:
    """校验并归一化 S3 目标（桶名 / 区域），返回 ``(target, error)``。

    桶名与区域都做过白名单式校验，避免把接口/用户给的字符串直接拼进 URL 与 Host。
    """
    bucket = _field(params, "bucket")
    if not bucket:
        return {}, "缺少桶名，请在表单里填写，或从「对象存储桶列表」双击「预览」进入"
    if not _BUCKET_RE.match(bucket):
        return {}, "桶名不合法"
    region = _field(params, "region") or REGION
    if not _REGION_RE.match(region):
        return {}, "区域不合法"
    return {"bucket": bucket, "region": region}, ""


def _bucket_region(client, bucket: str, listed: str, ak: str, sk: str, token: str) -> str:
    """桶所在区域：ListBuckets 给了 ``BucketRegion`` 就用它，否则问 ``?location`` 拿。

    结果按桶缓存（桶名全局唯一，「桶在哪个区域」是不会变的全局事实）。

    ``?location`` 需要注意：S3 早已不接受「用 us-east-1 签名去问别处的桶」，会回
    ``400 AuthorizationHeaderMalformed``——但**错误体里的 ``<Region>`` 就是答案**
    （官方 SDK 也靠这个提示纠偏）。所以：
    - 200 → 解析 ``LocationConstraint``（空 = us-east-1）；
    - 400 且带 ``<Region>`` → 直接采信它，不再补一次请求；
    - 其它情况（无权问 / 网络失败）→ 回落 us-east-1，只可能让后续对象操作报一个
      明确的区域错误，不会让整张桶列表崩掉。
    """
    if listed and _REGION_RE.match(listed):
        _BUCKET_REGIONS[bucket] = listed
        return listed
    cached = _BUCKET_REGIONS.get(bucket)
    if cached:
        return cached

    try:
        resp = _s3_get(client, f"{bucket}.s3.amazonaws.com", "/", {"location": ""}, ak, sk, token, REGION)
    except Exception:  # noqa: BLE001 - 补问区域失败不该毁掉桶列表
        return REGION

    region = ""
    if resp.status_code == 200:
        region = _location_constraint(resp.text)
    else:
        hinted = _xml_text(_xml_root(resp.text), "Region")
        region = hinted if _REGION_RE.match(hinted or "") else ""
    region = region or REGION
    _BUCKET_REGIONS[bucket] = region
    return region


def _location_constraint(text: str) -> str:
    """``<LocationConstraint>`` 的文本：空元素表示 us-east-1，个别老桶会回历史别名 ``EU``。"""
    root = _xml_root(text)
    value = (root.text or "").strip() if root is not None else ""
    return "eu-west-1" if value == "EU" else value


def _resolve_bucket_regions(client, buckets: list[tuple[str, str]], ak: str, sk: str, token: str) -> dict:
    """并发问出各桶的区域，返回 ``{桶名: 区域}``。

    ListBuckets 不给 ``BucketRegion``（当前 S3 只给 Name/CreationDate/BucketArn），
    于是每个桶都要多问一次；几十个桶串行问会让表格转上十几秒，所以同样并发。

    并发要点与实例扫描一致：``client`` 在**父线程**创建后共享给 worker——代理是按线程
    隔离的，worker 里各自 ``make_client()`` 会丢掉本次凭据指定的代理。
    单个桶问不出来只回落 us-east-1，不影响别的桶。
    """
    if not buckets:
        return {}

    def _one(name: str, listed: str) -> tuple[str, str]:
        try:
            return name, _bucket_region(client, name, listed, ak, sk, token)
        except Exception:  # noqa: BLE001 - 单个桶失败不毁整张列表
            return name, REGION

    resolved: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=_SWEEP_WORKERS) as pool:
        futures = [pool.submit(_one, name, listed) for name, listed in buckets]
        for future in as_completed(futures):
            name, region = future.result()
            resolved[name] = region
    return resolved


def _run_list_buckets(ak: str, sk: str, token: str) -> dict:
    """桶列表（S3 ListBuckets，全局端点，只读）。"""
    names: list[tuple[str, str, str]] = []      # (桶名, 创建时间, ListBuckets 给的区域)
    with make_client() as client:
        resp = _s3_get(client, S3_GLOBAL_HOST, "/", {}, ak, sk, token, REGION)
        if resp.status_code != 200:
            return {"ok": False, "error": _http_failure(resp, ak, sk, token)}
        for bucket in _xml_findall(resp.text, "Bucket"):
            name = _child_text(bucket, "Name")
            if name:
                names.append((name, _child_text(bucket, "CreationDate"), _child_text(bucket, "BucketRegion")))
        regions = _resolve_bucket_regions(
            client, [(name, listed) for name, _created, listed in names], ak, sk, token
        )

    rows = [
        {"name": name, "creationDate": created, "region": regions.get(name, REGION)}
        for name, created, _listed in names
    ]
    return {
        "ok": True,
        "rows": rows,
        "columns": _BUCKET_COLUMNS,
        "row_actions": [_OBJECT_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
    }


def _run_list_objects(ak: str, sk: str, token: str, params: dict) -> dict:
    """列出一个前缀下的对象与子目录（ListObjectsV2 + delimiter=/）。"""
    target, error = _s3_target(params)
    if error:
        return {"ok": False, "error": error}
    bucket = target["bucket"]
    region = target["region"]
    host = f"{bucket}.s3.{region}.amazonaws.com"
    prefix = _field(params, "prefix")

    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    with make_client() as client:
        resp = _s3_get(client, host, "/", query, ak, sk, token, region)
    if resp.status_code != 200:
        return {"ok": False, "error": _http_failure(resp, ak, sk, token)}

    root = _xml_root(resp.text)
    rows: list[dict] = []
    # CommonPrefixes 即「子目录」，排在文件前面
    for node in _xml_findall(root, "CommonPrefixes"):
        child = _child_text(node, "Prefix")
        if child:
            rows.append(_object_row_folder(bucket, region, child))
    for content in _xml_findall(root, "Contents"):
        key = _child_text(content, "Key")
        if not key or key.endswith("/"):     # 目录占位对象：已由 CommonPrefixes 表达
            continue
        rows.append(
            _object_row_file(
                bucket, region, key, _child_text(content, "Size"), _child_text(content, "LastModified")
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


# ── 下载 / 打包下载（纯本地骨架，与签名无关）─────────────

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


def _object_path(key: str) -> str:
    """对象的规范 URI：**只编码一次**（S3 的签名不做二次编码），斜杠保留。"""
    return "/" + quote(key, safe="/-_.~")


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
    ak: str, sk: str, token: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """下载单个对象（流式写入 save_to 指定的路径）；给了钩子就边写边上报 / 响应取消。"""
    target, error = _s3_target(params)
    if error:
        return {"ok": False, "error": error}
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    host = f"{target['bucket']}.s3.{target['region']}.amazonaws.com"
    with make_client() as client:
        with _s3_stream(client, host, _object_path(key), ak, sk, token, target["region"]) as resp:
            if resp.status_code != 200:
                resp.read()                       # 错误体很小，读出来好给出原因
                return {"ok": False, "error": _http_failure(resp, ak, sk, token)}
            if progress is None and should_cancel is None:
                ok, message = download_to_file(resp, path)
            else:
                ok, message = _download_with_progress(resp, path, key, progress, should_cancel)
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def _list_all_objects(
    client, bucket: str, region: str, ak: str, sk: str, token: str, prefix: str, limit: int
) -> tuple[list[tuple[str, int]], bool, str]:
    """翻页枚举前缀下的**全部**对象（打包下载用，不受预览的 50 条限制）。

    返回 ``(objects, over_limit, error)``；``over_limit`` 为真表示已超过 ``limit`` 个
    并提前停止（调用方据此明确报错，不做静默截断）。
    """
    host = f"{bucket}.s3.{region}.amazonaws.com"
    token_ = ""
    objects: list[tuple[str, int]] = []
    while True:
        query: dict[str, str] = {"list-type": "2", "max-keys": str(_ENUM_PAGE)}
        if prefix:
            query["prefix"] = prefix
        if token_:
            query["continuation-token"] = token_
        resp = _s3_get(client, host, "/", query, ak, sk, token, region)
        if resp.status_code != 200:
            return objects, False, _http_failure(resp, ak, sk, token)
        root = _xml_root(resp.text)
        for content in _xml_findall(root, "Contents"):
            key = _child_text(content, "Key")
            if not key or key.endswith("/"):
                continue
            size_raw = _child_text(content, "Size")
            objects.append((key, int(size_raw) if size_raw.isdigit() else 0))
            if len(objects) > limit:
                return objects, True, ""
        if _xml_text(root, "IsTruncated").lower() != "true":
            break
        token_ = _xml_text(root, "NextContinuationToken")
        if not token_:
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
    ak: str, sk: str, token: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """把某个前缀下的所有对象并发拉取后打包成 zip（本地临时文件 → 原子落盘）。

    给了 ``progress`` 时按**对象个数**上报（跨对象累加字节数对用户不直观）；
    给了 ``should_cancel`` 时在**每个对象开始前**和**对象内的每个 chunk** 各查一次
    （正在下的那个大对象也立刻断流），取消即停止后续下载并清干净临时文件。
    """
    target, error = _s3_target(params)
    if error:
        return {"ok": False, "error": error}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = _field(params, "prefix")
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表右键点击「打包下载」进入"}

    bucket = target["bucket"]
    region = target["region"]
    host = f"{bucket}.s3.{region}.amazonaws.com"

    # 整个操作共用一个 client：worker 线程里各自 make_client() 会丢掉按凭据注入的代理
    with make_client() as client:
        objects, over_limit, list_error = _list_all_objects(
            client, bucket, region, ak, sk, token, prefix, MAX_ZIP_OBJECTS
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

        tmpdir = tempfile.mkdtemp(prefix="vault_s3_zip_")
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
                with _s3_stream(client, host, _object_path(key), ak, sk, token, region) as resp:
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


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data[DISPLAY_FIELD] or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
