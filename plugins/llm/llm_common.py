"""LLM 类别共享模块：**类别特有**的欠费探测工具。

通用工具（结果类型、日志、代理、HTTP 客户端、响应解析、异常兜底、超时、写/列表辅助）
已上收框架层的 ``backend/plugin_sdk.py``，本模块**不再重复**它们 —— 需要时直接
``from backend.plugin_sdk import ...``。

这里只留 LLM 类别特有的东西：**「余额拿不到时发一次最小对话探测欠费」**这条能力
（`make_quota_probe` 通用实现 + 非通用鉴权的插件自建 `quota_probe` 时复用的读取/结论辅助）。

协议级工具（探针、简单对话等跨厂商完全相同的能力）在**独立的工具模块**
`plugins/llm/llm_protocols.py`，由需要的子插件按需 `from llm_protocols import ...`。

各厂商的默认地址、模型名、余额端点与解析等**专属逻辑**一律写在
`plugins/llm/<厂商>/plugin.py` 内（厂商之间允许重复，刻意为之）。

依赖方向：`plugin.py` → `llm_common.py` / `llm_protocols.py` → `backend.plugin_sdk`。
本模块只依赖标准库与 SDK，不导入 backend 的其它模块。
"""
from __future__ import annotations

import math
from typing import Optional

# ── 金额合理性 ───────────────────────────────────────────

# 金额上限（美元/人民币）：正规 LLM 账户的余额或配额不会到千万量级。
# one-api / new-api 类中转站在「不限额」时会把硬限额填成 99999999.99 这类哨兵值，
# 拿它去减消耗就会得出 99999996.47 这种假余额，所以任何要展示的金额都先过一遍这个闸。
AMOUNT_LIMIT = 1e7


def plausible_amount(value) -> bool:
    """金额是否可信到可以展示（有限数值，且绝对值不超过 ``AMOUNT_LIMIT``）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(num) and abs(num) < AMOUNT_LIMIT


# ── 欠费探测（余额拿不到时的可选项） ─────────────────────
#
# 契约：子插件可导出 ``quota_probe(data) -> Optional[dict]``，后端在「验证通过且没拿到
# 余额/使用率/套餐」时调用它（见 backend/plugin_manager._apply_quota_probe）。返回值：
#   None                          → 未开启 / 无结论 / 无异常，后端什么都不改
#   {"account_info": {...}}       → 补充展示字段（用 quota_ok()）
#   {"message": "..."}            → 替换状态说明（用 quota_insufficient()）
#
# 绝大多数插件用 ``quota_probe = make_quota_probe(DEFAULT_BASE_URL, "<对话协议>")`` 一行接入；
# **鉴权非通用格式的插件**（如百度文心要先拿 API Key + Secret Key 换 access_token、聊天端点
# 还带模型后缀）在自己的 plugin.py 里实现 quota_probe，复用下面这些读取/结论辅助函数即可。

def _truthy(value) -> bool:
    """凭据里的开关字段是否为真（兼容布尔与字符串形式）。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def probe_requested(data: dict) -> bool:
    """该凭据是否开启了「欠费探测」（凭据字段 ``probe_chat``，默认关闭）。"""
    return _truthy((data or {}).get("probe_chat"))


def probe_model(data: dict) -> str:
    """该凭据指定的探测模型（凭据字段 ``probe_model``）；空串 = 自动挑一个便宜的。"""
    return str((data or {}).get("probe_model") or "").strip()


def quota_ok() -> dict:
    """探测结论：实测可用 → 附加信息补一项「实测 可用」。"""
    return {"account_info": {"chat_probe": "可用"}}


def quota_insufficient() -> dict:
    """探测结论：实测欠费 → 替换状态说明（``status`` 仍为 valid，欠费 ≠ 凭据失效）。"""
    return {"message": "凭证有效，但账户欠费或无可用额度（已用最小对话实测）"}


def quota_unknown() -> dict:
    """探测结论：**试过但没跑通**，无法判断账户是否可用 → 附加信息补一项「实测 异常」。

    与 ``quota_ok`` / ``quota_insufficient`` 并列的第三种结局，**必须说出来**：只回「可用」
    的话，探测失败时附加信息列什么都不显示 —— 用户勾了开关却看不到任何反馈，也分不清
    「没开探测」和「探测失败」。

    用「异常」而不是「未通过」：这条凭据的**状态本来就是「有效」**，同屏再出现「未通过」
    会自相矛盾（看着像凭据有问题）。「异常」说的是**探测这一步**没跑通（模型都不可用 /
    鉴权 / 限流 / 网络），不涉及凭据本身的判定。失败原因写日志，不塞进界面上这一行。
    """
    return {"account_info": {"chat_probe": "异常"}}


def make_quota_probe(default_base_url, protocol="openai", token_param="max_tokens"):
    """生成子插件的 ``quota_probe(data)`` —— 通用（OpenAI / Anthropic / Gemini 协议）实现。

    ``token_param`` 原样透传给 ``quota_chat_probe``：阿里云百炼与 MiniMax 的官方文档已把
    ``max_tokens`` 标为 deprecated、要求改用 ``max_completion_tokens``，由这两家插件显式传入。

    ``default_base_url`` 与 ``protocol`` 可以是常量，也可以是 ``fn(data) -> 值`` 的**可调用对象**
    ——同一插件支持多种接口格式时（如智谱 GLM 同时提供 OpenAI 与 Anthropic 端点）用它按凭据取。

    开关与模型取自**凭据自身**的字段（``probe_chat`` / ``probe_model``），逐条凭据独立开启，
    不涉及全局设置。后端已查到余额 / 用量 / 套餐时不会调用本函数（见
    ``plugin_manager._has_billing_info``）。真正发请求的是 ``llm_protocols.quota_chat_probe``（延迟导入，避免模块成环）。
    非通用鉴权的插件请自行实现 ``quota_probe``，复用 ``probe_requested`` / ``probe_model`` /
    ``quota_ok`` / ``quota_insufficient`` 即可。
    """
    def quota_probe(data: dict) -> Optional[dict]:
        data = data or {}
        if not probe_requested(data):
            return None
        default = default_base_url(data) if callable(default_base_url) else default_base_url
        proto = protocol(data) if callable(protocol) else protocol
        base_url = (data.get("base_url") or "").strip() or default
        api_key = (data.get("api_key") or "").strip()
        if not base_url or not api_key:
            return None
        from llm_protocols import quota_chat_probe
        verdict = quota_chat_probe(base_url, api_key, proto, probe_model(data), token_param)
        if verdict:
            # 可能是 ok / 欠费，也可能带 status=invalid/error（鉴权失败、接口不可用）；
            # 后者由 plugin_manager._apply_quota_probe 升级凭据状态。
            return verdict
        # 试过但没得出确定结论 → 报「实测 异常」（见 quota_unknown 的说明）。
        # 「None == 无法判定」这个契约留给 `quota_chat_probe`（nvidia 那类自己遍历候选、
        # 靠真值判断的调用方依赖它），把它翻译成界面上那一行是这里的职责。
        return quota_unknown()

    return quota_probe
