"""百度 AI 开放平台插件（plugins/api/ 类别）。

支持两种认证方式：

- **API Key + Secret Key**：走百度 AI 开放平台 OAuth client_credentials 换取
  ``access_token``（约 30 天），只验证换取是否成功，不要求应用开通具体产品；
- **直填 access_token**：跳过换 token，用内置只读探针接口确认 token 是否被接受
  （与 AK/SK 模式一样不需要用户额外填写探针配置）。

人脸库分组 / 成员作为「百度人脸」操作提供；「权限枚举」逐个只读探测常见产品，
区分可用、无权限、额度受限与未知，供前端按 ``capabilities`` 隐藏用不了的菜单。

参考文档：
- OAuth: https://ai.baidu.com/ai-doc/REFERENCE/Ck3dwjhhu
- 人脸: https://ai.baidu.com/ai-doc/FACE/ek37c1qiz
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json

NAME = "百度 AI 开放平台"
DEFAULT_BASE_URL = "https://aip.baidubce.com"

MODE_AK_SK = "ak_sk"
MODE_TOKEN = "access_token"

_FACE_GROUP_PATH = "/rest/2.0/face/v3/faceset/group/getlist"
_FACE_USERS_PATH = "/rest/2.0/face/v3/faceset/group/getusers"
_DEFAULT_PROBE_PATH = _FACE_GROUP_PATH
_DEFAULT_PROBE_BODY = {"start": 0, "length": 1}

# 权限探测的单个请求超时（秒）：探测只为了给菜单/表格打标，不能让用户干等
_PROBE_TIMEOUT = 6.0

# 权限探测结论
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_QUOTA = "quota"
_PROBE_BUSINESS = "business"   # token 已被接受，但请求参数/业务层报错
_PROBE_UNKNOWN = "unknown"

# 110/111 明确是 token 无效/过期；100 的语义在部分文档里有歧义，暂不归入
_TOKEN_BAD_CODES = {110, 111}
_DENIED_CODES = {6, 14}        # 6=无权限访问数据；14=IAM 鉴权失败
_QUOTA_CODES = {4, 17, 18, 19}  # 集群超限 / 日限额 / QPS 限额 / 总量限额

_PROBE_TARGETS = (
    ("face", "百度人脸", "list_face_groups", _FACE_GROUP_PATH, _DEFAULT_PROBE_BODY),
    ("ocr", "文字识别 OCR", "-", "/rest/2.0/ocr/v1/general_basic", {"image": ""}),
    ("image", "图像识别", "-", "/rest/2.0/image-classify/v2/advanced_general", {"image": ""}),
    ("nlp", "自然语言处理", "-", "/rpc/2.0/nlp/v1/lexer", {"text": "权限探测"}),
    ("wenxin", "文心大模型", "-",
     "/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/completions", {"messages": []}),
)

_PROBE_COLUMNS = [
    {"key": "service", "label": "产品/能力"},
    {"key": "action", "label": "关联操作"},
    {"key": "status", "label": "状态", "type": "tag"},
    {"key": "detail", "label": "说明"},
]
_GROUP_COLUMNS = [{"key": "group_id", "label": "分组 ID", "mono": True}]
_USER_COLUMNS = [
    {"key": "user_id", "label": "用户 ID", "mono": True},
    {"key": "group_id", "label": "分组 ID", "mono": True},
]

_FACE_ACTION_IDS = ("list_face_groups", "list_face_users")


def _field(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int((params or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _base(data: dict) -> str:
    return _field(data, "base_url").rstrip("/") or DEFAULT_BASE_URL


def _mode(data: dict) -> str:
    """认证方式；缺失/非法值一律回落老模式 ak_sk（旧凭据零迁移）。"""
    mode = _field(data, "auth_mode").lower()
    return mode if mode in (MODE_AK_SK, MODE_TOKEN) else MODE_AK_SK


def _has_credentials(data: dict) -> bool:
    if _mode(data) == MODE_TOKEN:
        return bool(_field(data, "access_token"))
    return bool(_field(data, "api_key")) and bool(_field(data, "secret_key"))


def validate(data: dict) -> list[str]:
    """按当前密钥种类校验必填字段。另一种模式的残留字段不报错。"""
    data = data or {}
    errors: list[str] = []
    base = _field(data, "base_url")
    if base and not base.startswith(("http://", "https://")):
        errors.append("Base URL 必须是 http(s) 地址")

    raw_mode = _field(data, "auth_mode").lower()
    if raw_mode and raw_mode not in (MODE_AK_SK, MODE_TOKEN):
        errors.append("密钥种类只能是 API Key + Secret Key 或 access_token")
        return errors

    if _mode(data) == MODE_TOKEN:
        if not _field(data, "access_token"):
            errors.append("密钥种类为 access_token 时必须填写 access_token")
    else:
        if not _field(data, "api_key"):
            errors.append("API Key 不能为空")
        if not _field(data, "secret_key"):
            errors.append("Secret Key 不能为空")
    return errors


def _token_error_text(payload: dict) -> str:
    err = str(payload.get("error") or "")
    desc = str(payload.get("error_description") or "")
    low = f"{err} {desc}".lower()
    if "unknown client id" in low or "invalid_client" in low:
        return "API Key 不正确或已被禁用"
    if "client authentication failed" in low:
        return "Secret Key 不正确"
    return f"鉴权失败：{desc or err or '未知原因'}"


def _get_access_token(base_url: str, api_key: str,
                      secret_key: str) -> tuple[Optional[str], str, bool, int]:
    """OAuth 换取 access_token；返回 (token, error, rejected, expires_in)。

    ``rejected=True`` 表示上游明确拒绝凭据（invalid_client 等），可判 invalid；
    超时、网络错误与无法解析的响应都是 ``False``（判 error，不误伤凭据）。
    """
    url = base_url.rstrip("/") + "/oauth/2.0/token"
    try:
        with make_client(request_timeout=_PROBE_TIMEOUT) as client:
            resp = client.post(
                url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": api_key,
                    "client_secret": secret_key,
                },
            )
    except httpx.TimeoutException:
        return None, f"请求超时（{int(_PROBE_TIMEOUT)} 秒）", False, 0
    except httpx.HTTPError as exc:
        return None, f"网络请求失败（{type(exc).__name__}）", False, 0

    payload = safe_json(resp)
    if isinstance(payload, dict) and payload.get("access_token"):
        try:
            ttl = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            ttl = 0
        return str(payload["access_token"]), "", False, ttl
    if isinstance(payload, dict) and payload.get("error"):
        return None, _token_error_text(payload), True, 0
    return None, f"换取 access_token 失败（HTTP {resp.status_code}）", False, 0


def _call_api(base_url: str, token: str, path: str,
              body: dict) -> tuple[Optional[dict], int, Optional[str]]:
    """调用一个 AIP 接口；返回 (payload, status_code, error)。"""
    url = base_url.rstrip("/") + path
    try:
        with make_client(request_timeout=_PROBE_TIMEOUT) as client:
            resp = client.post(
                url,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                json=body,
            )
    except httpx.TimeoutException:
        return None, 0, f"请求超时（{int(_PROBE_TIMEOUT)} 秒）"
    except httpx.HTTPError as exc:
        return None, 0, f"网络请求失败（{type(exc).__name__}）"

    payload = safe_json(resp)
    if payload is None:
        return None, resp.status_code, f"接口返回非 JSON（HTTP {resp.status_code}）"
    return payload, resp.status_code, None


def _classify(payload: Any, status_code: int) -> str:
    """把 AIP 响应归成六态；拿不准时返回 unknown（调用方按 fail-open 处理）。"""
    if not isinstance(payload, dict):
        return _PROBE_UNKNOWN
    raw = payload.get("error_code")
    if raw is None:
        if payload.get("error"):
            return _PROBE_BAD_CRED
        return _PROBE_OK
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return _PROBE_UNKNOWN
    if code == 0:
        return _PROBE_OK
    if code in _TOKEN_BAD_CODES:
        return _PROBE_BAD_CRED
    if code in _DENIED_CODES:
        return _PROBE_DENIED
    if code in _QUOTA_CODES:
        return _PROBE_QUOTA
    if status_code in (401, 403):
        return _PROBE_DENIED
    return _PROBE_BUSINESS


def _token_for(data: dict) -> tuple[Optional[str], str, bool, int]:
    if _mode(data) == MODE_TOKEN:
        token = _field(data, "access_token")
        return (token, "", False, 0) if token else (None, "access_token 为空", False, 0)
    return _get_access_token(_base(data), _field(data, "api_key"), _field(data, "secret_key"))


def _probe_targets() -> list[tuple[str, str, str, str, dict]]:
    """权限枚举目标：全部使用内置只读接口，不接受用户自定义探针。"""
    return list(_PROBE_TARGETS)


def _status_text(state: str, payload: Optional[dict]) -> str:
    if state == _PROBE_OK:
        return "可用"
    if state == _PROBE_DENIED:
        return "无权限"
    if state == _PROBE_BAD_CRED:
        return "凭据无效"
    if state == _PROBE_QUOTA:
        return "可用（额度受限）"
    if state == _PROBE_BUSINESS:
        return "可用（业务错误）"
    return "未知"


def _detail_text(payload: Optional[dict], error: Optional[str]) -> str:
    if error:
        return error
    if not isinstance(payload, dict):
        return ""
    code = payload.get("error_code")
    msg = payload.get("error_msg") or payload.get("error") or ""
    if code in (None, 0):
        return str(msg or "")
    return f"{code} {msg}".strip()


def _action_error(payload: dict, state: str) -> str:
    """操作失败提示：只拼错误码/错误信息，绝不回显 token / API Key。"""
    return f"{_status_text(state, payload)}：{_detail_text(payload, None) or '请求失败'}"


def _verify_ak_sk(data: dict) -> VerifyResult:
    api_key, secret_key = _field(data, "api_key"), _field(data, "secret_key")
    if not api_key or not secret_key:
        return VerifyResult(status="invalid", message="API Key / Secret Key 不完整")

    token, err, rejected, ttl = _get_access_token(_base(data), api_key, secret_key)
    if not token:
        return VerifyResult(status="invalid" if rejected else "error",
                            message=err or "换取 access_token 失败")

    info = {"auth_mode": MODE_AK_SK}
    if ttl:
        info["access_token_expires_in"] = ttl
    # 换 token 成功即证明 AK/SK 有效；不要求应用已开通人脸，也不消耗业务配额。
    return VerifyResult(status="valid",
                        message="API Key / Secret Key 有效（已换取 access_token）",
                        account_info=info)


def _verify_token(data: dict) -> VerifyResult:
    token = _field(data, "access_token")
    if not token:
        return VerifyResult(status="invalid", message="access_token 为空")

    path = _DEFAULT_PROBE_PATH
    payload, status_code, err = _call_api(
        _base(data), token, path, _DEFAULT_PROBE_BODY)
    if err:
        return VerifyResult(status="error", message=err)

    state = _classify(payload, status_code)
    detail = _detail_text(payload, None)
    info = {"auth_mode": MODE_TOKEN, "probe_path": path}
    if state == _PROBE_OK:
        return VerifyResult(status="valid", message=f"access_token 有效（探针 {path}）",
                            account_info=info)
    if state == _PROBE_QUOTA:
        return VerifyResult(status="valid",
                            message=f"access_token 有效但受限：{detail or '额度/QPS 受限'}",
                            account_info=info)
    if state == _PROBE_DENIED:
        return VerifyResult(status="valid",
                            message=f"access_token 有效，但该应用未开通/未授权探测接口：{detail or path}",
                            account_info=info)
    if state == _PROBE_BAD_CRED:
        return VerifyResult(status="invalid",
                            message=f"access_token 无效或已过期：{detail or '上游拒绝'}")
    if state == _PROBE_BUSINESS:
        return VerifyResult(
            status="valid",
            message=f"access_token 未出现鉴权错误，但探测接口返回业务错误：{detail}；"
                    "请检查验证探测路径 / 请求体",
            account_info=info,
        )
    return VerifyResult(status="valid",
                        message="access_token 未返回鉴权错误，但响应无法识别；请检查探测接口",
                        account_info=info)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证凭据；保证不抛异常。"""
    data = data or {}
    if _mode(data) == MODE_TOKEN:
        return _verify_token(data)
    return _verify_ak_sk(data)


