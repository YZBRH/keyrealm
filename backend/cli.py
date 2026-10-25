"""命令行模式（CLI）。

设计目标
--------
- **不 open 任何端口**：普通命令只读写本地数据库，进程结束即退出；Web 是显式可选项（``--web``）。
- 与桌面 / Web **共用同一套后端函数**（``backend.api``），不复制业务逻辑。
- 适合脚本：``--json`` 输出机器可读结果，``--password`` / ``KEYREALM_MASTER_PASSWORD``
  或非交互 stdin 提供主密码；退出码 0=成功，1=失败，2=参数错误。

示例
----
    Keyrealm-CLI status
    Keyrealm-CLI list --type api_fofa
    Keyrealm-CLI show 3 --reveal
    Keyrealm-CLI add --type api_fofa --name fofa-main -f api_key=xxxx
    Keyrealm-CLI verify --all
    Keyrealm-CLI export backup.json
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Optional

from . import api, backup, bootstrap, mcp_server, paths, plugin_manager, proxy_store, single_instance, tls_store, updater, version


class CliError(Exception):
    """可预期的用户错误：打印一行原因并以退出码 1 结束。"""


# ── 通用工具 ──────────────────────────────────────────────

def _add_common(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """给主命令与子命令都挂一份全局参数。

    子命令里用 ``SUPPRESS`` 作默认值，避免子解析器的默认值把主解析器已解析的
    ``--json`` / ``--password`` 覆盖掉（argparse 的经典坑）。
    """
    default_json: Any = argparse.SUPPRESS if suppress else False
    default_pwd: Any = argparse.SUPPRESS if suppress else None
    default_yes: Any = argparse.SUPPRESS if suppress else False
    default_data: Any = argparse.SUPPRESS if suppress else None
    parser.add_argument("--json", action="store_true", default=default_json,
                        help="输出 JSON（便于脚本处理）")
    parser.add_argument("--password", "-p", default=default_pwd,
                        help="主密码；也可用环境变量 KEYREALM_MASTER_PASSWORD")
    parser.add_argument("--yes", "-y", action="store_true", default=default_yes,
                        help="跳过删除等操作的确认")
    parser.add_argument("--data-dir", default=default_data, metavar="PATH",
                        help="数据目录（仅 CLI；优先级高于配置文件）")


def _sub(parser: argparse.ArgumentParser, name: str, *, no_init: bool = False,
         **kwargs) -> argparse.ArgumentParser:
    """创建子命令解析器（自动带上全局参数）。"""
    sub = parser.add_parser(name, parents=[], **kwargs)
    _add_common(sub, suppress=True)
    if no_init:
        sub.set_defaults(no_init=True)
    return sub


def _width(text: str) -> int:
    """终端显示宽度（中文按 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [_width(h) for h in headers]
    for row in cells:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], _width(value))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in cells:
        lines.append("  ".join(_pad(v, widths[i]) for i, v in enumerate(row)).rstrip())
    return "\n".join(lines)


def _emit(args, payload: Any, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, default=str))
    elif human:
        print(human)


def _password(args, prompt: str = "主密码") -> str:
    pwd = getattr(args, "password", None) or os.environ.get("KEYREALM_MASTER_PASSWORD")
    if pwd:
        return pwd
    if not sys.stdin.isatty():
        raise CliError("需要主密码：请用 --password 或环境变量 KEYREALM_MASTER_PASSWORD"
                       "（非交互环境无法弹出输入提示）")
    return getpass.getpass(f"{prompt}: ")


def _unlock(args) -> None:
    """确认已初始化并解锁；失败统一抛 CliError。"""
    if not api.is_initialized().get("initialized"):
        raise CliError("保险库尚未初始化，请先执行：init")
    res = api.unlock(_password(args), auto_check=False)
    if not res.get("ok"):
        raise CliError(res.get("message") or "主密码错误")


