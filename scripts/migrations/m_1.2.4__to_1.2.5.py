"""1.2.4 → 1.2.5：插件类型 api_baidu_face 改名为 api_baidu_ai。

纯明文元数据迁移，不需要主密码；凭据的敏感字段仍是密文，原样保留。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

FROM_VERSION = "1.2.4"
TO_VERSION = "1.2.5"

OLD_TYPE = "api_baidu_face"
NEW_TYPE = "api_baidu_ai"

_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_FILE = _ROOT / "plugins" / ".state.json"
_log = logging.getLogger(__name__)


def _count(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def preview(conn) -> dict:
    return {
        "credentials": _count(
            conn, "SELECT COUNT(*) FROM credentials WHERE credential_type=?", (OLD_TYPE,)),
        "schedules_type": _count(
            conn,
            "SELECT COUNT(*) FROM schedules WHERE target_scope='type' AND target_id=?",
            (OLD_TYPE,)),
        "preset_plugin": _count(
            conn,
            "SELECT COUNT(*) FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
            (OLD_TYPE,)),
        "plugin_rows": [
            row[0] for row in conn.execute(
                "SELECT credential_type FROM plugins WHERE credential_type IN (?,?)",
                (OLD_TYPE, NEW_TYPE))
        ],
        "state_file": _STATE_FILE.is_file(),
    }


def _merge_preset(conn) -> None:
    """preset_defaults 主键是 (scope, scope_key)：旧行存在时合并到新行，新值优先。"""
    old = conn.execute(
        "SELECT payload FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
        (OLD_TYPE,)).fetchone()
    if not old:
        return
    new = conn.execute(
        "SELECT payload FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
        (NEW_TYPE,)).fetchone()
    if not new:
        conn.execute(
            "UPDATE preset_defaults SET scope_key=? "
            "WHERE scope='plugin' AND scope_key=?",
            (NEW_TYPE, OLD_TYPE))
        return

    def load(raw) -> dict:
        try:
            value = json.loads(raw or "{}")
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    merged = {**load(old[0]), **load(new[0])}
    conn.execute(
        "UPDATE preset_defaults SET payload=?, updated_at=CURRENT_TIMESTAMP "
        "WHERE scope='plugin' AND scope_key=?",
        (json.dumps(merged, ensure_ascii=False), NEW_TYPE))
    conn.execute(
        "DELETE FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
        (OLD_TYPE,))


def _rename_state_file() -> None:
    """plugins/.state.json 的 disabled：旧 id → 新 id、去重、原子替换。"""
    if not _STATE_FILE.is_file():
        return
    try:
        doc = json.loads(_STATE_FILE.read_text(encoding="utf-8") or "{}")
    except Exception:
        return
    disabled = doc.get("disabled") if isinstance(doc, dict) else None
    if not isinstance(disabled, list):
        return
    out, changed = [], False
    for item in disabled:
        mapped = NEW_TYPE if item == OLD_TYPE else item
        changed = changed or (mapped != item)
        if mapped not in out:
            out.append(mapped)
    if not changed:
        return
    doc["disabled"] = out
    tmp = _STATE_FILE.with_name(_STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def apply(conn) -> None:
    # 1) 凭据主表（密文数据不动）
    conn.execute(
        "UPDATE credentials SET credential_type=? WHERE credential_type=?",
        (NEW_TYPE, OLD_TYPE))

    # 2) 插件镜像表：新行已存在则删旧行，否则改名；字段表由启动时 sync_plugins 刷新
    conn.execute(
        "DELETE FROM plugins WHERE credential_type=? AND EXISTS "
        "(SELECT 1 FROM plugins WHERE credential_type=?)",
        (OLD_TYPE, NEW_TYPE))
    conn.execute(
        "UPDATE plugins SET credential_type=?, name=?, version=? "
        "WHERE credential_type=?",
        (NEW_TYPE, "百度 AI 开放平台", "1.1.0", OLD_TYPE))

    # 3) 按类型筛选的定时计划（target_id 是 SQLite 多态列，文本原样存）
    conn.execute(
        "UPDATE schedules SET target_id=? "
        "WHERE target_scope='type' AND target_id=?",
        (NEW_TYPE, OLD_TYPE))

    # 4) 插件级默认设置（主键冲突时合并，新值优先）
    _merge_preset(conn)

    # 5) 文件状态最后处理；失败只告警，不拖垮 DB 事务
    try:
        _rename_state_file()
    except Exception as exc:  # noqa: BLE001
        _log.warning("迁移 .state.json 失败（禁用状态可能被重置）：%s", exc)
