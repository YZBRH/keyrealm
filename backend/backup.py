"""备份 / 恢复：导出为单个 JSON 文件，可选加密（默认）或明文。

- **加密导出**：敏感字段直接沿用库中密文（`data_encrypted`），全程不落明文，
  但只能用**同一主密码**导入。
- **明文导出**：敏感字段以明文写入文件（敏感，慎用），可在任意主密码的库上导入。

导入是**填充式**的：以当前库（本质是各插件的 `fields_schema`）为蓝本，把备份条目里的值
往这个蓝本里填。因此条目要满足三条才导得进去（不满足即整条判失败，见下）：

1. 插件**存在且启用**——缺插件或插件被禁用都不导（禁用即功能闸门关闭，导进去也用不了）；
2. 备份里的字段**都能在当前插件里找到**——多出未知字段说明两边同构性已经不成立，
   与其猜它的含义，不如让用户自己看一眼。**保留键例外**（`_proxy_mode` / `_proxy_id` /
   `_tls_version`，见 `preset_store.RESERVED_KEYS`）：它们是凭据数据的一部分、却不由任何
   manifest 字段声明，不豁免的话**每条经表单存过的凭据都会因 `_proxy_mode` 被判失败**；
3. 插件的**必填字段在备份里有着落**——备份里没有、且插件也没给默认值的必填项，
   填进去就是一条注定验证失败的凭据。反之，插件新增的**选填**字段一律留空，
   这正是「库扩增后新列留空」的落点。

判定失败的条目不会被写入，但会逐条记下原因，并落到 `data/logs/` 下两份文件：
完整导入日志（含成功条目，敏感值脱敏）与导入失败日志（仅失败条目，**含明文**，供人工比对）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import data_upgrades
from . import database as db
from . import plugin_manager
from . import preset_store
from .crypto import decrypt, decrypt_dict, encrypt, encrypt_dict, mask_secret
from .logger import LOG_DIR

FORMAT = "credential-vault-backup"
VERSION = 3      # v3：MCP Token 不再单列（它现在是 settings 里的两条键，随 settings 段搬运）

# 不进 settings 段的键：
# - `schema_version` 是**结构**版本，跟着库走不合适；
# - `salt` / `verify_token` 归入 `vault` 段——**加密导出的可独立还原全靠它**：
#   没有 salt，即使原库丢了、主密码也对，也派生不出那把 key，密文永远是密文。
_SETTINGS_EXCLUDE = {"schema_version", "salt", "verify_token"}

# 「值本身是密文」的设置项：导出原样带；导入时要用**源 key** 解开、再用**当前 key** 重加密，
# 否则换库后这个值就是一段解不开的废密文。
_ENCRYPTED_SETTINGS = ("update_token",)

# v1 时代只备份这几个偏好键；v2 起 `settings` 段是全量，读取时仍兼容老文件的 `prefs`
_LEGACY_PREF_KEYS = ("verify_proxy", "global_proxy_id", "log_level", "close_action", "auto_lock_minutes")

# 返回给界面的失败条数上限（日志文件里是全量；IPC 载荷没必要跟着条目数一起涨）
_MAX_REPORTED_FAILURES = 200


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_keys(credential_type: str) -> set:
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return set()
    return {f["key"] for f in record.get("fields_schema", []) if f.get("sensitive")}


def _dedupe_key(credential_type: str, data: dict) -> tuple:
    """一条凭据的「身份」，用于重复判定。

    默认取**敏感字段的值**——敏感值相同基本就等同于同一把钥匙。但敏感值为空的情况很常见
    （插件压根没有敏感字段，或这条凭据没填任何敏感字段），此时敏感身份是空集：要么所有条目
    都算重复（会丢数据），要么都不算（导入两次就翻一倍）。所以**拿不到敏感身份就退化为整条
    记录做身份**——完全相同才算重复，这正是「重复导入同一份备份不出双份」需要的语义。
    """
    sens = _sensitive_keys(credential_type)
    key = tuple(sorted(f"{k}={data[k]}" for k in sens if data.get(k)))
    if key:
        return key
    return tuple(sorted(f"{k}={data[k]}" for k in data if data.get(k)))


def _field_visible(field: dict, values: dict) -> bool:
    """字段此刻该不该显示（manifest 的 ``show_if`` 条件显隐）。

    与前端 `frontend/src/utils/format.js` 的 `fieldVisible` 逐字同义——两边判据若不一致，
    会出现「表单里必填的字段导入时却不算数」这种对不上的怪事。
    条件形如 ``{"kind": "app"}`` 或 ``{"auth_mode": ["api_key", "chatgpt"]}``。
    """
    cond = field.get("show_if")
    if not isinstance(cond, dict) or not cond:
        return True
    for key, want in cond.items():
        allowed = want if isinstance(want, list) else [want]
        if not any(str(values.get(key, "") or "") == str(v) for v in allowed):
            return False
    return True


def _prepare_entry(schema: list, data: dict) -> tuple:
    """按当前插件的字段表规整一条备份数据，返回 ``(effective, errors)``。

    - 备份里有、字段表里没有的键 → 记错误（未知字段）；**保留键除外**（见下）；
    - 字段表里有、备份里没有的键 → 留空；**必填项**若带 default 则用 default 补上
      （插件后加的「模式选择」这类字段就是这种形态：备份里没有，但默认值让它依然可用）；
    - 补完默认值后再逐个核对必填项，仍为空的就是错误。

    ``effective`` 即使存在错误也照常返回，供失败日志把「原样 + 补过的默认值」一并打印。
    """
    keys = {f.get("key") for f in schema if f.get("key")}
    errors: list[str] = []

    # 保留键（代理 / TLS 版本）不是 manifest 字段，但确实属于凭据数据——从「未知字段」里豁免。
    unknown = sorted(k for k in data if k not in keys and k not in preset_store.RESERVED_KEYS)
    if unknown:
        errors.append("备份中的字段在当前插件中不存在：" + "、".join(unknown))

    effective = dict(data)
    # 两遍：先按字段表顺序补默认值（后面的字段可能依赖前面刚补上的值判 show_if），
    # 再统一核对必填。字段表通常一次就够，第二遍是给「条件字段也带默认值」留的余量。
    for _ in range(2):
        for f in schema:
            key = f.get("key")
            if not key or not f.get("required") or f.get("default") is None:
                continue
            if str(effective.get(key, "") or "").strip():
                continue
            if not _field_visible(f, effective):
                continue
            effective[key] = f["default"]

    missing = [
        f.get("label") or f.get("key")
        for f in schema
        if f.get("required")
        and _field_visible(f, effective)
        and not str(effective.get(f.get("key"), "") or "").strip()
    ]
    if missing:
        errors.append("缺少必填字段：" + "、".join(str(m) for m in missing))

    return effective, errors


def _fname_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _mask_value(value: Any) -> str:
    """完整日志里的字段值：敏感值走脱敏，其余照原样（它们本就以明文存在 data_plain 里）。"""
    return mask_secret(str(value))


def _write_import_logs(records: list[dict], *, encrypted: bool, source: str,
                       counts: dict) -> tuple:
    """写出完整导入日志与（有失败时的）失败日志，返回 ``(log_path, failed_log_path)``。

    日志是**普通文本文件**（与运行日志同目录），不用 logging：它是一次性的、条目化的审计记录，
    混进滚动日志只会把两者都搅浑。失败日志含明文，故以 0600 权限落盘。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _fname_stamp()
    log_path = LOG_DIR / f"import_{stamp}.log"
    failed_path = LOG_DIR / f"import_{stamp}_failed.log"

    head = [
        "Keyrealm 备份导入日志",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"源文件：{source or '（未记录）'}",
        f"备份加密：{'是' if encrypted else '否'}",
        f"条目总数：{counts['total']}　成功：{counts['imported']}　"
        f"重复跳过：{counts['duplicate']}　失败：{counts['failed']}",
        "",
        "说明：本日志中敏感字段已脱敏（保留首尾少量字符用于辨识）。",
        "      需要明文比对请查看同目录下的导入失败日志。",
        "",
        "=" * 78,
        "",
    ]
    body = [_render_record(rec, masked=True) for rec in records]
    log_path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    _restrict(log_path)

    if not counts["failed"]:
        return str(log_path), None

    failed_head = [
        "Keyrealm 备份导入失败日志",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"源文件：{source or '（未记录）'}",
        f"失败条数：{counts['failed']}",
        "",
        "⚠ 本文件包含未脱敏的凭据明文，仅用于人工比对与手动处理，请勿外传，用后请删除。",
        "",
        "=" * 78,
        "",
    ]
    failed_body = [_render_record(rec, masked=False) for rec in records if rec["status"] == "failed"]
    failed_path.write_text("\n".join(failed_head + failed_body) + "\n", encoding="utf-8")
    _restrict(failed_path)
    return str(log_path), str(failed_path)


