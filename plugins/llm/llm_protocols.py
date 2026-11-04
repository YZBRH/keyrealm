"""LLM 协议级工具模块（供各厂商子插件复用）。

不放最底层工具（那些在 llm_common.py），只放**协议级、跨厂商完全相同**的能力：

- 校验探针：`probe_openai_models` / `probe_anthropic_models` / `probe_openai_chat`
  （统一经 `_request_with_retry` 发请求：上游 5xx 退避重试，避免把偶发故障误判为失败）
- 简单对话：`chat_completion`（openai / anthropic / gemini 三种协议）
- 欠费探测：`quota_chat_probe`（通用实现）、`is_insufficient_balance`（关键字判定，
  供自建 `quota_probe` 的插件复用）

厂商的默认地址、模型名、余额端点与解析等专属逻辑仍写在各
plugins/llm/<厂商>/plugin.py 内。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.plugin_sdk import VerifyResult, body_snippet, log, make_client, safe_json, timeout

# 欠费/余额不足判定关键字（出现在错误正文中）
_INSUFFICIENT_BALANCE_KEYWORDS = (
    "余额不足",
    "充值",
    "欠费",
    "insufficient",
    "no balance",
    "out of balance",
    "1113",  # 智谱错误码：余额不足
    # ── 阿里云百炼：官方错误码表 help.aliyun.com/zh/model-studio/error-code ──
    # 400 Arrearage（响应原文）：{"type":"Arrearage","code":"Arrearage",
    #   "message":"Access denied, please make sure your account is in good standing.
    #    For details, see: …/error-code#overdue-payment"}
    "arrearage",
    "overdue",            # PrepaidBillOverdue / PostpaidBillOverdue
    "out_of_service",     # isv.OUT_OF_SERVICE：余额不足导致服务被暂停
    "freetieronly",       # AllocationQuota.FreeTierOnly：免费额度用尽，需认证并充值
    "budgetlimitexceeded",  # 预算管理额度用尽且开启「达预算即停」
    # ── 月之暗面 Moonshot：欠费/停用/额度不足**统一是 HTTP 429**，必须靠 error.type 区分 ──
    # `exceeded_current_quota_error`（账户欠费或已停用 / token 额度不足）
    # 同状态码的干扰项：`rate_limit_reached_error`（并发限流）、`engine_overloaded_error`
    # （节点过载，充值也不解决）——这两个**不含**下面这个词，所以不会误判。
    "exceeded_current_quota_error",
)

# **限流类关键字：必须先排除，再看欠费。**
# 阿里云百炼的限流码是 `429 Throttling.AllocationQuota`，而它同一页里另有
# `insufficient_quota` 字样 —— 只按 `insufficient` 匹配，会把「TPS/TPM 被限流」
# 误报成「账户欠费」，把用户往充值的错误方向引。命中限流一律不算欠费。
_THROTTLING_KEYWORDS = (
    "throttl",             # 百炼 Throttling.AllocationQuota
    "rate limit",
    "ratelimit",
    "too many requests",   # HTTP 429 的标准短语
)


def _anthropic_root(base_url: str) -> str:
    """Anthropic 协议的站点根：``/v1/messages`` 由本模块自己拼，Base URL 自带 ``/v1`` 要去掉。

    各插件默认值不统一——Anthropic 官方是 ``…/v1``，智谱 Anthropic 端点是
    ``…/api/anthropic``；不做归一就会拼出 ``…/v1/v1/messages``。
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


# ── 简单对话 ─────────────────────────────────────────────

