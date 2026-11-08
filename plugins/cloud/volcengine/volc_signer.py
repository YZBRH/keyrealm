# coding: utf-8
"""火山引擎（Volcano Engine，volcengineapi.com，OpenAPI 网关）SignerV4 签名实现。

按官方 Python SDK `volcengine/volc-sdk-python` 的 ``volcengine/auth/SignerV4.py`` 逐行对齐，
并由官方示例 ``example/DemoSignOnly.py`` 的固定向量校准（模块末尾自测可跑）。

本仓库未留存该 SDK 快照；重新校准步骤与结论摘要见 ``docs/SIGNER_CALIBRATION.md`` 第 4 节。

本模块**只负责产出** ``(method, url, headers, body)``，**不发任何请求** ——
实际发送必须由插件用 ``backend.plugin_sdk.make_client()`` 完成（只有它才带得上
当前凭据配置的代理）。签名器自己建 client 会绕过凭据级代理设置。

注意：这是火山引擎 **OpenAPI 网关** 的签名，与「火山方舟 Ark / 豆包大模型」的 API
（``ark.cn-beijing.volces.com``，把 API Key 直接放进 ``Authorization: Bearer``）
**完全不是一回事**，不要混用。

关键结论（已由官方 SDK 源码 + 实网响应双重钉死）：
  * ``Authorization`` 前缀是 ``HMAC-SHA256``（**不是** ``AWS4-HMAC-SHA256``）。
  * ``kDate = HMAC-SHA256(SK, date)`` 用的是**原始 SK**，不加任何 ``"AWS4"`` 前缀。
  * 头名精确为 ``X-Date`` / ``X-Content-Sha256``；``X-Security-Token`` 仅在带 STS
    临时凭证（session_token 非空）时才出现，纯 AK/SK 时**不存在**。
  * ``SignedHeaders`` 只收 ``Host`` / ``Content-Type`` / ``Content-Md5`` 以及所有
    ``X-`` 前缀头（小写），按字典序以 ``;`` 连接。GET 探针下即
    ``host;x-content-sha256;x-date``。
  * 凭证范围（Credential Scope）为 ``{yyyymmdd}/{region}/{service}/request``——
    地域只体现在这里，不进 query。所以「查哪个地域」是靠签名范围决定的。

参考文档：https://github.com/volcengine/volc-sdk-python/blob/main/volcengine/auth/SignerV4.py
参考文档：https://www.volcengine.com/docs/6491/1185994
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from urllib.parse import quote

__all__ = [
    "sign",
    "sign_probe",
    "interpret",
    "INVALID_CODES",
    "RESTRICTED_CODES",
    "DEFAULT_HOST",
    "DEFAULT_REGION",
    "DEFAULT_SERVICE",
    "ECS_API_VERSION",
    "IAM_HOST",
    "IAM_REGION",
    "IAM_SERVICE",
    "IAM_API_VERSION",
    "ALGORITHM",
]

DEFAULT_HOST = "open.volcengineapi.com"
DEFAULT_REGION = "cn-beijing"
DEFAULT_SERVICE = "ecs"
ECS_API_VERSION = "2020-04-01"

# IAM 是全局服务：主机独立、区域固定 cn-north-1（官方 SDK 里写死的 Credentials）。
IAM_HOST = "iam.volcengineapi.com"
IAM_REGION = "cn-north-1"
IAM_SERVICE = "iam"
IAM_API_VERSION = "2018-01-01"

ALGORITHM = "HMAC-SHA256"


# --------------------------------------------------------------------------- #
# 低层工具（逐行对齐 SDK 的 volcengine/util/Util.py）
# --------------------------------------------------------------------------- #
def _norm_uri(path: str) -> str:
    # Util.norm_uri
    return quote(path).replace("%2F", "/").replace("+", "%20")


def _norm_query(params: dict) -> str:
    # Util.norm_query：key 排序，quote(safe='-_.~')，'&' 连接，末尾 replace('+','%20')
    parts = []
    for key in sorted(params.keys()):
        val = params[key]
        if isinstance(val, list):
            for item in val:
                parts.append(f"{quote(str(key), safe='-_.~')}={quote(str(item), safe='-_.~')}")
        else:
            parts.append(f"{quote(str(key), safe='-_.~')}={quote(str(val), safe='-_.~')}")
    return "&".join(parts)


def _sha256_hex(content) -> str:
    # Util.sha256：str 走 utf-8，bytes 直接用，输出 hexdigest
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _hmac_sha256(key: bytes, content: str) -> bytes:
    # Util.hmac_sha256：key 为 bytes，content 为 str
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(sk: str, date: str, region: str, service: str) -> bytes:
    # SignerV4.get_signing_secret_key_v4：kDate 用原始 SK，无前缀
    k_date = _hmac_sha256(sk.encode("utf-8"), date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "request")


# --------------------------------------------------------------------------- #
# 通用签名器（对齐 SignerV4.sign / hashed_canonical_request_v4 / build_auth_header_v4）
# --------------------------------------------------------------------------- #
def _sign_request(
    ak: str,
    sk: str,
    *,
    method: str,
    host: str,
    path: str,
    query: dict,
    body: str,
    region: str,
    service: str,
    x_date: str,
    session_token: str = "",
):
    """返回 (method, url, headers, body)。headers 中已含 Authorization。

    完全复刻 SDK 的头收集规则与 CanonicalRequest 拼接。
    """
    if not path:
        path = "/"

    headers: dict[str, str] = {"Host": host, "X-Date": x_date}
    if method != "GET" and "Content-Type" not in headers:
        # SignerV4.sign 对非 GET 补齐默认 Content-Type
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    if session_token:
        headers["X-Security-Token"] = session_token

    body_hash = _sha256_hex(body)
    headers["X-Content-Sha256"] = body_hash  # hashed_canonical_request_v4 里写入

    # 参与签名的头：Host / Content-Type / Content-Md5 / 所有 X- 前缀头，键小写
    signed = {
        k.lower(): v
        for k, v in headers.items()
        if k in ("Content-Type", "Content-Md5", "Host") or k.startswith("X-")
    }
    # host 去掉 :80 / :443 端口
    if "host" in signed and ":" in signed["host"]:
        head, _, port = signed["host"].partition(":")
        if port in ("80", "443"):
            signed["host"] = head

    signed_str = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    signed_headers = ";".join(sorted(signed))

    # CanonicalRequest：注意 signed_str 自身以 '\n' 结尾，join 再补一个 '\n'
    # => 头块与 SignedHeaders 之间是空行（与 AWS SigV4 一致）
    canonical_request = "\n".join(
        [method, _norm_uri(path), _norm_query(query), signed_str, signed_headers, body_hash]
    )
    hashed_canon_req = _sha256_hex(canonical_request)

    credential_scope = "/".join([x_date[:8], region, service, "request"])
    string_to_sign = "\n".join([ALGORITHM, x_date, credential_scope, hashed_canon_req])
    signing_key = _signing_key(sk, x_date[:8], region, service)
    signature = _hmac_sha256(signing_key, string_to_sign).hex()

    headers["Authorization"] = (
        f"{ALGORITHM} Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    query_str = _norm_query(query)
    url = f"https://{host}{path}" + (f"?{query_str}" if query_str else "")
    return method, url, headers, body


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def sign(
    action: str,
    *,
    ak: str,
    sk: str,
    version: str,
    params: dict | None = None,
    region: str = DEFAULT_REGION,
    service: str = DEFAULT_SERVICE,
    host: str = DEFAULT_HOST,
    method: str = "GET",
    path: str = "/",
    body: str = "",
    x_date: str | None = None,
    session_token: str = "",
):
    """构造任意一个火山引擎 OpenAPI 请求，返回 ``(method, url, headers, body)``。

    ``Action`` / ``Version`` 由签名器统一拼进 query（``params`` 里不要重复给）；
    ``params`` 中值为 ``None`` 或空串的键会被丢弃——这样调用方可以直接把
    「用户没填」的表单字段原样传进来。

    ``session_token`` 用于 STS 临时凭证：非空时附带 ``X-Security-Token`` 头
    （它同样参与签名），长期 AK/SK 留空即可。

    ``x_date`` 仅供测试注入固定时间，正常运行留空（用当前 UTC 时间）。
    """
    query = {"Action": action, "Version": version}
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        query[key] = value
    return _sign_request(
        ak,
        sk,
        method=method.upper(),
        host=host,
        path=path or "/",
        query=query,
        body=body,
        region=region,
        service=service,
        x_date=x_date or _now_utc(),
        session_token=session_token or "",
    )


def sign_probe(ak: str, sk: str, *, region: str = DEFAULT_REGION, service: str = DEFAULT_SERVICE,
               host: str = DEFAULT_HOST, x_date: str | None = None,
               session_token: str = ""):
    """只读探针：ECS ``DescribeRegions``（无任何参数、不触碰任何资源）。"""
    return sign("DescribeRegions", ak=ak, sk=sk, version=ECS_API_VERSION,
                region=region, service=service, host=host, x_date=x_date,
                session_token=session_token)


# 判定为「凭据无效」的错误码
INVALID_CODES = {
    "InvalidAccessKey",       # 实网验证：AK 不存在/被禁用 -> 401, CodeN=100009
    "InvalidAccessKeyId",     # 别名（部分服务/文档表述）
    "SignatureDoesNotMatch",  # 签名不匹配（SK 错）-> 401, CodeN=100010
    "InvalidSignature",
    "MissingAuthenticationToken",  # 缺少鉴权信息 -> 401, CodeN=100003
    "InvalidCredential",      # 实网验证：Authorization 头缺失/格式错 -> 400, CodeN=100025
    "InvalidSecretToken",     # 实网验证：STS session_token 过期/无效 -> 401, CodeN=100026
}
# 判定为「凭据有效但受限」的错误码（权限不足 / 账号异常）
RESTRICTED_CODES = {
    "AccessDenied",           # 子用户无该操作权限 -> CodeN=100013
}


def interpret(status_code: int | None, body_text: str) -> dict:
    """据火山网关响应判定凭据状态。

    返回 ``{"verdict": "valid"|"invalid"|"restricted"|"error", "message": str}``。

    判定口径遵循框架统一规则：**只有明确「AK 不存在 / 签名校验失败 / STS 令牌过期」才判
    invalid**；「权限不足 / 账号被冻结」这类都是「凭据本身有效但受限」。未归类的错误码一律
    回 ``error``——**绝不猜成 invalid**，否则会把一把好密钥标成失效。
    """
    data = None
    try:
        data = json.loads(body_text)
    except (ValueError, TypeError):
        data = None

    error = {}
    if isinstance(data, dict):
        meta = data.get("ResponseMetadata")
        if isinstance(meta, dict) and isinstance(meta.get("Error"), dict):
            error = meta["Error"]

    code = error.get("Code")
    detail = error.get("Message") or ""
    code_n = error.get("CodeN")

    if not code:
        if status_code == 200 and data is not None:
            return {"verdict": "valid", "message": "鉴权通过（200 且无 Error）"}
        if data is None:
            return {"verdict": "error", "message": f"响应非 JSON（HTTP {status_code}），无法判定"}
        return {"verdict": "error", "message": f"HTTP {status_code} 且无 Error 字段，无法判定"}

    tag = f"{code}" + (f"(CodeN={code_n})" if code_n is not None else "")
    if code in INVALID_CODES:
        return {"verdict": "invalid", "message": f"凭据无效：{tag} {detail}".strip()}
    if code in RESTRICTED_CODES:
        return {"verdict": "restricted", "message": f"凭据有效但无权限：{tag} {detail}".strip()}
    return {"verdict": "error", "message": f"未归类错误：{tag} {detail}".strip()}


# --------------------------------------------------------------------------- #
# 自测：用官方 SDK 示例 DemoSignOnly.py 的固定 AK/SK 向量校准
# --------------------------------------------------------------------------- #
# 向量来源：volcengine/example/DemoSignOnly.py
#   ak=ak, sk=sk, service=iam, region=cn-north-1
#   date=datetime.utcfromtimestamp(1640712206) => 20211228T172326Z
#   query: Action=ListUsers, Version=2018-01-01, Limit=5, Offset=0
#   header_list: {Host: open.volcengineapi.com}
# 期望值由官方 SDK 1.0.228 真实运行得到。
EXPECTED_VECTOR = {
    "x_date": "20211228T172326Z",
    "x_content_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "authorization": (
        "HMAC-SHA256 Credential=ak/20211228/cn-north-1/iam/request, "
        "SignedHeaders=host;x-content-sha256;x-date, "
        "Signature=4f5b9d2b436092f11a41b5ad0d6eb6a868b30bd3955f480fa1d6bf1ed3073eba"
    ),
}


def _run_vector() -> bool:
    query = {
        "Action": "ListUsers",
        "Version": "2018-01-01",
        "Limit": "5",
        "Offset": "0",
    }
    method, url, headers, body = _sign_request(
        "ak",
        "sk",
        method="GET",
        host=DEFAULT_HOST,
        path="/",
        query=query,
        body="",
        region="cn-north-1",
        service="iam",
        x_date="20211228T172326Z",
    )
    print("  method  :", method)
    print("  url     :", url)
    print("  X-Date  :", headers.get("X-Date"))
    print("  X-Content-Sha256:", headers.get("X-Content-Sha256"))
    print("  Authorization:", headers.get("Authorization"))

    ok = True
    checks = [
        ("X-Date", headers.get("X-Date"), EXPECTED_VECTOR["x_date"]),
        ("X-Content-Sha256", headers.get("X-Content-Sha256"), EXPECTED_VECTOR["x_content_sha256"]),
        ("Authorization", headers.get("Authorization"), EXPECTED_VECTOR["authorization"]),
    ]
    for name, got, want in checks:
        same = got == want
        ok = ok and same
        print(f"  [{'PASS' if same else 'FAIL'}] {name}")
        if not same:
            print("        got :", got)
            print("        want:", want)
    return ok


def _run_interpret_demo() -> None:
    samples = [
        (401, '{"ResponseMetadata":{"Error":{"CodeN":100009,"Code":"InvalidAccessKey",'
              '"Message":"The security token[xxx] included in the request is invalid."}}}'),
        (401, '{"ResponseMetadata":{"Error":{"CodeN":100010,"Code":"SignatureDoesNotMatch",'
              '"Message":"The request signature we calculated does not match..."}}}'),
        (400, '{"ResponseMetadata":{"Error":{"CodeN":100025,"Code":"InvalidCredential",'
              '"Message":"Invalid credential in \'Authorization\'..."}}}'),
        (403, '{"ResponseMetadata":{"Error":{"CodeN":100013,"Code":"AccessDenied",'
              '"Message":"User is not authorized to perform..."}}}'),
        (200, '{"ResponseMetadata":{"RequestId":"x","Action":"DescribeRegions"},'
              '"Result":{"Regions":[]}}'),
        (502, "<html>bad gateway</html>"),
    ]
    print("\ninterpret() 自测：")
    for status, body in samples:
        r = interpret(status, body)
        print(f"  HTTP {status} -> {r['verdict']:<10} | {r['message']}")


if __name__ == "__main__":
    print("== 固定向量校准（官方 SDK DemoSignOnly.py，SDK 1.0.228 实跑值）==")
    ok = _run_vector()
    _run_interpret_demo()
    print("\n向量校验结果：", "PASS" if ok else "FAIL")
