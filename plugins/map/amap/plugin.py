"""高德地图 Key 验证插件（plugins/map/ 类别下的独立子插件）。

**按平台分档验证**——高德 Key 是按「服务平台」绑定的：

- **Web 服务**类型的 Key：用 Web 服务 REST 接口**实调**（默认）。
- **其它平台**（Web 端 JS API / iOS / Android / 微信小程序）：那类 Key 绑在**客户端 SDK** 上，
  服务端**无法**替它们发起真实调用。此时改判「**该 Key 是否存在**」——高德的
  ``USERKEY_PLAT_NOMATCH``（10009）恰恰说明 **Key 存在、只是绑给了别的平台**；
  ``INVALID_USER_KEY``（10001）才是「不存在或已过期」。

高德 Web 服务接口一律是 **HTTP 200 + body 内判定**：业务结果放在 ``status`` / ``info`` /
``infocode`` 里，HTTP 状态码只有网络层含义。因此这里不按状态码判成败——``status`` 为
``"1"`` 表示调用成功，``"0"`` 表示失败，原因看 ``info``（英文标识）与 ``infocode``
（数字码，仅供展示与核对）。

``key`` 出现在 **query string** 中（高德的固定设计，无法改到 Header），所以异常路径只回显
已知错误标识的中文映射、未知标识回显 ``info`` 原文，**绝不拼接异常对象自身的文本**——那会
带上完整请求 URL，把 Key 泄进日志与 ``status_detail``。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``geocode``  地理编码：结构化地址 → 经纬度
- ``regeo``    逆地理编码：经纬度 → 结构化地址
- ``district`` 行政区划：关键词 → 下级行政区
- ``weather``  实况天气：行政区划 → 实时天气
- ``forecast`` 预报天气：行政区划 → 未来数日预报
- ``ip``       IP 定位：IP → 所在省市
- ``distance`` 距离测量：多起点 → 终点距离

参考文档：https://lbs.amap.com/api/webservice/summary
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded, make_client, mask_secret, safe_json

NAME = "高德地图"
BASE = "https://restapi.amap.com"
DISPLAY_FIELD = "key"

# info → (判定, 中文说明)。分档遵循框架统一口径：**配额用尽 / 白名单 / 权限不足属
# 「有效但受限」**，只有明确指向「Key 本身不存在或已被删除」的才判 invalid。
# 说明文本既进验证消息，也进操作报错。
_INFO = {
    "OK": ("valid", "凭证有效"),
    "INVALID_USER_KEY": ("invalid", "Key 不正确或已过期"),
    "USER_KEY_RECYCLED": ("invalid", "Key 已被删除"),
    "USERKEY_PLAT_NOMATCH": (
        "invalid",
        "Key 与所绑定的服务平台不符（常见于把「Web 端(JS API)」的 Key 用在了 Web 服务接口上）",
    ),
    "INVALID_USER_IP": ("restricted", "本机出口 IP 不在该 Key 的 IP 白名单内"),
    "INVALID_USER_DOMAIN": ("restricted", "请求来源域名不在该 Key 的域名白名单内"),
    "INVALID_USER_SIGNATURE": (
        "restricted",
        "该 Key 已开启「数字签名」，本次请求未携带正确签名（Key 本身有效）",
    ),
    "INVALID_USER_SCODE": (
        "restricted",
        "该 Key 已开启「数字签名」，安全码校验未通过（Key 本身有效）",
    ),
    "DAILY_QUERY_OVER_LIMIT": ("restricted", "已超出当日调用量上限"),
    "ACCESS_TOO_FREQUENT": ("restricted", "单位时间内访问过于频繁（触发限流）"),
    "IP_QUERY_OVER_LIMIT": ("restricted", "该 IP 的访问次数超限"),
    "INSUFFICIENT_PRIVILEGES": ("restricted", "该 Key 无权调用此服务"),
    "USER_ABROAD": ("restricted", "请求来自海外 IP，该 Key 已限制海外访问"),
}


# ── 通用小工具 ───────────────────────────────────────────

def _scalar(value) -> str:
    """标量 → 展示文本。

    高德在「该字段无值」时会返回**空数组**（典型是 ``addressComponent.city`` 为
    ``[]`` 而非空串），所以这里把 list/dict 一并压平，避免界面上出现 ``[]``。
    """
    if value is None or value == [] or value == {}:
        return "—"
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(parts) if parts else "—"
    if isinstance(value, dict):
        parts = [f"{key}={_scalar(val)}" for key, val in value.items()]
        return "、".join(parts) if parts else "—"
    text = str(value).strip()
    return text or "—"


def _pick(item: dict, columns) -> dict:
    return {column["key"]: _scalar(item.get(column["key"])) for column in columns}


def _explain(payload: dict) -> str:
    """把高德的失败响应转成可读中文（不含任何来自 URL 的文本）。"""
    info = str(payload.get("info") or "").strip()
    code = str(payload.get("infocode") or "").strip()
    label = _INFO.get(info, (None, ""))[1]
    suffix = f"，错误码 {code}" if code else ""
    if label:
        return f"{label}{suffix}"
    return f"高德返回 {info or '未知错误'}{suffix}"


def _call(client, path: str, key: str, **params):
    """调用一个高德 Web 服务接口，返回 ``(payload, error, verdict)``。

    成功时 ``payload`` 是完整响应体（含业务字段）、``error`` 为空、``verdict`` 为空；
    失败时 ``payload`` 为 None，``error`` 可直接展示，``verdict`` 用于回到 VerifyResult。
    """
    query = {"key": key}
    for name, value in params.items():
        text = "" if value is None else str(value).strip()
        if text:
            query[name] = text
    resp = client.get(f"{BASE}{path}", params=query)
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, f"响应无法解析（HTTP {resp.status_code}）", "error"
    if str(payload.get("status")) == "1":
        return payload, "", ""
    info = str(payload.get("info") or "").strip()
    return None, _explain(payload), _INFO.get(info, ("error", ""))[0]


# ── Key 平台 ─────────────────────────────────────────────
# 高德 Key 按「服务平台」绑定；只有 Web 服务类型的 Key 能在服务端调 REST 接口。
_KEY_TYPES = {
    "web_service": "Web 服务",
    "jsapi": "Web 端（JS API）",
    "ios": "iOS",
    "android": "Android",
    "wechat_mini": "微信小程序",
    "other": "其它",
}


def _key_type(data) -> str:
    """取「Key 类型」（缺省 = Web 服务）。未知取值也按 Web 服务处理，不因此报错。"""
    value = str((data or {}).get("key_type") or "").strip()
    return value if value in _KEY_TYPES else "web_service"


def _probe(client, key: str):
    """打一次 IP 定位接口，**连同原始 ``info`` 一起返回**——verify 要按平台语义分档。

    与 ``_call`` 的区别：``_call`` 把 ``info`` 折成 verdict（供操作报错用），这里要拿到
    ``info`` 本身——``USERKEY_PLAT_NOMATCH``（Key 存在、平台不符）与 ``INVALID_USER_KEY``
    （Key 不存在）在 verify 里的判定**完全不同**。
    """
    resp = client.get(f"{BASE}/v3/ip", params={"key": key})
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, f"响应无法解析（HTTP {resp.status_code}）", ""
    if str(payload.get("status")) == "1":
        return payload, "", "OK"
    info = str(payload.get("info") or "").strip()
    return None, _explain(payload), info


# ── 列定义 ───────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]
_GEO_COLUMNS = [
    {"key": "formatted_address", "label": "格式化地址"},
    {"key": "province", "label": "省", "width": 110},
    {"key": "city", "label": "市", "width": 110},
    {"key": "district", "label": "区县", "width": 110},
    {"key": "location", "label": "经纬度", "mono": True, "width": 170},
    {"key": "level", "label": "匹配级别", "width": 110, "type": "tag"},
]
_DISTRICT_COLUMNS = [
    {"key": "name", "label": "名称"},
    {"key": "adcode", "label": "行政区划编码", "mono": True, "width": 130},
    {"key": "citycode", "label": "城市编码", "mono": True, "width": 110},
    {"key": "level", "label": "级别", "width": 110, "type": "tag"},
    {"key": "center", "label": "中心点", "mono": True, "width": 170},
]
_LIVE_COLUMNS = [
    {"key": "province", "label": "省", "width": 110},
    {"key": "city", "label": "城市", "width": 110},
    {"key": "adcode", "label": "编码", "mono": True, "width": 110},
    {"key": "weather", "label": "天气"},
    {"key": "temperature", "label": "气温(℃)", "width": 100},
    {"key": "winddirection", "label": "风向", "width": 100},
    {"key": "windpower", "label": "风力", "width": 90},
    {"key": "humidity", "label": "湿度", "width": 90},
    {"key": "reporttime", "label": "发布时间", "width": 170},
]
_FORECAST_COLUMNS = [
    {"key": "city", "label": "城市", "width": 100},
    {"key": "date", "label": "日期", "width": 120},
    {"key": "dayweather", "label": "白天天气"},
    {"key": "nightweather", "label": "夜间天气"},
    {"key": "daytemp", "label": "白天(℃)", "width": 90},
    {"key": "nighttemp", "label": "夜间(℃)", "width": 90},
    {"key": "daywind", "label": "白天风向", "width": 110},
    {"key": "daypower", "label": "白天风力", "width": 100},
]
_DISTANCE_COLUMNS = [
    {"key": "origin", "label": "起点", "mono": True, "width": 170},
    {"key": "distance", "label": "距离（米）", "width": 110},
    {"key": "duration", "label": "预计耗时（秒）", "width": 130},
]


def _rows(rows: list, columns, empty_text: str) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows),
        "has_more": False,
        "empty_text": empty_text,
    }


# ── 操作实现 ─────────────────────────────────────────────

def _h_geocode(client, key, params) -> dict:
    payload, error, _ = _call(
        client, "/v3/geocode/geo", key, address=params.get("address"), city=params.get("city")
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows = [
        _pick(item, _GEO_COLUMNS)
        for item in payload.get("geocodes") or []
        if isinstance(item, dict)
    ]
    return _rows(rows, _GEO_COLUMNS, "未匹配到地址")


def _h_regeo(client, key, params) -> dict:
    payload, error, _ = _call(
        client,
        "/v3/geocode/regeo",
        key,
        location=params.get("location"),
        radius=params.get("radius"),
        extensions="base",
    )
    if payload is None:
        return {"ok": False, "error": error}
    geo = payload.get("regeocode") if isinstance(payload.get("regeocode"), dict) else {}
    component = geo.get("addressComponent") if isinstance(geo.get("addressComponent"), dict) else {}
    rows = [
        {"item": "格式化地址", "value": _scalar(geo.get("formatted_address"))},
        {"item": "省", "value": _scalar(component.get("province"))},
        {"item": "市", "value": _scalar(component.get("city"))},
        {"item": "区县", "value": _scalar(component.get("district"))},
        {"item": "乡镇街道", "value": _scalar(component.get("township"))},
        {"item": "门牌", "value": _street(component.get("streetNumber"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出地址")


def _street(node) -> str:
    if not isinstance(node, dict):
        return "—"
    street = str(node.get("street") or "").strip()
    number = str(node.get("number") or "").strip()
    return " ".join(part for part in (street, number) if part) or "—"


def _h_district(client, key, params) -> dict:
    payload, error, _ = _call(
        client,
        "/v3/config/district",
        key,
        keywords=params.get("keywords"),
        subdistrict=params.get("subdistrict") or "1",
        extensions="base",
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows: list[dict] = []
    for top in payload.get("districts") or []:
        if not isinstance(top, dict):
            continue
        children = top.get("districts")
        # 命中的行政区若有下级，列下级；否则列它自己（高德对「无下级」会返回 [] 或 [[]]）
        listed = [child for child in children if isinstance(child, dict)] if isinstance(children, list) else []
        if listed:
            rows.extend(_pick(child, _DISTRICT_COLUMNS) for child in listed)
        else:
            rows.append(_pick(top, _DISTRICT_COLUMNS))
    return _rows(rows, _DISTRICT_COLUMNS, "未匹配到行政区划")


def _h_weather(client, key, params) -> dict:
    payload, error, _ = _call(
        client, "/v3/weather/weatherInfo", key, city=params.get("city"), extensions="base"
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows = [
        _pick(item, _LIVE_COLUMNS)
        for item in payload.get("lives") or []
        if isinstance(item, dict)
    ]
    return _rows(rows, _LIVE_COLUMNS, "未查询到实况天气")


def _h_forecast(client, key, params) -> dict:
    payload, error, _ = _call(
        client, "/v3/weather/weatherInfo", key, city=params.get("city"), extensions="all"
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows: list[dict] = []
    for item in payload.get("forecasts") or []:
        if not isinstance(item, dict):
            continue
        city = _scalar(item.get("city"))
        for cast in item.get("casts") or []:
            if isinstance(cast, dict):
                row = _pick(cast, _FORECAST_COLUMNS)
                row["city"] = city
                rows.append(row)
    return _rows(rows, _FORECAST_COLUMNS, "未查询到预报天气")


def _h_ip(client, key, params) -> dict:
    query_ip = str(params.get("ip") or "").strip()
    payload, error, _ = _call(client, "/v3/ip", key, ip=query_ip)
    if payload is None:
        return {"ok": False, "error": error}
    rows = [
        {"item": "IP", "value": query_ip or "（请求方出口 IP）"},
        {"item": "省", "value": _scalar(payload.get("province"))},
        {"item": "市", "value": _scalar(payload.get("city"))},
        {"item": "行政区划编码", "value": _scalar(payload.get("adcode"))},
        {"item": "矩形区域", "value": _scalar(payload.get("rectangle"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出归属地")


def _h_distance(client, key, params) -> dict:
    origins = str(params.get("origins") or "").strip()
    destination = str(params.get("destination") or "").strip()
    if not origins or not destination:
        return {"ok": False, "error": "请填写起点与终点经纬度"}
    payload, error, _ = _call(
        client, "/v3/distance", key, origins=origins, destination=destination, type="1"
    )
    if payload is None:
        return {"ok": False, "error": error}
    pairs = origins.split("|")
    rows: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            origin = pairs[int(str(item.get("origin_id"))) - 1]
        except (TypeError, ValueError, IndexError):
            origin = _scalar(item.get("origin_id"))
        rows.append({
            "origin": _scalar(origin),
            "distance": _scalar(item.get("distance")),
            "duration": _scalar(item.get("duration")),
        })
    return _rows(rows, _DISTANCE_COLUMNS, "未计算出距离")


_HANDLERS = {
    "geocode": _h_geocode,
    "regeo": _h_regeo,
    "district": _h_district,
    "weather": _h_weather,
    "forecast": _h_forecast,
    "ip": _h_ip,
    "distance": _h_distance,
}


# ── 操作声明 ─────────────────────────────────────────────

def actions(data) -> list:
    """声明可用操作——**只在「Web 服务」类型下**提供（其它平台的 Key 调不动这些 REST 接口）。"""
    data = data or {}
    if not str(data.get("key") or "").strip():
        return []
    if _key_type(data) != "web_service":
        return []
    return [
        {
            "id": "geocode",
            "name": "地理编码",
            "kind": "list",
            "group": "地理编码",
            "order": 10,
            "description": "结构化地址 → 经纬度坐标（可指定城市缩小范围）",
            "columns": _GEO_COLUMNS,
            "fields": [
                {"key": "address", "label": "地址", "type": "text", "required": True,
                 "placeholder": "例如：北京市朝阳区阜通东大街6号"},
                {"key": "city", "label": "城市", "type": "text", "required": False,
                 "placeholder": "可选，如 北京"},
            ],
            "submit": "查询",
            "empty_text": "未匹配到地址",
        },
        {
            "id": "regeo",
            "name": "逆地理编码",
            "kind": "list",
            "group": "地理编码",
            "order": 20,
            "description": "经纬度坐标 → 结构化地址（经纬度格式为「经度,纬度」）",
            "columns": _KV_COLUMNS,
            "fields": [
                {"key": "location", "label": "经纬度", "type": "text", "required": True,
                 "placeholder": "经度,纬度，例如 116.481488,39.990464"},
                {"key": "radius", "label": "搜索半径(米)", "type": "text", "required": False,
                 "placeholder": "可选，默认 1000"},
            ],
            "submit": "查询",
            "empty_text": "未解析出地址",
        },
        {
            "id": "district",
            "name": "行政区划",
            "kind": "list",
            "group": "行政区划",
            "order": 30,
            "description": "按关键词查行政区划；返回其下级行政区（留空关键词返回全国省级）",
            "columns": _DISTRICT_COLUMNS,
            "fields": [
                {"key": "keywords", "label": "关键词", "type": "text", "required": False,
                 "placeholder": "行政区名称 / 编码 / 经纬度，如 朝阳区、110000"},
                {"key": "subdistrict", "label": "下级层级", "type": "text", "required": False,
                 "placeholder": "0-3，默认 1（只列一级下级）"},
            ],
            "submit": "查询",
            "empty_text": "未匹配到行政区划",
        },
        {
            "id": "weather",
            "name": "实况天气",
            "kind": "list",
            "group": "天气",
            "order": 40,
            "description": "行政区划 → 实时天气（气温 / 风向 / 风力 / 湿度）",
            "columns": _LIVE_COLUMNS,
            "fields": [
                {"key": "city", "label": "行政区划编码或名称", "type": "text", "required": True,
                 "placeholder": "如 110000 或 北京市"},
            ],
            "submit": "查询",
            "empty_text": "未查询到实况天气",
        },
        {
            "id": "forecast",
            "name": "预报天气",
            "kind": "list",
            "group": "天气",
            "order": 50,
            "description": "行政区划 → 未来数日天气预报（白天 / 夜间）",
            "columns": _FORECAST_COLUMNS,
            "fields": [
                {"key": "city", "label": "行政区划编码或名称", "type": "text", "required": True,
                 "placeholder": "如 110000 或 北京市"},
            ],
            "submit": "查询",
            "empty_text": "未查询到预报天气",
        },
        {
            "id": "ip",
            "name": "IP 定位",
            "kind": "list",
            "group": "IP 定位",
            "order": 60,
            "description": "IP 归属地查询（留空 IP 则查请求方出口 IP）",
            "columns": _KV_COLUMNS,
            "fields": [
                {"key": "ip", "label": "IP 地址", "type": "text", "required": False,
                 "placeholder": "留空查询本机出口 IP"},
            ],
            "submit": "查询",
            "empty_text": "未解析出归属地",
        },
        {
            "id": "distance",
            "name": "距离测量",
            "kind": "list",
            "group": "距离测量",
            "order": 70,
            "description": "多起点到单终点的驾车距离与耗时；起点之间用 | 分隔（最多 100 个）",
            "columns": _DISTANCE_COLUMNS,
            "fields": [
                {"key": "origins", "label": "起点经纬度", "type": "text", "required": True,
                 "placeholder": "经度,纬度[|经度,纬度…]，如 116.481028,39.989643"},
                {"key": "destination", "label": "终点经纬度", "type": "text", "required": True,
                 "placeholder": "经度,纬度，如 116.434446,39.90816"},
            ],
            "submit": "测量",
            "empty_text": "未计算出距离",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    异常消息**只带异常类型名**，不带 ``str(exc)``——httpx 的异常文本里含完整请求 URL，
    而高德的 Key 就在 URL 的 query 里（见模块 docstring）。
    """
    params = params or {}
    data = data or {}
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return {"ok": False, "error": f"未知操作：{action_id}"}
    key = str(data.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少 Web 服务 Key"}
    try:
        with make_client() as client:
            return handler(client, key, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 验证 ─────────────────────────────────────────────────

def validate(data) -> list:
    if not data:
        return ["配置数据为空"]
    if not str(data.get("key") or "").strip():
        return ["Key 不能为空"]
    kt = str(data.get("key_type") or "").strip()
    if kt and kt not in _KEY_TYPES:
        return [f"未知的 Key 类型：{kt}"]
    return []


@guarded
def verify(data) -> VerifyResult:
    """按「Key 类型」验证。

    - **Web 服务**：用 IP 定位接口**实调**（无需额外参数、不消耗业务配额）。
    - **其它平台**：那类 Key 只能在客户端 SDK 里用，服务端调不动——于是改判「**Key 是否存在**」：
      平台不符（10009）说明它**存在**（即有效），只有「不存在 / 已过期」（10001）才判 invalid。
    """
    data = data or {}
    key = str(data.get("key") or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="Key 为空")

    kt = _key_type(data)
    with make_client() as client:
        payload, error, info = _probe(client, key)

    if payload is not None:
        # 附加信息只放**凭据自身**能确认的类型，不再展示请求方的 IP 归属
        # （那是验证出口的位置，与 Key 无关，走代理还会变）。
        # 调用既然成功，就说明它是「Web 服务」平台。
        account = {"Key 类型": "Web 服务"}
        if kt != "web_service":
            return VerifyResult(
                status="valid", account_info=account,
                message=(f"凭证有效——而且它是一个可用的「Web 服务」Key；"
                         f"你选的「{_KEY_TYPES[kt]}」可能记错了（该类型本该在客户端 SDK 里用）"))
        return VerifyResult(status="valid", message="凭证有效", account_info=account)

    if info == "USERKEY_PLAT_NOMATCH":
        # 平台不符 ⇒ Key **存在**。这正是「其它平台」判有效的依据：服务端替它做不了真实调用。
        if kt != "web_service":
            return VerifyResult(
                status="valid", account_info={"Key 类型": _KEY_TYPES[kt]},
                message=(f"该 Key 有效（存在），绑定的是「{_KEY_TYPES[kt]}」平台。它不能在服务端"
                         "调用 Web 服务接口，本插件只能确认它存在、无法替它做真实调用验证。"))
        return VerifyResult(
            status="invalid",
            message=("Key 与绑定的服务平台不符：它属于「Web 端(JS API)」等**非 Web 服务**类型。"
                     "把上面的「Key 类型」改成对应类型，即可按「该 Key 是否存在」判定；"
                     "要用本插件的各项查询，请到高德开放平台另建一个「Web 服务」Key。"))
    if info == "INVALID_USER_KEY":
        return VerifyResult(
            status="invalid",
            message=f"Key 不正确或已过期（{_KEY_TYPES[kt]} 平台下未找到该 Key）")

    verdict = _INFO.get(info, ("error", ""))[0]
    return VerifyResult(status=verdict or "error", message=error)


def get_display_summary(data) -> str:
    data = data or {}
    key = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {mask_secret(key)}" if key else NAME
