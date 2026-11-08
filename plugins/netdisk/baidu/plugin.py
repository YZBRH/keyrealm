# coding: utf-8
"""百度网盘（个人云）access_token 插件（plugins/netdisk/ 类别下的独立子插件）。

验证与查询全部走**网盘开放平台**的只读接口，只依赖 OAuth 授权拿到的 access_token：

- 验证：``GET /rest/2.0/xpan/nas?method=uinfo``（返回账号名、网盘昵称、uk、会员类型）；
- 操作：账号信息（``/rest/2.0/xpan/nas?method=uinfo``，并合并 ``/api/quota`` 的容量）、
  文件列表（``xpan/file?method=list``）、文件搜索（``xpan/file?method=search``）、
  文件详情（``xpan/multimedia?method=filemetas``）、文件下载（``dlink``，流式落盘）。

几条约定：

- **只读**：不删除、不上传、不分享、不改名；唯一的「下载」操作只读取文件内容写到本机，
  不产生任何远端变更。
- access_token 走**查询串**，这是网盘开放平台的接口形制；异常文本与上游提示统一经
  ``_redact`` 净化，绝不回显 token。
- 上游的业务错误用 **HTTP 200 + 数字 errno** 表达（鉴权失败也是 200），所以一律读信封，
  不看状态码。已知的鉴权类 errno（-6 / 110 / 111 / 20017 / 31064）才判 ``invalid``，
  其余未归类 errno 判 ``error``，不猜成失效。
- 网盘 API 有 QPS 限制，列表类操作只翻**单页**并由用户显式给偏移，不做后台循环翻页。

参考文档：https://pan.baidu.com/union/doc/
"""

from __future__ import annotations

import os
import re
import time

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "百度网盘"
DISPLAY_FIELD = "access_token"

API_BASE = "https://pan.baidu.com"
UINFO_URL = f"{API_BASE}/rest/2.0/xpan/nas"
QUOTA_URL = f"{API_BASE}/api/quota"
FILE_URL = f"{API_BASE}/rest/2.0/xpan/file"
MULTIMEDIA_URL = f"{API_BASE}/rest/2.0/xpan/multimedia"

#: 明确表示「token 无效 / 已过期 / 不是网盘 token」的业务错误码。
#: -6 身份验证失败；110 access_token 无效；111 access_token 过期；
#: 20017 nas token 无效；31064 该 token 不是网盘 token。
_INVALID_ERRNOS = {-6, 110, 111, 20017, 31064}

#: 单页条数上限（网盘列表接口的 limit 最大 1000）
_MAX_LIMIT = 1000

_VIP_TYPES = {0: "普通用户", 1: "会员", 2: "超级会员"}


# ── 基础工具 ────────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _text(value) -> str:
    """标量 → 展示文本；``None`` 与空串统一成空串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_int(value):
    """转 int；转不了返回 ``None``（``None`` 与 0 是两回事）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _clamp(value, default: int, low: int, high: int) -> int:
    """把表单里的数字夹到合法区间；缺省/非法时用 ``default``。"""
    number = _as_int(value)
    if number is None:
        return default
    return max(low, min(high, number))


def _redact(text: str, token: str) -> str:
    """去掉文本里可能出现的 access_token（异常体 / 上游提示都不该回显它）。"""
    text = (text or "").strip()
    if token:
        text = text.replace(token, "***")
    return text


def _fmt_size(value) -> str:
    """字节数 → 人类可读（1024 进制）；拿不到数字就原样返回。"""
    number = _as_int(value)
    if number is None:
        return _text(value)
    if number < 1024:
        return f"{number} B"
    size = float(number)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} EB"


def _fmt_time(value) -> str:
    """Unix 秒 → 本地时间；0 / 非法值返回空串。"""
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def _norm_dir(value: str) -> str:
    """目录必须是以 ``/`` 开头的绝对路径（网盘的 dir 参数形制）。"""
    value = (value or "").strip()
    if not value:
        return "/"
    return value if value.startswith("/") else f"/{value}"


def _errno(payload) -> int:
    """取业务错误码（不是 dict 的情况按 -1 处理，调用方会当作无法确认）。"""
    if not isinstance(payload, dict):
        return -1
    number = _as_int(payload.get("errno"))
    return -1 if number is None else number