def _restrict(path) -> None:
    """尽力把日志文件权限收成仅属主可读写（Windows 上 chmod 基本无效，忽略即可）。"""
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass


def _render_record(rec: dict, *, masked: bool) -> str:
    lines = [f"[{rec['index']}] {rec['name'] or '（无名称）'}"
             f"　类型：{rec['credential_type'] or '（缺失）'}"
             f"　分组：{rec['group'] or '（未分组）'}"]
    lines.append(f"    结果：{_STATUS_TEXT.get(rec['status'], rec['status'])}")
    for reason in rec.get("reasons") or []:
        lines.append(f"    - {reason}")
    data = rec.get("data") or {}
    known_sensitive: Optional[set] = rec.get("sensitive")
    if data:
        lines.append("    字段：")
        for key in sorted(data):
            value = data[key]
            # 完整日志要脱敏；但「哪些字段算敏感」来自插件字段表——插件没装上就没有字段表，
            # 这时**一律脱敏**（拿不准就不要落明文，这正是失败日志存在的理由）。
            if masked and (known_sensitive is None or key in known_sensitive):
                shown = _mask_value(value)
            else:
                shown = str(value)
            lines.append(f"      {key} = {shown}")
    else:
        lines.append("    字段：（无）")
    lines.append("")
    return "\n".join(lines)


