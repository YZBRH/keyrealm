"""腾讯位置服务（腾讯地图）WebService Key 验证插件（plugins/map/ 类别下的独立子插件）。

腾讯位置服务 WebService 接口一律 **HTTP 200 + body 内判定**：真正的结果在 ``status`` 里，
``0`` 为成功，其余看 ``message``（status 码另有专门的文档页）。因此这里不按 HTTP 状态码判成败。

签名校验（sig）
---------------
控制台创建 Key 时若勾选了「签名校验」，则每个请求都必须携带 ``sig``，否则服务端回
``status=111``（签名验证失败）。凭据里 ``sk``（Secret Key）为选填项，**填了才计算 sig**；
未填而恰好命中 111 时，验证消息会明确提示「可能是签名校验未开启签名所致」，而不是让用户
误以为 Key 已失效。

签名算法（``_sign``，与官方示例逐字对齐）：

    md5( path + "?" + <按参数名升序排列的 key=value，& 连接> + SK )  → 小写 32 位十六进制

两个要点：

- **参与签名的是「未编码的原始取值」**（中文即原文），``sig`` 本身不参与；
- ``SK`` **紧接在整串末尾、不加分隔符**（示例：``…&key=您的key`` + ``SK``）；
- 拼接时用**排序后**的参数；实际发送时对取值做一次 URL 编码，并按官方约定带上
  ``x-legacy-url-decode: no`` 头（「原始值签名 + 编码发送」这一组合的配套要求）。

``key`` / ``sk`` 都出现在 **query string** 中，所以异常路径只回显官方 status 码的中文映射，
**绝不拼接异常对象自身的文本**——httpx 异常文本含完整请求 URL，会把 Key 泄进日志。

坐标系与顺序：所有接口用 **GCJ-02**；``location`` 一律 **「纬度,经度」**（与高德的「经度,纬度」
相反，切勿套用）。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``geocode``  地理编码：结构化地址 → 经纬度
- ``regeo``    逆地址解析：经纬度 → 结构化地址
- ``place``    地点搜索：关键词 + 搜索范围 → POI 列表
- ``district`` 行政区划：关键词 → 行政区划（留空列出省级）
- ``distance`` 距离计算：一批起点 → 终点（驾车 / 步行）距离与耗时
- ``ip``       IP 定位：IP → 所在省市
- ``weather``  实况天气：行政区划编码 / 经纬度 → 实时天气
- ``forecast`` 预报天气：行政区划编码 / 经纬度 → 未来数日预报

参考文档：https://lbs.qq.com/service/webService/webServiceGuide/webServiceIp
参考文档：https://lbs.qq.com/service/webService/webServiceGuide/status
参考文档：https://lbs.qq.com/service/webService/webServiceGuide/getSig
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote

from backend.plugin_sdk import VerifyResult, guarded, make_client, mask_secret, safe_json

NAME = "腾讯地图"
BASE = "https://apis.map.qq.com"
DISPLAY_FIELD = "key"

# status → (判定, 说明)。分档遵循框架统一口径：**配额用尽 / 白名单 / 权限不足 / 签名未过 /
# 功能未开启属「有效但受限」**；只有明确指向「Key 本身无效或格式错误」的才判 invalid。
# 说明文本既进验证消息，也进操作报错。
_STATUS = {
    110: ("restricted", "请求来源未被授权（该 Key 配置了授权来源限制）"),
    111: ("restricted", "签名验证失败（该 Key 开启了「签名校验」，需在凭据中补填 SK）"),
    112: ("restricted", "本机出口 IP 未被授权（该 Key 配置了 IP 白名单）"),
    113: ("restricted", "该 Key 未获授权调用此功能"),
    120: ("restricted", "该 Key 的每秒请求量已达上限"),
    121: ("restricted", "该 Key 的每日调用量已达上限"),
    160: ("restricted", "sig 参数不适用于该请求类型"),
    161: ("restricted", "sig 参数不支持与非对象的 POST JSON 同用"),
    190: ("invalid", "无效的 Key"),
    199: ("restricted", "该 Key 未开启 WebService 功能（请在控制台勾选「WebServiceAPI」）"),
    300: ("error", "缺少必要字段"),
    301: ("error", "缺少必要字段 key"),
    306: ("error", "缺少参数"),
    310: ("error", "参数格式错误"),
    311: ("invalid", "Key 格式错误"),
    320: ("error", "参数数据类型错误"),
    347: ("error", "查询无结果"),
    375: ("restricted", "无法对局域网 IP 定位"),
    382: ("restricted", "该 IP 无法定位"),
    404: ("error", "请求路径错误"),
    405: ("error", "请求方法错误（应为 GET）"),
    500: ("error", "服务端超时"),
    510: ("error", "服务端无法连接上游服务"),
    520: ("error", "服务端请求失败"),
    530: ("error", "服务端返回数据解析失败"),
    531: ("error", "服务端报错"),
    600: ("error", "未知错误"),
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
    """腾讯坐标对象 ``{"lat":..,"lng":..}`` → ``"纬度,经度"``。"""
    if not isinstance(node, dict):
        return "—"
    lat, lng = node.get("lat"), node.get("lng")
    if lat is None or lng is None:
        return "—"
    return f"{lat},{lng}"


def _status_int(payload: dict) -> int:
    try:
        return int(payload.get("status"))
    except (TypeError, ValueError):
        return -1


# ── 签名与请求 ───────────────────────────────────────────

def _sign(path: str, pairs: list, sk: str) -> str:
    """计算 sig：``md5(path + "?" + 升序参数 + SK)``，小写十六进制。

    ``pairs`` 是 **(参数名, 未编码原始取值)** 列表，须已含 ``key``；``sig`` 自身不参与。
    """
    ordered = sorted(pairs, key=lambda kv: kv[0])
    raw = f"{path}?" + "&".join(f"{name}={value}" for name, value in ordered) + sk
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _build(path: str, params: dict, key: str, sk: str) -> tuple[str, dict]:
    """拼出完整 URL 与附加请求头。

    取值先按原始文本参与签名，再各自 URL 编码后发送；有 ``sk`` 时把 ``sig`` 追加为最后一个
    参数，并带上 ``x-legacy-url-decode: no``（「原始值签名 + 编码发送」的配套要求）。
    """
    pairs = [(name, str(value).strip()) for name, value in params.items()
             if value is not None and str(value).strip()]
    pairs.append(("key", key))
    headers: dict = {}
    if sk:
        pairs.append(("sig", _sign(path, pairs, sk)))
        headers["x-legacy-url-decode"] = "no"
    query = "&".join(f"{name}={quote(value, safe='')}" for name, value in pairs)
    return f"{BASE}{path}?{query}", headers


def _call(client, path: str, key: str, sk: str, **params):
    """调用一个腾讯 WebService 接口，返回 ``(payload, error, verdict)``；成功仅当 ``status == 0``。"""
    url, headers = _build(path, params, key, sk)
    resp = client.get(url, headers=headers)
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, f"响应无法解析（HTTP {resp.status_code}）", "error"
    status = _status_int(payload)
    if status == 0:
        return payload, "", ""
    verdict, label = _STATUS.get(status, ("error", ""))
    text = label or str(payload.get("message") or "").strip() or f"腾讯位置服务返回未知状态 {status}"
    if status == 111 and not sk:
        text += "；该 Key 可能已开启「签名校验」，请在凭据中补填「SK 密钥」后重试"
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
    {"key": "province", "label": "省", "width": 110},
    {"key": "city", "label": "市", "width": 110},
    {"key": "district", "label": "区县", "width": 110},
    {"key": "street", "label": "街道"},
    {"key": "adcode", "label": "行政区划编码", "mono": True, "width": 130},
    {"key": "reliability", "label": "可信度", "width": 90},
    {"key": "level", "label": "解析级别", "width": 110, "type": "tag"},
]
_PLACE_COLUMNS = [
    {"key": "title", "label": "名称"},
    {"key": "address", "label": "地址"},
    {"key": "province", "label": "省", "width": 100},
    {"key": "city", "label": "市", "width": 100},
    {"key": "district", "label": "区县", "width": 100},
    {"key": "location", "label": "经纬度", "mono": True, "width": 170},
    {"key": "category", "label": "分类", "width": 130, "type": "tag"},
    {"key": "distance", "label": "距离(米)", "width": 100},
]
_DISTRICT_COLUMNS = [
    {"key": "fullname", "label": "名称"},
    {"key": "id", "label": "行政区划编码", "mono": True, "width": 130},
    {"key": "level", "label": "级别", "width": 90},
    {"key": "location", "label": "中心点", "mono": True, "width": 170},
    {"key": "pinyin", "label": "拼音", "width": 150},
]
_DISTANCE_COLUMNS = [
    {"key": "origin", "label": "起点", "mono": True, "width": 170},
    {"key": "destination", "label": "终点", "mono": True, "width": 170},
    {"key": "distance", "label": "距离（米）", "width": 110},
    {"key": "duration", "label": "预计耗时（秒）", "width": 130},
]
_FORECAST_COLUMNS = [
    {"key": "date", "label": "日期", "width": 120},
    {"key": "week", "label": "星期", "width": 90},
    {"key": "day_weather", "label": "白天天气"},
    {"key": "night_weather", "label": "夜间天气"},
    {"key": "day_temp", "label": "白天(℃)", "width": 100},
    {"key": "night_temp", "label": "夜间(℃)", "width": 100},
    {"key": "day_wind", "label": "白天风向", "width": 110},
    {"key": "night_wind", "label": "夜间风力", "width": 110},
]


# ── 操作实现 ─────────────────────────────────────────────

def _h_geocode(client, key, sk, params) -> dict:
    payload, error, _ = _call(client, "/ws/geocoder/v1/", key, sk, address=params.get("address"))
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    comp = result.get("address_components") if isinstance(result.get("address_components"), dict) else {}
    ad = result.get("ad_info") if isinstance(result.get("ad_info"), dict) else {}
    row = {
        "location": _latlng(result.get("location")),
        "province": _scalar(comp.get("province")),
        "city": _scalar(comp.get("city")),
        "district": _scalar(comp.get("district")),
        "street": _scalar(comp.get("street")),
        "adcode": _scalar(ad.get("adcode")),
        "reliability": _scalar(result.get("reliability")),
        "level": _scalar(result.get("level")),
    }
    return _rows([row], _GEO_COLUMNS, "未匹配到地址")


def _h_regeo(client, key, sk, params) -> dict:
    payload, error, _ = _call(
        client, "/ws/geocoder/v1/", key, sk, location=params.get("location"), get_poi="0"
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    fmt = result.get("formatted_addresses") if isinstance(result.get("formatted_addresses"), dict) else {}
    comp = result.get("address_component") if isinstance(result.get("address_component"), dict) else {}
    ad = result.get("ad_info") if isinstance(result.get("ad_info"), dict) else {}
    rows = [
        {"item": "完整地址", "value": _scalar(result.get("address"))},
        {"item": "推荐描述", "value": _scalar(fmt.get("recommend"))},
        {"item": "省", "value": _scalar(comp.get("province"))},
        {"item": "市", "value": _scalar(comp.get("city"))},
        {"item": "区县", "value": _scalar(comp.get("district"))},
        {"item": "街道", "value": _scalar(comp.get("street"))},
        {"item": "门牌", "value": _scalar(comp.get("street_number"))},
        {"item": "行政区划编码", "value": _scalar(ad.get("adcode"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出地址")


def _h_place(client, key, sk, params) -> dict:
    payload, error, _ = _call(
        client,
        "/ws/place/v1/search",
        key,
        sk,
        keyword=params.get("keyword"),
        boundary=params.get("boundary"),
        page_size=params.get("page_size"),
        page_index=params.get("page_index"),
    )
    if payload is None:
        return {"ok": False, "error": error}
    rows: list[dict] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        ad = item.get("ad_info") if isinstance(item.get("ad_info"), dict) else {}
        rows.append({
            "title": _scalar(item.get("title")),
            "address": _scalar(item.get("address")),
            "province": _scalar(ad.get("province")),
            "city": _scalar(ad.get("city")),
            "district": _scalar(ad.get("district")),
            "location": _latlng(item.get("location")),
            "category": _scalar(item.get("category")),
            "distance": _scalar(item.get("_distance")),
        })
    return _rows(rows, _PLACE_COLUMNS, "未检索到地点")


def _h_district(client, key, sk, params) -> dict:
    keyword = str(params.get("keyword") or "").strip()
    if keyword:
        payload, error, _ = _call(client, "/ws/district/v1/search", key, sk, keyword=keyword)
    else:
        payload, error, _ = _call(client, "/ws/district/v1/list", key, sk)
    if payload is None:
        return {"ok": False, "error": error}
    # 行政区划接口的 result 是**二维数组**：result[0] 省级、result[1] 市级、result[2] 区县级
    # （每项内部才是 {id, name, fullname, location, cidx…} 字典）。带关键词搜索时命中项在
    # 第一层；不带关键词（list）会一次给出全国三级区划，故只取第一层（省级），不铺整棵国树。
    data = payload.get("result") or []
    levels = [data[0]] if (not keyword and data) else data
    rows: list[dict] = []
    for level in levels:
        items = level if isinstance(level, list) else [level]
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append({
                "fullname": _scalar(item.get("fullname") or item.get("name")),
                "id": _scalar(item.get("id")),
                "level": _scalar(item.get("level")),
                "location": _latlng(item.get("location")),
                "pinyin": _scalar(item.get("pinyin")),
            })
    return _rows(rows, _DISTRICT_COLUMNS, "未匹配到行政区划")


def _h_distance(client, key, sk, params) -> dict:
    origin = str(params.get("from") or "").strip()
    destination = str(params.get("to") or "").strip()
    if not origin or not destination:
        return {"ok": False, "error": "请填写起点与终点经纬度"}
    payload, error, _ = _call(
        client, "/ws/distance/v1/", key, sk,
        mode=params.get("mode") or "driving", **{"from": origin, "to": destination},
    )
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    rows = []
    for item in result.get("elements") or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "origin": _latlng(item.get("from")),
            "destination": _latlng(item.get("to")),
            "distance": _scalar(item.get("distance")),
            "duration": _scalar(item.get("duration")),
        })
    return _rows(rows, _DISTANCE_COLUMNS, "未计算出距离")


def _h_ip(client, key, sk, params) -> dict:
    query_ip = str(params.get("ip") or "").strip()
    payload, error, _ = _call(client, "/ws/location/v1/ip", key, sk, ip=query_ip)
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    ad = result.get("ad_info") if isinstance(result.get("ad_info"), dict) else {}
    rows = [
        {"item": "IP", "value": _scalar(result.get("ip")) or query_ip or "（请求方出口 IP）"},
        {"item": "国家", "value": _scalar(ad.get("nation"))},
        {"item": "省", "value": _scalar(ad.get("province"))},
        {"item": "市", "value": _scalar(ad.get("city"))},
        {"item": "区县", "value": _scalar(ad.get("district"))},
        {"item": "行政区划编码", "value": _scalar(ad.get("adcode"))},
        {"item": "经纬度", "value": _latlng(result.get("location"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未解析出归属地")


def _weather_target(params) -> dict:
    """天气接口的 adcode / location 二选一。"""
    return {"adcode": params.get("adcode"), "location": params.get("location")}


def _h_weather(client, key, sk, params) -> dict:
    target = _weather_target(params)
    if not str(target["adcode"] or "").strip() and not str(target["location"] or "").strip():
        return {"ok": False, "error": "请填写「行政区划编码」或「经纬度」之一"}
    payload, error, _ = _call(client, "/ws/weather/v1/", key, sk, type="now", **target)
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    realtime = result.get("realtime") or []
    first = realtime[0] if realtime and isinstance(realtime[0], dict) else {}
    infos = first.get("infos") if isinstance(first.get("infos"), dict) else {}
    rows = [
        {"item": "行政区划", "value": _scalar(first.get("city") or first.get("district") or first.get("adcode"))},
        {"item": "天气", "value": _scalar(infos.get("weather"))},
        {"item": "气温(℃)", "value": _scalar(infos.get("temperature"))},
        {"item": "风向", "value": _scalar(infos.get("wind_direction"))},
        {"item": "风力", "value": _scalar(infos.get("wind_power"))},
        {"item": "相对湿度(%)", "value": _scalar(infos.get("humidity"))},
        {"item": "气压(hPa)", "value": _scalar(infos.get("air_pressure"))},
        {"item": "更新时间", "value": _scalar(first.get("update_time"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未查询到实况天气")


def _h_forecast(client, key, sk, params) -> dict:
    target = _weather_target(params)
    if not str(target["adcode"] or "").strip() and not str(target["location"] or "").strip():
        return {"ok": False, "error": "请填写「行政区划编码」或「经纬度」之一"}
    payload, error, _ = _call(client, "/ws/weather/v1/", key, sk, type="future", **target)
    if payload is None:
        return {"ok": False, "error": error}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    forecast = result.get("forecast") or []
    first = forecast[0] if forecast and isinstance(forecast[0], dict) else {}
    rows: list[dict] = []
    for item in first.get("infos") or []:
        if not isinstance(item, dict):
            continue
        day = item.get("day") if isinstance(item.get("day"), dict) else {}
        night = item.get("night") if isinstance(item.get("night"), dict) else {}
        rows.append({
            "date": _scalar(item.get("date")),
            "week": _scalar(item.get("week")),
            "day_weather": _scalar(day.get("weather")),
            "night_weather": _scalar(night.get("weather")),
            "day_temp": _scalar(day.get("temperature")),
            "night_temp": _scalar(night.get("temperature")),
            "day_wind": _scalar(day.get("wind_direction")),
            "night_wind": _scalar(night.get("wind_power")),
        })
    return _rows(rows, _FORECAST_COLUMNS, "未查询到预报天气")


_HANDLERS = {
    "geocode": _h_geocode,
    "regeo": _h_regeo,
    "place": _h_place,
    "district": _h_district,
    "distance": _h_distance,
    "ip": _h_ip,
    "weather": _h_weather,
    "forecast": _h_forecast,
}

# 天气两项共用的输入字段
_WEATHER_FIELDS = [
    {"key": "adcode", "label": "行政区划编码", "type": "text", "required": False,
     "placeholder": "如 110000；与「经纬度」二选一"},
    {"key": "location", "label": "经纬度（选填）", "type": "text", "required": False,
     "placeholder": "纬度,经度，如 39.905023,116.724502；与「行政区划编码」二选一"},
]


# ── 操作声明 ─────────────────────────────────────────────

def actions(data) -> list:
    """声明可用操作（凭据含 WebService Key 时提供只读查询）。"""
    data = data or {}
    if not str(data.get("key") or "").strip():
        return []
    return [
        {
            "id": "geocode",
            "name": "地理编码",
            "kind": "list",
            "group": "地理编码",
            "order": 10,
            "description": "结构化地址 → 经纬度坐标（地址需含省市区信息）",
            "columns": _GEO_COLUMNS,
            "fields": [
                {"key": "address", "label": "地址", "type": "text", "required": True,
                 "placeholder": "例如：北京市海淀区彩和坊路海淀西大街74号"},
            ],
            "submit": "查询",
            "empty_text": "未匹配到地址",
        },
        {
            "id": "regeo",
            "name": "逆地址解析",
            "kind": "list",
            "group": "地理编码",
            "order": 20,
            "description": "经纬度坐标 → 结构化地址（经纬度格式为「纬度,经度」）",
            "columns": _KV_COLUMNS,
            "fields": [
                {"key": "location", "label": "经纬度", "type": "text", "required": True,
                 "placeholder": "纬度,经度，例如 39.984154,116.307461"},
            ],
            "submit": "查询",
            "empty_text": "未解析出地址",
        },
        {
            "id": "place",
            "name": "地点搜索",
            "kind": "list",
            "group": "地点检索",
            "order": 30,
            "description": "关键词 + 搜索范围 → POI 列表（含坐标、地址、分类）",
            "columns": _PLACE_COLUMNS,
            "fields": [
                {"key": "keyword", "label": "关键词", "type": "text", "required": True,
                 "placeholder": "如 银行、天安门"},
                {"key": "boundary", "label": "搜索范围", "type": "text", "required": True,
                 "placeholder": "region(北京,0) 或 nearby(39.908491,116.374328,1000) 或 rectangle(39.9,116.3,40.0,116.5)"},
                {"key": "page_size", "label": "每页条数", "type": "text", "required": False,
                 "placeholder": "1-20，默认 10"},
                {"key": "page_index", "label": "页码", "type": "text", "required": False,
                 "placeholder": "从 1 开始，默认 1"},
            ],
            "submit": "检索",
            "empty_text": "未检索到地点",
        },
        {
            "id": "district",
            "name": "行政区划",
            "kind": "list",
            "group": "行政区划",
            "order": 40,
            "description": "按关键词查行政区划（名称 / adcode）；留空关键词列出全国省级",
            "columns": _DISTRICT_COLUMNS,
            "fields": [
                {"key": "keyword", "label": "关键词", "type": "text", "required": False,
                 "placeholder": "行政区名称或 adcode，如 朝阳区、110000；留空列出省级"},
            ],
            "submit": "查询",
            "empty_text": "未匹配到行政区划",
        },
        {
            "id": "distance",
            "name": "距离计算",
            "kind": "list",
            "group": "距离计算",
            "order": 50,
            "description": "一批起点到终点（或一对多）的驾车 / 步行距离与耗时；坐标用 ; 分隔，from 与 to 仅可其一为多坐标",
            "columns": _DISTANCE_COLUMNS,
            "fields": [
                {"key": "mode", "label": "出行方式", "type": "select", "required": True,
                 "options": [{"value": "driving", "label": "驾车"}, {"value": "walking", "label": "步行"}]},
                {"key": "from", "label": "起点经纬度", "type": "text", "required": True,
                 "placeholder": "纬度,经度[;纬度,经度…]，如 39.984154,116.307461"},
                {"key": "to", "label": "终点经纬度", "type": "text", "required": True,
                 "placeholder": "纬度,经度[;纬度,经度…]，如 39.975116,116.324182"},
            ],
            "submit": "计算",
            "empty_text": "未计算出距离",
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
            "id": "weather",
            "name": "实况天气",
            "kind": "list",
            "group": "天气",
            "order": 70,
            "description": "行政区划编码 / 经纬度 → 实时天气（气温 / 风向风力 / 湿度）",
            "columns": _KV_COLUMNS,
            "fields": _WEATHER_FIELDS,
            "submit": "查询",
            "empty_text": "未查询到实况天气",
        },
        {
            "id": "forecast",
            "name": "预报天气",
            "kind": "list",
            "group": "天气",
            "order": 80,
            "description": "行政区划编码 / 经纬度 → 未来数日天气预报（白天 / 夜间）",
            "columns": _FORECAST_COLUMNS,
            "fields": _WEATHER_FIELDS,
            "submit": "查询",
            "empty_text": "未查询到预报天气",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    异常消息**只带异常类型名**，不带 ``str(exc)``——httpx 的异常文本里含完整请求 URL，
    而 Key / SK 就在 URL 的 query 里（见模块 docstring）。
    """
    params = params or {}
    data = data or {}
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return {"ok": False, "error": f"未知操作：{action_id}"}
    key = str(data.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少 WebService Key"}
    sk = str(data.get("sk") or "").strip()
    try:
        with make_client() as client:
            return handler(client, key, sk, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 验证 ─────────────────────────────────────────────────

def validate(data) -> list:
    if not data:
        return ["配置数据为空"]
    if not str(data.get("key") or "").strip():
        return ["WebService Key 不能为空"]
    return []


@guarded
def verify(data) -> VerifyResult:
    """用 IP 定位接口验证 Key：该接口无需额外参数、不消耗业务配额。"""
    data = data or {}
    key = str(data.get("key") or "").strip()
    sk = str(data.get("sk") or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="WebService Key 为空")

    with make_client() as client:
        payload, error, verdict = _call(client, "/ws/location/v1/ip", key, sk)

    if payload is not None:
        # 附加信息只放凭据自身能确认的属性：调用成功即说明该 Key 启用了 WebServiceAPI；
        # 签名状态按是否填了 sk 如实展示（不再展示请求方 IP 的省市——那与 Key 无关）。
        account = {"Key 类型": "WebService", "签名校验": "已启用" if sk else "未填"}
        return VerifyResult(status="valid", message="凭证有效", account_info=account)

    return VerifyResult(status=verdict or "error", message=error)


def get_display_summary(data) -> str:
    data = data or {}
    key = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {mask_secret(key)}" if key else NAME