def actions(data: dict) -> list[dict]:
    """声明可用操作（只读本地数据，绝不联网）；权限裁剪交给 capabilities。"""
    data = data or {}
    if not _has_credentials(data):
        return []

    items = [
        {
            "id": "probe_permissions",
            "name": "权限枚举",
            "kind": "list",
            "group": "百度 AI 平台",
            "order": 0,
            "description": "逐个调用只读接口，列出该应用已开通/未开通的百度 AI 产品；不修改任何远端数据",
            "submit": "开始探测",
            "columns": _PROBE_COLUMNS,
            "empty_text": "没有探测结果",
        },
        {
            "id": "list_face_groups",
            "name": "人脸库分组列表",
            "kind": "list",
            "group": "百度人脸",
            "order": 10,
            "description": f"POST {_FACE_GROUP_PATH}（只读）",
            "fields": [
                {"key": "start", "label": "起始位置", "type": "number", "default": 0},
                {"key": "length", "label": "数量", "type": "number", "default": 100},
            ],
            "submit": "查询",
            "columns": _GROUP_COLUMNS,
            "row_actions": [
                {"id": "list_face_users", "label": "查看成员",
                 "params": {"group_id": "$row.group_id"}},
            ],
            "empty_text": "该应用下没有人脸库分组",
        },
        {
            "id": "list_face_users",
            "name": "人脸库成员列表",
            "kind": "list",
            "group": "百度人脸",
            "order": 20,
            "description": f"POST {_FACE_USERS_PATH}（只读）",
            "fields": [
                {"key": "group_id", "label": "分组 ID", "type": "text", "required": True},
                {"key": "start", "label": "起始位置", "type": "number", "default": 0},
                {"key": "length", "label": "数量", "type": "number", "default": 100},
            ],
            "submit": "查询",
            "columns": _USER_COLUMNS,
            "empty_text": "该分组下没有成员",
        },
    ]
    if _mode(data) == MODE_AK_SK:
        items.append({
            "id": "refresh_access_token",
            "name": "用 AK/SK 换取新 access_token",
            "kind": "form",
            "group": "百度 AI 平台",
            "order": 5,
            "description": "OAuth client_credentials 换 token，并把新值原子写回本凭据的 access_token 字段",
            "submit": "换取",
            "write": True,
            "confirm": "将用当前 API Key / Secret Key 换取新的 access_token，并覆盖本凭据中已保存的 access_token；"
                       "旧 token 可能失效。是否继续？",
        })
    return items


