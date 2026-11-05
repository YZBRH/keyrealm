"""MCP 工具调用审计：记录函数名、操作描述、脱敏参数与结果。

只落库**已脱敏**的参数；错误信息也经过同一套替换，避免把凭据值带进审计日志。
"""
from __future__ import annotations

import functools
import inspect
import json
import time
from typing import Any, Callable

from . import database as db
from . import logger
from . import plugin_manager
from .redact import MIN_SECRET_LEN, redact_text, secrets_of

_log = logger.get_logger(__name__)

# 工具名 → 中文操作描述（审计日志的「执行了什么操作」列）
_OPERATIONS = {
    "vault_list_credentials": "查询凭据列表",
    "vault_get_credential": "查看凭据详情",
    "vault_create_credential": "创建凭据",
    "vault_update_credential": "修改凭据",
    "vault_delete_credential": "删除凭据",
    "vault_verify_credential": "验证单条凭据",
    "vault_batch_verify": "批量验证",
    "vault_list_groups": "查询分组列表",
    "vault_create_group": "创建分组",
    "vault_delete_group": "删除分组",
    "vault_list_plugins": "查询插件列表",
    "vault_get_secret": "读取敏感字段",
    "vault_export_backup": "导出备份",
    "vault_import_backup": "导入备份",
    "vault_list_proxies": "查询代理列表",
    "vault_create_proxy": "新增代理",
    "vault_update_proxy": "修改代理",
    "vault_delete_proxy": "删除代理",
    "vault_set_global_proxy": "设置全局代理",
    "vault_test_proxy": "测试代理连通性",
}

# 参数键名命中这些词就整段替换为 ***（不依赖插件字段表，覆盖任意工具的自由参数）
_SENSITIVE_TOKENS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
    "access_key", "secret_key", "private_key", "passphrase", "corpsecret",
    "webhook_key", "authorization", "cookie", "session_string",
)

_PARAMS_LIMIT = 6000
_MESSAGE_LIMIT = 500


def _sensitive_key(key: Any) -> bool:
    text = str(key or "").strip().lower().replace("-", "_")
    return any(token in text for token in _SENSITIVE_TOKENS)


def _collect_secrets(value: Any, out: list[str]) -> None:
    """收集递归结构里可直接用于文本替换的敏感值。

    除了键名命中敏感词的字段，还利用插件字段表：``{"credential_type": ..., "data": {...}}``
    形式的参数能拿到该插件声明的全部敏感字段值，避免插件自定义键名漏网。
    """
    if isinstance(value, dict):
        ctype = str(value.get("credential_type") or "").strip()
        data = value.get("data")
        if ctype and isinstance(data, dict):
            record = db.get_plugin_by_type(ctype) or {}
            out.extend(secrets_of(record.get("fields_schema"), data, ctype))
        for key, item in value.items():
            if _sensitive_key(key) and item not in (None, ""):
                text = str(item)
                if len(text) >= MIN_SECRET_LEN:
                    out.append(text)
            _collect_secrets(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_secrets(item, out)


def _redact(value: Any, secrets: list[str], key: Any = None) -> Any:
    if _sensitive_key(key):
        return "***"
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {str(k): _redact(v, secrets, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def _result_status(result: Any, error: BaseException | None) -> tuple[str, str]:
    if error is not None:
        return "error", type(error).__name__
    if isinstance(result, dict):
        if result.get("error"):
            return "error", str(result.get("error"))
        if result.get("ok") is False:
            return "error", str(result.get("message") or "操作失败")
    return "ok", ""


def record(tool: str, params: dict, result: Any = None,
           error: BaseException | None = None, duration_ms: int = 0) -> None:
    """写一条审计记录；任何失败都只记日志，绝不影响工具本身。"""
    try:
        secrets: list[str] = []
        _collect_secrets(params, secrets)
        safe_params = _redact(params, secrets)
        status, message = _result_status(result, error)
        message = redact_text(message, secrets) or ""
        if len(message) > _MESSAGE_LIMIT:
            message = message[:_MESSAGE_LIMIT - 1] + "…"
        text = json.dumps(safe_params, ensure_ascii=False, default=str, sort_keys=True)
        if len(text) > _PARAMS_LIMIT:
            text = text[:_PARAMS_LIMIT - 1] + "…}"
        db.add_mcp_operation_log(
            tool=tool,
            operation=_OPERATIONS.get(tool, tool),
            params=text,
            status=status,
            message=message,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不能影响 MCP 工具
        _log.warning("写 MCP 操作日志失败：%s", type(exc).__name__)


def audited(fn: Callable) -> Callable:
    """包装一个 MCP 工具函数：执行完（或抛错）后记一条审计。"""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        result = None
        error = None
        try:
            result = fn(*args, **kwargs)
            return result
        except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
            error = exc
            raise
        finally:
            try:
                bound = signature.bind_partial(*args, **kwargs)
                params = dict(bound.arguments)
            except Exception:  # noqa: BLE001 - 绑定失败就记录原始形状
                params = {"args": list(args), "kwargs": kwargs}
            record(
                getattr(fn, "__name__", "unknown"),
                params,
                result=result,
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    return wrapper
