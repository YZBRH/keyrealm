"""1.2.2 → 1.2.3：新增 MCP 操作审计表。

表只存工具名、操作描述、**已脱敏**的参数与耗时；不含任何凭据明文。
建表语句与应用内 `database.init_db()` 相同，脚本保证只跑迁移、不启动应用的场景也能补上。
"""

FROM_VERSION = "1.2.2"
TO_VERSION = "1.2.3"


def preview(conn) -> dict:
    return {"note": "新增表 mcp_operation_logs（MCP 操作审计，参数脱敏）"}


def apply(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_operation_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tool        TEXT NOT NULL,
            operation   TEXT NOT NULL DEFAULT '',
            params      TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'ok',
            message     TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_operation_logs_created
            ON mcp_operation_logs(id DESC);
    """)
