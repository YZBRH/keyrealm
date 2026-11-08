# coding: utf-8
"""火山引擎（Volcengine）AK/SK 插件（plugins/cloud/ 类别下的独立子插件）。

``session_token`` 为**选填**：STS 临时凭据（AccessKeyId 以 ``AKTP`` 开头）需一并填写，
签名器会附带 ``X-Security-Token`` 头（该头同样参与签名）；长期 AK/SK（``AKLT`` 开头）
留空即可。

火山引擎的 OpenAPI 走**统一网关**（``open.volcengineapi.com``），签名是自研的
**SignerV4**：名字与 AWS SigV4 神似但**不同源**，实现在同目录的 ``volc_signer.py``
（由官方 Python SDK 的固定向量逐条自证）。三个最容易踩的点：

- ``Authorization`` 的前缀是 ``HMAC-SHA256``，**不是** ``AWS4-HMAC-SHA256``；
- ``kDate = HMAC-SHA256(SK, 日期)`` 用的是**原始 SK**，不加 ``"AWS4"`` 前缀；
- **地域只出现在签名的 Credential Scope 里**（``{yyyymmdd}/{region}/{service}/request``），
  不进 query —— 所以「查哪个地域」是由本插件的「地域」字段决定的，不是接口参数。

签名器只产出 ``(method, url, headers, body)``，**所有请求都由本文件用
``make_client()`` 发出**（只有它带得上当前凭据配置的代理）。

**本插件的每一个操作都是只读的**：只调 ``DescribeRegions`` / ``DescribeZones`` /
``DescribeInstances`` / ``DescribeImages`` 与 IAM ``GetUser`` / ``ListAccessKeys``
这类查询接口，不创建 / 删除 / 开关机 / 绑定任何资源，也不签发任何令牌。

本插件提供的操作（``actions``）：

- ``whoami``           查看调用者身份（IAM GetUser + ListAccessKeys，并只读探测 ECS 权限）
- ``list_regions``     地域列表（ECS DescribeRegions；每行可下钻该地域的可用区）
- ``list_zones``       可用区列表（ECS DescribeZones；每行可下钻该可用区的实例）
- ``list_instances``   云服务器列表（ECS DescribeInstances）
- ``list_images``      镜像列表（ECS DescribeImages）

关于 Action 名的坑：网关**先校验 Action/Version、后校验凭据** —— 杜撰的 Action 一律回
``HTTP 404 InvalidActionOrVersion``（CodeN=100008），假凭据也盖不住它；而 Action 名写对了、
只差凭据时，回的是 ``HTTP 401 InvalidAccessKey``（CodeN=100009）。所以「这个 Action 到底
存不存在」是可以实网验证的，本文件里的四个 ECS Action 与两个 IAM Action **都已逐个验过**
（均卡在凭据校验上），同时又取自官方 SDK 的服务方法清单（见下方参考文档）—— 两处互相印证，
不在这两份清单里的接口一律不写。

另一个端点差异：**IAM 的鉴权失败回的是 HTTP 200**，错误藏在 ``ResponseMetadata.Error`` 里；
ECS 才用 401。故本文件一律按信封判读，不看状态码。

参考文档：https://github.com/volcengine/volcengine-go-sdk/tree/master/service/ecs
参考文档：https://github.com/volcengine/volcengine-go-sdk/tree/master/service/iam
参考文档：https://github.com/volcengine/volc-sdk-python/blob/main/volcengine/auth/SignerV4.py
参考文档：https://www.volcengine.com/docs/6491/1185994
"""

from __future__ import annotations

import os
import re
import sys