def chat_completion(base_url: str, api_key: str, model: str, messages: list,
                    protocol: str = "openai", max_tokens: int = 512,
                    token_param: str = "max_tokens") -> dict:
    """发起一次简单对话。返回 {"ok": bool, "output": str} 或 {"ok": False, "error": str}。

    protocol:
      - "openai"     → POST {base}/chat/completions，Bearer 头
      - "anthropic"  → POST {base}/v1/messages，x-api-key 头
      - "gemini"     → POST {base}/models/{model}:generateContent?key=…（模型名在路径里）

    ``token_param``：**只对 openai 协议生效**，默认 `max_tokens`。阿里云百炼与 MiniMax 的
    官方文档都已把 `max_tokens` 标为 **deprecated**、要求改用 `max_completion_tokens`，
    由这两家插件显式传入。anthropic 协议**必须**用 `max_tokens`（官方就这么定义的），
    gemini 用的是 `maxOutputTokens`、走的是另一条分支，都不受这个参数影响。
    """
    if not model:
        return {"ok": False, "error": "未指定模型"}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key"}
    root = base_url.rstrip("/")
    messages = messages or []
    try:
        with make_client() as client:
            if protocol == "gemini":
                contents = [
                    {
                        "role": "model" if (m.get("role") == "assistant") else "user",
                        "parts": [{"text": str(m.get("content", ""))}],
                    }
                    for m in messages
                ]
                resp = client.post(
                    f"{root}/models/{model}:generateContent",
                    params={"key": api_key},
                    headers={"Content-Type": "application/json"},
                    json={"contents": contents},
                )
            elif protocol == "anthropic":
                resp = client.post(
                    _anthropic_root(base_url) + "/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "max_tokens": max_tokens, "messages": messages},
                )
            else:
                resp = client.post(
                    root + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, token_param: max_tokens},
                )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）"}

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict):
        # MiniMax 的 200 响应里带 `base_resp`（官方文档：成功时 `status_code == 0`）。
        # 它的业务错误**可以藏在 200 里**，只看 HTTP 状态码会漏判成成功——这里必须叠加判断。
        # （它的 4xx 又是**另一套**信封 `{"type":"error","error":{…}}`，数字码塞在 message 文本里。）
        base = payload.get("base_resp")
        if isinstance(base, dict) and base.get("status_code") not in (0, None):
            msg = str(base.get("status_msg") or "")
            if is_insufficient_balance(msg):      # 1008 余额不足
                return {"ok": False, "error": f"账户欠费或无可用额度（{msg}）"}
            return {"ok": False, "error": f"业务错误（{base.get('status_code')}）：{msg}"}
        if protocol == "gemini":
            candidates = payload.get("candidates")
            if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                parts = (candidates[0].get("content") or {}).get("parts")
                if isinstance(parts, list):
                    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                    if text.strip():
                        return {"ok": True, "output": text.strip()}
        elif protocol == "anthropic":
            # **必须遍历全部 block 拼 `text`，不能写死 `content[0].text`**：官方在 thinking
            # （扩展思考）模型下会把 `{"type":"thinking", …}`（**无 `text` 字段**）放在
            # `content[0]`，正文块排在其后；取 `[0]` 会得到空串、在严格语言里还会抛 KeyError。
            # 遍历 + `.get("text","")` 同时兼容两种排布（官方 SDK 自己也是这么容错取的）。
            blocks = payload.get("content")
            if isinstance(blocks, list):
                text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
                if text.strip():
                    return {"ok": True, "output": text.strip()}
        else:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    text = str(message.get("content") or "").strip()
                    if text:
                        return {"ok": True, "output": text}
                    # **信封是完整的**（`choices[0].message` 在），只是文本为空。
                    # 最典型的成因：我们做欠费探测时发的是 `max_tokens=1`，而推理型模型会把这
                    # 1 个 token 全用在思维链上（OpenAI 兼容的 `reasoning_content`），`content`
                    # 于是为空串。请求**已经到达模型并成功返回**，就说明账号可用——
                    # 探测要的是「能不能用」，不是那句文本，所以带 `reached_model` 让探测能认。
                    #
                    # 带上响应体摘要 **只为了可诊断**：华为 MaaS 的官方文档说它**不**共用额度
                    # （``max_tokens`` 只限正文、思维链另算），与实际返回「没有可读内容」在文档层面
                    # 是矛盾的——光看文档解释不了，得有原始响应体才能定性。摘要会经
                    # `_redact_result` / 操作出口净化，不外泄密钥。
                    body = body_snippet(resp)
                    detail = f": {body}" if body else ""
                    return {"ok": False, "error": f"响应中没有可读内容{detail}", "reached_model": True}
        return {"ok": False, "error": "响应中没有可读内容"}
    # **同一个状态码含义可能完全不同**：不能见到 400 就说「鉴权或请求错误」——
    # 阿里云百炼欠费就是 HTTP 400 + `{"type":"Arrearage","code":"Arrearage"}`，
    # 判成鉴权错误会把用户往「密钥写错了」的方向引。先读响应体，欠费就说欠费。
    snippet = body_snippet(resp)
    if is_insufficient_balance(snippet):
        return {"ok": False, "error": f"账户欠费或无可用额度 (HTTP {status})"}
    detail = f": {snippet}" if snippet else ""
    if status in (400, 401, 403):
        return {"ok": False, "error": f"鉴权或请求错误 (HTTP {status}){detail}"}
    return {"ok": False, "error": f"HTTP {status}{detail}"}


# ── 流式简单对话 ─────────────────────────────────────────

