"""百度地图 服务端 AK 验证插件（plugins/map/ 类别下的独立子插件）。

百度地图 Web 服务接口一律 **HTTP 200 + body 内判定**：真正的结果在 ``status`` 里，
``0`` 为成功，其余看 ``message``。因此这里不按 HTTP 状态码判成败。

SN 校验与签名
-------------
控制台创建应用时若勾选了「SN 校验」，则每个请求都必须携带 ``sn`` 签名，否则服务端一律
回 ``status=200``、``"APP不存在，AK有误请检查再重试"``——**与 AK 真的不存在时的响应逐字
相同，无法区分**。这是本插件必须处理的核心陷阱：

- 凭据里 ``sk``（私钥）为选填项；填了才计算 ``sn``；
- 未填 ``sk`` 而恰好命中上述响应时，验证消息会明确提示「可能是 SN 校验未开启签名所致」，
  而不是让用户误以为 AK 已失效。

签名算法（``_sign``，已与官方示例向量逐字对齐）：

    md5( percent_encode( path + "?" + query + sk ) )  → 小写十六进制

其中：

- ``query`` 里的**取值**先各自 percent-encode 一次（与真正上网的字节一致，中文地址即
  ``%E7%99%BE...``）；
- **GET 的键顺序为插入顺序，不排序**（POST 才按 a–z 排序）；
- 拼好的整串**再整体 percent-encode 一次**（即 ``%`` → ``%25``）后才进 MD5；
- ``sn`` 必须是**最后一个** query 参数。

正因为签名覆盖的是「真正发出去的字节」，本插件**手工拼完整 URL 字符串**交给 httpx，
绝不使用 ``params=``——后者会按 httpx 自己的规则重新编码，签名字节与实际字节一旦不一致，
SN 型 AK 就会恒定报 ``status=200``。

``ak`` 同样出现在 **query string** 中，所以异常路径只回显官方状态码的中文映射，
**绝不拼接异常对象自身的文本**——httpx 异常文本含完整请求 URL，会把 AK 泄进日志。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``geocode``  地理编码：结构化地址 → 经纬度
- ``regeo``    逆地理编码：经纬度 → 结构化地址
- ``place``    地点检索：关键词 + 行政区 → POI 列表
- ``ip``       IP 定位：IP → 所在省市
- ``weather``  实况天气：行政区划编码 → 实时天气
- ``forecast`` 预报天气：行政区划编码 → 未来数日预报

参考文档：https://lbsyun.baidu.com/faq/api?title=webapi/appendix
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote

from backend.plugin_sdk import VerifyResult, guarded, make_client, mask_secret, safe_json

NAME = "百度地图"
BASE = "https://api.map.baidu.com"
DISPLAY_FIELD = "ak"

# 须走 SN 校验却未带签名的 AK，会与「AK 不存在」返回同一句提示。见模块 docstring。
_SN_TRAP = {200, 211, 102}

# status → (判定, 说明)。口径同框架：**配额用尽 / 白名单 / 权限不足 / 被禁用属「有效但
# 受限」**；只有明确指向「AK 本身不存在或非法」的才是 invalid。
_STATUS = {
    0: ("valid", "凭证有效"),
    1: ("error", "服务端内部错误"),
    2: ("error", "请求参数非法"),
    3: ("restricted", "权限校验失败"),
    4: ("restricted", "配额校验失败"),
    5: ("invalid", "AK 不存在或非法"),
    101: ("invalid", "请求未携带 AK 参数"),
    102: ("restricted", "AK 已被禁用"),
    200: ("invalid", "APP 不存在，AK 有误"),
    201: ("restricted", "APP 已被禁用"),
    202: ("restricted", "APP 已被删除"),
    203: ("restricted", "AK 未绑定任何服务"),
    204: ("restricted", "AK 未开通该服务的权限"),
    210: ("restricted", "配额超限"),
    211: ("restricted", "权限校验失败（未通过 SN 签名校验）"),
    212: ("restricted", "权限校验失败（签名或时间戳无效）"),
    220: ("restricted", "AK 未授权当前来源 IP"),
    240: ("restricted", "该 AK 无权限请求此接口"),
    251: ("restricted", "已达该 AK 的日调用量上限"),
    260: ("restricted", "AK 未通过域名白名单校验"),
    300: ("valid", "凭证有效（本次查询无结果）"),
}


# ── 通用小工具 ───────────────────────────────────────────

def _scalar(value) -> str:
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


def _latlng(node) -> str:
    """百度把坐标回成 ``{"lat":..,"lng":..}``（IP 接口是 ``{"x":..,"y":..}``）。"""
    if not isinstance(node, dict):
        return "—"
    lat = node.get("lat", node.get("y"))
    lng = node.get("lng", node.get("x"))
    if lat is None or lng is None:
        return "—"
    return f"{lat},{lng}"


def _status_int(payload: dict) -> int:
    try:
        return int(payload.get("status"))
    except (TypeError, ValueError):
        return -1


def _sign(path: str, query: str, sk: str) -> str:
    """计算 sn：``md5(quote(f"{path}?{query}{sk}", safe=""))``，小写十六进制。"""
    raw = f"{path}?{query}{sk}"
    return hashlib.md5(quote(raw, safe="").encode("utf-8")).hexdigest()


def _build_url(path: str, params: dict, ak: str, sk: str) -> str:
    """手工拼完整 URL：ak 追加在末尾，sn（若有）再追加在其后。

    取值只 percent-encode 一次，即真正上网的字节；``params`` 的顺序被保留（GET 不排序）。
    """
    pairs = [(name, str(value).strip()) for name, value in params.items() if str(value).strip()]
    pairs.append(("ak", ak))
    query = "&".join(f"{name}={quote(value, safe='')}" for name, value in pairs)
    if sk:
        query = f"{query}&sn={_sign(path, query, sk)}"
    return f"{BASE}{path}?{query}"


def _call(client, path: str, ak: str, sk: str, **params):
    """调用一个百度接口，返回 ``(payload, error, verdict)``；成功仅当 ``status == 0``。

    失败时 ``payload`` 为 None，``error`` 可直接展示，``verdict`` 供 VerifyResult 使用。
    """
    resp = client.get(_build_url(path, params, ak, sk))
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, f"响应无法解析（HTTP {resp.status_code}）", "error"
    status = _status_int(payload)
    if status == 0:
        return payload, "", ""
    verdict, label = _STATUS.get(status, ("error", ""))
    message = str(payload.get("message") or "").strip()
    text = label or message or f"百度返回未知状态 {status}"
    if status in _SN_TRAP and not sk:
        text += "；该 AK 可能已开启「SN 校验」，请在凭据中补填「私钥 SK」后重试"
    return None, text, verdict


def _rows(rows: list, columns, empty_text: str) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows),
        "has_more": False,
        "empty_text": empty_text,
    }


# ── 列定义 ───────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]
_GEO_COLUMNS = [
    {"key": "location", "label": "经纬度", "mono": True, "width": 170},
    {"key": "precise", "label": "是否精确", "width": 100, "type": "tag"},
    {"key": "confidence", "label": "置信度", "width": 100},
    {"key": "comprehension", "label": "解析度", "width": 100},
    {"key": "level", "label": "匹配级别", "width": 120, "type": "tag"},
]
_PLACE_COLUMNS = [
    {"key": "name", "label": "名称"},
    {"key": "address", "label": "地址"},
    {"key": "province", "label": "省", "width": 100},
    {"key": "city", "label": "市", "width": 100},
    {"key": "area", "label": "区县", "width": 100},
    {"key": "location", "label": "经纬度", "mono": True, "width": 170},
    {"key": "tag", "label": "分类", "width": 120, "type": "tag"},
]
_LIVE_COLUMNS = _KV_COLUMNS
_FORECAST_COLUMNS = [
    {"key": "date", "label": "日期", "width": 120},
    {"key": "week", "label": "星期", "width": 90},
    {"key": "text_day", "label": "白天天气"},
    {"key": "text_night", "label": "夜间天气"},
    {"key": "high", "label": "最高(℃)", "width": 100},
    {"key": "low", "label": "最低(℃)", "width": 100},
    {"key": "wd_day", "label": "白天风向", "width": 110},
    {"key": "wc_day", "label": "白天风力", "width": 110},
]


# ── 操作实现 ─────────────────────────────────────────────

def _h_geocode(client, ak, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/geocoding/v3/",
        ak,
        sk,
        address=params.get("address"),
        city=params.get("city"),
        output="json",
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    row = {
        "location": _latlng(result.get("location")),
        "precise": _scalar(result.get("precise")),
        "confidence": _scalar(result.get("confidence")),
        "comprehension": _scalar(result.get("comprehension")),
        "level": _scalar(result.get("level")),
    }
    return _rows([row], _GEO_COLUMNS, "未匹配到地址")


def _h_regeo(client, ak, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/reverse_geocoding/v3/",
        ak,
        sk,
        location=params.get("location"),
        coordtype=params.get("coordtype"),
        output="json",
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    component = result.get("addressComponent") if isinstance(result.get("addressComponent"), dict) else {}
    rows = [
        {"item": "格式化地址", "value": _scalar(result.get("formatted_address"))},
        {"item": "经纬度", "value": _latlng(result.get("location"))},
        {"item": "省", "value": _scalar(component.get("province"))},
        {"item": "市", "value": _scalar(component.get("city"))},
        {"item": "区县", "value": _scalar(component.get("district"))},
        {"item": "乡镇街道", "value": _scalar(component.get("town"))},
        {"item": "街道门牌", "value": _scalar(component.get("street"))},
        {"item": "所在商圈", "value": _scalar(result.get("business"))},
        {"item": "方位描述", "value": _scalar(result.get("sematic_description"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出地址")


def _h_place(client, ak, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/place/v2/search",
        ak,
        sk,
        query=params.get("query"),
        region=params.get("region"),
        tag=params.get("tag"),
        page_size=params.get("page_size") or "10",
        page_num=params.get("page_num") or "0",
        scope="2",
        output="json",
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        row = _pick(item, _PLACE_COLUMNS)
        row["location"] = _latlng(item.get("location"))
        detail = item.get("detail_info") if isinstance(item.get("detail_info"), dict) else {}
        if detail.get("tag"):
            row["tag"] = _scalar(detail.get("tag"))
        rows.append(row)
    return _rows(rows, _PLACE_COLUMNS, "未检索到地点")


def _h_ip(client, ak, sk, params) -> dict:
    query_ip = str(params.get("ip") or "").strip()
    payload, error, _ = _call(client, "/location/ip", ak, sk, ip=query_ip, coor="bd09ll")
    if payload is None:
        return {"ok": False, "error": error}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    detail = content.get("address_detail") if isinstance(content.get("address_detail"), dict) else {}
    rows = [
        {"item": "IP", "value": query_ip or "（请求方出口 IP）"},
        {"item": "归属地", "value": _scalar(payload.get("address") or content.get("address"))},
        {"item": "省", "value": _scalar(detail.get("province"))},
        {"item": "市", "value": _scalar(detail.get("city"))},
        {"item": "区县", "value": _scalar(detail.get("district"))},
        {"item": "行政区划编码", "value": _scalar(detail.get("city_code"))},
        {"item": "经纬度", "value": _latlng(content.get("point"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出归属地")


def _h_weather(client, ak, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/weather/v1/",
        ak,
        sk,
        district_id=params.get("district_id"),
        data_type="now",
        output="json",
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    now = result.get("now") if isinstance(result.get("now"), dict) else {}
    rows = [
        {"item": "行政区划", "value": _scalar(result.get("location") or params.get("district_id"))},
        {"item": "天气", "value": _scalar(now.get("text"))},
        {"item": "气温(℃)", "value": _scalar(now.get("temp"))},
        {"item": "体感温度(℃)", "value": _scalar(now.get("feels_like"))},
        {"item": "相对湿度(%)", "value": _scalar(now.get("rh"))},
        {"item": "风向", "value": _scalar(now.get("wind_dir"))},
        {"item": "风力", "value": _scalar(now.get("wind_class"))},
        {"item": "能见度(km)", "value": _scalar(now.get("vis"))},
        {"item": "发布时间", "value": _scalar(now.get("uptime"))},
    ]
    return _rows(rows, _LIVE_COLUMNS, "未查询到实况天气")


def _h_forecast(client, ak, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/weather/v1/",
        ak,
        sk,
        district_id=params.get("district_id"),
        data_type="fc",
        output="json",
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    rows = [
        _pick(item, _FORECAST_COLUMNS)
        for item in result.get("forecasts") or []
        if isinstance(item, dict)
    ]
    return _rows(rows, _FORECAST_COLUMNS, "未查询到预报天气")


_HANDLERS = {
    "geocode": _h_geocode,
    "regeo": _h_regeo,
    "place": _h_place,
    "ip": _h_ip,
    "weather": _h_weather,
    "forecast": _h_forecast,
}


# ── 操作声明 ─────────────────────────────────────────────

def actions(data) -> list:
    """声明可用操作（凭据含 AK 时提供只读查询）。"""
    data = data or {}
    if not str(data.get("ak") or "").strip():
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
                 "placeholder": "例如：北京市海淀区上地十街10号"},
                {"key": "city", "label": "城市", "type": "text", "required": False,
                 "placeholder": "可选，如 北京市"},
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
            "description": "经纬度坐标 → 结构化地址（经纬度格式为「纬度,经度」，注意与高德相反）",
            "columns": _KV_COLUMNS,
            "fields": [
                {"key": "location", "label": "经纬度", "type": "text", "required": True,
                 "placeholder": "纬度,经度，例如 39.983424,116.322987"},
                {"key": "coordtype", "label": "坐标系", "type": "text", "required": False,
                 "placeholder": "可选：bd09ll / gcj02ll / wgs84ll，默认 bd09ll"},
            ],
            "submit": "查询",
            "empty_text": "未解析出地址",
        },
        {
            "id": "place",
            "name": "地点检索",
            "kind": "list",
            "group": "地点检索",
            "order": 30,
            "description": "关键词 + 行政区 → POI 列表（含坐标、地址、分类）",
            "columns": _PLACE_COLUMNS,
            "fields": [
                {"key": "query", "label": "关键词", "type": "text", "required": True,
                 "placeholder": "如 银行、天安门"},
                {"key": "region", "label": "行政区", "type": "text", "required": True,
                 "placeholder": "如 北京市、131（城市编码）"},
                {"key": "tag", "label": "分类筛选", "type": "text", "required": False,
                 "placeholder": "可选，如 美食"},
                {"key": "page_size", "label": "每页条数", "type": "text", "required": False,
                 "placeholder": "1-20，默认 10"},
                {"key": "page_num", "label": "页码", "type": "text", "required": False,
                 "placeholder": "从 0 开始，默认 0"},
            ],
            "submit": "检索",
            "empty_text": "未检索到地点",
        },
        {
            "id": "ip",
            "name": "IP 定位",
            "kind": "list",
            "group": "IP 定位",
            "order": 40,
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
            "id": "weather",
            "name": "实况天气",
            "kind": "list",
            "group": "天气",
            "order": 50,
            "description": "行政区划编码 → 实时天气（气温 / 体感 / 湿度 / 风向风力）",
            "columns": _LIVE_COLUMNS,
            "fields": [
                {"key": "district_id", "label": "行政区划编码", "type": "text", "required": True,
                 "placeholder": "如 110100（北京市），可先跑「地点检索」拿 city_code"},
            ],
            "submit": "查询",
            "empty_text": "未查询到实况天气",
        },
        {
            "id": "forecast",
            "name": "预报天气",
            "kind": "list",
            "group": "天气",
            "order": 60,
            "description": "行政区划编码 → 未来数日天气预报（白天 / 夜间）",
            "columns": _FORECAST_COLUMNS,
            "fields": [
                {"key": "district_id", "label": "行政区划编码", "type": "text", "required": True,
                 "placeholder": "如 110100（北京市）"},
            ],
            "submit": "查询",
            "empty_text": "未查询到预报天气",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    异常消息**只带异常类型名**，不带 ``str(exc)``——httpx 的异常文本里含完整请求 URL，
    而 AK 就在 URL 的 query 里（见模块 docstring）。
    """
    params = params or {}
    data = data or {}
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return {"ok": False, "error": f"未知操作：{action_id}"}
    ak = str(data.get("ak") or "").strip()
    if not ak:
        return {"ok": False, "error": "缺少服务端 AK"}
    sk = str(data.get("sk") or "").strip()
    try:
        with make_client() as client:
            return handler(client, ak, sk, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 验证 ─────────────────────────────────────────────────

def validate(data) -> list:
    if not data:
        return ["配置数据为空"]
    if not str(data.get("ak") or "").strip():
        return ["服务端 AK 不能为空"]
    return []


@guarded
def verify(data) -> VerifyResult:
    """用 IP 定位接口验证 AK：无需额外参数，且不消耗配额型服务。"""
    data = data or {}
    ak = str(data.get("ak") or "").strip()
    sk = str(data.get("sk") or "").strip()
    if not ak:
        return VerifyResult(status="invalid", message="服务端 AK 为空")

    with make_client() as client:
        payload, error, verdict = _call(client, "/location/ip", ak, sk, coor="bd09ll")

    if payload is not None:
        # 附加信息只放凭据自身能确认的属性：调用成功即说明这是「服务端 AK」；
        # 签名状态按是否填了 sk 如实展示（不再展示请求方 IP 的省市——那与 AK 无关）。
        account = {"AK 类型": "服务端", "SN 签名": "已启用" if sk else "未填"}
        return VerifyResult(status="valid", message="凭证有效", account_info=account)

    return VerifyResult(status=verdict or "error", message=error)


def get_display_summary(data) -> str:
    data = data or {}
    ak = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {mask_secret(ak)}" if ak else NAME