def _confirm(question: str, args) -> None:
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise CliError("非交互环境请加 --yes 明确确认")
    answer = input(f"{question} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise CliError("已取消")


def _kv_pairs(items: Optional[list[str]], what: str = "字段") -> dict:
    result: dict[str, str] = {}
    for raw in items or []:
        if "=" not in raw:
            raise CliError(f"{what}格式应为 k=v：{raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise CliError(f"{what}名不能为空：{raw}")
        result[key] = value
    return result


def _json_object(args, merge: Optional[dict] = None) -> dict:
    """读取 --data-json（支持 ``-`` 表示 stdin）并与 -f 字段合并。"""
    data: dict = {}
    raw = getattr(args, "data_json", None)
    if raw:
        text = sys.stdin.read() if raw == "-" else raw
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CliError(f"--data-json 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise CliError("--data-json 必须是 JSON 对象")
        data.update(parsed)
    if merge:
        data.update(merge)
    return data


def _resolve_group(value: Any, *, allow_none: bool = True) -> Optional[int]:
    if value is None:
        return None if allow_none else None
    text = str(value).strip()
    if text.lower() in ("", "none", "-", "null"):
        return None
    groups = api.list_groups()
    if text.isdigit():
        gid = int(text)
        if any(int(g["id"]) == gid for g in groups):
            return gid
        raise CliError(f"分组 {gid} 不存在")
    matched = [g for g in groups if g["name"] == text]
    if len(matched) == 1:
        return int(matched[0]["id"])
    if len(matched) > 1:
        raise CliError(f"分组名「{text}」不唯一，请改用 ID")
    raise CliError(f"分组「{text}」不存在")


def _resolve_proxy(value: Any) -> dict:
    """按代理 id（UUID）或名称解析代理记录。"""
    text = str(value).strip()
    items = api.list_proxies().get("items") or []
    matched = [p for p in items if str(p.get("id")) == text]
    if not matched:
        matched = [p for p in items if p.get("name") == text]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        ids = ", ".join(str(p["id"]) for p in matched)
        raise CliError(f"代理名「{text}」不唯一，请改用 ID：{ids}")
    raise CliError(f"代理「{text}」不存在（用 proxy list 查看）")


def _apply_network_overrides(args, data: dict) -> dict:
    """把 --proxy / --proxy-mode / --tls 写进凭据数据的保留键。

    ``_proxy_mode`` / ``_proxy_id`` / ``_tls_version`` 不是 manifest 字段，但属于凭据数据，
    由框架在验证时读取（见 backend/proxy_store.py / backend/tls_store.py）。
    """
    proxy = getattr(args, "proxy", None)
    mode = getattr(args, "proxy_mode", None)
    if proxy:
        record = _resolve_proxy(proxy)
        data[proxy_store.KEY_MODE] = proxy_store.MODE_PROXY
        data[proxy_store.KEY_ID] = str(record["id"])
    elif mode:
        data[proxy_store.KEY_MODE] = mode
        data[proxy_store.KEY_ID] = ""
    tls = getattr(args, "tls", None)
    if tls is not None:
        value = str(tls).strip().lower()
        if value in ("", "inherit", "default"):
            data.pop(tls_store.KEY, None)
        else:
            data[tls_store.KEY] = value
    return data


def _network_summary(cred: dict) -> dict:
    """凭据实际使用的代理出口与 TLS 版本（含继承结果）。"""
    data = cred.get("data") or cred.get("data_plain") or {}
    ctype = cred.get("credential_type") or ""
    mode = str(data.get(proxy_store.KEY_MODE) or "").strip().lower()
    proxy_id = str(data.get(proxy_store.KEY_ID) or "").strip()
    proxy_record = None
    if mode == proxy_store.MODE_PROXY and proxy_id:
        try:
            proxy_record = next(
                (p for p in (api.list_proxies().get("items") or [])
                 if str(p.get("id")) == proxy_id),
                None,
            )
        except Exception:  # noqa: BLE001
            proxy_record = None
    outlet = proxy_store.describe_outlet(
        proxy_store.resolve_credential_proxy(data, ctype)
    )
    if mode == proxy_store.MODE_PROXY:
        label = f"指定代理（{proxy_record.get('name') if proxy_record else '已失效的代理'}）"
    elif mode == proxy_store.MODE_DIRECT:
        label = "直连"
    else:
        label = "跟随上层 / 全局"
    tls_own = str(data.get(tls_store.KEY) or "").strip().lower()
    tls_effective = tls_own or tls_store.describe_inherited(ctype) or tls_store.global_value()
    return {
        "proxy_mode": mode or "global",
        "proxy_id": proxy_id or None,
        "proxy_name": (proxy_record or {}).get("name"),
        "proxy_url": (proxy_record or {}).get("url"),
        "proxy_outlet": outlet,
        "proxy_label": label,
        "tls_version": tls_own or None,
        "tls_effective": tls_effective,
        "tls_label": tls_own or "跟随上层 / 全局",
    }


def _resolve_credential(value: Any) -> dict:
    text = str(value).strip()
    cred = None
    if text.isdigit():
        cred = api.get_credential(int(text))
        if not cred:
            raise CliError(f"凭据 {text} 不存在")
        return cred
    items = api.list_credentials(search=text)
    exact = [c for c in items if c.get("name") == text]
    if len(exact) == 1:
        cred = api.get_credential(int(exact[0]["id"]))
        if cred:
            return cred
    if len(exact) > 1:
        ids = ", ".join(str(c["id"]) for c in exact)
        raise CliError(f"名称「{text}」对应多条凭据，请改用 ID：{ids}")
    if items:
        ids = ", ".join(f"{c['id']}:{c.get('name')}" for c in items[:10])
        raise CliError(f"没有名称完全匹配「{text}」的凭据；相近结果：{ids}")
    raise CliError(f"凭据「{text}」不存在")


_STATUS_CN = {
    "valid": "有效",
    "invalid": "无效",
    "error": "错误",
    "unknown": "未验证",
}


def _cred_summary(cred: dict) -> dict:
    """列表 / JSON 用的精简结构——绝不输出 data_encrypted。"""
    return {
        "id": cred.get("id"),
        "name": cred.get("name"),
        "type": cred.get("credential_type"),
        "group": cred.get("group_name"),
        "status": cred.get("status"),
        "updated_at": cred.get("updated_at"),
        "masked": cred.get("data_masked") or {},
    }


def _display_data(cred: dict, reveal: bool) -> dict:
    data = dict(cred.get("data") or {})
    if not data:
        data = dict(cred.get("data_plain") or {})
    # 保留键（_proxy_mode / _proxy_id / _tls_version）由“代理 / TLS”两行单独展示，
    # 不再混在业务字段里，避免把它们误当成插件字段。
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    if not reveal:
        for key in api._sensitive_keys(cred.get("credential_type") or ""):
            if data.get(key):
                data[key] = api.mask_secret(str(data[key]))
    return data


# ── 命令实现 ──────────────────────────────────────────────

def cmd_init(args) -> int:
    if api.is_initialized().get("initialized"):
        raise CliError("主密码已设置；如需修改请用桌面 / Web 界面")
    pwd = getattr(args, "password", None) or os.environ.get("KEYREALM_MASTER_PASSWORD")
    if not pwd:
        if not sys.stdin.isatty():
            raise CliError("非交互环境请用 --password 或 KEYREALM_MASTER_PASSWORD 提供主密码")
        pwd = getpass.getpass("设置主密码: ")
        confirm = getpass.getpass("确认主密码: ")
        if pwd != confirm:
            raise CliError("两次输入的主密码不一致")
    res = api.setup_master_password(pwd)
    if not res.get("ok"):
        raise CliError(res.get("message") or "初始化失败")
    _emit(args, res, "主密码设置成功。")
    return 0


def cmd_status(args) -> int:
    initialized = bool(api.is_initialized().get("initialized"))
    unlocked = bool(api.is_unlocked().get("unlocked"))
    plugins = plugin_manager.list_installed_plugins(sync=False)   # bootstrap 已同步过
    proxies = api.list_proxies()
    tls = api.get_tls_options()
    payload = {
        "version": version.version_string(),
        "initialized": initialized,
        "unlocked": unlocked,
        "data_dir": str(Path(api.db.DATA_DIR)),
        "plugins": len(plugins),
        "plugins_enabled": sum(1 for p in plugins if p.get("enabled")),
        "global_proxy": proxies.get("global_url") or None,
        "tls_version": tls.get("tls_version"),
    }
    human = "\n".join([
        f"版本      : {payload['version']}",
        f"已初始化  : {'是' if initialized else '否'}",
        f"当前解锁  : {'是' if unlocked else '否'}",
        f"数据目录  : {payload['data_dir']}",
        f"插件      : {payload['plugins_enabled']}/{payload['plugins']} 已启用",
        f"全局代理  : {payload['global_proxy'] or '直连'}",
        f"TLS 版本  : {payload['tls_version']}",
    ])
    _emit(args, payload, human)
    return 0


def cmd_list(args) -> int:
    _unlock(args)
    group_id = _resolve_group(args.group) if args.group else None
    creds = api.list_credentials(
        group_id=group_id,
        credential_type=args.type,
        status=args.status,
        search=args.search,
    )
    rows = [[
        c.get("id"),
        _STATUS_CN.get(c.get("status"), c.get("status") or ""),
        c.get("credential_type"),
        c.get("name"),
        c.get("group_name") or "-",
        (c.get("updated_at") or "")[:19].replace("T", " "),
    ] for c in creds]
    human = _table(["ID", "状态", "类型", "名称", "分组", "更新时间"], rows) if rows else "（无凭据）"
    _emit(args, [_cred_summary(c) for c in creds], human)
    return 0


def cmd_show(args) -> int:
    _unlock(args)
    cred = _resolve_credential(args.credential)
    data = _display_data(cred, args.reveal)
    network = _network_summary(cred)
    payload = {
        **_cred_summary(cred),
        "last_verified_at": cred.get("last_verified_at"),
        "last_valid_at": cred.get("last_valid_at"),
        "network": network,
        "data": data,
    }
    if getattr(args, "json", False):
        _emit(args, payload, "")
        return 0
    lines = [
        f"ID        : {payload['id']}",
        f"名称      : {payload['name']}",
        f"类型      : {payload['type']}",
        f"分组      : {payload['group'] or '-'}",
        f"状态      : {_STATUS_CN.get(payload['status'], payload['status'] or '')}",
        f"最后验证  : {payload['last_verified_at'] or '-'}",
        f"代理      : {network['proxy_label']} → {network['proxy_outlet']}",
        f"TLS 版本  : {network['tls_label']} → {network['tls_effective']}",
        "",
        "字段：",
    ]
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"  {key} = {value}")
    if not data:
        lines.append("  （无）")
    if not args.reveal:
        lines.append("\n敏感字段已掩码；完整值请加 --reveal。")
    print("\n".join(lines))
    return 0


def cmd_add(args) -> int:
    _unlock(args)
    data = _json_object(args, _kv_pairs(args.field))
    if len(data) < 1:
        raise CliError("至少提供一个字段：-f key=value 或 --data-json")
    data = _apply_network_overrides(args, data)
    group_id = _resolve_group(args.group) if args.group else None
    res = api.create_credential(
        args.name, args.type, data, group_id,
        auto_verify=not args.no_verify,
    )
    if res.get("error"):
        details = res.get("details")
        msg = res.get("message") or res["error"]
        if details:
            msg += "：" + "；".join(str(d) for d in details)
        raise CliError(msg)
    payload = {
        "id": res.get("id"),
        "name": res.get("name"),
        "type": res.get("credential_type"),
        "status": res.get("status") or "unknown",
    }
    _emit(args, payload, f"已创建凭据 {payload['id']}（{payload['name']}），状态："
                         f"{_STATUS_CN.get(payload['status'], payload['status'])}")
    return 0


def cmd_update(args) -> int:
    _unlock(args)
    cred = _resolve_credential(args.credential)
    fields = _kv_pairs(args.field)
    data_json = getattr(args, "data_json", None)
    network_change = bool(args.proxy or args.proxy_mode or args.tls is not None)
    kwargs: dict = {}
    if args.name:
        kwargs["name"] = args.name
    if args.group is not None:
        kwargs["group_id"] = _resolve_group(args.group)
    if fields or data_json or network_change:
        merged = dict(cred.get("data") or cred.get("data_plain") or {})
        merged.update(_json_object(args, fields))
        _apply_network_overrides(args, merged)
        kwargs["data"] = merged
    if not kwargs:
        raise CliError("没有要修改的内容（用 --name / --group / -f / --data-json）")
    if args.verify and "data" in kwargs:
        kwargs["auto_verify"] = True
    res = api.update_credential(int(cred["id"]), **kwargs)
    if res.get("error"):
        raise CliError(res.get("message") or res["error"])
    _emit(args, {"ok": True, "id": cred["id"]}, f"已更新凭据 {cred['id']}。")
    return 0


def cmd_delete(args) -> int:
    _unlock(args)
    creds = [_resolve_credential(value) for value in args.credentials]
    ids = [int(c["id"]) for c in creds]
    _confirm(f"确认删除 {len(ids)} 条凭据：{', '.join(str(i) for i in ids)}？", args)
    res = api.delete_credentials(ids)
    _emit(args, res, f"已删除 {res.get('deleted', len(ids))} 条凭据。")
    return 0


def cmd_verify(args) -> int:
    _unlock(args)
    if args.all:
        group_id = _resolve_group(args.group) if args.group else None
        creds = api.list_credentials(group_id=group_id, credential_type=args.type)
    elif args.credentials:
        creds = [_resolve_credential(value) for value in args.credentials]
    else:
        raise CliError("请指定凭据 ID/名称，或使用 --all")
    if not creds:
        raise CliError("没有需要验证的凭据")
    results = []
    failed = 0
    for cred in creds:
        res = api.verify_credential(int(cred["id"]))
        status = (res or {}).get("status") or "error"
        if status != "valid":
            failed += 1
        results.append({
            "id": cred["id"],
            "name": cred.get("name"),
            "type": cred.get("credential_type"),
            "status": status,
            "message": (res or {}).get("message"),
            "account_info": (res or {}).get("account_info"),
            "duration_ms": (res or {}).get("duration_ms"),
        })
        if not getattr(args, "json", False):
            mark = {"valid": "✓", "invalid": "✗"}.get(status, "!")
            line = (f"{mark} {cred['id']} {cred.get('name')} [{cred.get('credential_type')}] "
                    f"{_STATUS_CN.get(status, status)}")
            msg = (res or {}).get("message")
            if msg:
                line += f" — {msg}"
            print(line)
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, default=str))
    else:
        print(f"\n共 {len(results)} 条：有效 {len(results) - failed}，失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_groups(args) -> int:
    _unlock(args)
    groups = api.list_groups()
    rows = [[g["id"], g["name"], g.get("credential_count", 0), g.get("description") or ""]
            for g in groups]
    human = _table(["ID", "名称", "凭据数", "描述"], rows) if rows else "（无分组）"
    _emit(args, groups, human)
    return 0


def cmd_group_add(args) -> int:
    _unlock(args)
    res = api.create_group(args.name, args.description, args.color)
    payload = {"id": res.get("id"), "name": args.name}
    _emit(args, payload, f"已创建分组 {payload['id']}（{args.name}）")
    return 0


def cmd_group_update(args) -> int:
    _unlock(args)
    group_id = _resolve_group(args.group)
    if group_id is None:
        raise CliError("必须指定分组")
    res = api.update_group(group_id, name=args.name, description=args.description,
                           color=args.color)
    if res.get("error"):
        raise CliError(res.get("message") or res["error"])
    _emit(args, {"ok": True, "id": group_id}, f"已更新分组 {group_id}。")
    return 0


def cmd_group_rm(args) -> int:
    _unlock(args)
    group_id = _resolve_group(args.group)
    if group_id is None:
        raise CliError("必须指定分组")
    _confirm(f"确认删除分组 {group_id}？（组内凭据不会被删除）", args)
    api.delete_group(group_id)
    _emit(args, {"ok": True, "id": group_id}, f"已删除分组 {group_id}。")
    return 0


def cmd_plugins(args) -> int:
    plugins = api.list_plugins()
    rows = [[
        p.get("credential_type"),
        "启用" if p.get("enabled") else "禁用",
        p.get("name"),
        p.get("credential_count", 0),
    ] for p in plugins]
    human = _table(["类型", "状态", "名称", "凭据数"], rows) if rows else "（无插件）"
    _emit(args, plugins, human)
    return 0


def cmd_plugin_toggle(args, enabled: bool) -> int:
    _unlock(args)
    res = api.set_plugin_enabled(args.type, enabled)
    if res.get("ok") is False:
        raise CliError(res.get("message") or res.get("error") or "操作失败")
    _emit(args, res, f"已{'启用' if enabled else '禁用'}插件：{res.get('name') or args.type}")
    return 0


def cmd_proxy_list(args) -> int:
    res = api.list_proxies()
    items = res.get("items") or []
    global_id = res.get("global_id")
    rows = []
    for p in items:
        rows.append([
            p.get("id"),
            p.get("name"),
            p.get("url"),
            "★ 全局" if str(p.get("id")) == str(global_id or "") else "",
        ])
    human = _table(["ID", "名称", "地址", "说明"], rows) if rows else "（未配置代理）"
    if not rows:
        human += f"\n当前全局出口：{res.get('global_url') or '直连'}"
    _emit(args, res, human)
    return 0


def cmd_proxy_add(args) -> int:
    res = api.create_proxy(args.name, args.url)
    if res.get("ok") is False:
        raise CliError(res.get("message") or "新增代理失败")
    record = res.get("created") or {}
    payload = {"ok": True, "proxy": record}
    _emit(args, payload, f"已新增代理：{args.name}（{record.get('id') or '?'}）")
    return 0


def cmd_proxy_update(args) -> int:
    record = _resolve_proxy(args.proxy)
    res = api.update_proxy(record["id"], name=args.name, url=args.url)
    if res.get("ok") is False:
        raise CliError(res.get("message") or "修改代理失败")
    _emit(args, {"ok": True, "id": record["id"]}, f"已更新代理 {record['id']}。")
    return 0


def cmd_proxy_rm(args) -> int:
    record = _resolve_proxy(args.proxy)
    _confirm(f"确认删除代理「{record.get('name')}」？（引用它的凭据会回退到全局）", args)
    res = api.delete_proxy(record["id"])
    if res.get("ok") is False:
        raise CliError(res.get("message") or "删除代理失败")
    _emit(args, res, f"已删除代理 {record['id']}。")
    return 0


def cmd_proxy_use(args) -> int:
    value = str(args.proxy or "").strip()
    if value.lower() in ("", "none", "direct", "off"):
        res = api.set_global_proxy(None)
        _emit(args, res, "全局代理已设为直连。")
        return 0
    record = _resolve_proxy(value)
    res = api.set_global_proxy(record["id"])
    if res.get("ok") is False:
        raise CliError(res.get("message") or "设置全局代理失败")
    _emit(args, res, f"全局代理已设为 {record.get('name')}（{record.get('url')}）。")
    return 0


def cmd_proxy_test(args) -> int:
    record = _resolve_proxy(args.proxy) if args.proxy else None
    res = api.test_proxy_id(record["id"] if record else None)
    if res.get("ok") is False:
        raise CliError(res.get("message") or "代理测试失败")
    verdict = res.get("verdict") or {}
    payload = {
        "ok": True,
        "proxy": (record or {}).get("url") or res.get("proxy") or "",
        "source": res.get("proxy_source"),
        "level": verdict.get("level"),
        "headline": verdict.get("headline"),
        "details": verdict.get("details") or [],
    }
    human = f"[{payload['level'] or '?'}] {payload['headline'] or '测试完成'}"
    for line in payload["details"]:
        human += f"\n  - {line}"
    _emit(args, payload, human)
    return 0 if payload["level"] != "fail" else 1


def cmd_tls_show(args) -> int:
    res = api.get_tls_options()
    payload = {
        "global": res.get("tls_version"),
        "options": res.get("tls_versions") or [],
    }
    _emit(args, payload,
          f"全局 TLS 版本：{payload['global']}\n"
          f"可选值：{', '.join(payload['options'])}")
    return 0


def cmd_tls_set(args) -> int:
    res = api.set_tls_version(args.value)
    payload = {"ok": True, "global": res.get("tls_version")}
    _emit(args, payload, f"全局 TLS 版本已设为 {payload['global']}。")
    return 0


def cmd_actions(args) -> int:
    _unlock(args)
    cred = _resolve_credential(args.credential)
    actions = api.list_credential_actions(int(cred["id"]))
    rows = [[a.get("id"), a.get("name"), a.get("group") or "", a.get("kind") or ""]
            for a in actions]
    human = _table(["ID", "名称", "分组", "类型"], rows) if rows else "（该凭据没有可用操作）"
    _emit(args, actions, human)
    return 0


def cmd_action(args) -> int:
    _unlock(args)
    cred = _resolve_credential(args.credential)
    params = _kv_pairs(args.param, "参数")
    res = api.run_credential_action(int(cred["id"]), args.action, params)
    ok = bool(res.get("ok")) and not res.get("error")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, default=str))
    else:
        if res.get("error") and not res.get("ok"):
            print(f"失败：{res.get('error')}", file=sys.stderr)
        if res.get("output"):
            print(res["output"])
        rows = res.get("rows")
        if isinstance(rows, list) and rows:
            columns = res.get("columns") or list(rows[0].keys())
            table_rows = [[row.get(col, "") for col in columns] for row in rows]
            print(_table([str(c) for c in columns], table_rows))
        if not res.get("output") and not rows and ok:
            print("操作成功（无输出）")
    return 0 if ok else 1