def _run_probe_permissions(token: str, base: str) -> dict:
    rows = []
    for _key, label, action_id, path, body in _probe_targets():
        payload, status_code, err = _call_api(base, token, path, body)
        state = _PROBE_UNKNOWN if err else _classify(payload, status_code)
        rows.append({
            "service": label,
            "action": action_id or "-",
            "status": _status_text(state, payload),
            "detail": _detail_text(payload, err),
        })
    return {"ok": True, "rows": rows, "columns": _PROBE_COLUMNS,
            "total": len(rows), "empty_text": "没有探测结果"}


def _run_list_face_groups(token: str, base: str, params: dict) -> dict:
    body = {"start": _int_param(params, "start", 0),
            "length": _int_param(params, "length", 100)}
    payload, status_code, err = _call_api(base, token, _FACE_GROUP_PATH, body)
    if err:
        return {"ok": False, "error": err}
    state = _classify(payload, status_code)
    if state != _PROBE_OK:
        return {"ok": False, "error": _action_error(payload or {}, state)}
    groups = ((payload or {}).get("result") or {}).get("group_id_list") or []
    rows = [{"group_id": str(g)} for g in groups]
    return {"ok": True, "rows": rows, "columns": _GROUP_COLUMNS,
            "total": len(rows), "empty_text": "该应用下没有人脸库分组"}


