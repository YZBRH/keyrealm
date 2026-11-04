"""Google Gemini API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://ai.google.dev/api

协议：GET {base}/models?key=<API_KEY>（密钥走 query 参数）。
注意：密钥在 URL 中，异常兜底（guarded）只暴露异常类型名，避免泄漏。
"""
from __future__ import annotations

from typing import Optional

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json
from llm_common import make_quota_probe
from llm_protocols import chat_completion, fetch_models, chat_completion_stream, resolve_chat_model

NAME = "Google Gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PROTOCOL = "gemini"

# 这些 reason 表示 Key 本身被 Google 识别，只是被 API/来源限制挡住 —— 不是「密钥无效」。
_RESTRICTED_KEY_REASONS = {
    "API_KEY_SERVICE_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED",
    "API_KEY_IP_ADDRESS_BLOCKED",
    "API_KEY_ANDROID_APP_BLOCKED",
    "API_KEY_IOS_APP_BLOCKED",
}

# 余额不可用时的可选校验（设置页开关，默认关闭）：发送最小对话请求判断账户状态
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "gemini")


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写 Base URL")
    return errors


def _org_info(payload) -> Optional[dict]:
    """从响应中提取组织信息（Gemini 通常无此字段，保持与其它协议一致的输出）。"""
    if not isinstance(payload, dict):
        return None
    info = {}
    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]
    return info or None


def _google_error(payload) -> tuple[str, str]:
    """从 Google 错误响应中提取 ``(status, reason)``。

    相关字段通常位于 ``error.status`` 与 ``error.details[].reason``。"""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return "", ""
    reason = ""
    for item in err.get("details") or []:
        if isinstance(item, dict) and item.get("reason"):
            reason = str(item["reason"])
            break
    return str(err.get("status") or ""), reason


def _probe(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/models"
    with make_client() as client:
        resp = client.get(url, params={"key": api_key})
    status = resp.status_code
    payload = safe_json(resp)
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_org_info(payload),
        )
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")

    # 400/403 不统一判为无效：INVALID_ARGUMENT/API_KEY_INVALID 表示密钥无效，
    # FAILED_PRECONDITION 或 ``User location is not supported`` 表示出口地区不受支持，凭据状态未知。
    err_status, reason = _google_error(payload)
    lowered = snippet.lower()
    if reason == "API_KEY_INVALID" or "api key not valid" in lowered or status == 401:
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if reason in _RESTRICTED_KEY_REASONS:
        return VerifyResult(
            status="error",
            message=f"API Key 有效，但被 API 限制禁止调用 Gemini API（{reason}）；"
                    "请检查 Key 的 API 限制或项目是否已启用 Generative Language API",
        )
    if err_status == "FAILED_PRECONDITION" or "location is not supported" in lowered:
        return VerifyResult(
            status="error",
            message=f"出口地区不被 Gemini 支持，未能确认凭证状态（{snippet}）",
        )
    # 其他 403、5xx 及未知 400 保留 Google 返回原因，状态设为 error。
    message = f"无法确认凭证状态 (HTTP {status})"
    if err_status or reason:
        message += f" [{reason or err_status}]"
    return VerifyResult(status="error", message=f"{message}: {snippet}" if snippet else message)


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return _probe(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("gemini", base_url, (data.get("api_key") or "").strip(), force)


# ── 可选操作：简单对话 ──────────────────────────────────


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 API Key 时才提供简单对话）。"""
    if not (data or {}).get("api_key"):
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": ""}]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话（Gemini generateContent）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="gemini",
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="gemini",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