def cmd_export(args) -> int:
    _unlock(args)
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    built = api.build_backup_doc(args.scope, encrypted=not args.plain, credential_ids=ids)
    if built.get("error") or built.get("ok") is False:
        raise CliError(built.get("message") or built.get("error") or "导出失败")
    doc = built["document"]
    if args.path == "-":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    target = Path(args.path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup.write_backup_file(str(target), doc)
    count = int((doc.get("counts") or {}).get("credentials") or 0)
    _emit(args, {"ok": True, "path": str(target), "count": count,
                 "encrypted": not args.plain, "scope": args.scope},
          f"已导出 {count} 条凭据到 {target}（{'明文' if args.plain else '加密'}）")
    if args.plain and not getattr(args, "json", False):
        print("警告：明文备份包含完整密钥，请妥善保管。", file=sys.stderr)
    return 0


def cmd_import(args) -> int:
    _unlock(args)
    source = Path(args.path).expanduser()
    try:
        doc = backup.read_backup_file(str(source))
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"读取失败：{exc}") from exc
    if not isinstance(doc, dict):
        raise CliError("备份文件格式不正确（应为 JSON 对象）")
    orig = args.orig_password
    if not orig and doc.get("encrypted"):
        vault = doc.get("vault") if isinstance(doc.get("vault"), dict) else {}
        doc_salt = str((vault or {}).get("salt") or "").strip()
        cur_salt = (api.db.get_setting("salt", "") or "").strip()
        if doc_salt and doc_salt != cur_salt and sys.stdin.isatty():
            import getpass as _gp
            orig = _gp.getpass("该备份来自另一个保险库，请输入它当时的主密码（可留空跳过）: ") or None
    res = api.apply_backup_doc(doc, dedupe=not args.no_dedupe,
                               source=str(source), orig_password=orig)
    if res.get("need_password"):
        raise CliError(res.get("message") or "需要备份所属保险库的主密码，请用 --orig-password 提供")
    if res.get("error"):
        raise CliError(res.get("message") or res["error"])
    payload = {
        "ok": res.get("ok"),
        "imported": res.get("imported"),
        "skipped_duplicate": res.get("skipped_duplicate"),
        "skipped_plugin": res.get("skipped_plugin"),
        "failed_count": res.get("failed_count"),
        "log_file": res.get("log_file"),
    }
    human = (f"导入完成：新增 {payload['imported']} 条，重复跳过 "
             f"{payload['skipped_duplicate']} 条，失败 {payload['failed_count']} 条")
    _emit(args, payload, human)
    return 0 if res.get("ok") else 1