def _sse_payload(line: str):
    """把一行 SSE 解析成 dict；不是 `data:` 行 / 空 / `[DONE]` / 坏 JSON → None。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "ignore")
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _delta_of(obj: dict, protocol: str) -> str:
    """该帧里的**正文**增量（无则空串）。"""
    if protocol == "gemini":
        candidates = obj.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            parts = (candidates[0].get("content") or {}).get("parts")
            if isinstance(parts, list):
                return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return ""
    if protocol == "anthropic":
        if obj.get("type") == "content_block_delta":
            return str((obj.get("delta") or {}).get("text") or "")
        return ""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str((choices[0].get("delta") or {}).get("content") or "")
    return ""


def _reasoning_of(obj: dict, protocol: str) -> str:
    """该帧里的**思维链**增量（无则空串）。

    字段名各家不同——**按官方文档逐个核对过，别只认一个**：

    - 百炼 / 智谱 / 华为 MaaS / MiniMax：`delta.reasoning_content`
    - 商汤：自家 `sensenova-*` 系列是 `delta.reasoning`，而它网关里**转挂**的
      `deepseek-*` / `glm-*` 仍是 `reasoning_content` —— 同一个网关内两种字段名并存。
    - Anthropic：扩展思考走 `content_block_delta` 且 `delta.type == "thinking_delta"`，
      正文在 `delta.thinking`（不是 `text`）。
    """
    if protocol == "anthropic":
        delta = obj.get("delta") or {}
        if isinstance(delta, dict) and str(delta.get("type") or "") == "thinking_delta":
            return str(delta.get("thinking") or "")
        return ""
    choices = obj.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return ""
    return str(delta.get("reasoning_content") or delta.get("reasoning") or "")


def chat_completion_stream(base_url: str, api_key: str, model: str, messages: list,
                           protocol: str = "openai", max_tokens: int = 512,
                           on_delta=None, token_param: str = "max_tokens") -> dict:
    """流式对话：每收到一段文本即调用 `on_delta(text)`。

    返回 {"ok": True, "output": 全文} 或 {"ok": False, "error": ..., "output": 已收到的部分}。

    **纯思考输出**（模型只产出思维链、正文为空）时，把思维链当正文返回，不留空白气泡——
    字段名各家不同，见 `_reasoning_of`。`token_param` 语义同 `chat_completion`。
    """
    if not model:
        return {"ok": False, "error": "未指定模型", "output": ""}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key", "output": ""}

    root = base_url.rstrip("/")
    messages = messages or []
    parts: list = []
    reasons: list = []        # 思维链增量（纯思考输出时兜底显示，见收尾处）
    saw_frame = False         # 是否见到过数据帧（区分「模型答了但没文本」与「什么都没回」）
    emit = on_delta or (lambda _t: None)

    if protocol == "gemini":
        contents = [
            {
                "role": "model" if (m.get("role") == "assistant") else "user",
                "parts": [{"text": str(m.get("content", ""))}],
            }
            for m in messages
        ]
        request = dict(
            method="POST",
            url=f"{root}/models/{model}:streamGenerateContent",
            params={"key": api_key, "alt": "sse"},
            headers={"Content-Type": "application/json"},
            json={"contents": contents},
        )
    elif protocol == "anthropic":
        request = dict(
            method="POST",
            url=_anthropic_root(base_url) + "/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={"model": model, "max_tokens": max_tokens, "messages": messages, "stream": True},
        )
    else:
        request = dict(
            method="POST",
            url=root + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, token_param: max_tokens, "stream": True},
        )

    try:
        with make_client() as client:
            with client.stream(**request) as resp:
                if resp.status_code != 200:
                    resp.read()
                    snippet = body_snippet(resp)
                    # 与 chat_completion 同一套判定：先读响应体认欠费，再说是不是鉴权问题
                    if is_insufficient_balance(snippet):
                        return {"ok": False, "error": f"账户欠费或无可用额度 (HTTP {resp.status_code})",
                                "output": "".join(parts)}
                    detail = f": {snippet}" if snippet else ""
                    if resp.status_code in (400, 401, 403):
                        return {"ok": False, "error": f"鉴权或请求错误 (HTTP {resp.status_code}){detail}",
                                "output": "".join(parts)}
                    return {"ok": False, "error": f"HTTP {resp.status_code}{detail}",
                            "output": "".join(parts)}
                for line in resp.iter_lines():
                    obj = _sse_payload(line)
                    if obj is None:
                        continue
                    saw_frame = True      # 见到过数据帧 = 模型确实回了（供 reached_model 用）
                    text = _delta_of(obj, protocol)
                    if text:
                        parts.append(text)
                        emit(text)
                    think = _reasoning_of(obj, protocol)
                    if think:
                        reasons.append(think)
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）", "output": "".join(parts)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）", "output": "".join(parts)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）", "output": "".join(parts)}

    output = "".join(parts)
    if not output.strip():
        # **纯思考输出**：模型只产出了思维链、没有正文。成因见 `_reasoning_of` 与 §2.4：
        # 思考内容走 `reasoning_content`（商汤自家走 `reasoning`），且思考 token 先于正文产生；
        # `max_tokens` 偏小时会被思考吃光，正文就空了。此时把**思维链**显示出来，
        # 而不是留一个空白气泡（用户明确要求）。
        thinking = "".join(reasons).strip()
        if thinking:
            return {"ok": True, "output": thinking}
        # 一个数据帧都没见到 → 不是「模型答了但没文本」，是真的没回东西
        return {"ok": False, "error": "响应中没有可读内容", "output": "", "reached_model": saw_frame}
    return {"ok": True, "output": output.strip()}


# ── 模型列表（供「简单对话」选择模型）────────────────────
def _fetch_model_items(protocol: str, base_url: str, api_key: str) -> list:
    """请求模型列表并返回**原始条目**（**走网络、不带缓存**；失败/无权限返回 []）。

    与 `fetch_models` 的差别只是**保留全部字段**：只取 id 会把扩展字段丢掉，而欠费探测
    要用 OpenRouter 的 `architecture.output_modalities`（官方字段）排除非文本输出的模型
    —— 否则靠名字猜会挑到 `google/gemini-nano-banana-2.1` 这种图像生成模型。
    """
    if not api_key:
        return []
    root = base_url.rstrip("/")
    try:
        with make_client() as client:
            if protocol == "gemini":
                resp = client.get(f"{root}/models", params={"key": api_key})
            elif protocol == "anthropic":
                # 与 `probe_anthropic_models` 同一套归一化：裸域名也要打到 `/v1/models`，
                # 别在「验证」与「列模型」两处用不同的拼法（会一个通一个不通）。
                resp = client.get(
                    _anthropic_root(base_url) + "/v1/models",
                    params={"limit": 100},
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            else:
                resp = client.get(root + "/models", headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            return []
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 取列表失败即返回空
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("models") if protocol == "gemini" else payload.get("data")
    return [i for i in (raw or []) if isinstance(i, dict)]


def _model_items(protocol: str, base_url: str, api_key: str, force: bool = False) -> list:
    """带 **24h 缓存**的模型条目读取（`force=True` 绕过缓存，供用户手动刷新）。

    命中且未过期就直接返回、**不发请求**；否则取一次并回写（见文件上方缓存说明）。
    ``api_key`` 为空时与 `_fetch_model_items` 一致直接返回 []；**空结果不落盘**——那多半是
    临时故障/限流，固化成 24h 反而更糟。
    """
    if not api_key:
        return []
    key = _cache_key(protocol, base_url, api_key)
    now = time.time()
    if not force:
        entry = _cache_read().get(key)
        if isinstance(entry, dict) and now - float(entry.get("ts") or 0) < _MODEL_CACHE_TTL:
            items = entry.get("items")
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
    items = _fetch_model_items(protocol, base_url, api_key)
    if items:
        cache = _cache_read()
        cache[key] = {"ts": now, "items": items}
        _cache_write(cache)
    return items


def _name_of(protocol: str, item: dict) -> str:
    """从一条模型条目里取模型名（gemini 的 `models/` 前缀要去掉）。"""
    raw = str(item.get("name") if protocol == "gemini" else item.get("id") or "")
    if protocol == "gemini" and raw.startswith("models/"):
        raw = raw[len("models/"):]
    return raw


def fetch_models(protocol: str, base_url: str, api_key: str, force: bool = False) -> list:
    """获取该厂商可用模型列表（best-effort，失败/无权限返回 []）。

    ``force=True`` 绕过 24h 缓存并回写——**用户的「刷新模型列表」按钮走这条**，
    所以它同时刷新的还有欠费探测用的候选模型（两者共用同一份缓存）。

    protocol:
      - "openai" / "openrouter" → GET {base}/models（Bearer）→ data[].id
      - "anthropic"             → GET {base}/v1/models（x-api-key）→ data[].id
      - "gemini"                → GET {base}/models?key=… → models[].name（去掉 "models/" 前缀）
    """
    names = {_name_of(protocol, item) for item in _model_items(protocol, base_url, api_key, force)}
    return sorted(n for n in names if n)


def model_items(protocol: str, base_url: str, api_key: str, force: bool = False) -> list:
    """模型清单的**原始条目**（带 24h 缓存；公开封装）。

    供**需要读扩展字段**的自定义 `list_models` 用——例如方舟要按 `status` 剔除已关停模型。
    只想拿模型名就用 `fetch_models`。``force=True`` 穿透缓存（用户手动刷新走这条）。
    """
    return _model_items(protocol, base_url, api_key, force)


def probe_model_candidates(base_url: str, api_key: str, protocol: str = "openai",
                           force: bool = False) -> list:
    """探测/欠费校验用的**候选模型名**（公开封装，供像 nvidia 这类自己遍历候选的插件复用）。

    返回按「像便宜对话模型」排序的候选（可能为空）；结果按 24h 缓存，
    ``force=True`` 穿透缓存（用户手动刷新走这条）。
    """
    return _probe_model_candidates(base_url, api_key, protocol, force)


def resolve_chat_model(requested: str, list_models) -> str:
    """「简单对话」的模型解析：**不写死任何模型名**。

    ① 调用方给了 model → 用它；
    ② 否则取**运行时**模型清单首项（``list_models`` 传可调用对象，延迟求值，避免无谓联网）；
    ③ 仍拿不到 → 返回空串，由调用方报错让用户手选（**绝不退回任何内置名**）。
    """
    name = str(requested or "").strip()
    if name:
        return name
    try:
        models = list_models() or []
    except Exception:  # noqa: BLE001 - 取列表失败按「没拿到」处理
        models = []
    for m in models:
        text = str(m).strip()
        if text:
            return text
    return ""


# ── 欠费探测（可选项，由应用设置开关控制）───────────────

# 便宜模型的命名线索（命中越靠前越优先）——「默认智能获取测试模型」的挑选依据
_PROBE_MODEL_HINTS = (
    "flash", "lite", "mini", "nano", "small", "haiku", "air", "turbo", "free", "8b", "7b",
)
# 不能用于对话的模型（嵌入 / 重排 / 语音 / 图像生成等），探测时排除。
# `banana` 是 Google 的 nano-banana 图像生成系列——它的名字里带 `nano`，会被「便宜款」关键词
# 误判成小型对话模型而排到候选最前面（输出里也确实含 text，光看 modality 拦不住）。
_PROBE_MODEL_EXCLUDE = (
    "embed", "rerank", "tts", "whisper", "moderation", "audio", "video", "image",
    "dall", "flux", "stable-diffusion", "sd3", "veo", "sora", "banana",
)
# 一次探测最多尝试几个候选模型（模型不可用时换下一个）。
# **别设太小**：OpenRouter 这类聚合站的模型表有几百个，靠名字猜出来的前几个未必真能对话
# （其清单头部常是 `google/gemini-nano-banana-2.1` 这类图像生成模型，以及各种冷门/受限条目）。
# 只试 3 个就放弃，会让「余额未知时发一次对话探测」这个开关**长期静默失效**——
# 用户勾了、验证也过了，界面上却什么都不显示，而且没有任何线索说明为什么。
# 每次尝试只是一发 max_tokens=1 的请求，放宽到 10 个的代价可以接受。
_PROBE_MAX_CANDIDATES = 10

# 厂商回这句 = 该模型**仅支持流式**（阿里云百炼的开源思考模型会这样回），
# 欠费探测遇到它要改用流式再试一次，否则这批模型的探测永远是「异常」。
_STREAM_ONLY_HINT = "only support stream"

# ── 模型清单缓存（默认 24h）──────────────────────────────
# **为什么要缓存**：`/models` 会在多条路径上被反复拉取——每次欠费探测、每次「简单对话」取默认
# 模型、每次打开模型下拉。同一把 key 的模型清单一天内几乎不变，反复请求纯属浪费，还可能撞限流。
# **为什么 24h**：够短——新模型上线一天内就会自动出现；够长——能把请求量压下来。
# **怎么绕过**：用户点「刷新模型列表」走 `list_models(..., force=True)`，穿透缓存并回写，
# 所以「手动刷新」永远拿到最新，无需另设清除入口。
# 位置放 `plugins/` 下（与 `.state.json` 同级，同样「临时文件 + 原子替换」写）；打包时会像
# `.state.json*` 一样排除，不进发布产物。**这份缓存只存模型名/条目，绝不存密钥明文**——
# 键里用的是密钥的 sha256 前 10 位（不可逆）。
_MODEL_CACHE_FILE = Path(__file__).resolve().parents[1] / ".model_cache.json"
_MODEL_CACHE_TTL = 24 * 3600


def _cache_key(protocol: str, base_url: str, api_key: str) -> str:
    """缓存键：协议 + Base URL + **密钥指纹**。

    带指纹是因为**不同账号在同一地址下能看到的模型可能不同**（最典型是账号级目录），
    只用 protocol+base_url 会把 A 账号的清单错给 B 账号。指纹是 sha256 前 10 位，非密钥本身。
    """
    fp = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:10]
    return f"{protocol}|{base_url.rstrip('/')}|{fp}"


def _cache_read() -> dict:
    try:
        with open(_MODEL_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 读不动就当没有缓存
        return {}


def _cache_write(payload: dict) -> None:
    """原子写：先写同目录临时文件再 replace，避免并发下读到半截 JSON（同 `.state.json`）。"""
    try:
        _MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(_MODEL_CACHE_FILE) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(_MODEL_CACHE_FILE)
    except Exception:  # noqa: BLE001 - 缓存写失败不影响主流程
        pass


def _probe_model_candidates(base_url: str, api_key: str, protocol: str, force: bool = False) -> list:
    """挑探测用的候选模型。

    先按厂商声明的输出模态排除**非文本输出**的模型（OpenRouter 的
    `architecture.output_modalities` 是官方字段；对纯图像/纯音频输出的模型有效）。

    > **但这挡不住 OpenRouter 那类问题**：其 465 个模型**全部**含 `text` 输出——包括
    > `google/gemini-nano-banana-2.1`（`text+image->text+image`，是图像生成却也能出文本）。
    > 真正把那一个挡掉的是 `_PROBE_MODEL_EXCLUDE` 里的 `banana`。
    > 也就是说「名字像便宜聊天模型、其实是生成模型」只能靠名字黑名单，modality 分不出来。

    字段不存在时（多数厂商只回 `data[].id`）退回老办法：按名字选「便宜款」，再兜底其它模型。
    """
    items = _model_items(protocol, base_url, api_key, force)
    # **剔掉已关停的**：方舟 `/models` 是过期超集（其中 83/135 条为 `status=="Shutdown"`，
    # 且按创建时间排序、**首项通常是关停款**）。不剔除的话，「按名字像 lite/flash 挑」会优先挑到
    # `doubao-lite-*` 这类早已关停的款，探测多次全部 404。
    items = [i for i in items if str(i.get("status") or "").strip().lower() != "shutdown"]
    if items and any(isinstance(i.get("architecture"), dict) for i in items):
        pool = [i for i in items
                if "text" in ((i.get("architecture") or {}).get("output_modalities") or [])]
    else:
        pool = items
    models = [_name_of(protocol, i) for i in pool]
    usable = [m for m in models if m and not any(x in m.lower() for x in _PROBE_MODEL_EXCLUDE)]
    hinted = [m for m in usable if any(h in m.lower() for h in _PROBE_MODEL_HINTS)]
    return hinted + [m for m in usable if m not in hinted]


def quota_chat_probe(base_url: str, api_key: str, protocol: str = "openai",
                     preferred_model: str = "", token_param: str = "max_tokens",
                     force: bool = False) -> Optional[dict]:
    """发一次最小对话请求，判断账户是否真的可用（欠费探测）。

    验证通过后，如果凭据开启了 ``probe_chat``，用 ``max_tokens=1`` 的一发请求确认
    账户**实际可用**：``/v1/models`` 与余额接口都正常、但对话渠道失效（上游 Key 失效、
    无可用渠道）的情况并不少见，只有实测才能发现。

    返回 ``None``（无法判定，例如超时/限流）或：

    - ``{"account_info": {"chat_probe": "可用"}}``：实测通过；
    - ``{"account_info": {"chat_probe": "欠费"}, "message": "..."}``：实测发现账户欠费；
    - ``{"status": "invalid", ...}``：实测对话接口返回 401/403 鉴权失败；
    - ``{"status": "error", ...}``：实测对话接口明确不可用（如 503 无可用渠道）。

    **``None`` 的语义保持「无法判定」不变**——`nvidia` 这类自己遍历候选、靠真值判断
    「这个模型有没有结论」的调用方依赖它（若失败也返回 dict，它们会在第一个候选失败时
    就收工）。把「异常」这一行摆到界面上是 ``make_quota_probe`` 的职责：
    它知道用户开了开关，``None`` 就说明「试过但没成」。

    具体失败原因（哪个模型、报了什么）写进日志，不塞进界面上那一行。

    模型选择：``preferred_model`` 优先（用户在设置里指定），否则自动挑一个便宜的；
    候选模型不可用时换下一个，全试完仍无结论则返回 ``None``（不误报欠费）。
    """
    if not base_url or not api_key:
        return None
    candidates = [preferred_model] if preferred_model else _probe_model_candidates(
        base_url, api_key, protocol, force
    )
    candidates = [c for c in candidates if c][:_PROBE_MAX_CANDIDATES]
    if not candidates:
        log(f"欠费探测：拿不到可用的模型列表（{base_url}），无法实测")
        return None

    tried: list[str] = []
    auth_errors: list[str] = []
    unavailable_errors: list[str] = []
    for model in candidates:
        resp = chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "hi"}],
            protocol=protocol, max_tokens=1, token_param=token_param,
        )
        # `reached_model`：HTTP 200 且信封完整、只是文本为空（推理型模型吃掉了那 1 个 token）。
        # 请求已经到过计费环节并成功返回 —— 对「账户能不能用」这个问题就是确证。
        if resp.get("ok") or resp.get("reached_model"):
            return {"account_info": {"chat_probe": "可用"}}
        error = str(resp.get("error") or "")
        # 402 / 欠费关键字 = 账户没钱（模型本身是能用的，说明请求已到计费环节）
        if "402" in error or is_insufficient_balance(error):
            return {
                "account_info": {"chat_probe": "欠费"},
                "message": "凭证有效，但账户欠费或无可用额度（已用最小对话实测）",
            }
        # **仅支持流式的模型**：百炼有一批开源思考模型用非流式调用会直接回
        # `parameter.enable_thinking only support stream call`。遇到它就用流式再试一次，
        # 否则这批模型的欠费探测永远是「异常」——而它们其实好得很。
        if _STREAM_ONLY_HINT in error.lower():
            streamed = chat_completion_stream(
                base_url, api_key, model,
                [{"role": "user", "content": "hi"}],
                protocol=protocol, max_tokens=1, token_param=token_param,
            )
            if streamed.get("ok") or streamed.get("reached_model"):
                return {"account_info": {"chat_probe": "可用"}}
            error = f"{error}；改走流式后：{streamed.get('error')}"
        # 其它错误（模型不存在 / 鉴权 / 限流）不构成欠费结论，换下一个候选；
        # 同时记下明确的鉴权失败与「接口不可用」，全部候选失败后用于定性。
        entry = f"{model}（{error[:80]}）"
        tried.append(entry)
        lowered = error.lower()
        if "http 429" in lowered:
            continue  # 限流只说明此刻繁忙，不能据此判凭据不可用
        if ("http 401" in lowered or "http 403" in lowered
                or "authentication" in lowered or "invalid api key" in lowered
                or "unauthorized" in lowered):
            auth_errors.append(entry)
        elif "http " in lowered or "业务错误" in error:
            unavailable_errors.append(entry)

    log("欠费探测：试了 %d 个模型都没能确认账户可用 —— %s"
        % (len(tried), "；".join(tried[:5]) + ("…" if len(tried) > 5 else "")))
    if auth_errors:
        return {
            "status": "invalid",
            "account_info": {"chat_probe": "鉴权失败"},
            "message": f"对话接口实测鉴权失败（已试 {len(tried)} 个模型）：{auth_errors[0]}",
        }
    if unavailable_errors:
        return {
            "status": "error",
            "account_info": {"chat_probe": "不可用"},
            "message": f"验证通过但对话接口实测不可用（已试 {len(tried)} 个模型）：{unavailable_errors[0]}",
        }
    return None


# ── 校验探针 ─────────────────────────────────────────────

# 上游偶发的 5xx：智谱 Anthropic 端点在免费模型上约 1/3 概率返回
# 500 Internal Network Failure，同一请求重试即成功——不重试会把好 key 误判为失败。
# 只重试 5xx：429（限流）与 4xx 重试没有意义，反而加重对端负担。
_TRANSIENT_STATUS = (500, 502, 503, 504)
_PROBE_ATTEMPTS = 3


def _request_with_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """发一次请求；上游返回 5xx 时退避重试，最多 ``_PROBE_ATTEMPTS`` 次。"""
    resp = client.request(method, url, **kwargs)
    for attempt in range(1, _PROBE_ATTEMPTS):
        if resp.status_code not in _TRANSIENT_STATUS:
            return resp
        time.sleep(0.4 * attempt)
        resp = client.request(method, url, **kwargs)
    return resp


def probe_openai_models(base_url: str, api_key: str) -> VerifyResult:
    """OpenAI 协议：GET {base}/models，Bearer 认证。"""
    url = base_url.rstrip("/") + "/models"
    with make_client() as client:
        resp = _request_with_retry(client, "GET", url,
                                   headers={"Authorization": f"Bearer {api_key}"})
    return _interpret_simple(resp)


def probe_anthropic_models(base_url: str, api_key: str, verify_path: str = "/models") -> VerifyResult:
    """Anthropic 协议：GET {base}/v1{verify_path}，x-api-key 认证。

    ``base_url`` 先经 `_anthropic_root()` 归一化、再补 ``/v1``——与 chat 的 ``/v1/messages``
    同一套规则。**归一化只用在这两处、必须一起用**：否则用户填裸域名（``https://x``，大量
    Claude 中转站的接入文档给的就是裸域名）时，chat 会打到 ``/v1/messages``（对）、
    而本探针会打到 ``/models``（错，应为 ``/v1/models``）→ 出现「验证失败但简单对话却能用」的矛盾。

    ``verify_path`` **不要**自带 ``/v1`` 前缀（默认 ``/models``）；对 Anthropic 官方
    （默认 base 以 ``/v1`` 结尾）归一化后结果与旧实现完全一致，无回归。
    """
    url = _anthropic_root(base_url) + "/v1" + verify_path
    with make_client() as client:
        resp = _request_with_retry(
            client, "GET", url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    return _interpret_simple(resp)


def probe_openai_chat(base_url: str, api_key: str, model: str,
                      timeout: Optional[float] = None,
                      token_param: str = "max_tokens") -> VerifyResult:
    """OpenAI 协议：POST {base}/chat/completions（消耗极小额度，可识别欠费）。

    ``timeout`` 为空时用全局超时；显式传入则单次请求用它（用于「连试多个候选
    模型、每个只给短超时」的验证策略）。

    ``token_param`` 语义同 `chat_completion`：默认 ``max_tokens``；对官方已弃用旧名、
    且与 o 系列**不兼容**的厂商（如 OpenAI 本家）由调用方传 ``max_completion_tokens``，
    否则这唯一一条探测路径仍会带旧名、被 o 系列直接 400。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], token_param: 1}
    with make_client(request_timeout=timeout) as client:
        resp = _request_with_retry(
            client, "POST", url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
    return _interpret_chat(resp)


# ── 响应判定 ─────────────────────────────────────────────

def _interpret_simple(resp: httpx.Response) -> VerifyResult:
    """简单 GET 探针（models 类）的通用判定。"""
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=_extract_account_info(payload))
    if status in (401, 403):
        # **403 不一定是密钥的错**——一律判「凭证无效」是**假红**，比漏报更坑人。已核实的两个：
        #   方舟   ：403 `AccountOverdueError` / `OperationDenied.ServiceOverdue`（账户欠费）
        #   百炼   ：403 `AllocationQuota.FreeTierOnly`（免费额度用尽，需认证并充值）
        # 先读响应体，认得出欠费就报「有效 + 账户欠费」，认不出才当无效。
        if is_insufficient_balance(body_snippet(resp)):
            return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度")
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, body_snippet(resp))