def _detail(payload, token: str) -> str:
    """把上游 ``errmsg`` / ``show_msg`` 拼成一句可读提示（已脱敏）。"""
    if not isinstance(payload, dict):
        return "响应格式无法识别"
    for key in ("show_msg", "errmsg", "message"):
        value = _text(payload.get(key))
        if value:
            return _redact(value, token)
    return "上游未给出原因"


def _call(client, url: str, params: dict, token: str):
    """发一个只读请求，返回 ``(payload, error)``；``payload`` 为 ``None`` 表示没拿到 JSON。"""
    resp = client.get(url, params=params)
    payload = safe_json(resp)
    if payload is None:
        snippet = _redact(body_snippet(resp), token)
        return None, f"响应不是 JSON（HTTP {resp.status_code}）：{snippet}"
    return payload, ""


def _ok_list(rows: list[dict], columns: list[dict], empty_text: str, *,
             has_more: bool = False, total=None) -> dict:
    """统一的列表结果信封。``total`` 未知时不要瞎填（界面会显示成确定的总数）。"""
    result = {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "empty_text": empty_text,
        "has_more": has_more,
    }
    if total is not None:
        result["total"] = total
    return result


# ── 共用列定义 ──────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值"},
]

_FILE_COLUMNS = [
    {"key": "name", "label": "名称"},
    {"key": "kind", "label": "类型", "width": 70, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100},
    {"key": "mtime", "label": "修改时间", "width": 170},
    {"key": "path", "label": "路径", "mono": True},
    {"key": "fs_id", "label": "fs_id", "width": 170, "mono": True},
]

_META_COLUMNS = [
    {"key": "name", "label": "名称"},
    {"key": "kind", "label": "类型", "width": 70, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100},
    {"key": "mtime", "label": "修改时间", "width": 170},
    {"key": "md5", "label": "MD5", "mono": True},
    {"key": "fs_id", "label": "fs_id", "width": 170, "mono": True},
    {"key": "path", "label": "路径", "mono": True},
]

# ── 行构造 ──────────────────────────────────────────────

def _kv(item: str, value: str) -> dict:
    return {"item": item, "value": value}


def _file_row(item: dict) -> dict:
    """文件列表 / 搜索结果 → 表格行。目录额外带上 ``dirpath``，供「打开目录」下钻。"""
    is_dir = _text(item.get("isdir")) == "1"
    path = _text(item.get("path"))
    row = {
        "name": _text(item.get("server_filename")),
        "kind": "目录" if is_dir else "文件",
        "size": "" if is_dir else _fmt_size(item.get("size")),
        "mtime": _fmt_time(item.get("server_mtime")),
        "path": path,
        "fs_id": _text(item.get("fs_id")),
    }
    if is_dir:
        # 只有目录行才带这个键：文件行的 row_action 解析不到 $row.dirpath，
        # 前端就不会给它显示「打开目录」。
        row["dirpath"] = path
    else:
        # 同理，只有文件行带 download_fsid，「下载」不会出现在目录右键里。
        row["download_fsid"] = _text(item.get("fs_id"))
    return row


def _meta_row(item: dict) -> dict:
    is_dir = _text(item.get("isdir")) == "1"
    return {
        "name": _text(item.get("filename")),
        "kind": "目录" if is_dir else "文件",
        "size": "" if is_dir else _fmt_size(item.get("size")),
        "mtime": _fmt_time(item.get("server_mtime")),
        "md5": _text(item.get("md5")),
        "fs_id": _text(item.get("fs_id")),
        "path": _text(item.get("path")),
    }


# ── 各操作的实现 ────────────────────────────────────────

def _size_cell(value) -> str:
    """容量单元格：人类可读 + 原始字节数（拿不到就返回空串）。"""
    number = _as_int(value)
    if number is None:
        return _fmt_size(value)
    text = _fmt_size(number)
    return text if text == f"{number} B" else f"{text}（{number} 字节）"