_STATUS_TEXT = {
    "imported": "已导入",
    "duplicate": "重复，已跳过",
    "failed": "导入失败",
}


def build_backup(
    key: bytes,
    scope: str = "all",
    encrypted: bool = True,
    credential_ids: Optional[list] = None,
) -> dict:
    """构建备份文档。

    - ``scope``：``'all'``（凭据 + 分组 + 偏好设置）/ ``'credentials'``（只有凭据）
    - ``credential_ids``：只导出这些凭据（``None`` = 全部）。**不影响** scope 的语义，
      所以「全部 + 选中若干」与「仅凭据 + 选中若干」的差别仍然只是分组/偏好设置。
    """
    groups = {g["id"]: g["name"] for g in db.list_groups()}
    selected: Optional[set] = None
    if credential_ids is not None:
        selected = {int(cid) for cid in credential_ids}

    entries: list[dict] = []
    for cred in db.list_credentials():
        if selected is not None and cred["id"] not in selected:
            continue
        entry: dict[str, Any] = {
            "name": cred["name"],
            "credential_type": cred["credential_type"],
            "group": groups.get(cred["group_id"]),
            "data_plain": cred.get("data_plain") or {},
        }
        if encrypted:
            entry["data_encrypted"] = cred["data_encrypted"]
        else:
            try:
                entry["data"] = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                entry["data"] = {}
        entries.append(entry)

    doc: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": _iso(),
        "scope": scope,
        "encrypted": bool(encrypted),
        "counts": {"credentials": len(entries)},
        "credentials": entries,
    }
    if selected is not None:
        doc["credential_ids"] = sorted(selected)
    if scope == "all":
        doc["groups"] = [
            {"name": g["name"], "description": g.get("description", ""), "color": g.get("color", "#409EFF")}
            for g in db.list_groups()
        ]
        # **KDF 材料**：让加密导出能独立还原（原库丢了也解得开）
        doc["vault"] = {
            "salt": db.get_setting("salt", ""),
            "verify_token": db.get_setting("verify_token", ""),
        }
        # **全部设置**（除结构版本与上面归入 vault 的两项）——新增设置项会自动跟着走
        doc["settings"] = {k: v for k, v in db.all_settings().items() if k not in _SETTINGS_EXCLUDE}
        # 代理注册表与类别/插件默认设置。代理**带上 id**（导入侧据此把凭据/预设里引用的
        # `_proxy_id` 改写成本库的 id）；地址与名字照旧，便于人读。
        doc["proxies"] = [
            {"id": p["id"], "name": p["name"], "url": p["url"], "sort_order": p.get("sort_order", 0)}
            for p in db.list_proxies()
        ]
        doc["preset_defaults"] = [
            {"scope": r["scope"], "scope_key": r["scope_key"], "values": r.get("payload") or "{}"}
            for r in db.list_preset_defaults()
        ]
        doc["schedules"] = [_schedule_doc(s) for s in db.list_schedules()]
        doc["mcp_permissions"] = [
            {"tool_name": p["tool_name"], "enabled": bool(p["enabled"])}
            for p in db.list_mcp_permissions()
        ]
        # MCP Token 不再单独成段：它现在就是 settings 里的两条键（`mcp_token_hash` / 前缀），
        # 随下面的 settings 段一起搬运 —— 跨库还原后同一个 bearer token 继续可用。
        doc["plugins_disabled"] = plugin_manager.disabled_types()
    return doc


