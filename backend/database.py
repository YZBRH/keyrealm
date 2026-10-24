"""
SQLite 数据访问层
数据库位置：<data_dir>/credentials.db
（默认 <程序入口>/data；可通过 keyrealm.conf.json 或 CLI --data-dir 改到别处）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, Union

from .paths import data_dir
# 版本号唯一事实来源见 backend/version.py（不再在这里写常量）
from .version import LEGACY_SCHEMA_MAX, SCHEMA_VERSION  # noqa: F401  （再导出）

# 数据目录（默认入口目录/data；启动早期由 paths.configure_data_dir 决定）
BASE_DIR = data_dir()
DATA_DIR = BASE_DIR
DB_PATH = DATA_DIR / "credentials.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#409EFF',
            sort_order  INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS credentials (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            credential_type  TEXT NOT NULL,
            group_id         INTEGER,
            data_encrypted   TEXT NOT NULL,
            data_plain       TEXT DEFAULT '{}',
            status           TEXT DEFAULT 'unknown',
            status_detail    TEXT DEFAULT '{}',
            last_verified_at TIMESTAMP,
            last_valid_at    TIMESTAMP,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_credentials_type   ON credentials(credential_type);
        CREATE INDEX IF NOT EXISTS idx_credentials_group  ON credentials(group_id);
        CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials(status);

        CREATE TABLE IF NOT EXISTS plugins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            version         TEXT NOT NULL,
            author          TEXT DEFAULT '',
            description     TEXT DEFAULT '',
            credential_type TEXT NOT NULL UNIQUE,
            icon            TEXT DEFAULT '🔑',
            fields_schema   TEXT NOT NULL,
            plugin_path     TEXT NOT NULL,
            category        TEXT DEFAULT '',
            category_name   TEXT DEFAULT '',
            enabled         INTEGER DEFAULT 1,
            batch_verify    INTEGER DEFAULT 1,
            installed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS mcp_permissions (
            tool_name   TEXT PRIMARY KEY,
            enabled     INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            trigger_type  TEXT NOT NULL,
            trigger_args  TEXT NOT NULL,
            target_scope  TEXT DEFAULT 'all',
            target_id     INTEGER,
            -- scope='credentials' 时勾选的具体凭据 id，JSON 数组；其它 scope 为空数组
            credential_ids TEXT DEFAULT '[]',
            enabled       INTEGER DEFAULT 1,
            last_run_at   TIMESTAMP,
            next_run_at   TIMESTAMP,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 代理注册表：可维护多个代理、各自取名，其它地方一律按 id 引用。
        -- id 用 **UUID 而不是自增整数**：自增 id 只在单个库里唯一，一旦导出/导入（代理表是
        -- 「按地址去重」重建的）就对不上，凭据与预设里存的引用会**静默悬空 → 回落直连**，
        -- 界面上却看不出任何异常。UUID 跨库稳定，导入时原样沿用即可。
        CREATE TABLE IF NOT EXISTS proxies (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 类别级 / 插件级默认设置：新建凭据时按「插件 > 类别」继承（payload 是 JSON）
        -- 注意列名不能叫 values —— 那是 SQLite 的保留字，建表就会 syntax error
        CREATE TABLE IF NOT EXISTS preset_defaults (
            scope       TEXT NOT NULL,
            scope_key   TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (scope, scope_key)
        );
    """)
    conn.commit()
    _seed_mcp_permissions(conn)
    _migrate(conn)
    _set_schema_version(conn)