# 「密钥没问题、是**请求本身**被拒」的业务码：命中就报「有效 + 具体原因」，
# 而不是含糊的「无法确认凭证状态」。**按业务码判，不按文案**——智谱官方错误码表
# （docs.bigmodel.cn/cn/api/api-code）里 `1113` 写的是「您的账户已欠费，请充值后重试」，
# 线上实际回的是「余额不足或无可用资源包,请充值。」，同一个码两套文案。
#   1211 模型不存在 / 1221 API 已下线 / 1222 API 不存在 / 1261 Prompt 超长 /
#   1301 内容被安全审核拦截
_REQUEST_LEVEL_CODES = frozenset({"1211", "1221", "1222", "1261", "1301"})


def _error_code(payload) -> str:
    """取错误响应体里的业务码（`{"error":{"code": …}}`）；取不到返回空串。"""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict) and err.get("code") is not None:
        return str(err["code"]).strip()
    return ""


def _interpret_chat(resp: httpx.Response) -> VerifyResult:
    """chat 探针判定：额外识别 429 余额不足（欠费）与速率限制。"""
    status = resp.status_code
    payload = safe_json(resp)
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=_extract_account_info(payload))
    # 400 不一定是「密钥有问题」。智谱那批码说明**密钥有效、是请求本身被拒**
    # （模型名写错 / 接口下线 / prompt 超长 / 内容被审核），报成「无法确认凭证状态」
    # 会把用户往「是不是 key 填错了」的方向引 —— 那是**假异常**。
    code = _error_code(payload)
    if status == 400 and code in _REQUEST_LEVEL_CODES:
        return VerifyResult(status="valid", message=f"凭证有效，但请求被拒（{code}）：{snippet}")
    if status in (401, 403):
        # **403 不一定是密钥的错**——一律判「凭证无效」是**假红**，比漏报更坑人。已核实的两个：
        #   方舟   ：403 `AccountOverdueError` / `OperationDenied.ServiceOverdue`（账户欠费）
        #   百炼   ：403 `AllocationQuota.FreeTierOnly`（免费额度用尽，需认证并充值）
        # 先读响应体，认得出欠费就报「有效 + 账户欠费」，认不出才当无效。
        if is_insufficient_balance(body_snippet(resp)):
            return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度")
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        if is_insufficient_balance(snippet):
            # 欠费不算失效：密钥本身有效，仅账户余额不足
            return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用资源包")
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    if status == 402:
        # 402 Payment Required：欠费不算失效
        return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, snippet)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def is_insufficient_balance(text: str) -> bool:
    """文本里是否表示「欠费 / 余额不足 / 无可用额度」。

    各协议通用；子插件自己实现 ``quota_probe`` 时直接复用这个判定（另可按需叠加厂商
    专属错误码，如百度文心的 17/18/19）。

    **先排除限流**：同一个词在不同厂商的含义可能相反——百炼的 ``insufficient_quota``
    出现在 ``Throttling.AllocationQuota``（**限流**）里，而 OpenAI 的 ``insufficient_quota``
    是**欠费**。命中限流关键字就直接返回 False，否则会把「被限流」误报成「欠费」。

    **只排除英文限流词，中文侧刻意不加泛词**：智谱的 ``1316/1317`` 原文是
    「主账号**余额不足**，无法使用超额按量付费。您的**限额**将在…重置」——它**同时**含
    「余额不足」和「限额/使用上限」，若把 `限额`、`使用上限` 当限流词先排除，就会把这条
    真·余额问题误判成限流。而智谱其余那些纯额度码（``1308 已达到 N 使用上限``、
    ``1318–1321 消费上限``）本来就不含任何欠费词，返回 False 是对的，不需要额外规则。
    """
    lowered = (text or "").lower()
    if any(kw in lowered for kw in _THROTTLING_KEYWORDS):
        return False
    return any(kw in lowered for kw in _INSUFFICIENT_BALANCE_KEYWORDS)


def _extract_account_info(payload: Any) -> Optional[dict]:
    """从响应中提取账号信息（organization / org_id）。

    LLM 的「附加信息」只展示余额 / 使用率；余额由各厂商 plugin.py 自行查询后合并。
    """
    if not isinstance(payload, dict):
        return None
    info: dict[str, Any] = {}
    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]
    return info or None