from backend.plugin_sdk import (
    VerifyResult,
    guarded,
    log,
    log_debug,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "火山引擎"
DISPLAY_FIELD = "access_key_id"

REQUIRED = (
    ("access_key_id", "Access Key ID (AK)"),
    ("secret_access_key", "Secret Access Key (SK)"),
)

#: 单次翻页条数（官方分页参数的取值上限）与最多翻几页。list 类操作没有进度条通道
#: （见 backend/plugin_sdk.py 的 make_client/stream_action 说明），不能在插件里无限翻页。
_PAGE_SIZE = 100
_MAX_PAGES = 5

#: 地域名的合法形态（形如 cn-beijing / ap-southeast-1）。它会被拼进签名的 Credential
#: Scope，形态不对会让签名整体作废，故在 validate 里先挡一道。
_REGION_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


# ── 加载同目录的签名模块 ──────────────────────────────────
# 文件名带厂商前缀，刻意**不用**通用的 ``signer.py``：``plugins/cloud/`` 是共享的类别目录、
# 也挂在 ``sys.path`` 上，几个云插件各带一份同名模块时，``sys.modules`` 里先加载的那份会
# 一直霸占该名字，后加载的插件 ``import signer`` 会拿到**别人的实现**（算法不同也不报错，
# 只会算出看不懂的错误码）。唯一命名后这类冲突从根上消失。
#
# 仍按**显式文件路径**加载而非裸 ``import``：框架加载插件时会临时把插件目录插进 ``sys.path``，
# 但 ``tests/test_real_vendors.py`` 这类直接 exec 插件文件的调用方只注入到**类别目录**，
# 裸 import 在那里会失败。
def _load_signer():
    """加载同目录的 ``volc_signer.py``（火山引擎 SignerV4 签名实现）。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "volc_signer.py")
    spec = importlib.util.spec_from_file_location("_vault_volcengine_signer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载火山引擎签名模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vault_volcengine_signer"] = module
    spec.loader.exec_module(module)
    return module
signer = _load_signer()

#: 默认地域：与签名器的常量同源（火山首发地域，ECS 一定可用）
DEFAULT_REGION = signer.DEFAULT_REGION


# ── 基础工具 ────────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _text(value) -> str:
    """标量 → 展示文本；``None`` 与空串统一成空串（不给界面留 ``None``）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_int(value):
    """转 int；转不了返回 ``None``（``None`` 与 0 是两回事，调用方要能区分）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在响应文本里的凭据替换掉，任何路径都不回显密钥。

    网关的 ``Message`` 偶尔会把 ``AccessKey`` 原样写回来（例如
    ``The security token[AKLT…] included in the request is invalid.``），
    而这句话会径直进入凭据的 ``status_detail``。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _result(payload) -> dict:
    """取网关信封里的业务体 ``Result``（不是 dict 就给空 dict）。"""
    if not isinstance(payload, dict):
        return {}
    value = payload.get("Result")
    return value if isinstance(value, dict) else {}


def _items(payload, key: str):
    """取信封里的列表字段（先看 ``Result.<key>``，再看顶层 ``<key>``）。

    **不是列表时返回 ``None``**（区别于「是空列表」）。这个区分是刻意的：``None`` 表示
    字段名或层次猜错了（要如实报「无法解析」），``[]`` 表示确实没有资源（该显示
    ``empty_text``）。把前者当后者，等于把「插件解析错了」伪装成「你账号里没有资源」。
    """
    if not isinstance(payload, dict):
        return None
    for node in (_result(payload), payload):
        value = node.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return None


def _error_of(payload) -> dict:
    """取网关信封里的 ``ResponseMetadata.Error``（不是 dict 就给空 dict）。"""
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("ResponseMetadata")
    if isinstance(meta, dict) and isinstance(meta.get("Error"), dict):
        return meta["Error"]
    return {}


# ── 请求构造与发送 ──────────────────────────────────────

def _request(client, ak: str, sk: str, action: str, *, sts: str = "", version: str,
             region: str, service: str, host: str, params: dict | None = None):
    """签名 + 发送 + 解析，返回 ``(payload, error)``。

    ``sts`` 为 STS 临时凭证的 session_token（长期 AK/SK 传空）：非空时签名器会附带
    ``X-Security-Token`` 头。

    ``payload`` 为 ``None`` 时 ``error`` 已是给人看的中文（且已脱敏）。网关报错时不抛
    异常，而是把 ``Code`` / ``Message`` 取出来交给调用方——调用方多半要把它当表格里的
    一行显示，而不是当异常。
    """
    method, url, headers, body = signer.sign(
        action, ak=ak, sk=sk, version=version, params=params or {},
        region=region, service=service, host=host, session_token=sts,
    )
    resp = client.request(method, url, headers=headers, content=body)
    payload = safe_json(resp)
    if payload is None:
        return None, f"响应不是 JSON（HTTP {resp.status_code}）"

    error = _error_of(payload)
    code = _text(error.get("Code"))
    if code:
        detail = _redact(_text(error.get("Message")), ak, sk, sts)
        return None, f"{code}：{detail}".rstrip("：")
    return payload, ""


def _ecs_call(client, ak: str, sk: str, action: str, *, region: str, sts: str = "",
              params: dict | None = None):
    """发一个 ECS 只读请求（``open.volcengineapi.com`` / service=ecs / Version=2020-04-01）。"""
    return _request(client, ak, sk, action, sts=sts,
                    version=signer.ECS_API_VERSION, region=region,
                    service=signer.DEFAULT_SERVICE, host=signer.DEFAULT_HOST, params=params)


def _iam_call(client, ak: str, sk: str, action: str, params: dict | None = None, *, sts: str = ""):
    """发一个 IAM 只读请求。

    IAM 是**全局服务**：主机与区域都和凭据里配的地域无关（官方 SDK 的 ``ServiceInfo``
    把区域写死成 ``cn-north-1``），故这里不带调用方的地域。
    """
    return _request(client, ak, sk, action, sts=sts,
                    version=signer.IAM_API_VERSION, region=signer.IAM_REGION,
                    service=signer.IAM_SERVICE, host=signer.IAM_HOST, params=params)


def _describe_page(client, ak: str, sk: str, action: str, key: str, region: str,
                   extra: dict, builder, *, sts: str = ""):
    """翻页拉一份列表，返回 ``(rows, total, has_more, error)``；``rows is None`` 表示失败。

    ``total`` 优先取上游的 ``TotalCount``，拿不到就退化成 ``len(rows)``（官方文档里
    ``DescribeInstances`` / ``DescribeImages`` 的返回参数**并没有列 TotalCount**，
    所以这个字段大概率压根不会回，别把它当主口径）。

    ``has_more`` 只看**最后一页的 ``NextToken`` 是否非空** —— 这是文档明确给出的翻页
    信号（「返回为空表示该页为末页」）。**不要**用 ``len(rows) < total`` 推：total 拿不到时
    两边都等于 len(rows)，翻页被 ``_MAX_PAGES`` 截断也会判成「已取全」，界面就不会打
    「仅显示部分」，等于拿一页数据冒充全量。
    """
    rows: list[dict] = []
    total = None
    token = ""
    has_more = False
    for _ in range(_MAX_PAGES):
        query = dict(extra)
        query["MaxResults"] = str(_PAGE_SIZE)
        if token:
            query["NextToken"] = token
        payload, error = _ecs_call(client, ak, sk, action, region=region,
                                   params=query, sts=sts)
        if payload is None:
            return None, 0, False, error
        items = _items(payload, key)
        if items is None:
            return None, 0, False, f"返回体里没有 {key}，无法解析"

        body = _result(payload)
        count = _as_int(body.get("TotalCount"))
        if count is not None:
            total = count
        rows.extend(builder(item) for item in items)

        token = _text(body.get("NextToken"))
        has_more = bool(token)
        if not token:
            break
    return rows, (len(rows) if total is None else total), has_more, ""


def _list_result(rows, columns, empty_text: str, *, total=None, has_more: bool = False) -> dict:
    """统一的列表结果信封（``total`` 默认取 ``len(rows)``）。"""
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows) if total is None else total,
        "has_more": has_more,
        "empty_text": empty_text,
    }


# ── 列定义 ───────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 180},
    {"key": "value", "label": "值"},
]

_REGION_COLUMNS = [
    {"key": "RegionId", "label": "地域", "mono": True},
]

#: 可用区列表混着「从地域行内下钻」与「手动填地域」两种来路，地域必须能一眼看出
_ZONE_COLUMNS = [
    {"key": "ZoneId", "label": "可用区", "mono": True},
    {"key": "Region", "label": "地域", "width": 130, "mono": True},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    {"key": "Region", "label": "地域", "width": 120},
    {"key": "ZoneId", "label": "可用区", "width": 130, "mono": True},
    {"key": "InstanceName", "label": "实例名"},
    {"key": "Status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "PublicIp", "label": "公网 IP", "width": 130, "mono": True},
    {"key": "PrivateIp", "label": "私网 IP", "width": 130, "mono": True},
    {"key": "InstanceTypeId", "label": "规格"},
    {"key": "CreatedAt", "label": "创建时间", "width": 165},
]

_IMAGE_COLUMNS = [
    {"key": "ImageId", "label": "镜像 ID", "mono": True},
    {"key": "ImageName", "label": "镜像名"},
    {"key": "Region", "label": "地域", "width": 120},
    {"key": "OsName", "label": "系统"},
    {"key": "Platform", "label": "平台", "width": 110},
    {"key": "Status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "Size", "label": "大小", "width": 100},
    {"key": "Visibility", "label": "可见性", "width": 110},
]

#: ``$row.<列key>`` 由前端解析，取不到值时按钮自动置灰（不会把空参数发出去）。
#: 行内动作的目标也必须在 ``actions()`` 里声明，故 ``list_zones`` / ``list_instances``
#: 的 ``description`` 里都写了「也可从 XXX 行内进入」。
_REGION_ROW_ACTIONS = [
    {"id": "list_zones", "label": "可用区", "params": {"region": "$row.RegionId"}},
]

_ZONE_ROW_ACTIONS = [
    {"id": "list_instances", "label": "云服务器",
     "params": {"region": "$row.Region", "zone_id": "$row.ZoneId"}},
]


# ── 行构造 ──────────────────────────────────────────────

def _region_row(item: dict) -> dict:
    return {"RegionId": _text(item.get("RegionId"))}


def _zone_row(item: dict, region: str) -> dict:
    return {"ZoneId": _text(item.get("ZoneId")), "Region": region}


def _instance_public_ip(item: dict) -> str:
    """公网地址：实例绑了弹性公网时在 ``EipAddress.IpAddress``；没绑就是空。"""
    eip = item.get("EipAddress")
    if isinstance(eip, dict):
        return _text(eip.get("IpAddress"))
    return ""


def _instance_private_ip(item: dict) -> str:
    """私网地址：取主网卡的 ``PrimaryIpAddress``（``NetworkInterfaces`` 的第一个）。"""
    nics = item.get("NetworkInterfaces")
    if isinstance(nics, list):
        for nic in nics:
            if isinstance(nic, dict):
                ip = _text(nic.get("PrimaryIpAddress"))
                if ip:
                    return ip
    return ""


def _instance_row(item: dict, region: str) -> dict:
    return {
        "InstanceId": _text(item.get("InstanceId")),
        "Region": region,
        "ZoneId": _text(item.get("ZoneId")),
        "InstanceName": _text(item.get("InstanceName")),
        "Status": _text(item.get("Status")),
        "PublicIp": _instance_public_ip(item),
        "PrivateIp": _instance_private_ip(item),
        "InstanceTypeId": _text(item.get("InstanceTypeId")),
        "CreatedAt": _text(item.get("CreatedAt")),
    }


def _image_row(item: dict, region: str) -> dict:
    return {
        "ImageId": _text(item.get("ImageId")),
        "ImageName": _text(item.get("ImageName")),
        "Region": region,
        "OsName": _text(item.get("OsName")),
        "Platform": _text(item.get("Platform")),
        "Status": _text(item.get("Status")),
        "Size": _text(item.get("Size")),
        "Visibility": _text(item.get("Visibility")),
    }


# ── 判定 ────────────────────────────────────────────────

#: 签名器的判定表之外，本插件再补两个「凭据有效但用不了」的网关码。
#: 口径遵循框架统一规则：**配额用尽 / IP 白名单 / 权限不足 / 账号未开通 / 被禁用
#: 都是「有效但受限」**，只有明确「AK 不存在 / 签名校验失败」才判 ``invalid``；
#: 拿不准一律 ``error`` —— 绝不猜成 ``invalid``，否则会把一把好密钥标成失效。
_EXTRA_RESTRICTED_CODES = {
    "AccountSuspend",   # 账号被冻结 / 已欠费停机
    "OperationDenied",  # 操作被拒（子用户权限不足、资源状态不允许等）
}

#: 回包文案里出现这些词时，**未归类**的错误码也按「受限」处理。
#: 用于兜住网关把「IP 白名单 / 未开通 / 欠费」塞在 Message 里、Code 却没进上表的场合。
_RESTRICTED_HINTS = (
    "whitelist", "not authorized", "permission", "forbidden", "suspend", "arrears",
    "白名单", "欠费", "未开通", "无权限", "受限",
)

_KNOWN_VERDICTS = {"valid", "invalid", "restricted", "error"}


def _gateway_error(resp) -> tuple[str, str, str]:
    """取 ``ResponseMetadata.Error`` 的 ``(Code, CodeN, Message)``（读不到就是空串）。"""
    error = _error_of(safe_json(resp))
    return (_text(error.get("Code")), _text(error.get("CodeN")),
            _text(error.get("Message")))


def _classify(resp, ak: str, sk: str, sts: str = "") -> tuple[str, str]:
    """把一次探针响应判成框架口径的 ``(status, message)``，消息已脱敏。

    基础判读**完全复用** ``signer.interpret()``（它拿的就是 ``ResponseMetadata.Error.Code``
    的分类表，与官方 SDK 校准过）。这里只补两件事：

    - 脱敏：网关的 ``Message`` 偶尔会把凭据原样写回来，而这句话会径直进入凭据的
      ``status_detail``，与框架出口的净化不是一个通道；
    - 把签名器尚未收录的「有效但受限」码从 ``error`` 降级为 ``restricted``。
    """
    base = signer.interpret(resp.status_code, resp.text)
    verdict = _text(base.get("verdict"))
    if verdict not in _KNOWN_VERDICTS:
        return "error", f"未识别的判定结果（{verdict or '空'}）"

    if verdict == "error":
        code, code_n, detail = _gateway_error(resp)
        detail = _redact(detail, ak, sk, sts)
        lowered = detail.lower()
        if code in _EXTRA_RESTRICTED_CODES or any(h in lowered for h in _RESTRICTED_HINTS):
            tag = code + (f"(CodeN={code_n})" if code_n else "")
            return "restricted", f"凭据有效但受限：{tag} {detail}".strip()

    return verdict, _redact(_text(base.get("message")), ak, sk, sts)


def _probe_account(resp) -> dict | None:
    """从探针回包里提炼能进「账号信息」的那一点点只读事实（拿不到就不填）。"""
    payload = safe_json(resp)
    regions = _items(payload, "Regions") if payload is not None else None
    if not regions:
        return None
    return {"可见地域": f"{len(regions)} 个"}


# ── 各操作的实现 ────────────────────────────────────────

#: 「身份信息」与「AccessKey」两组键值行的字段映射
_USER_FIELDS = (
    ("AccountId", "账号 ID"),
    ("UserName", "用户名"),
    ("DisplayName", "显示名"),
    ("Id", "用户 ID"),
    ("Trn", "TRN"),
    ("Email", "邮箱"),
    ("MobilePhone", "手机号"),
    ("CreateDate", "创建时间"),
    ("Description", "描述"),
)


def _run_whoami(client, ak: str, sk: str, params: dict, sts: str = "") -> dict:
    """账号信息：IAM GetUser + ListAccessKeys + ECS 只读探测。

    三段都是**尽力而为**：根账号的 AK 调 ``GetUser`` 可能被拒，子账号可能没有 IAM 读权限，
    ECS 也可能没开通 —— 任何一段失败都不该让整块信息空掉。所以这里的规则是：

    - 只要**有一段**取到了数据，就把取到的照常列出来，失败的那几段以「提示」行如实标注；
    - **三段全失败**（典型是 AK 本身就是假的）则整块返回失败 —— 不拿一屏「不可用」冒充
      查询成功，那会让「凭据是错的」看起来像「查到了但没权限」。

    另注：IAM 是**鉴权失败也回 HTTP 200**（错误藏在 ``ResponseMetadata.Error`` 里）的端点，
    判读一律看信封而不看状态码，这一点在 ``_request`` 里统一处理。
    """
    rows: list[dict] = []
    notes: list[str] = []
    hits = 0

    payload, error = _iam_call(client, ak, sk, "GetUser", sts=sts)
    user = _result(payload).get("User") if payload is not None else None
    if isinstance(user, dict):
        hits += 1
        for key, label in _USER_FIELDS:
            value = _text(user.get(key))
            if value:
                rows.append({"item": label, "value": value})
        for key, label in (("EmailIsVerify", "邮箱已验证"), ("MobilePhoneIsVerify", "手机已验证")):
            if key in user:
                rows.append({"item": label, "value": _text(user.get(key))})
    else:
        notes.append(f"身份信息（IAM GetUser）未取到：{error or '返回体里没有 User'}")

    payload, error = _iam_call(client, ak, sk, "ListAccessKeys", sts=sts)
    keys = _items(payload, "AccessKeyMetadata") if payload is not None else None
    if keys is None:
        notes.append(f"AccessKey 列表未取到：{error or '返回体里没有 AccessKeyMetadata'}")
    else:
        hits += 1
        if not keys:
            rows.append({"item": "AccessKey", "value": "0 个"})
        else:
            rows.append({"item": "AccessKey 数量", "value": f"{len(keys)} 个"})
            current = [item for item in keys if _text(item.get("AccessKeyId")) == ak]
            if current:
                rows.append({"item": "当前 AK 状态", "value": _text(current[0].get("Status"))})
                rows.append({"item": "当前 AK 创建时间",
                             "value": _text(current[0].get("CreateDate"))})
            else:
                rows.append({"item": "当前 AK 状态",
                             "value": "不在 IAM 的 AccessKey 列表里（多为根账号 AK，或该 AK 属别的用户）"})

    payload, error = _ecs_call(client, ak, sk, "DescribeRegions", region=DEFAULT_REGION, sts=sts)
    regions = _items(payload, "Regions") if payload is not None else None
    if regions is None:
        notes.append(f"ECS 只读权限不可用：{error or '返回体里没有 Regions'}")
    else:
        hits += 1
        rows.append({"item": "ECS 只读权限", "value": f"可用（可见 {len(regions)} 个地域）"})

    if not hits:
        return {"ok": False, "error": "；".join(notes) or "未取到任何账号信息"}
    rows.extend({"item": "提示", "value": note} for note in notes)
    return _list_result(rows, _KV_COLUMNS, "未取到账号信息")


def _run_list_regions(client, ak: str, sk: str, params: dict, sts: str = "") -> dict:
    """地域列表：ECS ``DescribeRegions``。

    地域清单是**全局**的（不随凭据里配的地域变），故固定用 ``cn-beijing`` 签名。
    """
    payload, error = _ecs_call(client, ak, sk, "DescribeRegions", region=DEFAULT_REGION, sts=sts)
    items = _items(payload, "Regions")
    if items is None:
        return {"ok": False, "error": error or "返回体里没有 Regions，无法解析"}
    has_more = bool(_text(_result(payload).get("NextToken")))
    return _list_result([_region_row(item) for item in items], _REGION_COLUMNS,
                        "没有可见的地域", has_more=has_more)


def _run_list_zones(client, ak: str, sk: str, params: dict, sts: str = "") -> dict:
    """可用区列表：ECS ``DescribeZones``（按签名范围里的地域）。"""
    region = _field(params, "region") or DEFAULT_REGION
    payload, error = _ecs_call(client, ak, sk, "DescribeZones", region=region, sts=sts)
    items = _items(payload, "Zones")
    if items is None:
        return {"ok": False, "error": error or f"地域 {region} 的返回体里没有 Zones，无法解析"}
    return _list_result([_zone_row(item, region) for item in items], _ZONE_COLUMNS,
                        f"地域 {region} 下没有可用区")


def _run_list_instances(client, ak: str, sk: str, params: dict, sts: str = "") -> dict:
    """云服务器列表：ECS ``DescribeInstances``（按签名范围里的地域）。"""
    region = _field(params, "region") or DEFAULT_REGION
    extra = {
        "ZoneId": _field(params, "zone_id"),
        "InstanceName": _field(params, "instance_name"),
        "Status": _field(params, "status"),
    }
    rows, total, has_more, error = _describe_page(
        client, ak, sk, "DescribeInstances", "Instances", region, extra,
        lambda item: _instance_row(item, region), sts=sts)
    if rows is None:
        return {"ok": False, "error": error}
    return _list_result(rows, _INSTANCE_COLUMNS, f"地域 {region} 下没有云服务器",
                        total=total, has_more=has_more)


def _run_list_images(client, ak: str, sk: str, params: dict, sts: str = "") -> dict:
    """镜像列表：ECS ``DescribeImages``（按签名范围里的地域）。"""
    region = _field(params, "region") or DEFAULT_REGION
    extra = {"ImageName": _field(params, "image_name")}
    rows, total, has_more, error = _describe_page(
        client, ak, sk, "DescribeImages", "Images", region, extra,
        lambda item: _image_row(item, region), sts=sts)
    if rows is None:
        return {"ok": False, "error": error}
    return _list_result(rows, _IMAGE_COLUMNS, f"地域 {region} 下没有镜像",
                        total=total, has_more=has_more)


# ── 操作声明 ─────────────────────────────────────────────

#: 说明：list 类操作的参数表单只渲染 input/select，**不渲染 ``hint``**
#: （见 frontend/src/views/Credentials.vue 的 list-fields 块），所以字段的详细解释写在
#: manifest 的凭据字段上（那里会渲染），这里只留一句必要的。
_REGION_FIELD = {
    "key": "region", "label": "地域", "type": "text", "required": False,
    "default": DEFAULT_REGION, "placeholder": DEFAULT_REGION,
    "hint": f"要查的地域，也是签名用的地域；留空按 {DEFAULT_REGION}。",
}
_ZONE_FIELD = {
    "key": "zone_id", "label": "可用区", "type": "text", "required": False,
    "placeholder": "选填，如 cn-beijing-a",
}
_INSTANCE_NAME_FIELD = {
    "key": "instance_name", "label": "实例名", "type": "text", "required": False,
    "placeholder": "选填，支持部分匹配",
}
_IMAGE_NAME_FIELD = {
    "key": "image_name", "label": "镜像名", "type": "text", "required": False,
    "placeholder": "选填，如 ubuntu",
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据不完整时返回空列表）。"""
    data = data or {}
    if not (_field(data, "access_key_id") and _field(data, "secret_access_key")):
        return []
    return [
        {
            "id": "whoami",
            "name": "账号信息",
            "kind": "list",
            "group": "账号",
            "order": 10,
            "description": (
                "IAM GetUser + ListAccessKeys + ECS DescribeRegions（均只读）：账号 ID / "
                "用户名 / TRN / 联系方式、AccessKey 数量与当前这把 AK 的状态，"
                "以及 ECS 只读权限是否可用"
            ),
            "columns": _KV_COLUMNS,
            "empty_text": "未取到账号信息",
        },
        {
            "id": "list_regions",
            "name": "地域列表",
            "kind": "list",
            "group": "地域",
            "order": 20,
            "description": (
                "ECS DescribeRegions（只读）：火山引擎的全部地域（该清单是全局的）。"
                "每行可下钻到该地域的可用区"
            ),
            "columns": _REGION_COLUMNS,
            "row_actions": _REGION_ROW_ACTIONS,
            "empty_text": "没有可见的地域",
        },
        {
            "id": "list_zones",
            "name": "可用区列表",
            "kind": "list",
            "group": "地域",
            "order": 30,
            "description": (
                "ECS DescribeZones（只读）：指定地域下的可用区（含可用区 ID）。"
                "从操作菜单直接打开时请先填「地域」；也可从「地域列表」的行内进入"
            ),
            "columns": _ZONE_COLUMNS,
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "row_actions": _ZONE_ROW_ACTIONS,
            "empty_text": "该地域下没有可用区",
        },
        {
            "id": "list_instances",
            "name": "云服务器列表",
            "kind": "list",
            "group": "计算",
            "order": 40,
            "description": (
                "ECS DescribeInstances（只读）：实例 ID / 可用区 / 名称 / 状态 / 公私网 IP / "
                f"规格 / 创建时间。分页拉取，最多 {_MAX_PAGES} 页，超出时表头会标注"
                "「仅显示部分」。从操作菜单直接打开时请先填「地域」；"
                "也可从「可用区列表」的行内进入"
            ),
            "columns": _INSTANCE_COLUMNS,
            "fields": [_REGION_FIELD, _ZONE_FIELD, _INSTANCE_NAME_FIELD],
            "submit": "查询",
            "empty_text": "该地域下没有云服务器",
        },
        {
            "id": "list_images",
            "name": "镜像列表",
            "kind": "list",
            "group": "计算",
            "order": 50,
            "description": (
                "ECS DescribeImages（只读）：镜像 ID / 名称 / 系统 / 平台 / 状态 / 大小 / "
                "可见性。从操作菜单直接打开时请先填「地域」"
            ),
            "columns": _IMAGE_COLUMNS,
            "fields": [_REGION_FIELD, _IMAGE_NAME_FIELD],
            "submit": "查询",
            "empty_text": "该地域下没有镜像",
        },
    ]


#: 操作 id → 处理函数。处理函数统一 ``(client, ak, sk, params)``。
_HANDLERS = {
    "whoami": _run_whoami,
    "list_regions": _run_list_regions,
    "list_zones": _run_list_zones,
    "list_instances": _run_list_instances,
    "list_images": _run_list_images,
}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作（**全部只读**，绝不产生远端变更）。"""
    params = params or {}
    data = data or {}
    try:
        ak = _field(data, "access_key_id")
        sk = _field(data, "secret_access_key")
        sts = _field(data, "session_token")
        if not ak or not sk:
            return {"ok": False, "error": "Access Key ID 或 Secret Access Key 为空"}

        handler = _HANDLERS.get(action_id)
        if handler is None:
            return {"ok": False, "error": f"未知操作：{action_id}"}

        with make_client() as client:
            return handler(client, ak, sk, params, sts)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        # 只回异常类型名：httpx 的异常文本含完整请求 URL 与请求头，会把 AK/SK 泄进界面
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    region = _field(data, "region")
    if region and not _REGION_RE.fullmatch(region):
        errors.append(f"地域格式不对：{region}（应形如 {DEFAULT_REGION}）")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """只读探针：``GET https://open.volcengineapi.com/?Action=DescribeRegions&Version=2020-04-01``。

    零副作用 —— ``DescribeRegions`` 只返回一份全局的只读地域清单，不创建 / 变更任何资源，
    也不签发任何令牌。

    为什么探针非得是 ``DescribeRegions``：网关**先校验 Action/Version、后校验凭据**（杜撰
    的 Action 回 ``HTTP 404 InvalidActionOrVersion``，假凭据盖不住），所以探针用的 Action
    名与版本串都已实网验过。选它是因为它不需要任何入参、不触碰任何资源、也不受地域限制 ——
    校验凭据时不该顺带依赖某个地域已开通。

    判定口径见 ``_classify``：明确「AK 不存在 / 签名不匹配」才是 ``invalid``；
    权限不足 / 账号被冻结一律 ``restricted``；拿不准一律 ``error``（绝不猜成 invalid）。
    """
    data = data or {}
    missing = [label for key, label in REQUIRED if not _field(data, key)]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    sts = _field(data, "session_token")
    region = _field(data, "region") or DEFAULT_REGION
    mode = "STS 临时凭据" if sts else "长期 AK/SK"
    log(f"校验火山引擎凭据 {mask_secret(ak)}（{mode}，ECS DescribeRegions @ {signer.DEFAULT_HOST}）")

    method, url, headers, body = signer.sign_probe(ak, sk, region=region, session_token=sts)
    with make_client() as client:
        resp = client.request(method, url, headers=headers, content=body)

    status, message = _classify(resp, ak, sk, sts)
    return VerifyResult(status=status, message=message, account_info=_probe_account(resp))


# ── 权限探测（capabilities）─────────────────────────────

def _probe_iam(client, ak: str, sk: str, sts: str = "") -> tuple[str, str]:
    """探一下 IAM 是否可用（``whoami`` 走 IAM，与 ECS 的授权是两套）。"""
    try:
        method, url, headers, body = signer.sign(
            "GetUser", ak=ak, sk=sk, version=signer.IAM_API_VERSION,
            region=signer.IAM_REGION, service=signer.IAM_SERVICE, host=signer.IAM_HOST,
            session_token=sts)
        resp = client.request(method, url, headers=headers, content=body)
    except Exception as exc:  # noqa: BLE001 - 探测失败按「未知」处理，绝不外抛
        return "error", f"执行异常：{type(exc).__name__}"
    return _classify(resp, ak, sk, sts)


def capabilities(data: dict) -> dict:
    """报告当前 AK/SK **真正能用**的操作（只读探测，拿不准就抛异常）。

    探针就是 ``verify`` 用那个（ECS ``DescribeRegions``），结论只有三种：

    - 明确成功 → ECS 那几个查询操作全部可用；``whoami`` 另算（它走 IAM，
      是另一个服务、另一套授权），单独再探一次；
    - 明确被拒（``invalid`` / ``restricted``）→ 不返回任何操作，并说明原因；
    - 探针本身没跑通（网络抖动 / 响应看不懂）→ **抛异常**，交给框架按「未知 → 全部可用」
      兜底。探测抖一下就把整块功能藏起来，比不探测还糟。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    sts = _field(data, "session_token")
    if not ak or not sk:
        return {"available": [], "note": "缺少 Access Key ID 或 Secret Access Key"}
    region = _field(data, "region") or DEFAULT_REGION

    method, url, headers, body = signer.sign_probe(ak, sk, region=region, session_token=sts)
    with make_client() as client:
        resp = client.request(method, url, headers=headers, content=body)
        status, message = _classify(resp, ak, sk, sts)

        if status == "valid":
            available = ["list_regions", "list_zones", "list_instances", "list_images"]
            iam_status, iam_message = _probe_iam(client, ak, sk, sts)
            # IAM 探针失败只降级、不拦路：whoami 本身对「缺权限」是容错的，
            # 只有明确判定这把 AK 在 IAM 侧无效才不给它 —— 那与 ECS 侧「有效」矛盾，
            # 属于异常情形，宁可少给一个操作。
            if iam_status != "invalid":
                available.insert(0, "whoami")
            else:
                log_debug(f"whoami 隐藏：IAM 探针判定 {iam_status}（{iam_message}）")
            return {"available": available, "note": "AK/SK 有效"}

        if status in ("invalid", "restricted"):
            # 探针是**明确**被拒（不是「拿不准」），那就如实隐藏：这把 Key 当前确实用不了
            return {"available": [], "note": message}

    raise RuntimeError("火山引擎权限探测失败（网络异常或响应无法识别）")


def get_display_summary(data) -> str:
    data = data or {}
    detail = _field(data, DISPLAY_FIELD)
    return f"{NAME} | {mask_secret(detail)}" if detail else NAME
