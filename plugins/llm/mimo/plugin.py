"""小米 MiMo API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call

MiMo 同时兼容 OpenAI 与 Anthropic 协议；按「有效凭据两种协议都能调通」的验证目标，
插件只走 OpenAI 兼容路径。官方支持两种计费方式，**Key 与 Base URL 都不能混用**：

- 按量付费：Key `sk-…`，默认地址 `https://api.xiaomimimo.com/v1`
- Token Plan：Key `tp-…`（个人）/ `ttp-…`（团队），必须用套餐页给出的
  `https://token-plan-<cn|sgp|ams>.xiaomimimo.com/v1`

官方 V2.2.0（2026-11-03）起提供账务查询 OpenAPI：
- sk- Key：``GET https://api.xiaomimimo.com/v1/platform/balance`` 查现金余额；
- tp-/ttp- Key：``GET {token-plan-<cn|sgp|ams>}/v1/platform/token_plan`` 查套餐余量。
两者都按 best-effort 处理，查询失败不影响验证结论。
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlparse

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe, plausible_amount
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


# 官方 V2.2.0（2026-11-03）新增账务查询 OpenAPI：
# - 余额：GET https://api.xiaomimimo.com/v1/platform/balance（固定主机，sk- Key）
# - 套餐余量：GET {token-plan-<cn|sgp|ams> 主机}/v1/platform/token_plan（tp-/ttp- Key）
BALANCE_URL = "https://api.xiaomimimo.com/v1/platform/balance"
PLAN_PATH = "/v1/platform/token_plan"
ASSET_CACHE_TTL = 60.0
_asset_cache: dict[tuple[str, str], tuple[float, dict | None, bool | None]] = {}

# 余额拿不到时的可选项（凭据级开关，默认关闭）：发一次最小对话请求判断是否欠费
_generic_quota_probe = make_quota_probe(_base_url, "openai")


def _origin(base_url: str) -> str:
    try:
        parsed = urlparse(base_url or "")
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _amount(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_balance(payload) -> tuple[dict | None, bool | None]:
    """解析 /v1/platform/balance；返回 (展示字段, is_available)。"""
    if not isinstance(payload, dict):
        return None, None
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
        return None, None
    item = infos[0]
    cash = _amount(item.get("cash_balance"))
    gift = _amount(item.get("gift_balance"))
    total = _amount(item.get("total_available"))
    if cash is not None or gift is not None:
        balance = (cash or 0.0) + (gift or 0.0)
    elif total is not None:
        balance = total
    else:
        return None, None
    if not plausible_amount(balance):
        return None, None
    info = {"balance": balance, "currency": str(item.get("currency") or "CNY")}
    available = payload.get("is_available")
    return info, (available if isinstance(available, bool) else None)


def _parse_plan(payload) -> dict | None:
    """解析 /v1/platform/token_plan：套餐名、本周期剩余 credits、到期时间。"""
    if not isinstance(payload, dict):
        return None
    info: dict[str, str] = {}
    plan_code = str(payload.get("plan_code") or "").strip()
    if plan_code:
        info["套餐"] = plan_code
    quota = payload.get("quota")
    if isinstance(quota, dict):
        total = _amount(quota.get("total_credits"))
        remaining = _amount(quota.get("remaining_credits"))
        if remaining is not None and total is not None:
            info["套餐剩余"] = f"{remaining:g}/{total:g}"
        elif remaining is not None:
            info["套餐剩余"] = f"{remaining:g}"
    period = payload.get("period")
    if isinstance(period, dict):
        end = str(period.get("end_at") or "").strip()
        if end:
            info["套餐到期"] = end[:10]
    return info or None


def _fetch_assets(data: dict):
    """按 Key 类型查询余额或套餐（best-effort + 60s 缓存）；返回 (info, is_available)。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return None, None
    kind = _key_kind(api_key)
    if kind.startswith("Token Plan"):
        url = _origin(_base_url(data)) + PLAN_PATH
    else:
        url = BALANCE_URL
    cache_key = (url, hashlib.sha256(api_key.encode("utf-8")).hexdigest())
    now = time.monotonic()
    hit = _asset_cache.get(cache_key)
    if hit and now - hit[0] < ASSET_CACHE_TTL:
        return hit[1], hit[2]
    info = None
    available = None
    try:
        with make_client() as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code == 200:
            payload = safe_json(resp)
            if kind.startswith("Token Plan"):
                info = _parse_plan(payload)
            else:
                info, available = _parse_balance(payload)
    except Exception:  # noqa: BLE001 - 账务查询失败绝不能影响验证结论
        info, available = None, None
    _asset_cache[cache_key] = (now, info, available)
    if len(_asset_cache) > 256:
        for key in [k for k, (ts, _i, _a) in _asset_cache.items()
                    if now - ts >= ASSET_CACHE_TTL]:
            _asset_cache.pop(key, None)
    return info, available


def quota_probe(data: dict):
    """验证通过后的欠费探测：有余额/套餐信息时不发对话探针，避免重复表述。"""
    info, _available = _fetch_assets(data or {})
    if info:
        return {"account_info": info}
    return _generic_quota_probe(data)


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
    if result.status == "valid":
        info = {}
        if kind:
            info["account_type"] = kind
        asset, available = _fetch_assets(data)
        if asset:
            info.update(asset)
        if info:
            result.account_info = {**(result.account_info or {}), **info}
        if available is False:
            result.message = "凭证有效，但账户余额不足"
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