def _schedule_doc(s: dict) -> dict:
    """定时计划的可搬运表示（**不带** last_run_at / next_run_at——那两个是运行态派生值）。"""
    return {
        "name": s.get("name"),
        "trigger_type": s.get("trigger_type"),
        "trigger_args": s.get("trigger_args") or {},
        "target_scope": s.get("target_scope") or "all",
        "target_id": s.get("target_id"),
        "credential_ids": s.get("credential_ids") or [],
        "enabled": bool(s.get("enabled")),
    }


def import_backup(key: bytes, doc: dict, dedupe: bool = True, source: str = "",
                  decrypt_key: Optional[bytes] = None) -> dict:
    """把备份文档**填充式**导入当前库，返回统计与逐条失败原因。

    ``source`` 只用于日志抬头（记一下这份备份是从哪个文件来的），不影响导入逻辑。

    ``decrypt_key``：**解密这份文档用的 key**。缺省 = ``key``（当前库的 key，即同一保险库）。
    文档来自**别的保险库**（v2 起带 ``vault.salt``）时，调用方先用「原主密码 + 文档里的 salt」
    派生出源 key 传进来——这样密文解得开、再用当前库的 key 重新加密写回，跨库还原才成立。
    加密备份解不开时直接抛 ``ValueError`` 中止整个导入（说明拿错了密码/文件，逐条报失败没意义）。
    """
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("不是有效的 Keyrealm 备份文件")

    encrypted = bool(doc.get("encrypted"))
    src_key = decrypt_key or key                 # 解开文档里密文用的 key
    entries = doc.get("credentials") or []

    # 1) 分组：按名称补建
    created_groups = 0
    name_to_id = {g["name"]: g["id"] for g in db.list_groups()}
    for g in doc.get("groups") or []:
        name = (g.get("name") or "").strip()
        if name and name not in name_to_id:
            ng = db.create_group(name, g.get("description", ""), g.get("color", "#409EFF"))
            name_to_id[name] = ng["id"]
            created_groups += 1

    # 1.5) 代理注册表与默认设置（仅整库备份带这两个 section）
    #      代理 id 是 **UUID**，跨库稳定 —— 导入时**原样沿用源库 id**，凭据/预设里的引用便自动继续有效。
    #      （老备份里的自增整数 id 沿用不了：新表 id 是 TEXT，那类引用会解析成「未配置」并回落直连，
    #       运行时有 WARNING 点名。）
    created_proxies = 0
    known_ids = {p["id"] for p in db.list_proxies()}
    for item in doc.get("proxies") or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        pid = str(item.get("id") or "").strip()
        if pid and pid in known_ids:
            continue
        # 有 id 就沿用（保证引用不断）；老备份没有 id 则现生成一个，引用只能靠用户重选
        db.create_proxy(str(item.get("name") or url), url, pid or None)
        if pid:
            known_ids.add(pid)
        created_proxies += 1

    for item in doc.get("preset_defaults") or []:
        scope = item.get("scope")
        scope_key = item.get("scope_key")
        if scope in ("category", "plugin") and scope_key:
            try:
                values = json.loads(item.get("values") or "{}")
            except Exception:
                continue
            if isinstance(values, dict):
                db.set_preset_defaults(scope, scope_key, values)

    # 2) 现有凭据的敏感值索引（去重）
    existing: dict[str, set] = {}
    if dedupe:
        for cred in db.list_credentials():
            try:
                data = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                continue
            existing.setdefault(cred["credential_type"], set()).add(
                _dedupe_key(cred["credential_type"], data)
            )

    imported = skipped_plugin = skipped_dup = failed = 0
    records: list[dict] = []

    for idx, row in enumerate(entries, start=1):
        if not isinstance(row, dict):
            continue
        ct = str(row.get("credential_type") or "")
        rec: dict[str, Any] = {
            "index": idx,
            "name": str(row.get("name") or ""),
            "credential_type": ct,
            "group": str(row.get("group") or ""),
            "status": "failed",
            "reasons": [],
            "data": {},
            # None = 该条目还没走到字段表（插件缺失/禁用），此时脱敏按「全都敏感」处理
            "sensitive": None,
        }
        records.append(rec)

        # 取明文：加密备份用主密码解。解不开说明密码不对，整体中止。
        if encrypted:
            blob = row.get("data_encrypted")
            if not blob:
                rec["reasons"].append("备份条目缺少加密数据（data_encrypted）")
                failed += 1
                continue
            try:
                data = decrypt_dict(src_key, blob)
            except Exception:
                raise ValueError("备份是加密的，且与提供的密码不匹配，无法导入")
        else:
            data = row.get("data") or {}
        rec["data"] = data

        # 填充式导入的蓝本 = 当前插件。插件不认识这条凭据，就无从谈「往哪儿填」。
        plugin = db.get_plugin_by_type(ct)
        if not plugin:
            rec["reasons"].append(f"插件未安装：{ct}")
            skipped_plugin += 1
            failed += 1
            continue
        if not plugin_manager.is_plugin_enabled(ct):
            rec["reasons"].append(f"插件已禁用：{plugin.get('name') or ct}（{ct}）")
            skipped_plugin += 1
            failed += 1
            continue

        schema = plugin.get("fields_schema") or []
        rec["sensitive"] = {f["key"] for f in schema if f.get("sensitive")}
        # 老格式 → 当前格式（一次性数据升级，规则见 backend/data_upgrades.py）：
        # 先升级再校验，于是导入进来的旧备份直接就是当前格式，插件无需做双格式兼容。
        data = data_upgrades.upgrade_credential(ct, data or {})
        effective, errors = _prepare_entry(schema, data)
        rec["data"] = effective
        if errors:
            rec["reasons"].extend(errors)
            failed += 1
            continue

        # 字段表形状之外再做一遍取值校验（select 选项 / number 类型）。
        # 未知字段已由 _prepare_entry 报过，这里不重复检查。
        value_errors = plugin_manager.validate_field_values(ct, effective, check_unknown=False)
        if value_errors:
            rec["reasons"].extend(value_errors)
            failed += 1
            continue

        values = _dedupe_key(ct, effective)
        if dedupe and values and values in existing.get(ct, set()):
            rec["status"] = "duplicate"
            skipped_dup += 1
            continue

        db.create_credential(
            name=row.get("name") or "（导入）",
            credential_type=ct,
            data_encrypted=encrypt_dict(key, effective),
            data_plain={k: v for k, v in effective.items() if k not in rec["sensitive"]},
            group_id=name_to_id.get(row.get("group") or ""),
        )
        if dedupe and values:
            existing.setdefault(ct, set()).add(values)
        rec["status"] = "imported"
        imported += 1

    # 3) 设置 / 定时计划 / MCP / 插件启停（仅整库范围）
    restored = {"settings": 0, "schedules": 0, "mcp_permissions": 0,
                "plugins_disabled": 0}
    if doc.get("scope") == "all":
        restored["settings"] = _import_settings(doc, src_key, key)
        restored["schedules"] = _import_schedules(doc)
        restored["mcp_permissions"] = _import_mcp_permissions(doc)
        restored["plugins_disabled"] = _import_plugins_disabled(doc)

    # 4) 落两份日志（无论成败都写完整日志；有失败才多写一份含明文的）
    counts = {
        "total": len(records),
        "imported": imported,
        "duplicate": skipped_dup,
        "failed": failed,
    }
    try:
        log_file, failed_log_file = _write_import_logs(
            records, encrypted=encrypted, source=source, counts=counts
        )
    except Exception as exc:  # noqa: BLE001 - 日志写不出来不该让已入库的数据「白导」
        log_file, failed_log_file = "", ""
        counts["log_error"] = f"导入日志写入失败：{exc}"

    failed_items = [
        {"name": r["name"], "credential_type": r["credential_type"], "reason": "；".join(r["reasons"])}
        for r in records
        if r["status"] == "failed"
    ]

    return {
        "ok": True,
        "encrypted": encrypted,
        "imported": imported,
        # 保持旧键名与旧含义（因插件缺失而没导进去的条数），现在把「插件已禁用」也算进来
        "skipped_plugin": skipped_plugin,
        "skipped_duplicate": skipped_dup,
        "failed_count": failed,
        "failed": failed_items[:_MAX_REPORTED_FAILURES],
        "failed_truncated": max(0, len(failed_items) - _MAX_REPORTED_FAILURES),
        "created_groups": created_groups,
        "created_proxies": created_proxies,
        "restored": restored,
        "log_file": log_file,
        "failed_log_file": failed_log_file,
    }


