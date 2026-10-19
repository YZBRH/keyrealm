"""兼容接口 API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

支持两种接口格式，由凭据字段 ``api_style`` 选择：

- ``openai``（默认）：``GET {base}/models``，Bearer 认证
- ``anthropic``：``GET {base}/models``，``x-api-key`` + ``anthropic-version``

两种格式的 Base URL 可填写至 ``/v1``，也可填写不带版本路径的域名；请求路径会统一进行
``/v1`` 归一化（见 ``llm_protocols._anthropic_root`` 与 ``probe_anthropic_models``）。

余额：one-api / new-api 类中转实现 ``/dashboard/billing/*`` 接口，采用尽力查询。"""
from __future__ import annotations

from datetime import date
from typing import Optional

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe, plausible_amount
from llm_protocols import (
    chat_completion,
    chat_completion_stream,
    fetch_models,
    probe_anthropic_models,
    probe_openai_models,
    resolve_chat_model,
)

NAME = "兼容接口"
DEFAULT_BASE_URL = ""
STYLES = ("openai", "anthropic")
DEFAULT_STYLE = "openai"

# OpenAI 协议分支使用 `max_completion_tokens`；`max_tokens` 已标记为 deprecated，前者兼容严格网关及 o 系列模型。
# `anthropic` 协议仍使用 `max_tokens`；本参数仅对 `openai` 分支生效。
TOKEN_PARAM = "max_completion_tokens"


def style(data: dict) -> str:
    """返回凭据使用的接口格式（字段 ``api_style``，默认 ``openai``）。

    中转站的协议能力无法从地址可靠推断，因此仅依据显式字段选择格式。"""
    raw = str((data or {}).get("api_style") or "").strip().lower()
    return raw if raw in STYLES else DEFAULT_STYLE


# 欠费探测也走同一个 token 参数名（anthropic 风格下该参数被忽略，只对 openai 分支生效）。
quota_probe = make_quota_probe(DEFAULT_BASE_URL, style, TOKEN_PARAM)


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


def _fetch_openai_billing(base_url: str, api_key: str) -> Optional[dict]:
    """查询中转服务余额：优先通过 ``/dashboard/billing/subscription`` 与 ``/usage`` 计算，失败后读取 ``/credit_grants``。

    ``hard_limit_usd`` 等哨兵值表示不限额，此时使用 ``total_available``；无法获得可信数值时返回 ``None``。"""
    if not base_url:
        return None
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    with make_client() as client:
        total = 0.0
        sub = client.get(f"{root}/dashboard/billing/subscription", headers=headers)
        if sub.status_code == 200:
            sub_data = safe_json(sub)
            if isinstance(sub_data, dict):
                try:
                    total = float(
                        sub_data.get("hard_limit_usd")
                        or sub_data.get("system_hard_limit_usd")
                        or 0
                    )
                except (TypeError, ValueError):
                    total = 0.0

        # `total_usage` 在 new-api 系实现表示令牌的终身累计用量，不一定是本月用量；
        # 日期参数仍然传递，以兼容按月统计的实现。
        if total > 0 and plausible_amount(total):
            used = 0.0
            today = date.today()
            usage = client.get(
                f"{root}/dashboard/billing/usage",
                headers=headers,
                params={"start_date": today.replace(day=1).isoformat(),
                        "end_date": today.isoformat()},
            )
            if usage.status_code == 200:
                u = safe_json(usage)
                if isinstance(u, dict):
                    try:
                        used = float(u.get("total_usage", 0)) / 100.0  # 美分 → 美元
                    except (TypeError, ValueError):
                        used = 0.0
            balance = round(total - used, 6)
            return {"balance": balance, "currency": "USD"} if plausible_amount(balance) else None

        # 无硬限额（哨兵值或字段缺失）：改查 credit_grants 的可用额度
        grants = client.get(f"{root}/dashboard/billing/credit_grants", headers=headers)
        if grants.status_code == 200:
            g = safe_json(grants)
            if isinstance(g, dict) and "total_available" in g:
                available = g.get("total_available")
                if plausible_amount(available):
                    return {"balance": round(float(available), 6), "currency": "USD"}
    return None


def _fetch_balance(base_url: str, api_key: str) -> Optional[dict]:
    """查询余额（best-effort），失败返回 None。"""
    try:
        return _fetch_openai_billing(base_url, api_key)
    except Exception:
        return None


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    if style(data) == "anthropic":
        result = probe_anthropic_models(base_url, api_key, "/models")
        if result.status == "invalid":
            # anthropic 风格的 `/models` 实现并不统一，部分中转站仅支持 OpenAI 风格 Bearer；
            # 404/403 可能仅表示端点未实现，因此保留不确定性并提示改用 OpenAI 格式或直接对话验证。
            result.message = (
                f"{result.message or '凭证无效'}；也可能该中转站未实现 anthropic 风格的 /models"
                "（可改用 openai 格式，或忽略此项、直接用「简单对话」验证）"
            )
    else:
        result = probe_openai_models(base_url, api_key)

    # 认证成功：查询余额，负余额标记欠费
    if result.status == "valid":
        balance = _fetch_balance(base_url, api_key)
        if balance:
            info = dict(result.account_info or {})
            info["balance"] = balance["balance"]
            info["currency"] = balance["currency"]
            if balance.get("usage_rate") is not None:
                info["usage_rate"] = balance["usage_rate"]
            result.account_info = info
            if balance["balance"] < 0:
                # 欠费不算失效：凭据本身有效，仅账户余额不足
                result.message = (
                    f"凭证有效，但账户欠费（余额 {balance['balance']} {balance['currency']}）"
                )

    return result


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该站点可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models(style(data), base_url, (data.get("api_key") or "").strip(), force)


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
        protocol=style(data),
        token_param=TOKEN_PARAM,
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
        protocol=style(data),
        token_param=TOKEN_PARAM,
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
