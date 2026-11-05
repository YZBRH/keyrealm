"""企业微信凭据验证插件（plugins/im/ 类别下的独立子插件）。

一个插件装两类**企业微信凭据**，由 `kind` 选择器区分（缺省回落 ``app``）：

- ``app``（自建应用）：``GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=&corpsecret=``
  判定：
  - ``errcode == 0`` → valid。换来的 ``access_token`` 属敏感临时值，**绝不存入 account_info**，
    只放 ``expires_in`` 供参考
  - 40001（不合法的 secret）/ 40013（不合法的 CorpID）→ invalid
  - 60020（访问 IP 不在「企业可信 IP」白名单）→ error（配置问题，不是凭据失效）
  - 其它 errcode / HTTP 非 200 → error（拿不准不猜）

- ``webhook``（群机器人）：机器人**只有 POST 一个端点**、没有只读接口，于是用社区通行的
  「非法消息类型」探测法：发一条 ``msgtype`` 为虚构值的请求，企微会在**校验类型**这一步就拒绝、
  **不会把任何内容投递到群里**，回包错误码恰好能区分三种状态：
  - 40008 invalid message type  → 密钥成立（机器人已被认领）⇒ valid
  - 93000 invalid webhook url   → 密钥不成立                      ⇒ invalid
  - 45009 api freq out of limit → 频率限制，与密钥无关            ⇒ error
  最后兜一层：万一机器人真把探测类型当普通消息收下了（errcode 0），如实说明
  「探测内容已被投递」，不假装什么都没发生。

**密钥卫生**：应用态的 corpsecret 与 webhook 态的 key 都走查询串，故任何提示文本、异常、日志
都不得带上完整 URL——网络异常一律只露类型名（``type(exc).__name__``）。

参考文档（gettoken）：https://developer.work.weixin.qq.com/document/path/91039
参考文档（全局错误码）：https://developer.work.weixin.qq.com/document/path/90313
参考文档（群机器人）：https://developer.work.weixin.qq.com/document/path/91770
"""
from __future__ import annotations

from typing import Any

import httpx

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "企业微信"

KIND_APP = "app"
KIND_WEBHOOK = "webhook"

CORPID_KEY = "corpid"
SECRET_KEY = "corpsecret"
WEBHOOK_KEY = "webhook_key"

GETTOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
BASE_URL = "https://qyapi.weixin.qq.com"
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

# app 形态：gettoken 这一步明确指向 corpid/secret 错误的错误码
_INVALID_CODES = {40001, 40013}   # 40001 不合法的 secret；40013 不合法的 CorpID
_IP_WHITELIST_CODE = 60020        # 不安全的访问 IP（不在白名单）→ 配置问题，非凭据失效

# webhook 形态：以虚构消息类型探测后的错误码
_WEBHOOK_VALID = {40008}      # invalid message type（类型非法 ⇒ 密钥被认领）
_WEBHOOK_INVALID = {93000}    # invalid webhook url（密钥不成立）
_WEBHOOK_BUSY = {45009}       # api freq out of limit（频率限制，与密钥无关）

# 虚构的消息类型：企微在校验类型这一步就拒绝，不会投递任何内容
PROBE_MSGTYPE = "__vault_probe__"

# ── 操作（只读查询 + 群机器人发送）──────────────────────────
_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]
_DEPT_COLUMNS = [
    {"key": "id", "label": "部门 ID", "width": 90},
    {"key": "name", "label": "部门名称"},
    {"key": "parentid", "label": "上级 ID", "width": 90},
    {"key": "leader", "label": "负责人"},
]
_USER_COLUMNS = [
    {"key": "userid", "label": "UserID", "width": 140},
    {"key": "name", "label": "姓名", "width": 110},
    {"key": "department", "label": "部门 ID"},
    {"key": "position", "label": "职务"},
    {"key": "status", "label": "状态", "width": 80},
]
_FOLLOW_COLUMNS = [{"key": "userid", "label": "跟进成员 UserID", "width": 180}]
_EXTERNAL_COLUMNS = [{"key": "external_userid", "label": "客户 ID", "width": 240}]