def _migrate(conn: sqlite3.Connection):
    """按 schema_version 执行增量迁移（在建表之后调用，ALTER 均有存在性检查）。

    **这几段只服务 1.0 之前的旧库，已冻结。** 1.0 起，数据库结构变动一律改成
    `scripts/` 下的一次性迁移脚本（见 backend/version.py 的版本规则），应用不再新增自动迁移。
    """
    # 1.0 之前 schema_version 是整数（0..6）；1.0 起是语义化版本（如 "1.0.0"）。
    # 读到**非整数**即说明是新格式 → 旧的整数迁移早已全部执行过，直接视为已过。
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        from_version = int(row["value"]) if row else 0
    except (TypeError, ValueError):
        from_version = LEGACY_SCHEMA_MAX
    except Exception:
        from_version = 0

    if from_version < 2:
        # v2: credentials.last_valid_at、plugins.category/category_name
        cred_cols = {r["name"] for r in conn.execute("PRAGMA table_info(credentials)")}
        if "last_valid_at" not in cred_cols:
            conn.execute("ALTER TABLE credentials ADD COLUMN last_valid_at TIMESTAMP")

        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "category" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN category TEXT DEFAULT ''")
        if "category_name" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN category_name TEXT DEFAULT ''")

        # 最后有效时间回填：当前状态为有效的凭据，以最后验证时间近似
        conn.execute(
            "UPDATE credentials SET last_valid_at=last_verified_at "
            "WHERE status='valid' AND last_valid_at IS NULL"
        )
        conn.commit()

    if from_version < 3:
        # v3: plugins.batch_verify —— 无法自动化验证或有写副作用的插件禁用批量验证
        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "batch_verify" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN batch_verify INTEGER DEFAULT 1")
        conn.commit()

    if from_version < 4:
        # v4: 插件不再区分内置/外置，移除 plugins.is_builtin
        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "is_builtin" in plugin_cols:
            conn.execute("ALTER TABLE plugins DROP COLUMN is_builtin")
        conn.commit()

    if from_version < 5:
        # v5: 代理 id 由自增整数改成 UUID（见建表处的注释）。
        # 旧表 id 是 ``INTEGER PRIMARY KEY``（rowid 别名），**塞不进 uuid 文本**，必须重建。
        # 1.0 前不写数据迁移：旧的代理行直接丢弃，重加一次即可；引用旧 id 的凭据/预设
        # 会解析成「未配置」→ 回落直连，运行时有 WARNING 点名（见 plugin_manager._log_proxy_outlet）。
        cols = {r["name"]: str(r["type"] or "").upper() for r in conn.execute("PRAGMA table_info(proxies)")}
        if cols and cols.get("id") != "TEXT":
            conn.execute("DROP TABLE proxies")
            conn.executescript("""
                CREATE TABLE proxies (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    sort_order  INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("UPDATE settings SET value='' WHERE key='global_proxy_id'")
        conn.commit()

    if from_version < 6:
        # v6: schedules.credential_ids —— 定时计划可以只验证**勾选的具体凭据**
        sched_cols = {r["name"] for r in conn.execute("PRAGMA table_info(schedules)")}
        if "credential_ids" not in sched_cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN credential_ids TEXT DEFAULT '[]'")
        conn.commit()


def _seed_mcp_permissions(conn: sqlite3.Connection):
    defaults = [
        ("vault_list_credentials",  1, "列出凭据（不含敏感字段明文）"),
        ("vault_get_credential",    0, "获取单条凭据详情"),
        ("vault_create_credential", 0, "创建凭据"),
        ("vault_update_credential", 0, "修改凭据"),
        ("vault_delete_credential", 0, "删除凭据"),
        ("vault_verify_credential", 1, "触发验证"),
        ("vault_batch_verify",      0, "批量验证"),
        ("vault_list_groups",       1, "列出分组"),
        ("vault_create_group",      0, "创建分组"),
        ("vault_delete_group",      0, "删除分组（组内凭据保留，仅解除归属）"),
        ("vault_list_plugins",      1, "列出插件"),
        ("vault_get_secret",        0, "获取解密后的敏感字段（高危）"),
        ("vault_export_backup",     0, "导出备份（触及整库数据，高危；encrypted=false 会写出明文密钥）"),
        ("vault_import_backup",     0, "导入备份（合并入库，高危：会新增凭据、可能覆盖偏好设置）"),
        # 验证代理管理。只读的列出默认开；其余（改动代理表）默认关。
        # vault_test_proxy 会真的朝外发一次请求、把本机出口暴露给被代理的对端，也让用户显式开。
        ("vault_list_proxies",      1, "列出验证代理与当前全局默认"),
        ("vault_create_proxy",      0, "新增验证代理"),
        ("vault_update_proxy",      0, "修改验证代理"),
        ("vault_delete_proxy",      0, "删除验证代理"),
        ("vault_set_global_proxy",  0, "设置全局默认代理（改为直连也会影响所有跟随全局的凭据）"),
        ("vault_test_proxy",        0, "实测代理连通性（会真实发出一次外网请求）"),
    ]
    # 以 defaults 为准，但**只对「描述」**：
    #   · 新工具   → 插入默认 enabled + 描述；
    #   · 已有工具 → **保留用户设的 enabled**（开关是用户资产，不能被每次启动重置），
    #                仅把 description 同步成源码默认值，让改了文案的旧库能追上。
    #                WHERE 保证文案真的不同时才写、才 bump updated_at。
    for tool, enabled, desc in defaults:
        conn.execute(
            """
            INSERT INTO mcp_permissions (tool_name, enabled, description)
            VALUES (?,?,?)
            ON CONFLICT(tool_name) DO UPDATE SET
                description = excluded.description,
                updated_at  = CURRENT_TIMESTAMP
            WHERE mcp_permissions.description IS NOT excluded.description
            """,
            (tool, enabled, desc),
        )
    # 清理已不存在的工具记录
    placeholders = ",".join("?" for _ in defaults)
    conn.execute(
        f"DELETE FROM mcp_permissions WHERE tool_name NOT IN ({placeholders})",
        [tool for tool, _, _ in defaults],
    )
    conn.commit()


def _set_schema_version(conn: sqlite3.Connection):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('schema_version', ?, ?)",
        (str(SCHEMA_VERSION), _now()),
    )
    conn.commit()


# ── settings ──────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with _lock:
        row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with _lock:
        get_conn().execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
            (key, value, _now()),
        )
        get_conn().commit()


def all_settings() -> dict:
    """全部设置键值（供**全量备份**导出；调用方自己决定排除哪些键）。"""
    with _lock:
        rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── groups ────────────────────────────────────────────────

def list_groups() -> list[dict]:
    with _lock:
        rows = get_conn().execute("""
            SELECT g.*, COUNT(c.id) as credential_count
            FROM groups g
            LEFT JOIN credentials c ON c.group_id = g.id
            GROUP BY g.id
            ORDER BY g.sort_order, g.name
        """).fetchall()
    return [dict(r) for r in rows]


def create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO groups (name, description, color, created_at) VALUES (?,?,?,?)",
            (name, description, color, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def update_group(group_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "description", "color", "sort_order"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return None
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [group_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE groups SET {sets} WHERE id=?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    return dict(row) if row else None


def delete_group(group_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.commit()


# ── credentials ───────────────────────────────────────────

def list_credentials(
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    credential_types: Optional[list] = None,
) -> list[dict]:
    sql = """
        SELECT c.*, g.name as group_name
        FROM credentials c
        LEFT JOIN groups g ON g.id = c.group_id
        WHERE 1=1
    """
    params: list = []
    if group_id is not None:
        sql += " AND c.group_id=?"
        params.append(group_id)
    if credential_type:
        sql += " AND c.credential_type=?"
        params.append(credential_type)
    if credential_types:
        placeholders = ",".join("?" for _ in credential_types)
        sql += f" AND c.credential_type IN ({placeholders})"
        params += list(credential_types)
    if status:
        sql += " AND c.status=?"
        params.append(status)
    if search:
        sql += " AND (c.name LIKE ? OR c.data_plain LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY c.updated_at DESC"
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["data_plain"] = json.loads(d.get("data_plain") or "{}")
        d["status_detail"] = json.loads(d.get("status_detail") or "{}")
        results.append(d)
    return results


def get_credential(cred_id: int) -> Optional[dict]:
    with _lock:
        row = get_conn().execute("""
            SELECT c.*, g.name as group_name
            FROM credentials c
            LEFT JOIN groups g ON g.id = c.group_id
            WHERE c.id=?
        """, (cred_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["data_plain"] = json.loads(d.get("data_plain") or "{}")
    d["status_detail"] = json.loads(d.get("status_detail") or "{}")
    return d


def create_credential(
    name: str,
    credential_type: str,
    data_encrypted: str,
    data_plain: dict,
    group_id: Optional[int] = None,
) -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO credentials
               (name, credential_type, group_id, data_encrypted, data_plain, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, credential_type, group_id, data_encrypted,
             json.dumps(data_plain, ensure_ascii=False), _now(), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM credentials WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["data_plain"] = json.loads(d.get("data_plain") or "{}")
    return d


def update_credential(cred_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "group_id", "data_encrypted", "data_plain", "status", "status_detail",
               "last_verified_at", "last_valid_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "data_plain" in fields and isinstance(fields["data_plain"], dict):
        fields["data_plain"] = json.dumps(fields["data_plain"], ensure_ascii=False)
    if "status_detail" in fields and isinstance(fields["status_detail"], dict):
        fields["status_detail"] = json.dumps(fields["status_detail"], ensure_ascii=False)
    if not fields:
        return None
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [cred_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE credentials SET {sets} WHERE id=?", vals)
        conn.commit()
    return get_credential(cred_id)


def delete_credential(cred_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM credentials WHERE id=?", (cred_id,))
        conn.commit()


def delete_credentials(cred_ids: list[int]) -> int:
    """批量删除，单事务一次提交，返回实际删除条数。

    走参数化占位符（`?,?,…`）而非拼接字面值：ids 全部来自 UI 勾选，但拼字符串
    既易错也容易引入 SQL 注入。空列表直接返回 0（`IN ()` 是语法错误）。
    """
    ids = [int(i) for i in (cred_ids or [])]
    if not ids:
        return 0
    with _lock:
        conn = get_conn()
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(f"DELETE FROM credentials WHERE id IN ({marks})", ids)
        conn.commit()
        return cur.rowcount or 0


# ── plugins ───────────────────────────────────────────────

def list_plugins(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM plugins"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY name"
    with _lock:
        rows = get_conn().execute(sql).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["fields_schema"] = json.loads(d.get("fields_schema") or "[]")
        results.append(d)
    return results


def get_plugin_by_type(credential_type: str) -> Optional[dict]:
    # 不按 enabled 过滤：本函数是「插件是否存在」的唯一判定入口，被备份导入/预设/字段净化
    # 等一批**非插件功能**共用。禁用只是关掉功能闸门（plugin_manager.get_module），
    # 插件元数据仍须可解析——导入时要靠它区分「未安装」与「已禁用」两种失败原因，
    # 并在完整/失败日志里带上正确的插件名与字段表。
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM plugins WHERE credential_type=?",
            (credential_type,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["fields_schema"] = json.loads(d.get("fields_schema") or "[]")
    return d


def upsert_plugin(
    name: str, version: str, credential_type: str, fields_schema: list,
    plugin_path: str, author: str = "", description: str = "",
    icon: str = "🔑",
    category: str = "", category_name: str = "", batch_verify: bool = True,
    enabled: bool = True,
):
    # enabled 只是 plugins/.state.json 的**只读镜像**（由 plugin_manager.sync_plugins 写入），
    # 不参与任何功能判定。默认 True 让不带该参数的调用方（如 scripts/check_schema.py）保持原语义。
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT INTO plugins (name, version, author, description, credential_type,
                                    icon, fields_schema, plugin_path, category, category_name,
                                    batch_verify, enabled, installed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(credential_type) DO UPDATE SET
                 name=excluded.name, version=excluded.version,
                 author=excluded.author, description=excluded.description,
                 icon=excluded.icon, fields_schema=excluded.fields_schema,
                 plugin_path=excluded.plugin_path,
                 category=excluded.category, category_name=excluded.category_name,
                 batch_verify=excluded.batch_verify,
                 enabled=excluded.enabled""",
            (name, version, author, description, credential_type, icon,
             json.dumps(fields_schema, ensure_ascii=False), plugin_path,
             category, category_name or category, int(batch_verify), int(enabled), _now()),
        )
        conn.commit()