# ── 整库档的其余 section 恢复（v2）──────────────────────────

def _import_settings(doc: dict, src_key: bytes, key: bytes) -> int:
    """恢复设置项。v2 用 `settings`（**全量**）；v1 老文件只有 `prefs`（少数几个）。"""
    settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else None
    if settings is None:                       # v1 兼容
        prefs = doc.get("prefs") if isinstance(doc.get("prefs"), dict) else {}
        settings = {k: prefs.get(k, "") for k in _LEGACY_PREF_KEYS if k in prefs}
    count = 0
    for k, v in settings.items():
        k = str(k)
        if k in _SETTINGS_EXCLUDE:
            continue                           # 结构版本 / KDF 材料：**绝不覆盖**当前库
        value = str(v if v is not None else "")
        if k in _ENCRYPTED_SETTINGS:
            # 值是密文：先按源 key 解、再按当前 key 重加密，换库后才用得了。
            # 注意这些设置项存的是**原始字符串**的密文（不是 encrypt_dict 的 JSON），故用 decrypt/encrypt。
            try:
                value = encrypt(key, decrypt(src_key, value))
            except Exception:
                continue                       # 解不开就跳过，别塞一段废密文进去
        db.set_setting(k, value)
        count += 1
    return count


def _import_schedules(doc: dict) -> int:
    """恢复定时计划（同名视为已有，不重复建）。"""
    existing = {s.get("name") or "" for s in db.list_schedules()}
    count = 0
    for s in doc.get("schedules") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not name or name in existing:
            continue
        try:
            created = db.create_schedule(
                name=name,
                trigger_type=str(s.get("trigger_type") or "cron"),
                trigger_args=s.get("trigger_args") or {},
                target_scope=str(s.get("target_scope") or "all"),
                target_id=s.get("target_id"),
                credential_ids=s.get("credential_ids") or [],
            )
            if not s.get("enabled", True):
                db.update_schedule(created["id"], enabled=0)
            existing.add(name)
            count += 1
        except Exception:  # noqa: BLE001
            continue


    return count


def _import_mcp_permissions(doc: dict) -> int:
    count = 0
    for p in doc.get("mcp_permissions") or []:
        name = str((p or {}).get("tool_name") or "").strip()
        if not name:
            continue
        try:
            db.set_mcp_permission(name, bool(p.get("enabled")))
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count

def _import_plugins_disabled(doc: dict) -> int:
    types = doc.get("plugins_disabled")
    if not isinstance(types, list):
        return 0
    try:
        plugin_manager.set_disabled(types)
    except Exception:  # noqa: BLE001
        return 0
    return len(types)


def read_backup_file(path: str) -> Optional[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_backup_file(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
