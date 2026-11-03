"""国信证券凭证验证插件（plugins/exchange/ 类别下的独立子插件）。

一个插件装两类**互不通用**的国信密钥，由 `kind` 选择器区分：

- ``skill_gateway``（``GS_API_KEY``，形如 ``V2V-...``）：国信 Skill 网关的单个 API Key。
  验证方式：带 ``apiKey`` + ``softName`` 调用只读行情接口 ``queryHQInfo``，取一只固定股票
  （600519）。网关返回 ``result.code == 0`` 即有效；``197001`` 是 Key 格式/有效性错误，
  ``65556`` 是 ``softName`` 未注册，``197009`` 是路由不存在。

- ``openapi_aksk``（AK / SK）：国信开放 API 平台的企业应用密钥，请求需按官方规范做
  HMAC-SHA256 签名（``X-GS-API-*`` 头）。该平台没有通用校验接口，必须针对一个**已授权
  接口**发起请求才能判断，因此凭据里提供可选的「验证接口 URL」与项目路由头；未填写时
  只保存、不做在线校验。

参考文档：
- Skill 网关：https://www.skillhub.cn/skills/guosen-securities/guosen-stock-market
- 开放 API 签名：https://openapi.guosen.com.cn/doc/signature/gsApiSignature/
"""
from __future__ import annotations

import hashlib
import hmac
import ssl
import uuid
from base64 import b64encode
from email.utils import formatdate
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    mask_secret,
    proxy,
    safe_json,
    timeout,
)

NAME = "国信证券"

KIND_SKILL = "skill_gateway"
KIND_OPENAPI = "openapi_aksk"
KIND_LABELS = {
    KIND_SKILL: "Skill 网关",
    KIND_OPENAPI: "开放 API",
}

DEFAULT_BASE_URL = "https://dgzt.guosen.com.cn/skills"
QUOTE_PATH = "/gsnews/market/agentbot/queryHQInfo/1.0"
# 只读、固定的样例标的：用于确认 Key 可用，不涉及账户或交易数据。
SAMPLE_CODE = "600519"
SAMPLE_SET_CODE = "1"

# 国信网关的错误码（实测）：
CODE_OK = "0"
CODE_BAD_KEY = "197001"      # APIKEY 格式/有效性错误
CODE_BAD_PARAM = "65556"     # softName 未注册等参数不匹配
CODE_NO_ROUTE = "197009"     # 网关无对应路由

OPENAPI_SIGNATURE_HEADER = "X-GS-API-SIGNATURE"
OPENAPI_DATE_HEADER = "X-GS-API-DATE"
OPENAPI_AK_HEADER = "X-GS-API-AK"
OPENAPI_ALGORITHM_HEADER = "X-GS-API-ALGORITHM"


def _kind(data: dict) -> str:
    kind = str((data or {}).get("kind") or KIND_SKILL).strip()
    return kind if kind in KIND_LABELS else KIND_SKILL