_STATUS_TEXT = {1: "已激活", 2: "已禁用", 4: "未激活", 5: "退出企业"}
_MAX_ROWS = 500


def _rows(rows: list, columns, empty_text: str, has_more: bool = False) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows),
        "has_more": has_more,
        "empty_text": empty_text,
    }


def _action_error(payload: dict) -> str:
    """把企业微信错误响应压成一句可操作的中文（不回显任何密钥 / token）。"""
    code = _errcode(payload)
    detail = _short((payload or {}).get("errmsg") or "")
    if code in _INVALID_CODES:
        return "CorpID 或 CorpSecret 无效"
    if code == _IP_WHITELIST_CODE:
        return "访问 IP 不在企业可信 IP 白名单（60020）"
    if code == 60011:
        return "该 Secret 没有调用此接口的权限（60011）"
    if code in _WEBHOOK_INVALID:
        return "Webhook 密钥无效"
    if code in _WEBHOOK_BUSY:
        return "接口调用频率超限，请稍后重试"
    if code is None:
        return "企业微信返回了无法识别的响应"
    return f"企业微信返回错误 {code}：{detail or '未知原因'}"


def _fetch_access_token(client, corpid: str, corpsecret: str):
    """取 access_token：返回 ``(token, expires_in, error)``；token 只在内存中使用。"""
    resp = client.get(GETTOKEN_URL, params={"corpid": corpid, "corpsecret": corpsecret})
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, None, f"响应无法解析（HTTP {resp.status_code}）"
    if _errcode(payload) == 0:
        return str(payload.get("access_token") or ""), payload.get("expires_in"), None
    return None, None, _action_error(payload)


def _get(client, path: str, token: str, params: dict | None = None):
    resp = client.get(f"{BASE_URL}{path}", params={**(params or {}), "access_token": token})
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None, f"响应无法解析（HTTP {resp.status_code}）"
    if _errcode(payload) != 0:
        return None, _action_error(payload)
    return payload, None


def _h_token(client, corpid, corpsecret, params):
    token, expires, error = _fetch_access_token(client, corpid, corpsecret)
    if error:
        return {"ok": False, "error": error}
    rows = [
        {"item": "CorpID", "value": corpid},
        {"item": "调用凭据", "value": mask_secret(token)},
        {"item": "剩余有效期(秒)", "value": str(expires) if expires is not None else "—"},
    ]
    return _rows(rows, _KV_COLUMNS, "未能取得调用凭据")


def _h_departments(client, corpid, corpsecret, params):
    token, _, error = _fetch_access_token(client, corpid, corpsecret)
    if error:
        return {"ok": False, "error": error}
    payload, error = _get(client, "/cgi-bin/department/list", token)
    if error:
        return {"ok": False, "error": error}
    departments = payload.get("department")
    departments = departments if isinstance(departments, list) else []
    rows = [
        {
            "id": item.get("id"),
            "name": item.get("name") or "—",
            "parentid": item.get("parentid"),
            "leader": item.get("leader") or "—",
        }
        for item in departments[:_MAX_ROWS] if isinstance(item, dict)
    ]
    return _rows(rows, _DEPT_COLUMNS, "未取得部门列表（需通讯录同步 Secret）",
                 has_more=len(departments) > _MAX_ROWS)


def _h_users(client, corpid, corpsecret, params):
    department_id = str((params or {}).get("department_id") or "").strip()
    if not department_id:
        return {"ok": False, "error": "请填写部门 ID"}
    fetch_child = str((params or {}).get("fetch_child") or "1").strip()
    token, _, error = _fetch_access_token(client, corpid, corpsecret)
    if error:
        return {"ok": False, "error": error}
    payload, error = _get(client, "/cgi-bin/user/list", token, {
        "department_id": department_id,
        "fetch_child": "1" if fetch_child not in ("0", "false", "False") else "0",
    })
    if error:
        return {"ok": False, "error": error}
    users = payload.get("userlist")
    users = users if isinstance(users, list) else []
    rows = [
        {
            "userid": item.get("userid") or "—",
            "name": item.get("name") or "—",
            "department": "、".join(str(x) for x in (item.get("department") or [])),
            "position": item.get("position") or "—",
            "status": _STATUS_TEXT.get(item.get("status"), str(item.get("status") or "—")),
        }
        for item in users[:_MAX_ROWS] if isinstance(item, dict)
    ]
    return _rows(rows, _USER_COLUMNS, "该部门暂无成员（需通讯录同步 Secret）",
                 has_more=len(users) > _MAX_ROWS)


