"""匿名模式：要求所有凭据验证 / 操作请求都必须经过代理。

设置键 ``anonymous_mode`` 默认关闭。开启后，只要某条凭据的最终出口是直连
（显式直连、或全局 / 环境里没有任何代理），``plugin_manager`` 与 SSH 控制台 / 文件会话
就会在发起网络请求前拒绝执行。
"""
from __future__ import annotations

from . import database as db
from . import proxy_store

SETTING_KEY = "anonymous_mode"


def enabled() -> bool:
    return (db.get_setting(SETTING_KEY, "0") or "").strip() == "1"


def blocked_reason(data: dict, credential_type: str = "") -> str:
    """匿名模式开启且该凭据最终会直连时，返回拒绝原因；否则返回空串。"""
    if not enabled():
        return ""
    resolved = proxy_store.resolve_credential_proxy(data or {}, credential_type)
    if proxy_store.effective_url(resolved):
        return ""
    return "匿名模式已开启：该凭据未配置可用代理，已拒绝直连请求"


def get_status() -> dict:
    return {"enabled": enabled()}


def set_enabled(value) -> dict:
    on = bool(value)
    db.set_setting(SETTING_KEY, "1" if on else "0")
    return {"ok": True, "enabled": on}
