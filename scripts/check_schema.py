"""在临时库上真跑一遍建表与各表 CRUD —— **打包/发版前必跑**。

为什么需要它
------------
建表语句写在 `backend/database.py` 的 `executescript` 里，只有在**真的连上 SQLite**
时才会被解析。而绝大多数单元测试会把 `database` 模块整个打桩（因为要避免动真库），
于是「SQL 本身写错了」这类问题**一通测试全绿、一打包运行就崩**。

要挡住的两类问题：把 SQLite **保留字**当列名（`CREATE TABLE ... values TEXT` 直接
`syntax error`），以及改名漏改的残留引用（`SELECT values FROM ...`）。打桩的测试都
查不出来，只有真连上 SQLite 才会暴露。

所以：**改了表结构、加了新表、或准备打包时，先跑它**。
它只碰临时目录，不读写 `data/` 里的真库。

用法
----
    python scripts/check_schema.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 输出里用了 ✓ / ✗ —— 这些符号**不在 GBK 里**，中文 Windows 控制台直接跑会抛
# UnicodeEncodeError。把标准输出钉成 UTF-8（build.py 也是按 UTF-8 读它的）。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend import database as db  # noqa: E402  (必须在 sys.path 调整之后导入)
from backend.version import SCHEMA_VERSION  # noqa: E402

# 建表脚本里应当出现的每一张表。
# **增删表时必须同步这里**——构建（scripts/build.py）的「建表自检」就是拿它核对的，
# 漏改会在打包时直接 ✗ 缺表/多表被挡下（这是有意的护栏）。
EXPECTED_TABLES = (
    "settings", "groups", "credentials", "plugins", "mcp_permissions",
    "schedules", "proxies", "preset_defaults", "mcp_operation_logs",
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vault-schema-check-"))
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "credentials.db"
    try:
        # 1) 建表 —— SQL 写错会在这里原样抛出，正是本脚本要挡住的
        db.init_db()
        conn = db.get_conn()
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in EXPECTED_TABLES if name not in tables]
        if missing:
            print(f"✗ 缺表：{missing}", file=sys.stderr)
            return 1
        print(f"✓ 建表通过（{len(tables)} 张表）")

        # 1.1) MCP 审计表的关键列（来源 IP / 返回结果）必须存在
        audit_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(mcp_operation_logs)")}
        missing_cols = {"client_ip", "result"} - audit_cols
        if missing_cols:
            print(f"✗ mcp_operation_logs 缺列：{sorted(missing_cols)}", file=sys.stderr)
            return 1

        # 2) 设置表
        db.set_setting("verify_proxy", "socks5://x:1")
        assert db.get_setting("verify_proxy") == "socks5://x:1"

        # 3) 代理表 CRUD
        first = db.create_proxy("示例代理", "socks5://192.0.2.5:1080")
        second = db.create_proxy("备用", "http://127.0.0.1:7890")
        assert {p["id"] for p in db.list_proxies()} == {first["id"], second["id"]}
        assert db.get_proxy(first["id"])["name"] == "示例代理"
        assert db.update_proxy(first["id"], name="改过的")["name"] == "改过的"
        assert db.delete_proxy(second["id"]) is True
        print("✓ 代理表 CRUD 通过")

        # 4) 默认设置表 CRUD（列名是 payload，不是保留字 values）
        db.set_preset_defaults("category", "llm", {"base_url": "https://cat/v1"})
        db.set_preset_defaults("plugin", "llm_glm", {"base_url": "https://glm/v1"})
        assert db.get_preset_defaults("plugin", "llm_glm") == {"base_url": "https://glm/v1"}
        assert db.get_preset_defaults("plugin", "不存在") == {}
        rows = db.list_preset_defaults()
        assert all("payload" in row for row in rows), "list_preset_defaults 应返回 payload 列"
        db.delete_preset_defaults("plugin", "llm_glm")
        assert db.get_preset_defaults("plugin", "llm_glm") == {}
        print("✓ 默认设置表 CRUD 通过")

        # 5) 插件表 upsert（每次启动都会走）
        db.upsert_plugin(name="Demo", version="1.0.0", credential_type="llm_demo",
                         fields_schema=[{"key": "api_key", "sensitive": True}],
                         plugin_path=str(tmp), author="", description="", icon="🔑",
                         category="llm", category_name="LLM 模型", batch_verify=True)
        db.upsert_plugin(name="Demo2", version="2.0.0", credential_type="llm_demo",
                         fields_schema=[], plugin_path=str(tmp), author="", description="",
                         icon="🔑", category="dns", category_name="域名解析", batch_verify=False)
        record = db.get_plugin_by_type("llm_demo")
        assert record["category"] == "dns" and record["batch_verify"] == 0, record
        print("✓ 插件表 upsert 通过")

        # 6) 分组 / 凭据
        group = db.create_group("测试组", "", "#fff")
        cred = db.create_credential(name="c1", credential_type="llm_demo",
                                    data_encrypted="BLOB", data_plain={"notes": "n"},
                                    group_id=group["id"])
        assert db.get_credential(cred["id"])["group_id"] == group["id"]
        print("✓ 分组 / 凭据通过")

        # 7) 幂等：打包后每次启动都会跑 init_db
        db.init_db()
        db.init_db()
        print("✓ 重复 init_db 幂等")

        # 8) 升级路径：把这两次新加的表删掉，再跑一次应当补建（不需要迁移、不需要改版本号）
        conn.execute("DROP TABLE preset_defaults")
        conn.execute("DROP TABLE proxies")
        conn.commit()
        db.init_db()
        rebuilt = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"proxies", "preset_defaults"} <= rebuilt, rebuilt
        print("✓ 老库补建新表（升级路径）")

        # 9) 代理 id 由「自增整数」改成「UUID」的升级路径：老表必须被重建，
        #    否则 uuid 文本塞不进 INTEGER PRIMARY KEY（rowid 别名）→ 建代理直接报类型错。
        conn.execute("DROP TABLE proxies")
        conn.execute(
            "CREATE TABLE proxies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " url TEXT NOT NULL, sort_order INTEGER DEFAULT 0,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO proxies (name, url) VALUES ('老代理','socks5://127.0.0.1:1080')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('global_proxy_id','1')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version','4')")
        conn.commit()
        db.init_db()
        cols = {r["name"]: str(r["type"] or "").upper()
                for r in conn.execute("PRAGMA table_info(proxies)")}
        assert cols.get("id") == "TEXT", cols
        assert db.list_proxies() == [], "旧的自增 id 行应被丢弃（1.0 前不做数据迁移）"
        assert db.get_setting("global_proxy_id", "") == "", "旧引用必须清掉，否则指向不存在的代理"
        created = db.create_proxy("新代理", "socks5://127.0.0.1:10808")
        assert isinstance(created["id"], str) and len(created["id"]) == 32, created
        print("✓ 代理 id 改 UUID（老库重建 + 引用清理）")

        # 10) 定时计划：credential_ids 列（v6）的读写
        sched = db.create_schedule(
            name="勾选巡检", trigger_type="interval", trigger_args={"hours": 1},
            target_scope="credentials", target_id=None, credential_ids=[3, 1],
        )
        assert sched["credential_ids"] == [3, 1], sched
        listed = next(s for s in db.list_schedules() if s["id"] == sched["id"])
        assert listed["credential_ids"] == [3, 1], listed
        db.update_schedule(sched["id"], credential_ids=[2])
        assert next(s for s in db.list_schedules()
                    if s["id"] == sched["id"])["credential_ids"] == [2]
        # 其它 scope 不传时读数必须是 []，不是 None（前端 v-model 要数组）
        plain = db.create_schedule(name="全量", trigger_type="interval",
                                   trigger_args={"hours": 1}, target_scope="all")
        assert next(s for s in db.list_schedules()
                    if s["id"] == plain["id"])["credential_ids"] == []
        print("✓ 定时计划 credential_ids 通过")

        # 11) 升级路径：v5 的老 schedules 表（无 credential_ids 列）跑 init_db 应自动补列
        conn.execute("DROP TABLE schedules")
        conn.execute(
            "CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL, trigger_type TEXT NOT NULL,"
            " trigger_args TEXT NOT NULL DEFAULT '{}',"
            " target_scope TEXT NOT NULL DEFAULT 'all', target_id TEXT,"
            " enabled INTEGER NOT NULL DEFAULT 1, last_run_at TIMESTAMP,"
            " next_run_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO schedules (name, trigger_type, target_scope)"
                     " VALUES ('老计划','interval','all')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value)"
                     " VALUES ('schema_version','5')")
        conn.commit()
        db.init_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(schedules)")}
        assert "credential_ids" in cols, cols
        assert db.get_setting("schema_version", "") == SCHEMA_VERSION, db.get_setting("schema_version", "")
        old = next(s for s in db.list_schedules() if s["name"] == "老计划")
        assert old["credential_ids"] == [], old
        print("✓ 老库补 credential_ids 列（v5 → v6）")

        print("\n全部通过：建表脚本与各表 SQL 都可用。")
        return 0
    finally:
        try:
            db._conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