def _quota_rows(client, token: str) -> tuple[list[dict], str]:
    """容量行（尽力而为）：拿不到就返回 ``([], 原因)``，不拖垮账号信息。

    ``used`` 可能大于 ``total``（历史 / 回收站占用），使用率会超过 100%，如实展示。
    """
    payload, error = _call(client, QUOTA_URL, {"access_token": token}, token)
    if payload is None:
        return [], error
    errno = _errno(payload)
    if errno != 0:
        return [], f"errno {errno}：{_detail(payload, token)}"

    total = _as_int(payload.get("total"))
    used = _as_int(payload.get("used"))
    free = _as_int(payload.get("free"))
    percent = ""
    if total and used is not None:
        percent = f"{used / total * 100:.1f}%"
    rows = [
        _kv("总容量", _size_cell(total)),
        _kv("已使用", _size_cell(used)),
        _kv("剩余（free）", _size_cell(free)),
        _kv("使用率", percent or "—"),
    ]
    if payload.get("expire") is not None:
        expired = payload.get("expire") in (True, 1, "1", "true", "True")
        rows.append(_kv("会员已过期", "是" if expired else "否"))
    return rows, ""


def _run_uinfo(client, token: str, params: dict) -> dict:
    """账号信息：``method=uinfo`` + ``/api/quota``（均只读；容量失败不拖垮账号信息）。"""
    payload, error = _call(client, UINFO_URL,
                           {"method": "uinfo", "access_token": token}, token)
    if payload is None:
        return {"ok": False, "error": error}
    errno = _errno(payload)
    if errno != 0:
        return {"ok": False, "error": f"errno {errno}：{_detail(payload, token)}"}

    rows = [
        _kv("百度账号", _text(payload.get("baidu_name"))),
        _kv("网盘昵称", _text(payload.get("netdisk_name")) or "（未设置）"),
        _kv("用户 UID (uk)", _text(payload.get("uk"))),
        _kv("会员类型", _VIP_TYPES.get(_as_int(payload.get("vip_type")), "未知")),
        _kv("头像地址", _text(payload.get("avatar_url"))),
    ]
    quota_rows, quota_error = _quota_rows(client, token)
    rows.extend(quota_rows)
    if quota_error:
        rows.append(_kv("容量", f"未取到：{quota_error}"))
    return _ok_list(rows, _KV_COLUMNS, "未取到账号信息", total=len(rows))


def _run_list_files(client, token: str, params: dict) -> dict:
    """文件列表：``xpan/file?method=list``（只读，单页）。"""
    directory = _norm_dir(_field(params, "dir"))
    order = _field(params, "order") or "name"
    if order not in ("name", "time", "size", "-name", "-time", "-size"):
        order = "name"
    limit = _clamp(_field(params, "limit"), 100, 1, _MAX_LIMIT)
    start = _clamp(_field(params, "start"), 0, 0, 1_000_000)

    payload, error = _call(client, FILE_URL, {
        "method": "list", "dir": directory, "order": order,
        "start": start, "limit": limit, "access_token": token,
    }, token)
    if payload is None:
        return {"ok": False, "error": error}
    errno = _errno(payload)
    if errno != 0:
        return {"ok": False, "error": f"errno {errno}：{_detail(payload, token)}"}

    items = payload.get("list")
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    rows = [_file_row(item) for item in items]
    # list 接口不返回 has_more：拿满一页就认为后面可能还有
    has_more = len(items) >= limit
    return _ok_list(rows, _FILE_COLUMNS, f"目录 {directory} 下没有文件", has_more=has_more)


def _run_search(client, token: str, params: dict) -> dict:
    """文件搜索：``xpan/file?method=search``（只读）。``has_more`` 由上游给出。"""
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "请填写搜索关键词"}
    directory = _norm_dir(_field(params, "dir"))
    recursion = "1" if _field(params, "recursion") != "0" else "0"
    page = _clamp(_field(params, "page"), 1, 1, 10_000)

    # 实测 search 接口会一次返回该关键词的全部命中（page≥1 时返回同一批），
    # ``num`` 参数不生效，所以不提供「每页条数」；page 保留给确有分页的服务端行为。
    payload, error = _call(client, FILE_URL, {
        "method": "search", "dir": directory, "key": key, "recursion": recursion,
        "page": page, "access_token": token,
    }, token)
    if payload is None:
        return {"ok": False, "error": error}
    errno = _errno(payload)
    if errno != 0:
        return {"ok": False, "error": f"errno {errno}：{_detail(payload, token)}"}

    items = payload.get("list")
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    rows = [_file_row(item) for item in items]
    has_more = bool(payload.get("has_more") is True or payload.get("has_more") == 1)
    return _ok_list(rows, _FILE_COLUMNS, "没有匹配的文件", has_more=has_more)