def _base_url(data: dict) -> str:
    base = str((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL
    return base.rstrip("/")


def _result_of(payload: Any) -> dict:
    """网关的 ``result`` 可能是对象，也可能是单元素数组。"""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else {}


def validate(data) -> list[str]:
    """按密钥种类校验必填项；通用字段校验（select 选项等）由框架负责。"""
    data = data or {}
    kind = _kind(data)
    errors: list[str] = []
    if kind == KIND_SKILL:
        if not str(data.get("api_key") or "").strip():
            errors.append("GS_API_KEY 不能为空")
        if not str(data.get("soft_name") or "").strip():
            errors.append("接入渠道（softName）不能为空")
        base = _base_url(data)
        if not base.startswith(("http://", "https://")):
            errors.append("Skill 网关地址必须是 http(s) URL")
    else:
        if not str(data.get("access_key") or "").strip():
            errors.append("Access Key（AK）不能为空")
        if not str(data.get("secret_key") or "").strip():
            errors.append("Secret Key（SK）不能为空")
        verify_url = str(data.get("verify_url") or "").strip()
        if verify_url and not verify_url.startswith(("http://", "https://")):
            errors.append("验证接口 URL 必须是 http(s) 地址")
    return errors


# ── Skill 网关 ────────────────────────────────────────────

def _legacy_client() -> httpx.Client:
    """国信网关的 TLS 兼容客户端。

    国信 Skill 网关与开放 API 网关在部分出口上只接受 TLS 1.2 + 旧密码套件；标准握手会
    直接断连。这里沿用官方 Skill 脚本的做法：宽松密码表、允许 legacy server connect、
    不校验证书。代理仍然走 ``plugin_sdk.proxy()``，匿名模式下拿不到代理会抛错，不会静默直连。
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x00000004)
    transport = httpx.HTTPTransport(verify=context, proxy=proxy())
    return httpx.Client(transport=transport, timeout=timeout(), follow_redirects=True)


def _quote_params(data: dict) -> dict:
    return {
        "code": SAMPLE_CODE,
        "setCode": SAMPLE_SET_CODE,
        "target": "0",
        "softName": str(data.get("soft_name") or "tx_skills").strip(),
        "apiKey": str(data.get("api_key") or "").strip(),
        "sysVer": "1.0",
        "skillName": "gs_stock_financial_query",
        "reqId": str(uuid.uuid4()),
    }


def _verify_skill(data: dict, api_key: str) -> VerifyResult:
    soft_name = str(data.get("soft_name") or "tx_skills").strip()
    log(f"校验国信 Skill 网关 Key {mask_secret(api_key)}（softName={soft_name}）")
    with _legacy_client() as client:
        resp = client.get(_base_url(data) + QUOTE_PATH, params=_quote_params(data))

    payload = safe_json(resp) or {}
    result = _result_of(payload)
    code = str(result.get("code", "")).strip()
    message = str(result.get("msg") or "").strip()

    if code == CODE_OK and isinstance(payload.get("object") or payload.get("data"), (dict, list)):
        return VerifyResult(
            status="valid",
            message=f"GS_API_KEY 有效（softName={soft_name}）",
        )
    if code == CODE_BAD_KEY:
        return VerifyResult(status="invalid", message="GS_API_KEY 无效或格式不被接受")
    if code == CODE_BAD_PARAM:
        return VerifyResult(
            status="error",
            message=f"接入渠道 softName 未注册或参数不匹配：{message or code}",
        )
    if code == CODE_NO_ROUTE:
        return VerifyResult(status="error", message=f"网关路由不可用：{message or code}")
    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"GS_API_KEY 被拒绝（HTTP {resp.status_code}）")
    detail = message or body_snippet(resp)
    return VerifyResult(
        status="error",
        message=f"无法确认 GS_API_KEY 状态（HTTP {resp.status_code}"
                + (f"，code {code}" if code else "") + f"）：{detail}",
    )


# ── 开放 API（AK/SK）──────────────────────────────────────

def _canonical_query(query: str) -> str:
    """按开放 API 规范生成 canonical_query_string：参数名/值 URL 编码后按 key 排序。"""
    pairs = parse_qsl(query or "", keep_blank_values=True)
    pairs.sort(key=lambda item: (item[0], item[1]))
    return urlencode(pairs)


def openapi_signature(
    method: str,
    path: str,
    query: str,
    access_key: str,
    secret_key: str,
    date_str: str,
    signed_headers: Optional[dict] = None,
) -> str:
    """按国信开放 API 文档生成 ``X-GS-API-SIGNATURE``（base64(HMAC-SHA256)）。

    ``signed_headers`` 仅用于与其他语言示例对齐；默认不参与签名。
    """
    canonical_query = _canonical_query(query)
    headers_string = ""
    if signed_headers:
        headers_string = "\n".join(f"{k}:{v}" for k, v in signed_headers.items()) + "\n"
    signing_string = "\n".join([
        method.upper(), path or "/", canonical_query,
        access_key, date_str, headers_string,
    ])
    digest = hmac.new(
        secret_key.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256
    ).digest()
    return b64encode(digest).decode("ascii")


def _openapi_headers(data: dict, method: str, verify_url: str) -> dict:
    parts = urlsplit(verify_url)
    access_key = str(data.get("access_key") or "").strip()
    secret_key = str(data.get("secret_key") or "").strip()
    date_str = formatdate(usegmt=True)
    headers = {
        OPENAPI_AK_HEADER: access_key,
        OPENAPI_DATE_HEADER: date_str,
        OPENAPI_ALGORITHM_HEADER: "hmac-sha256",
        OPENAPI_SIGNATURE_HEADER: openapi_signature(
            method, parts.path or "/", parts.query, access_key, secret_key, date_str,
        ),
    }
    for key, field in (
        ("X-GS-API-PROJECT", "route_project"),
        ("X-GS-API-CLUSTER", "route_cluster"),
        ("X-GS-API-ROUTER", "route_router"),
    ):
        value = str(data.get(field) or "").strip()
        if value:
            headers[key] = value
    return headers


def _verify_openapi(data: dict, access_key: str) -> VerifyResult:
    verify_url = str(data.get("verify_url") or "").strip()
    if not verify_url:
        return VerifyResult(
            status="error",
            message="开放 API 需要填写「验证接口 URL」才能在线校验："
                    "AK/SK 已保存，但没有已授权接口时无法判断有效性",
        )
    parts = urlsplit(verify_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return VerifyResult(status="error", message="验证接口 URL 不合法（必须是完整的 http(s) 地址）")

    log(f"校验国信开放 API AK {mask_secret(access_key)} → {parts.scheme}://{parts.netloc}{parts.path}")
    headers = _openapi_headers(data, "GET", verify_url)
    with _legacy_client() as client:
        resp = client.get(verify_url, headers=headers)

    if 200 <= resp.status_code < 300:
        payload = safe_json(resp)
        if payload is None and resp.content:
            return VerifyResult(
                status="error",
                message="验证接口返回的不是 JSON（可能填成了网页地址），未判定有效性",
            )
        code = str(payload.get("code", "")).strip() if isinstance(payload, dict) else ""
        if code in ("401", "403"):
            return VerifyResult(
                status="invalid",
                message=f"开放 API 凭证无效（响应 code {code}）",
            )
        return VerifyResult(
            status="valid",
            message=f"开放 API 凭证有效（验证接口返回 HTTP {resp.status_code}）",
            account_info={"http_status": resp.status_code},
        )
    if resp.status_code == 401:
        return VerifyResult(
            status="invalid",
            message=f"开放 API 凭证无效（HTTP 401）：{body_snippet(resp)}",
        )
    if resp.status_code == 403:
        return VerifyResult(
            status="error",
            message="无法确认（HTTP 403）：AK/SK 可能有效但无权访问该接口，或签名被网关拒绝",
        )
    if resp.status_code == 404:
        return VerifyResult(
            status="error",
            message="验证接口不存在（HTTP 404）：请确认 URL、项目与路由头",
        )
    return VerifyResult(
        status="error",
        message=f"无法确认开放 API 凭证状态（HTTP {resp.status_code}）：{body_snippet(resp)}",
    )


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    kind = _kind(data)
    if kind == KIND_SKILL:
        api_key = str(data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="GS_API_KEY 为空")
        return _verify_skill(data, api_key)

    access_key = str(data.get("access_key") or "").strip()
    if not access_key:
        return VerifyResult(status="invalid", message="Access Key（AK）为空")
    if not str(data.get("secret_key") or "").strip():
        return VerifyResult(status="invalid", message="Secret Key（SK）为空")
    return _verify_openapi(data, access_key)
