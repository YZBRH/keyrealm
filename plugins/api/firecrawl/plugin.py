"""Firecrawl API Key 验证插件（plugins/api/ 类别下的独立子插件）。

自带完整的校验与展示逻辑，只复用框架层 backend.plugin_sdk 的通用工具；
复制本目录即可派生出新的服务插件。

- **account_info**：验证通过后 best-effort 查额度（`GET /v1/team/credit-usage`；v1 字段是
  snake_case，拿不到再退 v2 的 camelCase），展示**剩余额度**（单位 credits）；有 `plan_credits`
  时一并给出使用率。查不到就不显示——不猜。
- **actions**：`scrape`（form）抓取网页并输出 markdown。

参考文档：https://docs.firecrawl.dev/api-reference
"""
from __future__ import annotations

import logging

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json

_log = logging.getLogger("plugin")

NAME = "Firecrawl"
DEFAULT_BASE_URL = "https://api.firecrawl.dev"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _fetch_credit_usage(base_url: str, api_key: str) -> dict | None:
    """查剩余额度（best-effort，失败返回 None）。

    Firecrawl 两代 API 的路径与字段名不同：v1 是 ``remaining_credits`` / ``plan_credits``
    （snake_case），v2 是 ``remainingCredits`` / ``planCredits``（camelCase）。先试 v1，
    再退 v2；都拿不到就返回 None——**不猜、不编**。
    """
    base = base_url.rstrip("/")
    for path in ("/v1/team/credit-usage", "/v2/team/credit-usage"):
        try:
            with make_client() as client:
                resp = client.get(
                    base + path,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except Exception as exc:  # noqa: BLE001 - 余额只是附加信息，失败不影响验证结论
            _log.debug("Firecrawl 额度查询异常 %s: %s", path, type(exc).__name__)
            return None
        if resp.status_code != 200:
            _log.debug("Firecrawl 额度查询 %s → HTTP %s", path, resp.status_code)
            continue
        payload = safe_json(resp) or {}
        # 文档里 200 响应带 success=true；这里不强制该字段，只要 data 里能取到额度就算数
        if not isinstance(payload, dict) or payload.get("success") is False:
            _log.debug("Firecrawl 额度查询 %s → 响应异常: %s", path, body_snippet(resp))
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        remaining = data.get("remaining_credits", data.get("remainingCredits"))
        plan = data.get("plan_credits", data.get("planCredits"))
        try:
            remaining_f = float(remaining)
        except (TypeError, ValueError):
            continue
        if remaining_f < 0:
            continue
        info: dict = {"balance": remaining_f, "currency": "credits"}
        try:
            plan_f = float(plan)
        except (TypeError, ValueError):
            plan_f = 0.0
        if plan_f > 0:
            used = max(0.0, plan_f - remaining_f)
            info["usage_rate"] = round(min(1.0, used / plan_f), 6)
        return info
    return None


def _verify_firecrawl(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/scrape"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": "https://example.com"},
        )
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        if isinstance(payload, dict) and payload.get("success"):
            return VerifyResult(
                status="valid",
                message="凭证有效",
                account_info=_fetch_credit_usage(base_url, api_key),
            )
        return VerifyResult(status="error", message=f"响应异常: {body_snippet(resp)}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 402:
        return VerifyResult(status="invalid", message="账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if not DEFAULT_BASE_URL and not base_url:
        errors.append(f"服务「{NAME}」需要填写 Base URL")

    return errors


@guarded
def verify(data):
    """验证 Firecrawl API Key 是否有效。"""
    data = data or {}

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"服务「{NAME}」需要填写 Base URL")

    return _verify_firecrawl(base_url, api_key)


# ── 可选操作：抓取网页 ──────────────────────────────────

def actions(data):
    """声明可用操作（凭据含 api_key 时提供网页抓取）。"""
    data = data or {}
    if not (data.get("api_key") or "").strip():
        return []
    return [{
        "id": "scrape",
        "name": "抓取网页",
        "kind": "form",
        "submit": "抓取",
        "fields": [
            {"key": "url", "label": "网页 URL", "type": "text",
             "placeholder": "https://example.com", "required": True},
        ],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：调用 Firecrawl 抓取网页并输出 markdown（只读，不回显 API Key）。"""
    data = data or {}
    params = params or {}
    try:
        if action_id != "scrape":
            return {"ok": False, "error": "未知操作"}

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "缺少 API Key"}
        target = (params.get("url") or "").strip()
        if not target:
            return {"ok": False, "error": "请填写网页 URL"}
        base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL

        with make_client() as client:
            resp = client.post(
                base_url.rstrip("/") + "/v1/scrape",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"url": target, "formats": ["markdown"]},
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"抓取失败（HTTP {resp.status_code}）"}

        payload = safe_json(resp) or {}
        if not payload.get("success"):
            return {"ok": False, "error": "抓取失败：接口返回 success=false"}
        info = payload.get("data")
        info = info if isinstance(info, dict) else {}
        markdown = str(info.get("markdown", "")).strip()
        if not markdown:
            return {"ok": False, "error": "未取到 markdown 内容"}
        lines = [f"{target} 抓取成功，markdown 前 1500 字：", "", markdown[:1500]]
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
