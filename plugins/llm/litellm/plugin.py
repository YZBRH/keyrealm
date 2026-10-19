"""LiteLLM Proxy API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://docs.litellm.ai/docs/proxy/virtual_keys

LiteLLM 是开源的 LLM 网关（OpenAI 兼容）。验证走 `GET {base}/models`；
验证通过后再 best-effort 查 `GET {root}/key/info`，展示该 Key 的消费情况：

- 展示格式：`已消耗 $<spend> / $<max_budget>`（LiteLLM 的 spend 以美元计）；`max_budget` 为 null（不限额度）时只显示消耗，不显示总额
- LiteLLM 没有「预充值余额」概念，只有累计消费额
- 过期/无权限/接口不存在时不显示，不影响验证结论
"""
from __future__ import annotations

from typing import Optional

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe
from llm_protocols import (
    probe_openai_models,
    chat_completion,
    fetch_models,
    chat_completion_stream,
    resolve_chat_model,
)

NAME = "LiteLLM API"
DEFAULT_BASE_URL = ""
PROTOCOL = "openai_models"


def _base_url(data: dict) -> str:
    return ((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL


def _api_base(data: dict) -> str:
    """LiteLLM 的 API 固定挂在 `/v1`：用户只填域名时自动补齐。

    不补的话，裸域名 + `/models` 可能命中站点首页（200 HTML），被误判为「验证通过」。
    """
    base = _base_url(data).rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


# 欠费探测的默认地址与验证保持一致（含 /v1 归一化）；必须在 _api_base 定义之后
quota_probe = make_quota_probe(_api_base, "openai")


def _root(base_url: str) -> str:
    """`/key/info` 挂在服务根上：去掉末尾的 `/v1`，保留可能存在的部署子路径。"""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _fetch_key_info(base_url: str, api_key: str) -> Optional[dict]:
    """best-effort 查询 Key 的消费/预算（LiteLLM `GET /key/info`）。"""
    if not base_url or not api_key:
        return None
    try:
        with make_client() as client:
            resp = client.get(
                f"{_root(base_url)}/key/info",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            return None
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 余额查询失败绝不能影响验证结论
        return None
    if not isinstance(payload, dict):
        return None

    info = payload.get("info")
    if not isinstance(info, dict):
        info = payload

    spend = _num(info.get("spend"))
    budget = _num(info.get("max_budget"))
    out: dict = {}
    if spend is not None:
        # 有预算 → 「已消耗 / 总额」；无预算（max_budget=null）→ 只显示消耗，不编造总额
        if budget is not None:
            out["usage_label"] = f"已消耗 ${spend:.2f} / ${budget:.2f}"
        else:
            out["usage_label"] = f"已消耗 ${spend:.2f}"
    for key in ("key_name", "budget_duration", "budget_reset_at"):
        value = info.get(key)
        if value not in (None, ""):
            out[key] = value
    return out or None


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

    base_url = _api_base(data)
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    result = probe_openai_models(base_url, api_key)
    if result.status == "valid":
        info = _fetch_key_info(base_url, api_key)
        if info:
            result.account_info = {**(result.account_info or {}), **info}
    return result


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该网关可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    return fetch_models("openai", _api_base(data), (data.get("api_key") or "").strip(), force)


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
    base_url = _api_base(data)
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
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
    base_url = _api_base(data)
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
    return f"{NAME} | {_base_url(data)}"