def cmd_dedupe(args) -> int:
    _unlock(args)
    if args.keep or args.remove:
        if not args.keep or not args.remove:
            raise CliError("合并重复项需要同时提供 --keep 与 --remove")
        keep = _resolve_credential(args.keep)
        removes = [_resolve_credential(x) for x in args.remove]
        res = api.resolve_duplicate_group(int(keep["id"]),
                                          [int(c["id"]) for c in removes],
                                          merge_notes=args.merge_notes)
        if res.get("ok") is False:
            raise CliError(res.get("message") or "合并失败")
        _emit(args, res, f"已保留 {keep['id']}，删除 {res.get('deleted')} 条重复项。")
        return 0
    group_id = _resolve_group(args.group) if args.group else None
    res = api.find_duplicate_credentials(group_id=group_id, credential_type=args.type)
    if res.get("ok") is False:
        raise CliError(res.get("message") or "扫描失败")
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0
    groups = res.get("groups") or []
    if not groups:
        print("未发现重复凭据。")
        return 0
    for index, group in enumerate(groups, 1):
        print(f"重复组 {index}（{group.get('plugin_name') or group.get('credential_type')}）：")
        for item in group.get("items") or []:
            print(f"  {item.get('id'):>5}  {item.get('name')}  "
                  f"{_STATUS_CN.get(item.get('status'), item.get('status') or '')}")
    print("\n用 --keep ID --remove ID[,ID] 合并；加 --merge-notes 合并备注。")
    return 0


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((target, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_mcp_status(args) -> int:
    status = api.get_mcp_status()
    options = api.get_mcp_options()
    record = api.db.get_mcp_token_record()
    host = status.get("host") or options.get("host")
    port = status.get("port") or options.get("port")
    if status.get("running"):
        host, port = status.get("host"), status.get("port")
    else:
        host, port = options.get("host"), options.get("port")
    port_open = _port_open(str(host or "127.0.0.1"), int(port or 0))
    payload = {
        "running_in_this_process": bool(status.get("running")),
        "port_open": port_open,
        "enabled_setting": api.read_mcp_enabled(),
        "host": host,
        "port": port,
        "url": status.get("url") or f"http://{host}:{port}/sse",
        "token_prefix": (record or {}).get("token_prefix"),
    }
    human = "\n".join([
        f"监听地址   : {host}:{port}",
        f"本进程运行 : {'是' if payload['running_in_this_process'] else '否'}",
        f"端口可连接 : {'是' if port_open else '否'}"
        + ("（可能由其他进程运行）" if port_open and not payload["running_in_this_process"] else ""),
        f"上次开关   : {'开启' if payload['enabled_setting'] else '关闭'}",
        f"Token 前缀 : {payload['token_prefix'] or '（尚未创建）'}",
        f"SSE 地址   : {payload['url']}",
    ])
    _emit(args, payload, human)
    return 0


def _mcp_issue_token(args, rotate: bool = False) -> tuple[str, str]:
    """确保有 Token；返回 (前缀, 本次生成的明文或空串)。"""
    record = api.db.get_mcp_token_record()
    if rotate or not record:
        created = api.rotate_mcp_token()
        return created["token_prefix"], created["token"]
    return record["token_prefix"], ""


def cmd_mcp_start(args) -> int:
    # 长驻服务：占住单实例锁，避免桌面端同时运行两套 MCP/写同一库
    if not single_instance.acquire():
        raise CliError("已有 Keyrealm 实例在运行，请先退出后再从 CLI 启动 MCP")
    _unlock(args)

    if args.host is not None or args.port is not None:
        current = api.get_mcp_options()
        host = args.host or current.get("host")
        port = args.port or current.get("port")
        saved = api.set_mcp_options(host, port)
        if saved.get("ok") is False:
            raise CliError(saved.get("message") or "保存 MCP 监听地址失败")

    prefix, plain = _mcp_issue_token(args, rotate=args.new_token)
    res = api.start_mcp()
    if res.get("ok") is False:
        raise CliError(res.get("message") or "启动 MCP 失败")

    print(f"MCP 已启动：{res.get('url')}", flush=True)
    if plain:
        print(f"Token（仅显示一次）：{plain}", flush=True)
    else:
        print(f"使用已有 Token（前缀 {prefix}…）；需要新 Token 请加 --new-token 或运行 mcp token --rotate。",
              flush=True)
    print("按 Ctrl+C 停止 MCP。", flush=True)

    # 用信号处理而不是裸 KeyboardInterrupt：PyInstaller onefile 下 Ctrl+C 可能同时落到
    # 引导进程与子进程，裸异常会在清理途中被第二个信号打断，表现为“停了但没打印/没关端口”。
    stop_event = threading.Event()

    def _request_stop(_signum, _frame):
        stop_event.set()

    old_handlers: dict = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _request_stop)
        except Exception:  # noqa: BLE001 - 非主线程/平台不支持时忽略
            pass
    try:
        while mcp_server.is_running() and not stop_event.is_set():
            stop_event.wait(0.3)
    finally:
        if mcp_server.is_running():
            api.stop_mcp()
        for sig, handler in old_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception:  # noqa: BLE001
                pass
        print("MCP 已停止。", flush=True)
    return 0


def cmd_mcp_token(args) -> int:
    rotate = args.rotate
    prefix, plain = _mcp_issue_token(args, rotate=rotate)
    payload = {
        "ok": True,
        "token_prefix": prefix,
        "token": plain or None,
        "rotated": bool(rotate),
    }
    if plain:
        human = f"新 Token：{plain}\n（明文只显示这一次，请立即保存）"
    else:
        human = (f"当前 Token 前缀：{prefix}…\n"
                 "明文无法找回；要获取新 Token 请加 --rotate。")
    _emit(args, payload, human)
    return 0


def cmd_mcp_permissions(args) -> int:
    items = api.list_mcp_permissions()
    rows = [[i.get("tool_name"), "启用" if i.get("enabled") else "禁用",
             i.get("description") or ""] for i in items]
    human = _table(["工具", "状态", "说明"], rows) if rows else "（无权限记录）"
    _emit(args, items, human)
    return 0


def cmd_mcp_permission(args) -> int:
    known = {item.get("tool_name") for item in api.list_mcp_permissions()}
    if args.tool not in known:
        raise CliError(f"未知 MCP 工具：{args.tool}（用 mcp permissions 查看）")
    enabled = args.state == "on"
    api.set_mcp_permission(args.tool, enabled)
    _emit(args, {"ok": True, "tool": args.tool, "enabled": enabled},
          f"已{'启用' if enabled else '禁用'} MCP 工具：{args.tool}")
    return 0


def cmd_data_dir_show(args) -> int:
    payload = {
        "data_dir": str(paths.data_dir()),
        "source": paths.data_dir_source(),
        "default": str(paths.default_data_dir()),
        "config_file": str(paths.config_file()),
        "config_exists": paths.config_file().is_file(),
        "config_error": paths.config_error() or None,
    }
    source_cn = {"cli": "命令行 --data-dir", "config": "配置文件", "default": "默认"}.get(
        payload["source"], payload["source"])
    human = "\n".join([
        f"当前数据目录 : {payload['data_dir']}",
        f"来源         : {source_cn}",
        f"默认目录     : {payload['default']}",
        f"配置文件     : {payload['config_file']}"
        f"{'' if payload['config_exists'] else '（不存在）'}",
    ])
    if payload["config_error"]:
        human += f"\n配置错误     : {payload['config_error']}"
    _emit(args, payload, human)
    return 0


def cmd_data_dir_set(args) -> int:
    try:
        target = paths.write_config_data_dir(args.path)
    except (OSError, ValueError) as exc:
        raise CliError(f"写入数据目录配置失败：{exc}") from exc
    payload = {"ok": True, "data_dir": str(target), "config_file": str(paths.config_file())}
    _emit(args, payload,
          f"已记录数据目录：{target}\n配置文件：{paths.config_file()}\n"
          "下次启动生效（当前命令仍使用启动时的目录）。")
    return 0


def cmd_data_dir_clear(args) -> int:
    changed = paths.clear_config_data_dir()
    payload = {"ok": True, "changed": changed, "config_file": str(paths.config_file())}
    _emit(args, payload,
          "已清除数据目录配置；下次启动使用默认目录。" if changed else "配置文件里没有数据目录设置。")
    return 0


def _unlock_for_update(args) -> bool:
    """更新前可选解锁：解密已保存的更新访问令牌，并让备份带上全量导出文档。"""
    pwd = getattr(args, "password", None) or os.environ.get("KEYREALM_MASTER_PASSWORD")
    if not pwd or not api.is_initialized().get("initialized"):
        return False
    res = api.unlock(pwd, auto_check=False)
    if not res.get("ok"):
        raise CliError(res.get("message") or "主密码错误")
    try:
        api._sync_update_token(force=True)
    except Exception:  # noqa: BLE001 - 令牌解密失败不影响匿名更新
        pass
    return True


def cmd_update_check(args) -> int:
    kind = "test" if args.test else "formal"
    _unlock_for_update(args)
    res = api.update_check(kind)
    payload = {
        "ok": bool(res.get("ok")),
        "kind": kind,
        "has_update": bool(res.get("has_update")),
        "reason": res.get("reason"),
        "current": res.get("current"),
        "latest": res.get("latest"),
        "asset": res.get("asset"),
        "prepared": res.get("prepared"),
        "message": res.get("message") or res.get("error"),
    }
    if payload["ok"]:
        current = (payload["current"] or {}).get("version") or "?"
        latest = (payload["latest"] or {}).get("version") or "?"
        human = "\n".join([
            f"渠道      : {'测试版' if kind == 'test' else '正式版'}",
            f"当前版本  : {current}",
            f"远端版本  : {latest}",
            f"有更新    : {'是' if payload['has_update'] else '否'}",
            f"说明      : {payload['reason'] or '-'}",
            f"更新包    : {payload['asset'] or '-'}",
        ])
    else:
        human = f"检查更新失败：{payload['message'] or '未知错误'}"
    _emit(args, payload, human)
    return 0 if payload["ok"] else 1


def cmd_update_status(args) -> int:
    res = api.update_status()
    _emit(args, res, json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_update_install(args) -> int:
    """同步执行「准备 → 交棒」；交棒进程在 CLI 退出后替换程序。"""
    kind = "test" if args.test else "formal"
    if kind == "test" and not updater.git_available():
        raise CliError("测试版更新需要 git 与源码；当前环境不可用")
    # 更新期间占住单实例锁：既避免桌面端同时启动，也让交棒进程等到本进程退出后再替换文件。
    if not single_instance.acquire():
        raise CliError("已有 Keyrealm 实例在运行，请先彻底退出后再执行更新")

    # 密码可选：提供了就解锁，以便解密更新令牌、并随备份写入全量导出文档。
    _unlock_for_update(args)

    last = {"text": ""}

    def _progress(cur, total, text=""):
        if getattr(args, "json", False):
            return
        if text and text != last["text"]:
            last["text"] = text
            print(text, file=sys.stderr, flush=True)

    try:
        res = updater.prepare_update(
            kind,
            frontend=not args.no_frontend,
            progress_cb=_progress,
            export_doc=api._full_export_doc() if api.is_unlocked().get("unlocked") else None,
            log=lambda m: print(m, file=sys.stderr, flush=True),
        )
    except KeyboardInterrupt:
        raise CliError("已取消更新准备") from None
    if not res.get("ok"):
        raise CliError(res.get("message") or "更新准备失败")
    if not res.get("has_update"):
        _emit(args, {"ok": True, "has_update": False, "kind": kind,
                     "message": res.get("message") or "已是最新版本"},
              res.get("message") or "已是最新版本")
        return 0

    prepared = updater.read_prepared(updater.repo_root())
    # 交棒后重启时不要再执行一遍 update install；只保留模式与一次性的数据目录参数，
    # 否则桌面版的 `--cli` 会变成开窗口，临时 `--data-dir` 也会丢。
    relaunch: list[str] = []
    if "--cli" in sys.argv:
        relaunch.append("--cli")
    if paths.data_dir_source() == "cli":
        relaunch += ["--data-dir", str(paths.data_dir())]
    sys.argv = [sys.argv[0], *relaunch]
    handoff = updater.begin_handoff(
        kind,
        restart=not args.no_restart,
        prepared=prepared,
    )
    if not handoff.get("ok"):
        raise CliError(handoff.get("message") or "启动更新进程失败")
    payload = {
        "ok": True,
        "kind": kind,
        "to_version": res.get("to_version"),
        "reused": bool(res.get("reused")),
        "pid": handoff.get("pid"),
        "restart": not args.no_restart,
        "message": handoff.get("message"),
    }
    human = (
        f"更新已准备并交棒（pid={payload['pid']}）。\n"
        f"{payload['message']}\n"
        f"本进程即将退出；完成后请重新运行 Keyrealm-CLI --version 确认版本。"
    )
    if args.no_restart:
        human += "\n（已指定 --no-restart，不会自动重启）"
    _emit(args, payload, human)
    return 0


def cmd_logs(args) -> int:
    res = api.get_logs(limit=args.limit, level=args.level, search=args.search)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0
    entries = res.get("entries") or []
    print("\n".join(entries) if entries else "（无日志）")
    return 0


def cmd_version(args) -> int:
    payload = api.get_version()
    _emit(args, payload, payload.get("display") or version.version_string())
    return 0


# ── 参数解析 ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keyrealm",
        description="Keyrealm 命令行：直接操作本地保险库（不会开启任何端口）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "模式：\n"
            "  keyrealm --gui …        桌面窗口（CLI 版不含此模式）\n"
            "  keyrealm --web …        浏览器界面（会监听端口）\n"
            "      --web [--host H] [--port P] [--token T] [--no-browser]\n"
            "  keyrealm <命令> …       命令行模式（默认，不监听端口）\n"
            "\n"
            "每条命令的专属参数用 `keyrealm <命令> --help` 查看，例如：\n"
            "  show --reveal           显示完整敏感字段（默认掩码）\n"
            "  add -f K=V              字段；敏感值建议用 --data-json - 从 stdin 读\n"
            "  verify --all            验证全部凭据\n"
            "  proxy use ID|none       设置全局代理；proxy test 测试连通性\n"
            "  tls set 1.2             设置全局 TLS 版本（auto/1.0~1.3）\n"
            "  self-update check       检查更新；self-update install 自更新\n"
            "  data-dir show           查看数据目录；data-dir set PATH 持久化\n"
            "  mcp start               前台启动 MCP（Ctrl+C 停止）；mcp status 查看\n"
            "  add/update --proxy ID --tls 1.2   为单条凭据覆盖代理/TLS\n"
            "  export --plain          导出明文（危险，文件含完整密钥）\n"
            "  export --scope/--ids    导出范围与凭据选择\n"
        ),
    )
    _add_common(parser)
    parser.add_argument("--version", action="version",
                        version=f"Keyrealm {version.version_string()}",
                        help="显示版本")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = _sub(sub, "init", help="首次使用：设置主密码")
    p.set_defaults(func=cmd_init)

    p = _sub(sub, "status", help="查看初始化 / 解锁 / 插件状态")
    p.set_defaults(func=cmd_status)

    p = _sub(sub, "list", aliases=["ls"], help="列出凭据")
    p.add_argument("--type", help="按插件类型过滤（如 api_fofa）")
    p.add_argument("--group", help="按分组 ID 或名称过滤")
    p.add_argument("--status", choices=("valid", "invalid", "error", "unknown"),
                   help="按验证状态过滤")
    p.add_argument("--search", help="按名称 / 非敏感字段搜索")
    p.set_defaults(func=cmd_list)

    p = _sub(sub, "show", help="查看单条凭据（--reveal 显示完整敏感字段）")
    p.add_argument("credential", help="凭据 ID 或名称")
    p.add_argument("--reveal", action="store_true", help="显示完整敏感字段（默认掩码）")
    p.set_defaults(func=cmd_show)

    p = _sub(sub, "add", help="新增凭据（-f K=V / --data-json -）")
    p.add_argument("--type", required=True, help="插件类型（见 plugins 命令）")
    p.add_argument("--name", required=True, help="凭据名称")
    p.add_argument("-f", "--field", action="append", default=[], metavar="K=V",
                   help="字段，可重复；敏感值建议用 --data-json - 从 stdin 读")
    p.add_argument("--data-json", help="JSON 对象；传 - 表示从 stdin 读取")
    p.add_argument("--group", help="分组 ID 或名称")
    p.add_argument("--proxy", metavar="ID|名称", help="该凭据使用指定代理（覆盖全局）")
    p.add_argument("--proxy-mode", choices=("global", "direct"),
                   help="该凭据的代理策略：跟随全局 / 直连")
    p.add_argument("--tls", metavar="VERSION",
                   choices=("auto", "1.0", "1.1", "1.2", "1.3", "inherit"),
                   help="该凭据的 TLS 版本；inherit=跟随上层")
    p.add_argument("--no-verify", action="store_true", help="创建后不立即联网验证")
    p.set_defaults(func=cmd_add)

    p = _sub(sub, "update", help="修改凭据")
    p.add_argument("credential", help="凭据 ID 或名称")
    p.add_argument("--name", help="新名称")
    p.add_argument("--group", help="新分组；none 表示移出分组")
    p.add_argument("-f", "--field", action="append", default=[], metavar="K=V",
                   help="要修改的字段，可重复")
    p.add_argument("--data-json", help="JSON 对象；传 - 表示从 stdin 读取")
    p.add_argument("--proxy", metavar="ID|名称", help="改为使用指定代理（覆盖全局）")
    p.add_argument("--proxy-mode", choices=("global", "direct"),
                   help="改为跟随全局 / 直连")
    p.add_argument("--tls", metavar="VERSION",
                   choices=("auto", "1.0", "1.1", "1.2", "1.3", "inherit"),
                   help="改为指定 TLS 版本；inherit=跟随上层")
    p.add_argument("--verify", action="store_true", help="修改后立即验证")
    p.set_defaults(func=cmd_update)

    p = _sub(sub, "delete", aliases=["rm"], help="删除凭据")
    p.add_argument("credentials", nargs="+", help="凭据 ID 或名称，可多个")
    p.set_defaults(func=cmd_delete)

    p = _sub(sub, "verify", help="验证凭据（--all；任一失败时退出码为 1）")
    p.add_argument("credentials", nargs="*", help="凭据 ID 或名称；配合 --all 时可省略")
    p.add_argument("--all", action="store_true", help="验证全部（可再用 --type/--group 过滤）")
    p.add_argument("--type", help="配合 --all：按插件类型过滤")
    p.add_argument("--group", help="配合 --all：按分组过滤")
    p.set_defaults(func=cmd_verify)

    p = _sub(sub, "groups", help="列出分组")
    p.set_defaults(func=cmd_groups)

    gp = _sub(sub, "group", help="分组管理")
    gsub = gp.add_subparsers(dest="group_command", metavar="<操作>", required=True)
    g = _sub(gsub, "add", help="新增分组")
    g.add_argument("name", help="分组名称")
    g.add_argument("--description", default="", help="分组描述")
    g.add_argument("--color", default="#409EFF", help="颜色（如 #409EFF）")
    g.set_defaults(func=cmd_group_add)
    g = _sub(gsub, "update", help="修改分组")
    g.add_argument("group", help="分组 ID 或名称")
    g.add_argument("--name", help="新名称")
    g.add_argument("--description", help="新描述")
    g.add_argument("--color", help="新颜色（如 #409EFF）")
    g.set_defaults(func=cmd_group_update)
    g = _sub(gsub, "rm", help="删除分组（组内凭据保留）")
    g.add_argument("group", help="分组 ID 或名称")
    g.set_defaults(func=cmd_group_rm)

    p = _sub(sub, "plugins", help="列出插件")
    p.set_defaults(func=cmd_plugins)

    pp = _sub(sub, "plugin", help="启用 / 禁用插件")
    psub = pp.add_subparsers(dest="plugin_command", metavar="<操作>", required=True)
    g = _sub(psub, "enable", help="启用插件")
    g.add_argument("type", help="插件类型（见 plugins 命令）")
    g.set_defaults(func=lambda a: cmd_plugin_toggle(a, True))
    g = _sub(psub, "disable", help="禁用插件")
    g.add_argument("type", help="插件类型（见 plugins 命令）")
    g.set_defaults(func=lambda a: cmd_plugin_toggle(a, False))

    p = _sub(sub, "mcp", help="MCP 服务（状态 / 启动 / Token / 权限）")
    msub = p.add_subparsers(dest="mcp_command", metavar="<操作>", required=True)
    g = _sub(msub, "status", help="查看 MCP 状态")
    g.set_defaults(func=cmd_mcp_status)
    g = _sub(msub, "start", help="前台启动 MCP（Ctrl+C 停止）")
    g.add_argument("--host", help="监听地址（会保存）")
    g.add_argument("--port", type=int, help="监听端口（会保存）")
    g.add_argument("--new-token", action="store_true", help="轮换并显示新 Token")
    g.set_defaults(func=cmd_mcp_start)
    g = _sub(msub, "token", help="查看 Token 前缀 / 轮换 Token")
    g.add_argument("--rotate", action="store_true", help="轮换并显示新 Token")
    g.set_defaults(func=cmd_mcp_token)
    g = _sub(msub, "permissions", help="列出 MCP 工具权限")
    g.set_defaults(func=cmd_mcp_permissions)
    g = _sub(msub, "permission", help="启用 / 禁用某个 MCP 工具")
    g.add_argument("tool", help="工具名（见 mcp permissions）")
    g.add_argument("state", choices=("on", "off"), help="on=启用，off=禁用")
    g.set_defaults(func=cmd_mcp_permission)

    p = _sub(sub, "data-dir", help="查看 / 设置数据目录（写入入口目录配置文件）")
    dsub = p.add_subparsers(dest="data_dir_command", metavar="<操作>", required=True)
    g = _sub(dsub, "show", help="查看当前生效的数据目录与来源")
    g.set_defaults(func=cmd_data_dir_show, no_init=True)
    g = _sub(dsub, "set", help="持久化数据目录到配置文件")
    g.add_argument("path", help="数据目录路径（相对路径按当前工作目录解析）")
    g.set_defaults(func=cmd_data_dir_set, no_init=True)
    g = _sub(dsub, "clear", help="清除配置文件里的数据目录")
    g.set_defaults(func=cmd_data_dir_clear, no_init=True)

    p = _sub(sub, "proxy", help="代理管理（列表 / 新增 / 修改 / 删除 / 全局 / 测试）")
    psub = p.add_subparsers(dest="proxy_command", metavar="<操作>", required=True)
    g = _sub(psub, "list", help="列出代理")
    g.set_defaults(func=cmd_proxy_list)
    g = _sub(psub, "add", help="新增代理")
    g.add_argument("name", help="代理名称")
    g.add_argument("url", help="代理地址，如 socks5://127.0.0.1:1080")
    g.set_defaults(func=cmd_proxy_add)
    g = _sub(psub, "update", help="修改代理")
    g.add_argument("proxy", help="代理 ID 或名称")
    g.add_argument("--name", help="新名称")
    g.add_argument("--url", help="新地址")
    g.set_defaults(func=cmd_proxy_update)
    g = _sub(psub, "rm", help="删除代理")
    g.add_argument("proxy", help="代理 ID 或名称")
    g.set_defaults(func=cmd_proxy_rm)
    g = _sub(psub, "use", help="设置全局默认代理")
    g.add_argument("proxy", help="代理 ID/名称；none 表示直连")
    g.set_defaults(func=cmd_proxy_use)
    g = _sub(psub, "test", help="测试代理（省略则测当前全局）")
    g.add_argument("proxy", nargs="?", help="代理 ID 或名称")
    g.set_defaults(func=cmd_proxy_test)

    p = _sub(sub, "tls", help="TLS 版本设置")
    tsub = p.add_subparsers(dest="tls_command", metavar="<操作>", required=True)
    g = _sub(tsub, "show", help="查看全局 TLS 版本")
    g.set_defaults(func=cmd_tls_show)
    g = _sub(tsub, "set", help="设置全局 TLS 版本")
    g.add_argument("value", choices=("auto", "1.0", "1.1", "1.2", "1.3"),
                   help="TLS 版本；auto=自动协商并回退")
    g.set_defaults(func=cmd_tls_set)

    p = _sub(sub, "actions", help="列出某凭据可用的插件操作")
    p.add_argument("credential", help="凭据 ID 或名称")
    p.set_defaults(func=cmd_actions)

    p = _sub(sub, "action", help="执行插件操作")
    p.add_argument("credential", help="凭据 ID 或名称")
    p.add_argument("action", help="操作 ID（见 actions 命令）")
    p.add_argument("--param", action="append", default=[], metavar="K=V",
                   help="操作参数，可重复")
    p.set_defaults(func=cmd_action)

    p = _sub(sub, "export", help="导出备份（--plain 明文 / --ids 指定凭据）")
    p.add_argument("path", help="输出文件；- 表示写到 stdout")
    p.add_argument("--scope", choices=("all", "credentials"), default="all",
                   help="导出范围：all=全部，credentials=仅凭据")
    p.add_argument("--ids", help="只导出这些凭据 ID（逗号分隔）")
    p.add_argument("--plain", action="store_true",
                   help="导出明文敏感值（危险：文件包含完整密钥）")
    p.set_defaults(func=cmd_export)

    p = _sub(sub, "import", help="导入备份")
    p.add_argument("path", help="备份文件路径")
    p.add_argument("--orig-password", help="备份来自另一个保险库时，它当时的主密码")
    p.add_argument("--no-dedupe", action="store_true", help="不跳过重复项")
    p.set_defaults(func=cmd_import)

    p = _sub(sub, "dedupe", help="扫描 / 合并重复凭据")
    p.add_argument("--type", help="只扫描该插件类型")
    p.add_argument("--group", help="只扫描该分组")
    p.add_argument("--keep", help="合并时保留的凭据 ID/名称")
    p.add_argument("--remove", action="append", default=[],
                   help="合并时删除的凭据 ID/名称，可重复")
    p.add_argument("--merge-notes", action="store_true", help="把备注合并到保留项")
    p.set_defaults(func=cmd_dedupe)

    p = _sub(sub, "self-update", help="检查 / 安装程序自更新")
    usub = p.add_subparsers(dest="update_command", metavar="<操作>", required=True)
    g = _sub(usub, "check", help="检查是否有新版本")
    g.add_argument("--test", action="store_true", help="检查测试版（远端 main 分支）")
    g.set_defaults(func=cmd_update_check)
    g = _sub(usub, "status", help="查看最近的更新状态")
    g.set_defaults(func=cmd_update_status)
    g = _sub(usub, "install", help="下载/构建并安装更新（完成后交棒）")
    g.add_argument("--test", action="store_true", help="安装测试版（从源码构建）")
    g.add_argument("--no-restart", action="store_true", help="安装后不自动重启")
    g.add_argument("--no-frontend", action="store_true",
                   help="测试版构建时跳过前端构建（沿用现有 dist）")
    g.set_defaults(func=cmd_update_install)

    p = _sub(sub, "logs", help="查看日志")
    p.add_argument("--limit", type=int, default=100, help="最多读取多少条（默认 100）")
    p.add_argument("--level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                   help="最低日志级别")
    p.add_argument("--search", help="关键词（多个词需全部命中）")
    p.set_defaults(func=cmd_logs)

    p = _sub(sub, "version", help="显示版本", no_init=True)
    p.set_defaults(func=cmd_version)

    return parser


def _ensure_console() -> None:
    """Windows 桌面版是窗口化 exe（console=False）：从终端调用 `Keyrealm --cli` 时
    默认没有 stdout/stdin。这里附着到父控制台，命令行输出与密码输入才能正常工作。
    CLI 版本身是控制台程序，不会走到这条路径。
    """
    if os.name != "nt" or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - 没有父控制台时静默降级
        pass
    # 仍然没有输出流（无父控制台）时指向空设备，避免 print 抛 AttributeError
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：解析参数、初始化核心服务、执行并返回退出码。"""
    _ensure_console()
    # Windows 控制台/管道默认可能是 GBK：输出中文与 ✓/✗ 时容易 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 无 stdout 等场景忽略
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    # data-dir 命令只读写配置文件，不应打开数据库 / 初始化日志目录
    if not getattr(args, "no_init", False):
        # 更新安装/迁移期间并发写库会与收尾进程抢同一个 SQLite：先拒绝，稍后再试。
        if updater.update_in_progress():
            print("更新正在进行中（安装或数据库迁移），请稍后再试。", file=sys.stderr)
            return 1
        bootstrap.init_core("cli", full=False)
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - 命令行给一行原因，默认不甩 traceback
        if os.environ.get("KEYREALM_DEBUG"):
            raise
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
