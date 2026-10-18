"""小米 MiMo API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call

MiMo 同时兼容 OpenAI 与 Anthropic 协议；按「有效凭据两种协议都能调通」的验证目标，
插件只走 OpenAI 兼容路径。官方支持两种计费方式，**Key 与 Base URL 都不能混用**：

- 按量付费：Key `sk-…`，默认地址 `https://api.xiaomimimo.com/v1`
- Token Plan：Key `tp-…`（个人）/ `ttp-…`（团队），必须用套餐页给出的
  `https://token-plan-<cn|sgp|ams>.xiaomimimo.com/v1`

官方没有公开的额度 / 余额查询接口（控制台用量只暴露给浏览器会话），因此不查询余额，
只提供可选的欠费探测。
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import make_quota_probe
from llm_protocols import (
    probe_openai_models,
    chat_completion,
    fetch_models,
    chat_completion_stream,
    resolve_chat_model,
)

NAME = "小米 MiMo"
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
PROTOCOL = "openai_models"

# 模型清单里混有语音 / 转写类模型（asr / tts / voiceclone / voicedesign），
# 它们不能用于对话，从「简单对话」的候选里剔除。
_NON_CHAT_HINTS = ("asr", "tts", "voiceclone", "voicedesign", "embed", "rerank")


def _base_url(data: dict) -> str:
    return ((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL


# 余额拿不到时的可选项（凭据级开关，默认关闭）：发一次最小对话请求判断是否欠费
quota_probe = make_quota_probe(_base_url, "openai")


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    return errors


def _key_kind(api_key: str) -> str:
    """按 Key 前缀标注计费方式（官方约定的三种前缀）。"""
    if api_key.startswith("ttp-"):
        return "Token Plan（团队）"
    if api_key.startswith("tp-"):
        return "Token Plan（个人）"
    if api_key.startswith("sk-"):
        return "按量付费"
    return ""


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = _base_url(data)
    result = probe_openai_models(base_url, api_key)
    kind = _key_kind(api_key)
    if result.status == "valid" and kind:
        result.account_info = {**(result.account_info or {}), "account_type": kind}
    return result


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取可用于对话的模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    models = fetch_models("openai", _base_url(data), (data.get("api_key") or "").strip(), force)
    return [m for m in models if not any(h in m.lower() for h in _NON_CHAT_HINTS)]


# ── 可选操作：简单对话 ──────────────────────────────────


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 API Key 时才提供简单对话）。"""
    if not (data or {}).get("api_key"):
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": ""}]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = _base_url(data)
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = _base_url(data)
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    return f"{NAME} | {_base_url(data)}"