def _run_file_meta(client, token: str, params: dict) -> dict:
    """文件详情：``xpan/multimedia?method=filemetas``（只读，不申请下载直链）。"""
    raw = _field(params, "fsids")
    fsids = []
    for token_text in re.findall(r"\d+", raw):
        value = int(token_text)
        if value not in fsids:
            fsids.append(value)
    if not fsids:
        return {"ok": False, "error": "请填写至少一个 fs_id（可逗号 / 空格 / JSON 数组分隔）"}
    fsids = fsids[:200]

    payload, error = _call(client, MULTIMEDIA_URL, {
        "method": "filemetas", "fsids": "[" + ",".join(str(i) for i in fsids) + "]",
        "dlink": 0, "access_token": token,
    }, token)
    if payload is None:
        return {"ok": False, "error": error}
    errno = _errno(payload)
    if errno != 0:
        return {"ok": False, "error": f"errno {errno}：{_detail(payload, token)}"}

    items = payload.get("list")
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    rows = [_meta_row(item) for item in items]
    missing = len(fsids) - len(rows)
    empty = "没有查到这些 fs_id 对应的文件"
    if missing > 0 and rows:
        empty = f"有 {missing} 个 fs_id 未查到（可能已删除或无权限）"
    return _ok_list(rows, _META_COLUMNS, empty, total=len(rows))


def _dlink_of(client, token: str, fsid: str) -> tuple[str, str]:
    """取文件的下载直链（``dlink=1``）；返回 ``(dlink, error)``。"""
    payload, error = _call(client, MULTIMEDIA_URL, {
        "method": "filemetas", "fsids": f"[{fsid}]", "dlink": 1,
        "access_token": token,
    }, token)
    if payload is None:
        return "", error
    errno = _errno(payload)
    if errno != 0:
        return "", f"errno {errno}：{_detail(payload, token)}"
    items = payload.get("list")
    if not isinstance(items, list) or not items:
        return "", "上游没有返回该文件的下载信息"
    dlink = _text(items[0].get("dlink"))
    if not dlink:
        return "", "上游没有返回下载直链（目录或文件已删除？）"
    return dlink, ""


def _run_download(client, token: str, params: dict, progress=None, should_cancel=None) -> dict:
    """下载单个文件：取 dlink → 流式写入用户选择的路径（``.part`` + 原子替换）。

    网盘直链要求 ``User-Agent: pan.baidu.com``，且下载时必须在查询串里再带上
    access_token，否则返回 403。``dlink`` 有效期约 8 小时。
    """
    fsid = _field(params, "fs_id")
    if not fsid.isdigit():
        return {"ok": False, "error": "请填写合法的 fs_id"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    dlink, error = _dlink_of(client, token, fsid)
    if not dlink:
        return {"ok": False, "error": error}
    url = dlink + ("&" if "?" in dlink else "?") + "access_token=" + token

    name = _field(params, "name") or f"fs_{fsid}"
    target = path
    tmp = target + ".part"
    emitted = written = 0
    last = time.monotonic()

    def _report(current: int, total: int, text: str = "") -> None:
        if progress is not None:
            progress(current, total, text)

    try:
        with client.stream("GET", url, headers={"User-Agent": "pan.baidu.com"}) as resp:
            if resp.status_code != 200:
                # 直链错误体通常很小，读出来便于提示（已脱敏）
                body = _redact(resp.read().decode("utf-8", "replace"), token)
                return {"ok": False, "error": f"下载失败（HTTP {resp.status_code}）：{body[:200]}"}
            total = _as_int(resp.headers.get("content-length")) or 0
            _report(0, total, f"开始下载 {name}")
            with open(tmp, "wb") as handle:
                for chunk in resp.iter_bytes(65536):
                    if should_cancel is not None and should_cancel():
                        raise _DownloadCancelled
                    handle.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if written - emitted >= 262144 or now - last >= 0.2:
                        emitted, last = written, now
                        shown = f"{_fmt_size(written)} / {_fmt_size(total)}" if total else _fmt_size(written)
                        _report(written, total, shown)
        os.replace(tmp, target)
    except _DownloadCancelled:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "error": "下载已取消"}
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "error": f"下载失败：{type(exc).__name__}"}

    if written != emitted:
        _report(written, total, _fmt_size(written))
    return {"ok": True, "output": f"已保存到 {target}（{_fmt_size(written)}）"}