def delete_plugin_record(credential_type: str):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM plugins WHERE credential_type=?", (credential_type,))
        conn.commit()


# ── mcp_permissions ───────────────────────────────────────

def list_mcp_permissions() -> list[dict]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM mcp_permissions ORDER BY tool_name").fetchall()
    return [dict(r) for r in rows]


def set_mcp_permission(tool_name: str, enabled: bool):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE mcp_permissions SET enabled=?, updated_at=? WHERE tool_name=?",
            (int(enabled), _now(), tool_name),
        )
        conn.commit()


def is_tool_enabled(tool_name: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "SELECT enabled FROM mcp_permissions WHERE tool_name=?", (tool_name,)
        ).fetchone()
    return bool(row and row["enabled"])


# ── MCP Token ─────────────────────────────────────────────
# 全局**只有一个**活跃 Token，也不需要生命周期（撤销/过期/最后使用时间）——所以不再单开一张表，
# 直接放 settings：只记 token 的 **hash** 与 **前缀**（前缀用于界面显示，hash 用于鉴权比对）。

MCP_TOKEN_HASH_KEY = "mcp_token_hash"
MCP_TOKEN_PREFIX_KEY = "mcp_token_prefix"


def get_mcp_token_record() -> Optional[dict]:
    """当前 MCP Token 的 `{token_hash, token_prefix}`；未设置时 None。"""
    token_hash = (get_setting(MCP_TOKEN_HASH_KEY, "") or "").strip()
    if not token_hash:
        return None
    return {
        "token_hash": token_hash,
        "token_prefix": get_setting(MCP_TOKEN_PREFIX_KEY, "") or "",
    }