def _h_follow_users(client, corpid, corpsecret, params):
    token, _, error = _fetch_access_token(client, corpid, corpsecret)
    if error:
        return {"ok": False, "error": error}
    payload, error = _get(client, "/cgi-bin/externalcontact/get_follow_user_list", token)
    if error:
        return {"ok": False, "error": error}
    users = payload.get("follow_user")
    users = users if isinstance(users, list) else []
    rows = [{"userid": str(item)} for item in users[:_MAX_ROWS] if str(item).strip()]
    return _rows(rows, _FOLLOW_COLUMNS, "没有具备客户联系权限的成员",
                 has_more=len(users) > _MAX_ROWS)


def _h_external_contacts(client, corpid, corpsecret, params):
    userid = str((params or {}).get("userid") or "").strip()
    if not userid:
        return {"ok": False, "error": "请填写跟进成员 UserID"}
    token, _, error = _fetch_access_token(client, corpid, corpsecret)
    if error:
        return {"ok": False, "error": error}
    payload, error = _get(client, "/cgi-bin/externalcontact/list", token, {"userid": userid})
    if error:
        if "84061" in error:
            return _rows([], _EXTERNAL_COLUMNS, "该成员暂无外部联系人")
        return {"ok": False, "error": error}
    contacts = payload.get("external_userid")
    contacts = contacts if isinstance(contacts, list) else []
    rows = [{"external_userid": str(item)} for item in contacts[:_MAX_ROWS] if str(item).strip()]
    return _rows(rows, _EXTERNAL_COLUMNS, "该成员暂无外部联系人",
                 has_more=len(contacts) > _MAX_ROWS)


_APP_HANDLERS = {
    "token": _h_token,
    "departments": _h_departments,
    "users": _h_users,
    "follow_users": _h_follow_users,
    "external_contacts": _h_external_contacts,
}


def _webhook_send(client, key: str, content: str) -> dict:
    resp = client.post(
        WEBHOOK_URL,
        params={"key": key},
        headers={"Content-Type": "application/json"},
        json={"msgtype": "text", "text": {"content": content}},
    )
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"响应无法解析（HTTP {resp.status_code}）"}
    if _errcode(payload) == 0:
        return {"ok": True, "output": "文本消息已发送"}
    return {"ok": False, "error": _action_error(payload)}