class _DownloadCancelled(Exception):
    """协作式取消的内部控制流（不对外暴露）。"""


# ── 操作声明 ─────────────────────────────────────────────

_DIR_FIELD = {
    "key": "dir", "label": "目录", "type": "text", "required": False,
    "default": "/", "placeholder": "/",
}
_LIMIT_FIELD = {
    "key": "limit", "label": "每页条数", "type": "number", "required": False,
    "default": 100, "placeholder": "1 - 1000",
}
_START_FIELD = {
    "key": "start", "label": "起始偏移", "type": "number", "required": False,
    "default": 0, "placeholder": "0",
}
_ORDER_FIELD = {
    "key": "order", "label": "排序", "type": "select", "required": False, "default": "name",
    "options": [
        {"value": "name", "label": "文件名升序"},
        {"value": "-name", "label": "文件名降序"},
        {"value": "time", "label": "修改时间升序"},
        {"value": "-time", "label": "修改时间降序"},
        {"value": "size", "label": "大小升序"},
        {"value": "-size", "label": "大小降序"},
    ],
}

_OPEN_DIR_ACTION = {
    "id": "list_files", "label": "打开目录", "params": {"dir": "$row.dirpath"},
}
_DOWNLOAD_ROW_ACTION = {
    "id": "download", "label": "下载",
    "params": {"fs_id": "$row.download_fsid", "name": "$row.name"},
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（access_token 为空时返回空列表）。"""
    data = data or {}
    if not _field(data, "access_token"):
        return []
    return [
        {
            "id": "uinfo",
            "name": "账号信息",
            "kind": "list",
            "group": "账号",
            "order": 10,
            "description": "拉取百度账号、网盘昵称、uk、会员类型与容量（只读）",
            "columns": _KV_COLUMNS,
            "empty_text": "未取到账号信息",
        },
        {
            "id": "list_files",
            "name": "文件列表",
            "kind": "list",
            "group": "文件",
            "order": 30,
            "description": "浏览指定目录下的文件与子目录（只读）；目录行可右键继续下钻",
            "fields": [_DIR_FIELD, _ORDER_FIELD, _START_FIELD, _LIMIT_FIELD],
            "submit": "查询",
            "columns": _FILE_COLUMNS,
            "row_actions": [_OPEN_DIR_ACTION, _DOWNLOAD_ROW_ACTION],
            "empty_text": "该目录下没有文件",
        },
        {
            "id": "search",
            "name": "文件搜索",
            "kind": "list",
            "group": "文件",
            "order": 40,
            "description": "按关键词搜索网盘文件（只读）；可指定目录与是否递归子目录",
            "fields": [
                {"key": "key", "label": "关键词", "type": "text", "required": True},
                _DIR_FIELD,
                {
                    "key": "recursion", "label": "递归子目录", "type": "select",
                    "required": False, "default": "1",
                    "options": [
                        {"value": "1", "label": "是"},
                        {"value": "0", "label": "否"},
                    ],
                },
                {"key": "page", "label": "页码", "type": "number", "required": False,
                 "default": 1, "placeholder": "从 1 开始"},
            ],
            "submit": "搜索",
            "columns": _FILE_COLUMNS,
            "row_actions": [_OPEN_DIR_ACTION, _DOWNLOAD_ROW_ACTION],
            "empty_text": "没有匹配的文件",
        },
        {
            "id": "file_meta",
            "name": "文件详情",
            "kind": "list",
            "group": "文件",
            "order": 50,
            "description": "按 fs_id 查询文件元信息：大小 / 修改时间 / MD5 / 路径（只读，不生成下载直链）",
            "fields": [
                {"key": "fsids", "label": "fs_id", "type": "textarea", "required": True,
                 "placeholder": "可填多个，逗号 / 空格 / JSON 数组分隔，如 909783573631782, 890466248594548"},
            ],
            "submit": "查询",
            "columns": _META_COLUMNS,
            "empty_text": "没有查到对应的文件",
        },
        {
            "id": "download",
            "name": "下载文件",
            "kind": "form",
            "group": "文件",
            "order": 60,
            "description": "把网盘文件下载到本机（弹「另存为」；从文件列表 / 搜索结果右键「下载」进入会自动填好）。大文件请耐心等待，非会员账号可能被限速",
            "fields": [
                {"key": "fs_id", "label": "fs_id", "type": "text", "required": True,
                 "placeholder": "从文件列表 / 搜索结果里取"},
                {"key": "name", "label": "保存文件名", "type": "text",
                 "required": False, "placeholder": "选填"},
            ],
            "save_to": {"key": "path", "default_name": "$row.name"},
            "progress": True,
            "submit": "下载",
        },
    ]


_HANDLERS = {
    "uinfo": _run_uinfo,
    "list_files": _run_list_files,
    "search": _run_search,
    "file_meta": _run_file_meta,
}


def run_action(action_id: str, params: dict, data: dict,
               progress=None, should_cancel=None) -> dict:
    """执行操作。除「下载」外全部只读；``progress`` / ``should_cancel`` 为下载钩子。"""
    params = params or {}
    data = data or {}
    try:
        token = _field(data, "access_token")
        if not token:
            return {"ok": False, "error": "access_token 为空"}
        with make_client() as client:
            if action_id == "download":
                return _run_download(client, token, params, progress, should_cancel)
            handler = _HANDLERS.get(action_id)
            if handler is None:
                return {"ok": False, "error": f"未知操作：{action_id}"}
            return handler(client, token, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        # 只回异常类型名：httpx 的异常文本含完整请求 URL，会把 access_token 泄进界面
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(action_id: str, params: dict, data: dict, on_delta,
                  on_progress=None, should_cancel=None) -> dict:
    """下载类操作走流式入口，边下边上报进度并响应取消。"""
    if action_id == "download":
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not _field(data, "access_token"):
        errors.append("Access Token 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """只读探针：``GET /rest/2.0/xpan/nas?method=uinfo&access_token=…``。

    只返回账号公开信息，不读取任何文件、不消耗业务配额。上游用业务 errno 表达结果
    （HTTP 恒为 200），只有已知的鉴权类 errno 才判 ``invalid``。
    """
    data = data or {}
    token = _field(data, "access_token")
    if not token:
        return VerifyResult(status="invalid", message="access_token 为空")

    log(f"校验百度网盘 access_token {mask_secret(token)}（uinfo）")
    with make_client() as client:
        payload, error = _call(client, UINFO_URL,
                               {"method": "uinfo", "access_token": token}, token)
    if payload is None:
        return VerifyResult(status="error", message=error)

    errno = _errno(payload)
    if errno == 0:
        info = {}
        if _text(payload.get("baidu_name")):
            info["账号"] = _text(payload.get("baidu_name"))
        if _text(payload.get("netdisk_name")):
            info["网盘昵称"] = _text(payload.get("netdisk_name"))
        if _text(payload.get("uk")):
            info["uk"] = _text(payload.get("uk"))
        vip = _VIP_TYPES.get(_as_int(payload.get("vip_type")))
        if vip:
            info["会员类型"] = vip
        stored_uk = _field(data, "uk")
        if stored_uk and stored_uk != _text(payload.get("uk")):
            info["备注 uk"] = f"{stored_uk}（与实测不一致）"
        return VerifyResult(status="valid", message="access_token 有效", account_info=info or None)

    detail = _detail(payload, token)
    if errno in _INVALID_ERRNOS:
        return VerifyResult(status="invalid",
                            message=f"access_token 无效或已过期：{detail}")
    return VerifyResult(status="error",
                        message=f"无法确认凭据状态（errno {errno}）：{detail}")


def get_display_summary(data) -> str:
    data = data or {}
    parts = [NAME, mask_secret(_field(data, DISPLAY_FIELD))]
    uk = _field(data, "uk")
    if uk:
        parts.append(f"uk {uk}")
    return " | ".join(part for part in parts if part)
