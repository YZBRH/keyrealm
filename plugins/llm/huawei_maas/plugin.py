"""华为云 ModelArts MaaS API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://support.huaweicloud.com/intl/zh-cn/model-call-maas/model-call-019.html  （MaaS 标准 API V2 · 对话）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-029.html             （获取模型列表）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-035.html             （错误码）
参考文档：https://support.huaweicloud.com/Token-plan-maas/tokenplan-maas-0008.md          （Token Plan · OpenCode 接入）
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
    is_insufficient_balance,
)

NAME = "华为云 ModelArts MaaS"
DEFAULT_BASE_URL = "https://api-ap-southeast-1.modelarts-maas.com/v2"
PROTOCOL = "openai_models"

# Token Plan（个人版套餐）走专属 Base URL，与按量付费的普通端点不同：
#   OpenAI 兼容：https://api.modelarts-maas.com/plan/v2
# 本插件只做凭据验证：有效凭据在两种兼容协议下都能调通，验证一条路径即可。
# 额度查询只能在控制台「订阅管理」看，没有公开 API。
_PLAN_MARK = "/plan/"
# 官方接入文档列出的 Plan 可用模型。Plan 端点**不保证**提供 `GET /models`，
# 校验时用它们做兜底候选（只用于读错误码，不写入凭据的默认模型）。
_PLAN_MODELS = (
    "deepseek-v4-flash", "deepseek-v4.1-flash", "glm-5.1",
    "glm-5.3", "kimi-k2.6", "deepseek-v3.2",
)


def _is_plan(base_url: str) -> bool:
    return _PLAN_MARK in (base_url or "")


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
    # Token Plan 端点不保证有 /models；用官方列出的套餐模型兜底读错误码
    if not model and _is_plan(base_url):
        model = _PLAN_MODELS[0]

    if model:
        resp = chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "hi"}],
            protocol="openai", max_tokens=1,
        )
        error = str(resp.get("error") or "")
        low = error.lower()
        if any(hint in low for hint in _ACCESS_DENIED_HINTS):
            message = ("凭证有效，但未开通调用的预置服务（ModelArts.81004）" if not _is_plan(base_url)
                       else "凭证有效，但 Token Plan 未开通、已过期或该模型不在套餐内（ModelArts.81004）")
            return {"account_info": {"chat_probe": "未开通"}, "message": message}
        if _is_plan(base_url) and ("429" in error or "quota" in low or "exceeded" in low):
            return {
                "account_info": {"chat_probe": "额度用尽"},
                "message": "凭证有效，但 Token Plan 套餐额度可能已用尽（额度只能在控制台查看）",
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


def _verify_plan(base_url: str, api_key: str) -> VerifyResult:
    """Token Plan 专属端点的校验。

    Plan 端点与按量付费端点的**响应能力不同**：官方没有把 `GET /models` 列为 Plan 能力，
    所以先试模型列表（有就用），拿不到再按官方模型清单发最小对话请求。
    判定：401 = Key 无效；402/欠费 = 有效但欠费；429 = 套餐额度用尽；
    81004 = 未开通/过期/该模型不在套餐。
    有效凭据在 OpenAI / Anthropic 两种兼容协议下都能调通，这里只验 OpenAI 一条路径。
    """
    probe = probe_openai_models(base_url, api_key)
    if probe.status == "valid":
        return VerifyResult(status="valid", message="凭证有效（Token Plan）",
                            account_info={"plan": "Token Plan"})
    # 只有 401 能确定是 Key 的问题；/models 不存在、403 等都用对话实测再下结论
    if probe.status == "invalid" and "HTTP 401" in (probe.message or ""):
        return probe

    denied = False
    for model in _PLAN_MODELS:
        resp = chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "hi"}],
            protocol="openai", max_tokens=1,
        )
        if resp.get("ok") or resp.get("reached_model"):
            return VerifyResult(status="valid", message="凭证有效（Token Plan）",
                                account_info={"plan": "Token Plan"})
        error = str(resp.get("error") or "")
        low = error.lower()
        if "401" in error or "invalid authorization" in low:
            return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
        if "402" in error or is_insufficient_balance(error):
            return VerifyResult(status="valid",
                                message="凭证有效，但账户欠费或无可用额度",
                                account_info={"plan": "Token Plan"})
        if "429" in error or "quota" in low or "exceeded" in low:
            return VerifyResult(status="valid",
                                message="凭证有效，但 Token Plan 套餐额度可能已用尽（额度只能在控制台查看）",
                                account_info={"plan": "Token Plan"})
        if any(hint in low for hint in _ACCESS_DENIED_HINTS):
            # 81004 可能是「该模型不在套餐内」——换下一个套餐模型继续试，
            # 全部都被拒才说明套餐本身未开通 / 已过期。
            denied = True
            continue
        # 404 / 模型不存在 / 限流等 → 换下一个套餐模型
    if denied:
        return VerifyResult(status="valid",
                            message="凭证有效，但 Token Plan 未开通、已过期或所选模型不在套餐内（ModelArts.81004）",
                            account_info={"plan": "Token Plan"})
    return VerifyResult(status="error",
                        message="无法确认凭证状态：Token Plan 模型均不可用")


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    if _is_plan(base_url):
        return _verify_plan(base_url, api_key)
    return probe_openai_models(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。

    Token Plan 端点不保证提供 `/models`；取不到时回退到官方文档列出的套餐模型，
    否则「简单对话」会因为没有候选模型而无法发起。
    """
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    models = fetch_models("openai", base_url, (data.get("api_key") or "").strip(), force)
    if not models and _is_plan(base_url):
        return list(_PLAN_MODELS)
    return models


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
