"""华为云 AK/SK 插件（plugins/cloud/ 类别下的独立子插件）。

**两套签名都在本文件内维护**（华为云的云 API 与对象存储用的不是同一套算法）：

- ``SDK-HMAC-SHA256`` —— IAM / STS / ECS 等云 API 用这套。注意它的 CanonicalURI
  **必须以 ``/`` 结尾**（路径不含尾斜杠时签名前要补上），这是与 AWS SigV4 最容易搞混的地方；
- ``AWS Signature Version 4`` —— 对象存储（OBS）用这套，OBS 兼容 S3，行为遵循 AWS 规范
  （CanonicalURI **不补**尾斜杠，CanonicalQueryString 对参数名与值都做 ``%XY`` 编码）。

全部调用均为**只读**，不产生任何资源变更（不签发临时凭证、不创建/删除任何东西）。

本插件提供的操作（``actions``）：

- ``whoami``           查看调用者身份（STS GetCallerIdentity + OBS 属主 + 只读权限探测）
- ``list_regions``     区域列表（内置静态表；行内「实例」下钻到该区域的云主机）
- ``list_instances``   云主机实例列表（ECS；区域可填，留空则并发枚举全部区域）
- ``list_buckets``     对象存储桶列表（OBS ListBuckets，任意区域 endpoint 都返回全部区域的桶）
- ``list_objects``     对象预览（OBS ListObjectsV2 + delimiter，文件与文件夹分行）
- ``download_object``  下载单个对象（``save_to``，前端弹「另存为」）
- ``download_prefix``  把某个前缀（文件夹）下的对象并发拉取后打包成 zip 下载

关于 ECS 的 ``project_id``：华为的 ECS 接口形如 ``/v1/{project_id}/cloudservers/detail``，
没有 project_id 就查不了。凭据里可以手填；留空时插件会**只读**调 IAM 的
``GET /v3/auth/projects`` 拿当前账号可访问的项目列表来推断（无副作用）。
（``POST /v3.0/OS-CREDENTIAL/securitytokens`` 不能直接用 AK/SK 换临时凭证 —— 公有云对
``methods:["hw_ak_sk"]`` 返回 ``Unsupported method``，故不作回落路径。）

两个下载操作都声明了 ``progress``，由 ``stream_action`` 边下边回调进度、并响应取消。

另实现可选的 ``capabilities(data)``：点「操作」前只读探测 STS / OBS / ECS，
只列出这组 AK/SK **真正能用**的操作，并附一句中文说明。**探测拿不准一律 fail-open**
（按可用处理），只有接口明确说「没权限 / 密钥被拒」才隐藏。

参考文档：https://support.huaweicloud.com/devg-apisign/api-sign-algorithm.html
参考文档：https://support.huaweicloud.com/api-obs/obs_04_0020.html
参考文档：https://support.huaweicloud.com/intl/zh-cn/api-iam5/GetCallerIdentity.html
参考文档：https://support.huaweicloud.com/usermanual-iam5/iam_01_1334.html
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
from urllib.parse import parse_qsl, quote, urlparse

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    download_to_file,
    guarded,
    log,
    make_client,
    mask_secret,
)

NAME = "华为云"
DEFAULT_REGION = "ap-southeast-3"

# ── 两套签名的标识 ───────────────────────────────────────
OBS_SERVICE = "s3"                      # OBS 走 AWS SigV4，service 固定 s3
ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()
SDK_ALGORITHM = "SDK-HMAC-SHA256"       # 云 API（IAM/STS/ECS）的签名算法

# IAM 是**全局**端点（不分区）；STS 与 ECS 是区域级端点，见各自函数
IAM_ENDPOINT = "https://iam.myhuaweicloud.com"

# 对象预览每页条数；枚举（打包下载）时按 OBS 单页上限翻页
_PAGE_SIZE = 50
_ENUM_PAGE = 1000

# 全域扫描（区域留空时逐区域取实例）的并发线程数
_SWEEP_WORKERS = 8

# 打包下载上限：**宁可明确报错，也不静默截断**（前端会原样展示 error/output）
MAX_ZIP_OBJECTS = 1000                # 单次最多打包的对象数
MAX_ZIP_BYTES = 512 * 1024 * 1024     # 单次最多打包的原始总字节数（512 MiB）
_ZIP_WORKERS = 8                      # 并发拉取对象的线程数

# 下载进度上报的节流：每写入这么多字节、或隔这么久才回调一次
_PROGRESS_STEP = 256 * 1024           # 256 KiB
_PROGRESS_INTERVAL = 0.2              # 秒

# 权限探测 / 项目查询这类「要很快结束」的请求的超时（秒）
_PROBE_TIMEOUT = 6.0

REQUIRED = (("access_key_id", "Access Key ID (AK)"), ("secret_access_key", "Secret Access Key (SK)"))
DISPLAY_FIELD = "access_key_id"

# OBS 明确的「AK/SK 不成立」错误码（其余一律按「无法确认」处理）
INVALID_CODES = {"SignatureDoesNotMatch", "InvalidAccessKeyId", "AccessDeniedException"}
# OBS 明确的「AK/SK 成立、但没这个权限」错误码（与上一条严格区分：那是密钥本身废了）
OBS_DENIED_CODES = {"AccessDenied", "UserDisable", "Forbidden"}

# 权限探测的四种结论。为什么要分四态而不是布尔：``capabilities`` 必须区分
# 「明确没权限」（隐藏该操作）与「拿不准」（fail-open，照常展示），布尔做不到。
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# OBS 桶名规则：小写字母/数字/``-``/``.``，字母或数字开头结尾，3~63 位
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
# 端点白名单式校验：避免把用户/接口给的字符串直接拼进 URL
_HOST_RE = re.compile(r"^[a-z0-9.\-]+$")

# ── 静态区域表 ──────────────────────────────────────────
# 华为没有「AK/SK 可调用」的区域列表接口：IAM 的 KeystoneListRegions 要 X-Auth-Token，
# 而 AK/SK 能直调的 ``/v3/auth/projects`` 只返回**该账号实际开了项目**的区域，
# 拿它当「区域全集」会漏掉没开项目的区域。所以这里内置一张按官方文档整理的静态表。
# 取值来源见模块 docstring 的区域对照表页面。
OBS_REGIONS = [
    ("cn-north-1", "华北-北京一"),
    ("cn-north-4", "华北-北京四"),
    ("cn-north-9", "华北-乌兰察布一"),
    ("cn-north-11", "华北-乌兰察布-汽车一"),
    ("cn-north-12", "华北三"),
    ("cn-east-2", "华东-上海二"),
    ("cn-east-3", "华东-上海一"),
    ("cn-east-4", "华东二"),
    ("cn-east-5", "华东-青岛"),
    ("cn-south-1", "华南-广州"),
    ("cn-south-4", "华南-广州-友好用户环境"),
    ("cn-southwest-2", "西南-贵阳一"),
    ("ap-southeast-1", "中国-香港"),
    ("ap-southeast-2", "亚太-曼谷"),
    ("ap-southeast-3", "亚太-新加坡"),
    ("ap-southeast-4", "亚太-雅加达"),
    ("ap-southeast-5", "亚太-马尼拉"),
    ("af-south-1", "非洲-约翰内斯堡"),
    ("af-north-1", "非洲-开罗"),
    ("la-north-2", "拉美-墨西哥城二"),
    ("la-south-2", "拉美-圣地亚哥"),
    ("na-mexico-1", "拉美-墨西哥城一"),
    ("sa-brazil-1", "拉美-圣保罗一"),
    ("me-east-1", "中东-利雅得"),
    ("tr-west-1", "土耳其-伊斯坦布尔"),
]
def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


# ── AWS Signature Version 4（OBS 兼容）─────────────────────

def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _obs_signing_key(secret_key: str, datestamp: str, region: str) -> bytes:
    key = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _sign(key, region)
    key = _sign(key, OBS_SERVICE)
    return _sign(key, "aws4_request")


def _obs_canonical_query(query: dict | None) -> str:
    """AWS 规范的查询串：参数名与值都做 ``%XY`` 编码（``/`` 编成 ``%2F``），按名升序。

    服务端会把收到的原始查询串按同一规则重新编码后比对，所以这里用 ``safe='-_.~'``
    而实际发送时 httpx 保留 ``/`` 原样，两边算出来仍然一致。
    """
    if not query:
        return ""
    return "&".join(
        f"{quote(str(key), safe='-_.~')}={quote(str(value), safe='-_.~')}"
        for key, value in sorted(query.items())
    )


def _obs_headers(
    method: str, host: str, path: str, query: dict | None, region: str, access_key: str, secret_key: str
) -> dict:
    """生成一次 OBS 请求的签名头（头名按字母序，与 SignedHeaders 一致）。

    ``path`` 必须是**已百分号编码**的资源路径（对象 key 由调用方 ``quote(key, safe='/~')``）。
    与云 API 的 SDK 签名不同，OBS 的 CanonicalURI **不补尾斜杠**。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")

    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{EMPTY_PAYLOAD_HASH}\n"
        f"x-amz-date:{amzdate}\n"
    )
    canonical_request = "\n".join([
        method,
        path or "/",
        _obs_canonical_query(query),
        canonical_headers,
        signed_headers,
        EMPTY_PAYLOAD_HASH,
    ])

    scope = f"{datestamp}/{region}/{OBS_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        ALGORITHM, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    ])
    signature = hmac.new(
        _obs_signing_key(secret_key, datestamp, region), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "x-amz-date": amzdate,
        "x-amz-content-sha256": EMPTY_PAYLOAD_HASH,
        "Authorization": (
            f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def _obs_get(
    client,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    params: dict | None = None,
    timeout: float | None = None,
):
    """发起一次 OBS SigV4 签名的只读 GET。"""
    parsed = urlparse(url)
    headers = _obs_headers(
        "GET", parsed.netloc, parsed.path or "/", params, region, access_key, secret_key
    )
    kwargs: dict[str, Any] = {"params": params, "headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(url, **kwargs)


# ── SDK-HMAC-SHA256（IAM / STS / ECS 用）──────────────────

def _sdk_canonical_uri(path: str) -> str:
    """云 API 的 CanonicalURI：**路径不以 ``/`` 结尾时要补上**。

    这是华为云 SDK 签名最容易出错的地方，且是服务端行为：签名漏了尾斜杠会直接
    401 ``verify ak sk signature fail``，回包里的 canonical_request 却带着尾斜杠。
    """
    if not path:
        return "/"
    if not path.endswith("/"):
        path += "/"
    return quote(path, safe="/-_.~")


def _sdk_canonical_query(query: str) -> str:
    """云 API 的 CanonicalQueryString：按参数名升序、名与值都做 ``%XY`` 编码。"""
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(
        f"{quote(str(key), safe='-_.~')}={quote(str(value), safe='-_.~')}" for key, value in pairs
    )


def _sdk_headers(
    method: str, url: str, body: bytes, access_key: str, secret_key: str, extra_signed: dict | None = None
) -> dict:
    """生成一次云 API 请求的签名头（SDK-HMAC-SHA256）。

    ``extra_signed`` 里放需要**参与签名**的额外请求头（如 ``Content-Type``）；
    不参与签名的头（如 ``X-Project-Id``）直接由调用方另加，不要塞进来。
    """
    parsed = urlparse(url)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    signed: dict[str, str] = {"host": parsed.netloc, "x-sdk-date": now}
    for name, value in (extra_signed or {}).items():
        signed[name.lower()] = str(value)
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name].strip()}\n" for name in names)

    canonical_request = "\n".join([
        method,
        _sdk_canonical_uri(parsed.path),
        _sdk_canonical_query(parsed.query),
        canonical_headers,
        ";".join(names),
        hashlib.sha256(body).hexdigest(),
    ])
    string_to_sign = "\n".join([
        SDK_ALGORITHM, now, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    ])
    signature = hmac.new(
        secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers = {name: value for name, value in signed.items() if name != "host"}
    headers["Authorization"] = (
        f"{SDK_ALGORITHM} Access={access_key}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return headers


def _sdk_call(
    client,
    method: str,
    url: str,
    access_key: str,
    secret_key: str,
    body: bytes | None = None,
    extra_signed: dict | None = None,
    timeout: float | None = None,
):
    """发起一次 SDK-HMAC-SHA256 签名的请求（本插件只用到只读的 GET）。"""
    payload = body or b""
    headers = _sdk_headers(method, url, payload, access_key, secret_key, extra_signed)
    kwargs: dict[str, Any] = {"headers": headers}
    if payload:
        kwargs["content"] = payload
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.request(method, url, **kwargs)


# ── XML 解析（OBS 返回 XML；用本地名匹配，绕开 namespace 差异）──

def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _xml_root(text: str):
    try:
        return ET.fromstring(text)
    except Exception:  # noqa: BLE001 - 非 XML 响应（网关错误页等）按「解析不出」处理
        return None


def _xml_findall(text_or_elem, local_name: str) -> list:
    """返回所有本地名匹配的后代元素（namespace 无关）。"""
    root = text_or_elem
    if isinstance(root, str):
        root = _xml_root(root)
    if root is None:
        return []
    return [elem for elem in root.iter() if _tag(elem) == local_name]


def _xml_text(elem, local_name: str) -> str:
    """取某个后代元素的文本（第一个）。"""
    if elem is None:
        return ""
    for child in elem.iter():
        if _tag(child) == local_name:
            return (child.text or "").strip()
    return ""


def _xml_error(text: str) -> tuple[str, str]:
    """OBS 错误响应 <Error><Code/><Message/></Error> → (code, message)。"""
    root = _xml_root(text)
    if root is None:
        return "", ""
    return _xml_text(root, "Code"), _xml_text(root, "Message")


def _parse_buckets(text: str) -> dict:
    """解析 ListAllMyBucketsResult；解析失败返回空 dict（由调用方走兜底分支）。"""
    root = _xml_root(text)
    if root is None:
        return {}
    owner: dict[str, Any] = {}
    buckets: list[str] = []
    for node in root.iter():
        name = _tag(node)
        if name == "ID" and node.text and node.text.strip() and "domain_id" not in owner:
            owner["domain_id"] = node.text.strip()
        elif name == "DisplayName" and node.text and node.text.strip():
            owner["iam_user"] = node.text.strip()
        elif name == "Name" and node.text and node.text.strip():
            buckets.append(node.text.strip())
    if owner or buckets:
        owner["buckets"] = buckets
    return owner


# ── 只读权限探测（capabilities 与 whoami 共用；一律不抛出）──

def _obs_probe_buckets(client, ak: str, sk: str, region: str) -> str:
    """探测 OBS ListBuckets（只读），返回四态结论；异常一律 unknown（不抛出）。"""
    try:
        resp = _obs_get(client, f"https://obs.{region}.myhuaweicloud.com/", region, ak, sk,
                        timeout=_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 - 探测失败不能让整个 capabilities 崩
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    code, _ = _xml_error(resp.text)
    if code in INVALID_CODES:
        return _PROBE_BAD_CRED
    if code in OBS_DENIED_CODES:
        return _PROBE_DENIED
    if resp.status_code == 403 and not code:
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _sts_probe(client, ak: str, sk: str, region: str) -> str:
    """探测 STS 身份接口（只读，``whoami`` 用的就是它），返回四态结论。

    403 在这里**不当作「没权限」**：存在「IAM 用户在该区域被禁止」这类**区域级**限制，
    那是区域选得不对，不是密钥缺权限，按 unknown 放行（fail-open）。
    """
    try:
        resp = _sdk_call(
            client, "GET", f"https://sts.{region}.myhuaweicloud.com/v5/caller-identity", ak, sk,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    if resp.status_code == 401:
        return _PROBE_BAD_CRED     # SDK-HMAC 签名被拒 ⇒ 密钥本身不成立
    return _PROBE_UNKNOWN


def _ecs_probe_one(client, ak: str, sk: str, region: str, project_id: str) -> str:
    """在**单个**区域探测 ECS 云主机列表（只读），返回四态结论。"""
    if not project_id:
        # 没有 project_id 就拼不出接口路径 —— 这是「探测不了」，不是「没权限」
        return _PROBE_UNKNOWN
    try:
        resp = _sdk_call(
            client,
            "GET",
            f"https://ecs.{region}.myhuaweicloud.com/v1/{project_id}/cloudservers/detail",
            ak,
            sk,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    if resp.status_code == 401:
        return _PROBE_BAD_CRED     # 签名被拒 ⇒ 换区域也没用
    if resp.status_code == 403:
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _ecs_candidates(client, ak: str, sk: str, region: str, project_id: str):
    """按「配置区域优先、必要时再换一个区域复核」的顺序产出 ``(区域, project_id)``。

    生成器是**惰性**的：只有第一个候选被拒、真需要复核时才会去查 ``/v3/auth/projects``
    （多花一次只读请求）。为什么要复核：同一组 AK 扫全域时，个别区域会回 403
    ``The IAM user is forbidden in the currently selected region``（区域级限制），
    只看一个区域就断言「没有云主机权限」，会把本来能用的操作**错误地藏掉**。
    """
    projects: dict[str, str] | None = None
    if project_id:
        yield region, project_id        # 用户手填的 project_id 只对配置区域成立
        first_region = region
    else:
        projects, _error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)
        if not projects:
            return                      # 查不到项目 ⇒ 没有候选，结论留在 unknown（fail-open）
        first_region = region if region in projects else sorted(projects)[0]
        yield first_region, projects[first_region]

    if projects is None:                # 复核用的项目列表：只有真要复核时才会走到这里
        projects, _error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)
    for other in sorted(projects):
        if other != first_region:
            yield other, projects[other]
            return


def _ecs_probe(client, ak: str, sk: str, region: str, project_id: str) -> str:
    """探测 ECS 云主机列表（只读）：被拒时换一个区域复核，都拒才算真没权限。"""
    verdict = _PROBE_UNKNOWN
    seen = 0
    for candidate_region, candidate_project in _ecs_candidates(client, ak, sk, region, project_id):
        seen += 1
        verdict = _ecs_probe_one(client, ak, sk, candidate_region, candidate_project)
        if verdict != _PROBE_DENIED:
            return verdict              # OK / 密钥无效 / 拿不准：都不必再换区域
    # 一个区域被拒、却换不出第二个区域来复核（查不到项目列表）⇒ 只能算「拿不准」。
    # 宁可在菜单里多留一个用不了的操作，也不能凭单个区域就把它藏掉。
    return verdict if seen > 1 else _PROBE_UNKNOWN


def _probe_obs(client, ak: str, sk: str, region: str) -> bool:
    """``whoami`` 用的布尔探针（只读），异常一律视为「没探到」。"""
    return _obs_probe_buckets(client, ak, sk, region) == _PROBE_OK


def _probe_ecs(client, ak: str, sk: str, region: str, project_id: str) -> bool:
    """``whoami`` 用的布尔探针（只读），异常一律视为「没探到」。"""
    return _ecs_probe_one(client, ak, sk, region, project_id) == _PROBE_OK


# ── IAM：账号可访问的项目（无副作用，AK/SK 可直调）──────────

def _list_projects(client, ak: str, sk: str, timeout: float | None = None) -> tuple[dict[str, str], str]:
    """``GET /v3/auth/projects`` → ``({region: project_id}, error)``。

    这是 AK/SK **能直接调用**的只读接口（不像 KeystoneListRegions 要 X-Auth-Token），
    回包里每个 project 的 ``name`` 就是区域 ID，``id`` 就是该区域的 project_id。
    """
    url = f"{IAM_ENDPOINT}/v3/auth/projects"
    try:
        resp = _sdk_call(client, "GET", url, ak, sk, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探测失败要能降级，不能把异常抛给前端
        return {}, f"请求项目列表失败：{type(exc).__name__}"
    if resp.status_code != 200:
        return {}, f"请求项目列表失败 (HTTP {resp.status_code})"
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return {}, "项目列表响应不是 JSON"
    mapping: dict[str, str] = {}
    for item in payload.get("projects") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        project_id = str(item.get("id") or "").strip()
        # 只收形如区域 ID 的项目名：华为的 project 名还会出现 MOS 这类非区域项
        if name and project_id and _REGION_RE.match(name):
            mapping[name] = project_id
    return mapping, ""


# ── 并发 ────────────────────────────────────────────────

def _sweep(items: list, worker) -> list[tuple[Any, Any, str]]:
    """并发对每个 item 调一次 ``worker(item)``，按**原顺序**返回 ``(item, 结果, 异常名)``。

    两个必须点：

    - ``client`` 由**父线程**创建后用闭包共享进来：代理是线程隔离的
      （``vault_proxy_ctx`` 用 ``threading.local``），worker 里各自 ``make_client()``
      拿不到本次凭据的代理。httpx client 本身线程安全，共享即可。
    - 单个 item 抛异常（Throttling / 无权限 / 网络抖动）**只记一笔**，绝不毁掉整批。

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
            except Exception as exc:  # noqa: BLE001 - 单个区域失败不毁整张表
                ordered[index] = (items[index], None, type(exc).__name__)
    return ordered


# ── 权限探测汇总（capabilities）───────────────────────────

def _probe_all(ak: str, sk: str, region: str, project_id: str) -> dict:
    """依次只读探测 OBS / STS / ECS，返回 ``{action_id: 四态结论}``。

    **串行**执行：共用同一个 client，串行能让最坏耗时（超时上限）可预期，
    也避免为「点开操作菜单」这一次探测打出并发请求。

    OBS 先探：它是「密钥是否成立」最早的定论点。若它明确说密钥废了，
    STS/ECS 就不必再问（省掉后面最多 3 次请求），结论上也不会变
    （``capabilities`` 见 ``list_buckets`` 为 bad_cred 即整体判废）。
    """
    status: dict[str, str] = {
        "whoami": _PROBE_UNKNOWN,
        "list_regions": _PROBE_UNKNOWN,
        "list_instances": _PROBE_UNKNOWN,
        "list_buckets": _PROBE_UNKNOWN,
        "list_objects": _PROBE_UNKNOWN,
        "download_object": _PROBE_UNKNOWN,
        "download_prefix": _PROBE_UNKNOWN,
    }
    with make_client() as client:
        buckets = _obs_probe_buckets(client, ak, sk, region)
        status["list_buckets"] = buckets
        # 对象预览 / 单对象下载 / 打包下载按与桶列表同一个 OBS 权限位处理
        # （近似：OBS 的 ListBucket 与 GetObject 其实可分授，这里不细分——
        #  分不开也只是「多显示一个用不了的操作」，比误藏要轻）
        status["list_objects"] = buckets
        status["download_object"] = buckets
        status["download_prefix"] = buckets

        if buckets != _PROBE_BAD_CRED:
            # 区域表是静态的、不联网，只受「账号本身是否成立」影响，故与 whoami 同结论
            status["whoami"] = status["list_regions"] = _sts_probe(client, ak, sk, region)
            status["list_instances"] = _ecs_probe(client, ak, sk, region, project_id)
    return status


def capabilities(data: dict) -> dict:
    """返回当前 AK/SK **真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    探测 STS / OBS / ECS 三个只读接口，各自独立 try：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 密钥被拒」→ 隐藏该操作；
    - 网络异常、响应看不懂、或拿不到 project_id 这类**探测不了**的情况 → ``unknown``，
      按可用处理（fail-open）。

    若全部接口都拿不准，则直接抛异常，交给框架按「未知 → 全部可用」兜底。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 Access Key ID 或 Secret Access Key"}

    region = _field(data, "region").lower() or DEFAULT_REGION
    if not _HOST_RE.match(region) or not _REGION_RE.match(region):
        # 区域会被拼进 URL，格式不对就不能往接口上打（存库前 validate 已拦过一道，
        # 这里防的是手改过的老记录）；拿不准 → 抛出去交给框架按「未知 → 全部可用」兜底
        raise RuntimeError(f"区域格式不合法：{region}")
    status = _probe_all(ak, sk, region, _field(data, "project_id"))

    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定 —— 让框架按「未知」处理，绝不返回空列表把操作全隐藏
        raise RuntimeError("华为云权限探测全部失败（网络异常或响应无法识别）")

    if _PROBE_BAD_CRED in (status.get("whoami"), status.get("list_buckets")):
        # 身份接口 / OBS 都明确拒了这对 AK —— 同一对密钥在别处必然也被拒，
        # 此时剩下几个探针的 unknown 不是「拿不准」而是「密钥已废」，没必要假装可用。
        return {"available": [], "note": "AK/SK 无效或已禁用，任何操作都会失败"}

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


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    region = str(data.get("region") or "").strip()
    if region and not _REGION_RE.match(region):
        errors.append("OBS 区域格式不正确（形如 ap-southeast-3）")
    return errors


def _verify(access_key: str, secret_key: str, region: str) -> VerifyResult:
    endpoint = f"https://obs.{region}.myhuaweicloud.com"
    with make_client() as client:
        resp = _obs_get(client, endpoint + "/", region, access_key, secret_key)

    if resp.status_code == 200:
        info = _parse_buckets(resp.text)
        buckets = info.get("buckets") or []
        parts = [f"AK/SK 有效（{region}）"]
        if info.get("iam_user"):
            parts.append(f"IAM 用户 {info['iam_user']}")
        parts.append(f"{len(buckets)} 个桶")
        # account_info 只留账号：其余身份信息归 whoami 操作，列表里不必抢占展示位
        account_id = info.get("domain_id")
        return VerifyResult(
            status="valid",
            message="，".join(parts),
            account_info={"account_id": account_id} if account_id else None,
        )

    code, _ = _xml_error(resp.text)
    if code in INVALID_CODES:
        return VerifyResult(status="invalid", message=f"AK/SK 无效（{code}）")
    if code == "AccessDenied":
        # 签名已通过、只是该子用户无列举桶权限 ⇒ AK/SK 本身有效
        return VerifyResult(status="valid", message="AK/SK 有效，但无列举桶权限（AccessDenied）")
    detail = code or body_snippet(resp)
    hint = "（若 AK/SK 无误，请确认「OBS 区域」是否是该账号开通的区域）"
    return VerifyResult(
        status="error",
        message=f"无法确认 AK/SK 状态 (HTTP {resp.status_code})：{detail} {hint}" if detail
        else f"无法确认 AK/SK 状态 (HTTP {resp.status_code}) {hint}",
    )


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    access_key = str(data["access_key_id"]).strip()
    secret_key = str(data["secret_access_key"]).strip()
    region = str(data.get("region") or "").strip() or DEFAULT_REGION
    log(f"校验华为云凭据 {mask_secret(access_key)}（OBS {region}）")
    return _verify(access_key, secret_key, region)


# ── 操作 ────────────────────────────────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _obs_failure(resp, ak: str, sk: str) -> str:
    """把 OBS 失败响应（XML）转成一句脱敏后的错误信息。"""
    code, message = _xml_error(resp.text)
    detail = message or code or f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


def _sdk_failure(resp, ak: str, sk: str) -> str:
    """把云 API 失败响应转成一句脱敏后的错误信息（华为的错误体形状不固定，做兜底）。"""
    detail = ""
    try:
        payload = resp.json()
        node = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(node, dict):
            detail = str(node.get("message") or node.get("code") or "")
        elif isinstance(payload, dict):
            detail = str(payload.get("error_msg") or payload.get("message") or "")
    except Exception:  # noqa: BLE001 - 非 JSON 就用截断的原文
        detail = ""
    detail = detail or body_snippet(resp)
    return f"请求失败 (HTTP {resp.status_code})：{_redact(detail, ak, sk)}"


_WHOAMI_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

_REGION_COLUMNS = [
    {"key": "RegionId", "label": "区域 ID", "mono": True},
    {"key": "LocalName", "label": "区域名称"},
    # 该账号在这个区域有没有项目；没有就下钻不了「实例」，也填不进 project_id
    {"key": "ProjectId", "label": "项目 ID (project_id)", "mono": True},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    # 「区域留空 = 枚举全部区域」时一张表里混着多个区域的实例，必须能一眼看出各在哪
    {"key": "Region", "label": "区域", "width": 120},
    {"key": "InstanceName", "label": "实例名"},
    {"key": "Status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "PublicIp", "label": "公网 IP", "width": 130, "mono": True},
    {"key": "InstanceType", "label": "规格"},
]

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "region", "label": "区域", "width": 120},
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
    "params": {"region": "$row.RegionId"},
}

# 桶列表双击「预览」下钻到 list_objects
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
        "params": {"bucket": "$row.bucket", "region": "$row.region", "prefix": "$row.folderPath"},
    },
    {
        "id": "download_object",
        "label": "下载",
        "params": {"bucket": "$row.bucket", "region": "$row.region", "key": "$row.fileKey"},
    },
    {
        "id": "download_prefix",
        "label": "打包下载",
        "params": {"bucket": "$row.bucket", "region": "$row.region", "prefix": "$row.folderPath"},
    },
]

_BUCKET_FIELD = {"key": "bucket", "label": "桶名", "placeholder": "如 my-bucket"}
_REGION_FIELD = {
    "key": "region",
    "label": "区域（bucket 所在区域）",
    "placeholder": f"默认 {DEFAULT_REGION}，如 cn-north-4",
    "hint": "OBS 的对象操作只在桶所在区域生效，填错会报「桶不存在」。",
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的身份 / 区域 / 实例 / 对象存储查询）。"""
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
            "description": "STS GetCallerIdentity 身份信息 + OBS 属主 + 只读权限探测",
            "columns": _WHOAMI_COLUMNS,
            "empty_text": "未获取到身份信息",
        },
        {
            "id": "list_regions",
            "name": "区域列表",
            "kind": "list",
            "group": "计算",
            "order": 15,
            "description": (
                "内置区域表（华为没有 AK/SK 可调的区域列表接口）；"
                "「项目 ID」列来自该账号的 IAM 项目，有值才能行内下钻「实例」"
            ),
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
                f"ECS 查询云服务器详情列表（区域默认 {DEFAULT_REGION}，可直接填，"
                "或从「区域列表」行内进入；**区域留空则并发枚举该账号全部区域**）。"
                "查 ECS 必须有 project_id：凭据里填了就直用，没填则只读查询 IAM 项目列表自动推断"
            ),
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「刷新」时并入参数
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "empty_text": f"暂无云主机实例（默认区域 {DEFAULT_REGION}）",
        },
        {
            "id": "list_buckets",
            "name": "对象存储桶列表",
            "kind": "list",
            "group": "存储",
            "order": 30,
            "description": (
                "OBS ListBuckets（只读；任何区域的 Endpoint 都返回全部区域的桶，无需先选区域）。"
                "区域列由逐桶的 GetBucketLocation 补齐"
            ),
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
            "description": "OBS ListObjectsV2（按 / 分隔列出文件与文件夹；双击可「进入」，右键可「下载」「打包下载」）",
            "columns": _OBJECT_COLUMNS,
            "fields": [
                _BUCKET_FIELD,
                # 非必填：留空 = 从桶根目录开始列（与阿里云同款）
                {"key": "prefix", "label": "前缀", "placeholder": "留空为桶根目录，如 dir/"},
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
                {"key": "prefix", "label": "前缀（文件夹）", "placeholder": "如 dir/sub/", "required": True},
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
        if not ak or not sk:
            return {"ok": False, "error": "Access Key ID 或 Secret Access Key 为空"}
        region = _field(data, "region") or DEFAULT_REGION
        project_id = _field(data, "project_id")

        if action_id == "whoami":
            return _run_whoami(ak, sk, region, project_id)
        if action_id == "list_regions":
            return _run_list_regions(ak, sk)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, params, region, project_id)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk, region)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, params, region)
        if action_id == "download_object":
            return _run_download_object(ak, sk, params, region, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(ak, sk, params, region, progress, should_cancel)
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
    """下载类操作的流式执行：边下边上报进度、并响应取消。其它操作回退到 run_action。"""
    if action_id in ("download_object", "download_prefix"):
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


def _run_whoami(ak: str, sk: str, region: str, project_id: str) -> dict:
    """身份信息 + 权限探测（各接口独立只读调用）。"""
    with make_client() as client:
        resp = _sdk_call(
            client, "GET", f"https://sts.{region}.myhuaweicloud.com/v5/caller-identity", ak, sk
        )
        if resp.status_code != 200:
            return {"ok": False, "error": _sdk_failure(resp, ak, sk)}
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = {}
        urn = str(payload.get("principal_urn") or "")
        rows = [
            {"item": "账号 ID", "value": payload.get("account_id") or "—"},
            {"item": "主体 URN", "value": urn or "—"},
            {"item": "IAM 用户名", "value": urn.rsplit(":", 1)[-1] if urn else "—"},
            {"item": "主体 ID", "value": payload.get("principal_id") or "—"},
        ]

        # OBS 属主：与 STS 的身份互为印证，也能看出这组 AK 归谁
        try:
            obs = _obs_get(client, f"https://obs.{region}.myhuaweicloud.com/", region, ak, sk,
                           timeout=_PROBE_TIMEOUT)
            if obs.status_code == 200:
                owner = _parse_buckets(obs.text)
                rows.append({"item": "OBS 属主", "value": owner.get("iam_user") or "—"})
                rows.append({"item": "OBS Domain ID", "value": owner.get("domain_id") or "—"})
        except Exception:  # noqa: BLE001 - 属主只是加分项，拿不到不影响身份信息
            pass

        rights = []
        if _probe_obs(client, ak, sk, region):
            rights.append("对象存储")
        projects, _error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)
        probe_region = region if region in projects else next(iter(sorted(projects)), "")
        if _probe_ecs(client, ak, sk, probe_region, project_id or projects.get(probe_region, "")):
            rights.append("云主机管理")
        rows.append({"item": "探测到的权限", "value": "、".join(rights) if rights else "未探测到额外资源权限"})

    return {"ok": True, "rows": rows, "columns": _WHOAMI_COLUMNS, "total": len(rows), "has_more": False}


def _run_list_regions(ak: str, sk: str) -> dict:
    """区域列表（内置静态表 + 该账号的项目标注）。

    静态表给出「华为云有哪些区域」，``/v3/auth/projects`` 给出「这组 AK 能在哪些区域干活」，
    两者合起来才既有全集又有可用性 —— 单靠后者会漏掉没开项目的区域。
    """
    projects: dict[str, str] = {}
    with make_client() as client:
        projects, _error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)

    rows = [
        {"RegionId": region, "LocalName": name, "ProjectId": projects.get(region, "")}
        for region, name in OBS_REGIONS
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


def _instance_ips(server: dict) -> list[str]:
    """从 ECS 的 ``addresses`` 里挑 IP：优先弹性公网 IP，没有再退到内网 IP。"""
    floating: list[str] = []
    fixed: list[str] = []
    for entries in (server.get("addresses") or {}).values():
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            addr = str(item.get("addr") or "").strip()
            if not addr:
                continue
            if str(item.get("OS-EXT-IPS:type") or "") == "floating":
                floating.append(addr)
            else:
                fixed.append(addr)
    return floating or fixed


def _instances_one(
    client, ak: str, sk: str, region: str, project_id: str
) -> tuple[list[dict], int, str]:
    """取**单个区域**的 ECS 实例；返回 ``(rows, total, error)``（error 非空时 rows 必为空）。

    ``client`` 由调用方（父线程）建好传进来：代理按线程隔离，worker 里不能再建。
    """
    if not project_id:
        return [], 0, "该区域没有可用项目（project_id）"
    url = f"https://ecs.{region}.myhuaweicloud.com/v1/{project_id}/cloudservers/detail"
    resp = _sdk_call(client, "GET", url, ak, sk, timeout=25.0)
    if resp.status_code != 200:
        return [], 0, _sdk_failure(resp, ak, sk)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return [], 0, "响应不是 JSON"

    rows: list[dict] = []
    for server in payload.get("servers") or []:
        if not isinstance(server, dict):
            continue
        ips = _instance_ips(server)
        flavor = server.get("flavor") if isinstance(server.get("flavor"), dict) else {}
        rows.append({
            "InstanceId": server.get("id") or "",
            "Region": region,
            "InstanceName": server.get("name") or "",
            "Status": server.get("status") or "",
            "PublicIp": ips[0] if ips else "",
            "InstanceType": flavor.get("id") or flavor.get("name") or "",
        })
    return rows, int(payload.get("count") or len(rows)), ""


def _run_list_instances(ak: str, sk: str, params: dict, default_region: str, project_id: str) -> dict:
    """云主机实例列表：填了区域就只查它；**留空则枚举该账号全部区域**并发取完汇总成一张表。

    project_id 的来源与副作用说明见 ``actions()`` 里该操作的 description：插件只做
    ``GET /v3/auth/projects`` 这一只读查询，**不会签发任何临时凭证**。
    """
    region = _field(params, "region").lower() or ""

    # client 在父线程建好、共享给 worker（见 _sweep 的说明），所有分支共用同一个
    with make_client() as client:
        if region:
            # 填了区域 —— 需要该区域的 project_id。凭据里那个 project_id 只属于凭据自己的
            # 区域（region 字段），拿它去查别的区域会拿到错误项目，所以只有区域一致时才直用；
            # 其余情况一律查项目列表推断。
            resolved = project_id if (project_id and region == default_region) else ""
            project_error = ""
            if not resolved:
                projects, project_error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)
                resolved = projects.get(region, "")
            if not resolved:
                return {"ok": False, "error": (
                    f"没有区域「{region}」的 project_id，无法查询 ECS"
                    f"（{project_error or '该区域不在本账号的项目列表中'}）。"
                    "请在凭据的「项目 ID」里手动填写，或改用「区域列表」中带项目 ID 的区域。"
                )}
            rows, total, error = _instances_one(client, ak, sk, region, resolved)
            if error:
                return {"ok": False, "error": error}
            return {
                "ok": True,
                "rows": rows,
                "columns": _INSTANCE_COLUMNS,
                "total": total,
                "empty_text": f"暂无云主机实例（区域 {region}）",
                "has_more": total > len(rows),
            }

        # 留空 —— 用项目列表定出「(区域, project_id)」全集，再逐区域并发取实例
        projects, project_error = _list_projects(client, ak, sk, _PROBE_TIMEOUT)
        if not projects:
            return {"ok": False, "error": (
                f"未填区域，且取项目列表失败（{project_error or '账号下没有可访问的项目'}），"
                "无法枚举区域。请手动填写「区域」，并在凭据里填好对应的「项目 ID」。"
            )}
        targets = sorted(projects.items())
        swept = _sweep(targets, lambda item: _instances_one(client, ak, sk, item[0], item[1]))

    rows: list[dict] = []
    total = 0
    has_more = False
    for _target, result, exc_name in swept:      # target 是 (区域, project_id)，这里只用结果
        if exc_name:
            continue                       # 单个区域查询失败（限流 / 该区域无权限）直接跳过，不进结果
        part_rows, part_total, part_error = result
        if part_error:
            continue
        rows.extend(part_rows)
        total += part_total
        has_more = has_more or part_total > len(part_rows)

    # 只列**成功取到**的区域：失败的区域既不进结果表，也不在空态文案里提一句——
    # 这类失败（如子用户被限制在某几个区域）是常态，报出来只是噪音。
    empty_text = f"未填区域，已扫描全部 {len(targets)} 个区域，均无云主机实例"

    return {
        "ok": True,
        "rows": rows,
        "columns": _INSTANCE_COLUMNS,
        "total": total or len(rows),
        "has_more": has_more,
        "empty_text": empty_text,
    }


def _bucket_targets(bucket: str, region: str, suffix: str = "") -> list[tuple[str, str]]:
    """桶级请求的候选 ``(host, 根路径)``，**虚拟主机风格优先**。

    （同一组 AK、14 个桶、同一端点）：8 个桶两种风格都通，另外 6 个桶用 path 风格
    会被 OBS 直接拒掉 —— ``403 VirtualHostDomainRequired``（"Virtual host domain is
    required while accessing a specific bucket"），换虚拟主机风格立刻 200。所以主用
    虚拟主机风格。

    但虚拟主机风格要求 Host 就是 ``{桶名}.obs.…``，**桶名含「.」时证书是单层通配符
    匹配不上**，所以保留 path 风格兜底。签名里的 host 必须与真实请求的 host 一致
    （否则必然 SignatureDoesNotMatch），所以两种风格各自带自己的 host 一起返回。

    ``suffix`` 是桶内资源路径（如对象 key），已由调用方百分号编码。
    """
    return [
        (f"{bucket}.obs.{region}.myhuaweicloud.com", suffix or "/"),
        (f"obs.{region}.myhuaweicloud.com", f"/{bucket}{suffix}"),
    ]


def _open_bucket_get(client, bucket: str, region: str, ak: str, sk: str,
                     params: dict | None = None, timeout: float | None = None,
                     suffix: str = ""):
    """打开一个桶级只读 GET 的**流式**响应，返回 ``(resp, error)``。

    成功：resp 待调用方读取并 ``close()``、error 为空；失败：resp 为 None、error 是可直接
    展示的文案（已脱敏）。

    先试虚拟主机风格；只有「风格不对」（连接层失败，或 OBS 说要用虚拟主机域名）才换
    path 风格重试 —— 权限不足、桶不存在这类错误换风格也救不了，直接如实回报。
    """
    error = ""
    for host, root in _bucket_targets(bucket, region, suffix):
        headers = _obs_headers("GET", host, root, params, region, ak, sk)
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = client.send(client.build_request("GET", f"https://{host}{root}", **kwargs),
                               stream=True)
        except Exception as exc:          # 连接/TLS 失败（如带「.」的桶名）→ 换风格
            error = f"请求失败：{type(exc).__name__}"
            continue
        if resp.status_code == 200:
            return resp, ""
        resp.read()                       # 错误体很小，读出来好给出原因
        code, message = _xml_error(resp.text)
        error = f"请求失败：{_redact(message or code or f'HTTP {resp.status_code}', ak, sk)}"
        resp.close()
        if code != "VirtualHostDomainRequired":
            return None, error
    return None, error


def _bucket_read(client, bucket: str, region: str, ak: str, sk: str,
                 params: dict | None = None, timeout: float | None = None) -> tuple[str, str]:
    """桶级只读 GET 并读回响应体文本，返回 ``(text, error)``。"""
    resp, error = _open_bucket_get(client, bucket, region, ak, sk, params, timeout)
    if resp is None:
        return "", error
    try:
        resp.read()
        return resp.text, ""
    finally:
        resp.close()


def _bucket_location(client, bucket: str, base_region: str, ak: str, sk: str) -> str:
    """GetBucketLocation：拿到桶的**真实区域**。

    两个结论决定了这里为什么能这么写：

    - ListBuckets 的回包**没有** ``Location`` / ``ExtranetEndpoint`` 字段（只有
      ``Name`` / ``CreationDate`` / ``BucketType``），区域只能另外问；
    - GetBucketLocation **不受 Endpoint 区域限制** —— 用哪个区域的 Endpoint 问，
      返回的都是桶自己的区域，所以直接用列桶时的同一个 Endpoint 逐个问即可。

    返回空串表示问不出来（无权限 / 桶已删 / 网络抖动），由调用方留空展示。
    """
    text, _error = _bucket_read(client, bucket, base_region, ak, sk,
                                params={"location": ""}, timeout=_PROBE_TIMEOUT)
    # 华为把根元素命名为 CreateBucketConfiguration，真正的区域在 LocationConstraint 里
    return _xml_text(_xml_root(text), "LocationConstraint")


def _run_list_buckets(ak: str, sk: str, region: str) -> dict:
    """桶列表：ListBuckets 拿桶名，再并发逐桶问 GetBucketLocation 补区域。"""
    with make_client() as client:
        resp = _obs_get(client, f"https://obs.{region}.myhuaweicloud.com/", region, ak, sk)
        if resp.status_code != 200:
            return {"ok": False, "error": _obs_failure(resp, ak, sk)}

        rows = []
        names = []
        for bucket in _xml_findall(resp.text, "Bucket"):
            name = _xml_text(bucket, "Name")
            if not name:
                continue
            names.append(name)
            rows.append({
                "name": name,
                "creationDate": _xml_text(bucket, "CreationDate"),
                "region": "",
            })

        if names:
            # 逐桶并发问区域：桶可能分布在很多区域，串行问太慢；单桶问不到不影响别的桶。
            # _sweep 按输入顺序返回，所以第 i 个结果就对应 rows[i]，不必再按名字回查。
            for position, (_name, location, _exc) in enumerate(
                _sweep(names, lambda name: _bucket_location(client, name, region, ak, sk))
            ):
                if isinstance(location, str) and location:
                    rows[position]["region"] = location

    missing = sum(1 for row in rows if not row["region"])
    result = {
        "ok": True,
        "rows": rows,
        "columns": _BUCKET_COLUMNS,
        "row_actions": [_OBJECT_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
        "empty_text": "暂无对象存储桶",
    }
    if missing:
        # 区域列空着时「预览」下钻就没有区域可传，得让用户知道为什么
        result["empty_text"] = f"暂无对象存储桶；{missing} 个桶查不到区域（无 GetBucketLocation 权限？）"
    return result


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


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


def _obs_target(params: dict, default_region: str) -> tuple[dict, str]:
    """校验并归一化 OBS 目标（桶名 / 区域），返回 ``(target, error)``。

    华为的 OBS 端点完全由区域决定（``obs.{区域}.myhuaweicloud.com``），所以不单列
    Endpoint 字段 —— 少一个字段就少一处「签名的 host 与请求的 host 不一致」的风险。

    桶名与区域都做过白名单式校验，避免把接口/用户给的字符串直接拼进 URL。
    """
    bucket = _field(params, "bucket")
    if not bucket:
        return {}, "缺少桶名，请在表单里填写，或从「对象存储桶列表」双击「预览」进入"
    if not _BUCKET_RE.match(bucket):
        return {}, "桶名不合法"
    region = _field(params, "region").lower() or default_region
    if not _HOST_RE.match(region) or not _REGION_RE.match(region):
        return {}, "区域格式不合法"
    return {"bucket": bucket, "region": region}, ""


def _run_list_objects(ak: str, sk: str, params: dict, default_region: str) -> dict:
    """列出一个前缀下的对象与子目录（ListObjectsV2 + delimiter=/）。"""
    target, error = _obs_target(params, default_region)
    if error:
        return {"ok": False, "error": error}
    bucket, region = target["bucket"], target["region"]
    prefix = _field(params, "prefix")

    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    with make_client() as client:
        text, error = _bucket_read(client, bucket, region, ak, sk, params=query)
    if error:
        return {"ok": False, "error": error}

    root = _xml_root(text)
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
        rows.append(_object_row_file(
            bucket, region, key, _xml_text(content, "Size"), _xml_text(content, "LastModified")
        ))
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
    """
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    total = _content_length(response)

    def _emit(current: int, text: str) -> None:
        if progress is not None:
            progress(current, total, text)

    def _amount(current: int) -> str:
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
    ak: str, sk: str, params: dict, default_region: str, progress=None, should_cancel=None
) -> dict:
    """下载单个对象（流式写入 save_to 指定的路径）；给了钩子就边写边上报 / 响应取消。"""
    target, error = _obs_target(params, default_region)
    if error:
        return {"ok": False, "error": error}
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    bucket, region = target["bucket"], target["region"]
    obj_path = "/" + quote(key, safe="/~")      # 对象 key 保留 / 作为路径分隔
    with make_client() as client:
        resp, error = _open_bucket_get(client, bucket, region, ak, sk, suffix=obj_path)
        if resp is None:
            return {"ok": False, "error": error}
        try:
            if progress is None and should_cancel is None:
                ok, message = download_to_file(resp, path)
            else:
                ok, message = _download_with_progress(resp, path, key, progress, should_cancel)
        finally:
            resp.close()
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def _list_all_objects(
    client, bucket: str, region: str, ak: str, sk: str, prefix: str, limit: int
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
        text, error = _bucket_read(client, bucket, region, ak, sk, params=query)
        if error:
            return objects, False, error
        root = _xml_root(text)
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
        token = _xml_text(root, "NextContinuationToken")
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
    ak: str, sk: str, params: dict, default_region: str, progress=None, should_cancel=None
) -> dict:
    """把某个前缀下的所有对象并发拉取后打包成 zip（本地临时文件 → 原子落盘）。

    给了 ``progress`` 时按**对象个数**上报；给了 ``should_cancel`` 时在**每个对象开始前**
    和**对象内的每个 chunk** 各查一次（正在下的那个大对象也立刻断流）。
    """
    target, error = _obs_target(params, default_region)
    if error:
        return {"ok": False, "error": error}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = _field(params, "prefix")
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表右键点击「打包下载」进入"}

    bucket, region = target["bucket"], target["region"]

    # client 在父线程建好、共享给下面的下载线程（代理按线程隔离，worker 里不能再建）
    with make_client() as client:
        objects, over_limit, list_error = _list_all_objects(
            client, bucket, region, ak, sk, prefix, MAX_ZIP_OBJECTS
        )
        if list_error:
            return {"ok": False, "error": list_error}
        if over_limit:
            return {"ok": False, "error": (
                f"「{prefix}」下的对象数超过打包上限（{MAX_ZIP_OBJECTS} 个），已枚举前 "
                f"{MAX_ZIP_OBJECTS + 1} 个即停止。请换一个更小的前缀，或逐个下载。"
            )}
        if not objects:
            return {"ok": False, "error": f"「{prefix}」下没有可下载的对象"}

        total = sum(size for _, size in objects)
        if total > MAX_ZIP_BYTES:
            return {"ok": False, "error": (
                f"「{prefix}」下共 {len(objects)} 个对象、{_human_size(total)}，"
                f"超过单次打包上限 {_human_size(MAX_ZIP_BYTES)}。请换一个更小的前缀，或逐个下载。"
            )}

        tmpdir = tempfile.mkdtemp(prefix="vault_obs_zip_")
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
                obj_path = "/" + quote(key, safe="/~")
                # 复用父线程的 client：worker 里自建会丢掉本次凭据的代理
                resp, error = _open_bucket_get(client, bucket, region, ak, sk, suffix=obj_path)
                if resp is None:
                    return key, "", f"{key} {error}"
                try:
                    with open(local, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            # 正在下的这个对象也要能随时断流：500 MB 的对象不能等它下完
                            if should_cancel is not None and should_cancel():
                                raise _Cancelled
                            handle.write(chunk)
                finally:
                    resp.close()
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
                            _remove_quietly(local)
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
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