def set_mcp_token(token_hash: str, token_prefix: str) -> None:
    """写入（或覆盖）唯一的 MCP Token。"""
    set_setting(MCP_TOKEN_HASH_KEY, token_hash)
    set_setting(MCP_TOKEN_PREFIX_KEY, token_prefix)


def clear_mcp_token() -> None:
    set_setting(MCP_TOKEN_HASH_KEY, "")
    set_setting(MCP_TOKEN_PREFIX_KEY, "")


# ── schedules ─────────────────────────────────────────────

def _parse_id_list(raw) -> list[int]:
    """把 credential_ids 规整成 int 列表。入参可以是 JSON 文本（存储层）或 list（调用方）。

    坏值一律**丢掉而不是抛错**：一条计划的勾选数据坏了，顶多这条计划暂时没有可验证的凭据，
    不能让整个计划列表（以及调度器启动时的 reload）跟着崩。
    """
    if isinstance(raw, (list, tuple)):
        value = raw
    else:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def list_schedules() -> list[dict]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM schedules ORDER BY created_at").fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["trigger_args"] = json.loads(d.get("trigger_args") or "{}")
        # credential_ids 对外一律是 list（存储层是 JSON 文本）——与 trigger_args 同一套约定
        d["credential_ids"] = _parse_id_list(d.get("credential_ids"))
        results.append(d)
    return results


