"""1.2.3 → 1.2.4：MCP 操作审计补充来源 IP 与脱敏后的返回结果。"""

FROM_VERSION = "1.2.3"
TO_VERSION = "1.2.4"


def _columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(mcp_operation_logs)")}


def preview(conn) -> dict:
    cols = _columns(conn)
    missing = [name for name in ("client_ip", "result") if name not in cols]
    return {"note": "mcp_operation_logs 新增 client_ip / result 列",
            "missing": missing}


def apply(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_operation_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client_ip   TEXT NOT NULL DEFAULT '',
            tool        TEXT NOT NULL,
            operation   TEXT NOT NULL DEFAULT '',
            params      TEXT NOT NULL DEFAULT '{}',
            result      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'ok',
            message     TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_operation_logs_created
            ON mcp_operation_logs(id DESC);
    """)
    cols = _columns(conn)
    if "client_ip" not in cols:
        conn.execute("ALTER TABLE mcp_operation_logs ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
    if "result" not in cols:
        conn.execute("ALTER TABLE mcp_operation_logs ADD COLUMN result TEXT NOT NULL DEFAULT ''")