def actions(data) -> list[dict]:
    """声明可用操作：应用态提供通讯录 / 客户联系只读查询；Webhook 提供发送文本。"""
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        if not str(data.get(WEBHOOK_KEY) or "").strip():
            return []
        return [{
            "id": "webhook_send",
            "name": "发送文本消息（写操作）",
            "kind": "form",
            "group": "群机器人",
            "order": 10,
            "submit": "发送",
            "write": True,
            "fields": [{
                "key": "content",
                "label": "消息内容",
                "type": "textarea",
                "required": True,
                "placeholder": "将直接发送到该机器人所在群",
            }],
        }]

    if not (str(data.get(CORPID_KEY) or "").strip()
            and str(data.get(SECRET_KEY) or "").strip()):
        return []
    return [
        {
            "id": "token",
            "name": "获取调用凭据",
            "kind": "list",
            "group": "凭据",
            "order": 10,
            "description": "确认 Secret 可用并查看 access_token 有效期（掩码展示）",
            "columns": _KV_COLUMNS,
            "fields": [],
            "submit": "获取",
            "empty_text": "未能取得调用凭据",
        },
        {
            "id": "departments",
            "name": "部门列表",
            "kind": "list",
            "group": "通讯录",
            "order": 20,
            "description": "读取企业部门树（需通讯录同步 Secret）",
            "columns": _DEPT_COLUMNS,
            "fields": [],
            "submit": "查询",
            "empty_text": "未取得部门列表",
        },
        {
            "id": "users",
            "name": "成员列表",
            "kind": "list",
            "group": "通讯录",
            "order": 30,
            "description": "按部门读取成员（需通讯录同步 Secret）",
            "columns": _USER_COLUMNS,
            "fields": [
                {"key": "department_id", "label": "部门 ID", "type": "text",
                 "required": True, "placeholder": "例如 1 / 80"},
                {"key": "fetch_child", "label": "包含子部门", "type": "text",
                 "required": False, "default": "1",
                 "placeholder": "1 = 包含，0 = 仅本部门"},
            ],
            "submit": "查询",
            "empty_text": "该部门暂无成员",
        },
        {
            "id": "follow_users",
            "name": "客户联系成员",
            "kind": "list",
            "group": "客户联系",
            "order": 40,
            "description": "读取具备客户联系权限的成员（需客户联系 Secret）",
            "columns": _FOLLOW_COLUMNS,
            "fields": [],
            "submit": "查询",
            "empty_text": "没有具备客户联系权限的成员",
        },
        {
            "id": "external_contacts",
            "name": "成员的外部联系人",
            "kind": "list",
            "group": "客户联系",
            "order": 50,
            "description": "按跟进成员读取外部联系人 ID（需客户联系 Secret）",
            "columns": _EXTERNAL_COLUMNS,
            "fields": [
                {"key": "userid", "label": "跟进成员 UserID", "type": "text",
                 "required": True, "placeholder": "先在上一个操作里查成员 UserID"},
            ],
            "submit": "查询",
            "empty_text": "该成员暂无外部联系人",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作；异常只暴露类型名（Secret / token 都在查询串里，不能带 URL）。"""
    data = data or {}
    params = params or {}
    try:
        if _kind(data) == KIND_WEBHOOK:
            if action_id != "webhook_send":
                return {"ok": False, "error": f"未知操作：{action_id}"}
            key = str(data.get(WEBHOOK_KEY) or "").strip()
            if not key:
                return {"ok": False, "error": "缺少 Webhook 凭据"}
            content = str(params.get("content") or "").strip()
            if not content:
                return {"ok": False, "error": "消息内容不能为空"}
            with make_client() as client:
                return _webhook_send(client, key, content)

        handler = _APP_HANDLERS.get(action_id)
        if handler is None:
            return {"ok": False, "error": f"未知操作：{action_id}"}
        corpid = str(data.get(CORPID_KEY) or "").strip()
        corpsecret = str(data.get(SECRET_KEY) or "").strip()
        if not corpid or not corpsecret:
            return {"ok": False, "error": "缺少 CorpID 或 CorpSecret"}
        with make_client() as client:
            return handler(client, corpid, corpsecret, params)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败：{type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def _kind(data) -> str:
    """凭据形态：`kind` 缺省或非法值一律回落 ``app``（旧凭据没有该字段，仍按应用态校验）。"""
    k = str((data or {}).get("kind") or "").strip()
    return k if k in (KIND_APP, KIND_WEBHOOK) else KIND_APP


def _short(text: Any, limit: int = 160) -> str:
    """把回包文案压到一行；顺手切掉企微 errmsg 里的 ``hint: [...]`` 噪音。"""
    flat = " ".join(str(text or "").split())
    flat = flat.split(", hint:", 1)[0]
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _errcode(payload) -> int | None:
    raw = (payload or {}).get("errcode")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。按 kind 分支，避免 webhook 态却报 CorpID 为空。"""
    data = data or {}
    if not data:
        return ["配置数据为空"]
    if _kind(data) == KIND_WEBHOOK:
        return [] if str(data.get(WEBHOOK_KEY) or "").strip() else ["Webhook 凭据不能为空"]
    errors: list[str] = []
    if not str(data.get(CORPID_KEY) or "").strip():
        errors.append("CorpID 不能为空")
    if not str(data.get(SECRET_KEY) or "").strip():
        errors.append("CorpSecret 不能为空")
    return errors


def _verify_app(corpid: str, corpsecret: str) -> VerifyResult:
    """应用态：GET /cgi-bin/gettoken。

    corpsecret 走 query，只经 ``params`` 传给 httpx，不拼字符串、不进日志/消息。
    """
    with make_client() as client:
        resp = client.get(GETTOKEN_URL, params={"corpid": corpid, "corpsecret": corpsecret})
    status = resp.status_code
    payload = safe_json(resp)
    code = _errcode(payload) if isinstance(payload, dict) else None

    if code == 0:
        info: dict[str, Any] = {}
        # access_token 是换来的敏感值，绝不进 account_info；只放有效期
        expires = (payload or {}).get("expires_in")
        if expires is not None:
            try:
                info["expires_in"] = int(expires)
            except (TypeError, ValueError):
                pass
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if code in _INVALID_CODES:
        detail = _short((payload or {}).get("errmsg") or "")
        return VerifyResult(
            status="invalid",
            message=f"凭证无效（{code}" + (f"，{detail}" if detail else "") + "）",
        )

    if code == _IP_WHITELIST_CODE:
        return VerifyResult(
            status="error",
            message="访问 IP 不在企业可信 IP 白名单内（60020），请到企业微信后台配置后重试",
        )

    if code is not None:
        detail = _short((payload or {}).get("errmsg") or "")
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态（{code}" + (f"，{detail}" if detail else "") + "）",
        )

    return _unconfirmed(status, body_snippet(resp))


def _verify_webhook(key: str) -> VerifyResult:
    """群机器人态：POST /cgi-bin/webhook/send，发一条虚构消息类型做只读探测。

    密钥只经 ``params`` 传给 httpx，绝不拼进字符串、也不写日志。
    """
    try:
        log(f"校验企业微信群机器人 webhook（密钥长度 {len(key)}）")
        with make_client() as client:
            resp = client.post(
                WEBHOOK_URL,
                params={"key": key},
                headers={"Content-Type": "application/json"},
                json={"msgtype": PROBE_MSGTYPE},
            )
    except httpx.HTTPError as exc:
        # 密钥在查询串里，异常文本可能带 URL ⇒ 只露类型名
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")

    payload = safe_json(resp)
    if not isinstance(payload, dict):
        payload = {}
    code = _errcode(payload)
    detail = _short(payload.get("errmsg") or body_snippet(resp))

    if code == 0:
        # 探测内容真的被投递了 —— 如实说明，不掩盖
        return VerifyResult(
            status="valid",
            message="密钥有效（注意：该机器人接受了探测类型，探测消息已被投递到群里）",
        )
    if code is None:
        if resp.status_code in (401, 403):
            return VerifyResult(status="invalid", message=f"密钥无效 (HTTP {resp.status_code})")
        return VerifyResult(
            status="error",
            message=f"无法确认密钥状态 (HTTP {resp.status_code})" + (f"：{detail}" if detail else ""),
        )

    if code in _WEBHOOK_INVALID:
        return VerifyResult(
            status="invalid",
            message=f"密钥无效（{code}{'，' + detail if detail else ''}）",
        )
    if code in _WEBHOOK_VALID:
        return VerifyResult(
            status="valid",
            message=f"密钥有效（{code}：机器人已认领该密钥）",
        )
    if code in _WEBHOOK_BUSY:
        return VerifyResult(status="error", message=f"返回频率限制（{code}），请稍后重试")
    return VerifyResult(
        status="error",
        message=f"无法确认密钥状态（{code}{'，' + detail if detail else ''}）",
    )


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        key = str(data.get(WEBHOOK_KEY) or "").strip()
        if not key:
            return VerifyResult(status="invalid", message="缺少必填字段: Webhook 凭据")
        return _verify_webhook(key)

    corpid = str(data.get(CORPID_KEY) or "").strip()
    corpsecret = str(data.get(SECRET_KEY) or "").strip()
    if not corpid or not corpsecret:
        return VerifyResult(status="invalid", message="缺少必填字段: CorpID / CorpSecret")
    return _verify_app(corpid, corpsecret)


def get_display_summary(data) -> str:
    """列表展示：应用态显示 CorpID（非敏感），webhook 态只显示形态、不回显密钥。"""
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        return f"{NAME} | 群机器人"
    corpid = str(data.get(CORPID_KEY) or "").strip()
    return f"{NAME} | {corpid}" if corpid else NAME