def create_schedule(
    name: str, trigger_type: str, trigger_args: dict,
    target_scope: str = "all", target_id: Optional[Union[int, str]] = None,
    credential_ids: Optional[list] = None,
) -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO schedules
                   (name, trigger_type, trigger_args, target_scope, target_id, credential_ids, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, trigger_type, json.dumps(trigger_args), target_scope, target_id,
             json.dumps(_parse_id_list(credential_ids)), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["trigger_args"] = json.loads(d.get("trigger_args") or "{}")
    d["credential_ids"] = _parse_id_list(d.get("credential_ids"))
    return d


def update_schedule(schedule_id: int, **kwargs):
    allowed = {"name", "trigger_type", "trigger_args", "target_scope", "target_id",
               "credential_ids", "enabled", "last_run_at", "next_run_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "trigger_args" in fields and isinstance(fields["trigger_args"], dict):
        fields["trigger_args"] = json.dumps(fields["trigger_args"])
    if "credential_ids" in fields and isinstance(fields["credential_ids"], (list, tuple)):
        fields["credential_ids"] = json.dumps(_parse_id_list(fields["credential_ids"]))
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [schedule_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE schedules SET {sets} WHERE id=?", vals)
        conn.commit()


def delete_schedule(schedule_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        conn.commit()


# ── 代理注册表 ────────────────────────────────────────────

def list_proxies() -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            # 按**创建时间**排，不能按 id：id 是 UUID，字典序等于随机序，
            # 用它当 tie-break 会让界面上的代理列表每次刷新都换位置。
            "SELECT * FROM proxies ORDER BY sort_order, created_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_proxy(proxy_id: int) -> Optional[dict]:
    with _lock:
        row = get_conn().execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
    return dict(row) if row else None


def create_proxy(name: str, url: str, proxy_id: Optional[str] = None) -> dict:
    """新建代理。``proxy_id`` 用于**导入备份时原样沿用源库的 UUID**（默认现生成一个）。"""
    pid = str(proxy_id or "").strip() or uuid.uuid4().hex
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO proxies (id, name, url, created_at) VALUES (?,?,?,?)",
            (pid, name, url, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM proxies WHERE id=?", (pid,)).fetchone()
    return dict(row)


def update_proxy(proxy_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "url", "sort_order"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return get_proxy(proxy_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [proxy_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE proxies SET {sets} WHERE id=?", vals)
        conn.commit()
    return get_proxy(proxy_id)


def delete_proxy(proxy_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        conn.commit()
    return cur.rowcount > 0


# ── 类别级 / 插件级默认设置 ────────────────────────────────

def get_preset_defaults(scope: str, scope_key: str) -> dict:
    """读取某一作用域的默认设置（JSON 对象）；不存在时返回 {}。"""
    with _lock:
        row = get_conn().execute(
            "SELECT payload FROM preset_defaults WHERE scope=? AND scope_key=?",
            (scope, scope_key),
        ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["payload"] or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def list_preset_defaults() -> list[dict]:
    """全部默认设置记录（备份导出用）。"""
    with _lock:
        rows = get_conn().execute("SELECT * FROM preset_defaults").fetchall()
    return [dict(r) for r in rows]


def set_preset_defaults(scope: str, scope_key: str, values: dict) -> dict:
    """覆盖写入某一作用域的默认设置。"""
    payload = json.dumps(values or {}, ensure_ascii=False)
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO preset_defaults (scope, scope_key, payload, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(scope, scope_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (scope, scope_key, payload, _now()),
        )
        conn.commit()
    return values or {}


def delete_preset_defaults(scope: str, scope_key: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM preset_defaults WHERE scope=? AND scope_key=?", (scope, scope_key))
        conn.commit()
