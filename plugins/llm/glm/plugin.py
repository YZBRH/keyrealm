"""智谱 GLM API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://docs.bigmodel.cn/api-reference

智谱**不按协议分权**：同一把 key 既能调 OpenAI 兼容端点，也能调 Anthropic 兼容端点
（`claude-sonnet-4` 这类 Claude 模型名也由智谱做了映射）。插件按 **Base URL 自动选协议**：

- OpenAI 兼容（默认）：`/api/paas/v4`；GLM Coding Plan 的 OpenAI 端点是 `/api/coding/paas/v4`
- Anthropic 兼容：`/api/anthropic`（Claude Code / Anthropic SDK 的 `ANTHROPIC_BASE_URL`，
  路径里含 `/anthropic` 即按 Anthropic 协议处理；国际站 `api.z.ai/api/anthropic` 同理）

> 注意：Coding Plan 套餐额度**只走 Anthropic 端点**。同一把 coding-plan key 打到
> `/api/paas/v4` 会报「余额不足或无可用资源包」，用 `/api/anthropic` 才正常计套餐额度。

GLM 有 `/models`（无鉴权访问返回 401，说明端点存在），但 **chat 探针**能额外识别
429 里的余额不足错误码 **1113**，因此 verify 优先走 chat 探针、拿不到模型时退回 `/models` 探针。

余额：控制台「财务总览」自身调用的只读接口
``GET /api/biz/account/query-customer-account-report``（Authorization: Bearer <API Key>）。
国内站 ``open.bigmodel.cn`` 与 Z.ai 国际站 ``api.z.ai`` 同路径可用，认证失败分别返回
``code=1001/401``。该接口不属于公开 OpenAPI，按 best-effort 处理：查询失败不影响验证结论。
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlparse

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe, plausible_amount
from llm_protocols import (
    chat_completion,
    chat_completion_stream,
    fetch_models,
    probe_anthropic_messages,
    probe_anthropic_models,
    probe_openai_chat,
    probe_openai_models,
    resolve_chat_model,
)

NAME = "智谱 GLM"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
# 控制台财务总览自己调用的余额接口（非公开 OpenAPI，best-effort）
BALANCE_PATH = "/api/biz/account/query-customer-account-report"
# Coding Plan：订阅列表 + 额度用量（同样是控制台内部接口）
PLAN_SUBSCRIPTION_PATH = "/api/biz/subscription/list"
PLAN_QUOTA_PATH = "/api/monitor/usage/quota/limit"
PLAN_CACHE_TTL = 60.0
_plan_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
BALANCE_HOSTS = (
    ("bigmodel.cn", "CNY"),
    ("z.ai", "USD"),
)

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"


def base_url_of(data: dict) -> str:
    """实际使用的 Base URL：凭据填写优先，留空取默认地址。"""
    return ((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL


def protocol_for(base_url: str) -> str:
    """按 Base URL 的路径判断协议：含 ``/anthropic`` 走 Anthropic，否则走 OpenAI 兼容。"""
    try:
        path = urlparse(base_url or "").path or ""
    except ValueError:
        path = ""
    return PROTOCOL_ANTHROPIC if "/anthropic" in path.lower() else PROTOCOL_OPENAI


def protocol_of(data: dict) -> str:
    return protocol_for(base_url_of(data or {}))


# 余额拿不到时的可选项（凭据级开关，默认关闭）：发一次最小对话请求判断是否欠费。
# 协议跟随 Base URL —— 同一插件支持 OpenAI / Anthropic 两种端点，不能写死 openai。
_generic_quota_probe = make_quota_probe(base_url_of, protocol_of)


def quota_probe(data: dict):
    """验证通过后的欠费探测。

    **官方站点**（bigmodel / z.ai）已经直接查询套餐与余额，附加信息里该有的都有了，
    不再发对话探针：否则会多出「chat_probe 欠费 / 实测」这类重复表述，甚至覆盖
    套餐结论。只有自定义 / 中转 Base URL（查不到余额）才退回通用对话探测。
    """
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return None
    base_url = base_url_of(data)
    origin, _currency = _balance_origin(base_url)
    if origin:
        plan = _fetch_plan(base_url, api_key)
        return {"account_info": plan} if plan else None
    return _generic_quota_probe(data)


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


def _balance_origin(base_url: str) -> tuple[str, str]:
    """余额接口所在的站点与币种；非官方站点（中转 / 自建）返回空，不去瞎试。"""
    try:
        parsed = urlparse(base_url or "")
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").lower()
    for suffix, currency in BALANCE_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return f"{parsed.scheme or 'https'}://{host}", currency
    return "", ""


_CN_NUMBERS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
               6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _cn_number(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return _CN_NUMBERS.get(number, str(number))


def _plan_quota_fields(quota_payload) -> dict:
    """把 quota/limit 里各个 TOKENS_LIMIT 窗口摊成「XX剩余 NN%」字段。

    ``percentage`` 是**已用**百分比（新账号为 0），展示时换算成剩余。
    5 小时窗口 → ``五小时剩余 56%``；1 周窗口 → ``周剩余 77%``；其他窗口按单位兜底。
    """
    data = (quota_payload or {}).get("data")
    if not isinstance(data, dict):
        return {}
    fields: dict[str, str] = {}
    for item in data.get("limits") or []:
        if not isinstance(item, dict) or item.get("type") != "TOKENS_LIMIT":
            continue
        pct = item.get("percentage")
        if pct is None:
            continue
        try:
            used = float(pct)
        except (TypeError, ValueError):
            continue
        remaining = max(0, min(100, round(100 - used)))
        unit, number = item.get("unit"), item.get("number")
        if unit == 3:
            label = f"{_cn_number(number)}小时剩余"
        elif unit == 6:
            label = "周剩余" if str(number) == "1" else f"{_cn_number(number)}周剩余"
        else:
            label = f"{_cn_number(number)}窗口剩余"
        fields[label] = f"{remaining}%"
    return fields


def _parse_plan(sub_payload, quota_payload) -> dict | None:
    """解析「当前是否有有效 Coding Plan」；返回可直接并入 account_info 的单个字段。

    优先看订阅列表（能拿到产品名与续费时间）；列表为空时用 quota 接口兜底
    （`data.limits` 非空即认为存在套餐）。没有有效套餐返回 ``None``。
    """
    rows = (sub_payload or {}).get("data")
    active = None
    if isinstance(rows, list):
        active = next(
            (row for row in rows
             if isinstance(row, dict) and str(row.get("status") or "").upper() == "VALID"),
            None,
        )
    if active:
        name = str(active.get("productName") or active.get("productBigTitle")
                   or "GLM Coding Plan").strip()
        plan = {"套餐": name}
        plan.update(_plan_quota_fields(quota_payload))
        return plan

    data = (quota_payload or {}).get("data")
    if isinstance(data, dict) and data.get("limits"):
        level = str(data.get("level") or "").strip()
        name = f"GLM Coding Plan{' · ' + level if level else ''}"
        plan = {"套餐": name}
        plan.update(_plan_quota_fields(quota_payload))
        return plan
    return None


def _fetch_plan(base_url: str, api_key: str) -> dict | None:
    """查询 Coding Plan（best-effort + 60s 缓存）；非官方站点直接返回 None。"""
    origin, _ = _balance_origin(base_url)
    if not origin:
        return None
    cache_key = (origin, hashlib.sha256(api_key.encode("utf-8")).hexdigest())
    now = time.monotonic()
    hit = _plan_cache.get(cache_key)
    if hit and now - hit[0] < PLAN_CACHE_TTL:
        return hit[1]
    plan = None
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        with make_client() as client:
            sub_resp = client.get(origin + PLAN_SUBSCRIPTION_PATH, headers=headers)
            quota_resp = client.get(origin + PLAN_QUOTA_PATH, headers=headers)
        sub_payload = safe_json(sub_resp) if sub_resp.status_code == 200 else None
        quota_payload = safe_json(quota_resp) if quota_resp.status_code == 200 else None
        plan = _parse_plan(sub_payload, quota_payload)
    except Exception:  # noqa: BLE001 - 套餐查询失败绝不能影响验证结论
        plan = None
    _plan_cache[cache_key] = (now, plan)
    if len(_plan_cache) > 256:
        for key in [k for k, (ts, _v) in _plan_cache.items() if now - ts >= PLAN_CACHE_TTL]:
            _plan_cache.pop(key, None)
    return plan


def _fetch_balance(base_url: str, api_key: str) -> dict | None:
    """查询账户余额（best-effort）；任何失败都返回 ``None``，绝不影响验证结论。"""
    origin, currency = _balance_origin(base_url)
    if not origin:
        return None
    try:
        with make_client() as client:
            resp = client.get(
                origin + BALANCE_PATH,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            return None
        payload = safe_json(resp)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        raw = data.get("availableBalance")
        if raw in (None, ""):
            raw = data.get("balance")
        balance = float(raw)
    except Exception:  # noqa: BLE001 - 余额查询失败绝不能影响验证结论
        return None
    if not plausible_amount(balance):
        return None
    return {"balance": balance, "currency": currency}


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = base_url_of(data)
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    # 探测模型**绝不写死**：优先凭据自填的 `probe_model`，其次取运行时 `/models` 清单首项。
    protocol = protocol_for(base_url)
    model = resolve_chat_model(data.get("probe_model"), lambda: list_models(data))
    if not model:
        # 拿不到模型（多半是 key 无效导致 /models 401）→ 退回**不需要模型**的 `/models` 探针。
        # 否则「坏 key」会被误报成「无法确定探测模型」，**假红比漏报更坑**。
        if protocol == PROTOCOL_ANTHROPIC:
            result = probe_anthropic_models(base_url, api_key)
        else:
            result = probe_openai_models(base_url, api_key)
    elif protocol == PROTOCOL_ANTHROPIC:
        result = probe_anthropic_messages(base_url, api_key, model)
    else:
        result = probe_openai_chat(base_url, api_key, model)

    if result.status == "valid":
        # 官方 Key：先判断有没有 Coding Plan，再看余额。套餐额度与现金余额是两套计费，
        # plan 账号的「按量付费余额为 0」不能当成欠费。
        plan = _fetch_plan(base_url, api_key)
        balance = _fetch_balance(base_url, api_key)
        info = dict(result.account_info or {})
        if plan:
            info.update(plan)
        if balance:
            info.update(balance)
        if info:
            result.account_info = info

        if plan and any(word in result.message for word in ("欠费", "余额不足", "资源包")):
            if protocol == PROTOCOL_ANTHROPIC:
                result.message = "凭证有效，使用 GLM Coding Plan 套餐"
            else:
                result.message = ("凭证有效，账户有 GLM Coding Plan 套餐；"
                                  "该套餐仅适用于 /api/anthropic（Anthropic 兼容）端点")
        elif (protocol == PROTOCOL_ANTHROPIC and not plan
              and ("套餐" in result.message or "订阅" in result.message)):
            # 1309：Anthropic 端点只认 Coding Plan；按量付费余额要走 OpenAI 兼容端点
            result.message = ("凭证有效，但该 Anthropic 端点需要 GLM Coding Plan 套餐"
                              "（已到期或未开通）；按量付费余额请改用 /api/paas/v4（OpenAI 兼容）")
    return result


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    return fetch_models(protocol_of(data), base_url_of(data),
                        (data.get("api_key") or "").strip(), force)


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
    base_url = base_url_of(data)
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
        protocol=protocol_for(base_url),
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = base_url_of(data)
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
        protocol=protocol_for(base_url),
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    return f"{NAME} | {base_url_of(data or {})}"
