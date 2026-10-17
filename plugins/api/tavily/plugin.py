"""Tavily API Key 验证插件（plugins/api/ 类别下的独立子插件）。

自带完整的校验与展示逻辑，只复用框架层 backend.plugin_sdk 的通用工具；
复制本目录即可派生出新的服务插件。

- **account_info**：验证通过后 best-effort 查用量（`GET /usage`），展示套餐与使用率
  （`account.plan_usage / plan_limit`）。查不到就不显示——不猜。

参考文档：https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "Tavily"
DEFAULT_BASE_URL = "https://api.tavily.com"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _fetch_usage(base_url: str, api_key: str) -> dict | None:
    """查用量（best-effort，失败返回 None）。

    响应里 ``key`` 是当前 Key 的用量、``account`` 是账号维度；这里用 ``account`` 的
    ``plan_usage / plan_limit`` 算使用率，并带上 ``current_plan``（套餐）。
    """
    try:
        with make_client() as client:
            resp = client.get(
                base_url.rstrip("/") + "/usage",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            return None
        payload = safe_json(resp) or {}
        if not isinstance(payload, dict):
            return None
        acct = payload.get("account")
        acct = acct if isinstance(acct, dict) else {}
        info: dict = {}
        plan = str(acct.get("current_plan") or "").strip()
        if plan:
            info["plan"] = plan
        try:
            used = float(acct.get("plan_usage"))
            limit = float(acct.get("plan_limit"))
        except (TypeError, ValueError):
            used = limit = 0.0
        if limit > 0:
            info["usage_rate"] = round(min(1.0, max(0.0, used / limit)), 6)
        return info or None
    except Exception:  # noqa: BLE001 - 用量只是附加信息，失败不影响验证结论
        return None


def _verify_tavily(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/search"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": "hello", "max_results": 1},
        )
    status = resp.status_code
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_fetch_usage(base_url, api_key),
        )
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
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
    """验证 Tavily API Key 是否有效。"""
    data = data or {}

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"服务「{NAME}」需要填写 Base URL")

    return _verify_tavily(base_url, api_key)


# ── 可选操作：执行搜索 ──────────────────────────────────

def _as_count(value, default: int = 5, low: int = 1, high: int = 20) -> int:
    """条数兼容 int / 数字字符串；非法或越界时回落默认值。"""
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(count, high))


def actions(data):
    """声明可用操作（凭据含 api_key 时提供搜索）。"""
    data = data or {}
    if not (data.get("api_key") or "").strip():
        return []
    return [{
        "id": "search",
        "name": "执行搜索",
        "kind": "form",
        "submit": "搜索",
        "fields": [
            {"key": "query", "label": "搜索词", "type": "text",
             "placeholder": "例如：latest AI news", "required": True},
            {"key": "max_results", "label": "结果条数", "type": "text", "default": "5"},
        ],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：调用 Tavily 搜索接口并列出来源（只读，不回显 API Key）。"""
    data = data or {}
    params = params or {}
    try:
        if action_id != "search":
            return {"ok": False, "error": "未知操作"}

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "缺少 API Key"}
        query = (params.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "请填写搜索词"}
        base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
        max_results = _as_count(params.get("max_results"), 5, 1, 20)

        with make_client() as client:
            resp = client.post(
                base_url.rstrip("/") + "/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "max_results": max_results},
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"搜索失败（HTTP {resp.status_code}）"}

        payload = safe_json(resp) or {}
        results = payload.get("results")
        results = results if isinstance(results, list) else []
        lines = [f"「{query}」共返回 {len(results)} 条结果：", ""]
        for index, item in enumerate(results, 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip() or "(无标题)"
            url = str(item.get("url", "")).strip()
            content = " ".join(str(item.get("content", "")).split())[:200]
            lines.append(f"{index}. {title}")
            if url:
                lines.append(f"   {url}")
            if content:
                lines.append(f"   {content}")
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
