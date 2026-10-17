"""华为云 ModelArts MaaS API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://support.huaweicloud.com/intl/zh-cn/model-call-maas/model-call-019.html  （MaaS 标准 API V2 · 对话）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-029.html             （获取模型列表）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-035.html             （错误码）
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import (
    probe_requested,
    probe_model,
    quota_unknown,
)
from llm_protocols import (
    probe_openai_models,
    chat_completion,
    fetch_models,
    chat_completion_stream,
    resolve_chat_model,
    quota_chat_probe,
)

NAME = "华为云 ModelArts MaaS"
DEFAULT_BASE_URL = "https://api-ap-southeast-1.modelarts-maas.com/v2"
PROTOCOL = "openai_models"

# 余额不可获取时，可选用最小对话请求判断额度状态，默认关闭。
# `max_tokens` 仅限制正文，不包含思维链，且不能与 `max_completion_tokens` 同时设置。
# 正文为空不必然表示调用失败；`chat_completion` 依据完整响应信封设置 `reached_model`。

# 华为云「Key 认证通过、但没有开通任何预置模型服务」时，对话接口返回
# 403 `ModelArts.81004`（官方错误码文档：尚未开通调用的预置服务）。
# 通用 `quota_chat_probe` 只认 402/欠费关键词，会把这类 403 当成「该模型不可用」
# 逐个换候选，最后只剩含糊的「异常」；这里补一次读错误码的请求，把原因说清楚。
_ACCESS_DENIED_HINTS = ("81004", "do not have access")


def quota_probe(data: dict):
    """余额未知时的实测：在通用探测之外补一条「未开通预置服务」的判定。

    通用 `quota_chat_probe` 只认 402/欠费关键词，会把 403 `ModelArts.81004`
    当成「该模型不可用」逐个换候选，最后只剩含糊的「异常」。这里保留完整的
    候选遍历（部分模型可用时仍可能得出「可用」），只有**全部候选都给不出结论**
    时才补一次读错误码的请求，把真实原因说出来。
    """
    data = data or {}
    if not probe_requested(data):
        return None
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    api_key = (data.get("api_key") or "").strip()
    if not base_url or not api_key:
        return None

    verdict = quota_chat_probe(base_url, api_key, "openai", probe_model(data))
    if verdict:
        return verdict

    model = probe_model(data)
    if not model:
        try:
            models = fetch_models("openai", base_url, api_key)
        except Exception:  # noqa: BLE001 - 取列表失败按「没拿到」处理
            models = []
        model = models[0] if models else ""

    if model:
        resp = chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "hi"}],
            protocol="openai", max_tokens=1,
        )
        error = str(resp.get("error") or "")
        if any(hint in error.lower() for hint in _ACCESS_DENIED_HINTS):
            return {
                "account_info": {"chat_probe": "未开通"},
                "message": "凭证有效，但未开通调用的预置服务（ModelArts.81004）",
            }
    return quota_unknown()


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


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return probe_openai_models(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip(), force)


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
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    params = params or {}
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
        protocol="openai",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