def _run_list_face_users(token: str, base: str, params: dict) -> dict:
    group_id = _field(params, "group_id")
    if not group_id:
        return {"ok": False, "error": "分组 ID 不能为空"}
    body = {"group_id": group_id,
            "start": _int_param(params, "start", 0),
            "length": _int_param(params, "length", 100)}
    payload, status_code, err = _call_api(base, token, _FACE_USERS_PATH, body)
    if err:
        return {"ok": False, "error": err}
    state = _classify(payload, status_code)
    if state != _PROBE_OK:
        return {"ok": False, "error": _action_error(payload or {}, state)}
    users = ((payload or {}).get("result") or {}).get("user_id_list") or []
    rows = [{"user_id": str(u), "group_id": group_id} for u in users]
    return {"ok": True, "rows": rows, "columns": _USER_COLUMNS,
            "total": len(rows), "empty_text": "该分组下没有成员"}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作；所有操作均为只读（refresh_access_token 只写本凭据，不改远端业务数据）。"""
    data, params = data or {}, params or {}
    try:
        if action_id == "refresh_access_token":
            if _mode(data) != MODE_AK_SK:
                return {"ok": False, "error": "仅 API Key + Secret Key 模式支持换取 access_token"}
            api_key, secret_key = _field(data, "api_key"), _field(data, "secret_key")
            if not api_key or not secret_key:
                return {"ok": False, "error": "API Key / Secret Key 不能为空"}
            token, err, _rejected, ttl = _get_access_token(_base(data), api_key, secret_key)
            if not token:
                return {"ok": False, "error": err or "换取 access_token 失败"}
            return {
                "ok": True,
                "output": f"已换取新的 access_token（expires_in={ttl or '未知'} 秒），已写回本凭据。",
                "updated_data": {"access_token": token},
            }

        token, err, _rejected, _ttl = _token_for(data)
        if not token:
            return {"ok": False, "error": err or "无法获取 access_token"}
        base = _base(data)

        if action_id == "probe_permissions":
            return _run_probe_permissions(token, base)
        if action_id == "list_face_groups":
            return _run_list_face_groups(token, base, params)
        if action_id == "list_face_users":
            return _run_list_face_users(token, base, params)
        return {"ok": False, "error": f"未知操作：{action_id}"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def capabilities(data: dict) -> dict:
    """探测人脸权限，隐藏用不了的人脸操作；拿不准一律 fail-open。"""
    data = data or {}
    if not _has_credentials(data):
        return {"available": [], "note": "缺少当前认证方式所需的密钥"}

    token, err, rejected, _ttl = _token_for(data)
    if not token:
        if rejected:
            return {"available": [], "note": err}
        raise RuntimeError(f"百度 AI 权限探测失败：{err}")

    payload, status_code, req_err = _call_api(
        _base(data), token, _FACE_GROUP_PATH, _DEFAULT_PROBE_BODY)
    if req_err:
        return {"available": None, "note": ""}       # 网络/非 JSON：未知 → 全部可用
    state = _classify(payload, status_code)

    if state == _PROBE_BAD_CRED:
        return {"available": [], "note": "access_token / API Key 被拒绝，凭据可能已失效"}

    available = ["probe_permissions"]
    if _mode(data) == MODE_AK_SK:
        available.append("refresh_access_token")
    if state != _PROBE_DENIED:
        available.extend(_FACE_ACTION_IDS)

    note = "缺少人脸识别权限（已隐藏人脸操作）" if state == _PROBE_DENIED else ""
    return {"available": available, "note": note}


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（预留接口）。"""
    mode = "access_token" if _mode(data) == MODE_TOKEN else "AK/SK"
    return f"{NAME} | {_base(data or {})} | {mode}"
