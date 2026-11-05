"""1.2.4 → 1.2.5 迁移：百度人脸 → 百度 AI 开放平台类型改名（幂等）。"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIG_PATH = ROOT / "scripts" / "migrations" / "m_1.2.4__to_1.2.5.py"
_spec = importlib.util.spec_from_file_location("mig_baidu_ai_test", MIG_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

OLD = "api_baidu_face"
NEW = "api_baidu_ai"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE credentials (
            id INTEGER PRIMARY KEY, name TEXT, credential_type TEXT
        );
        CREATE TABLE plugins (
            credential_type TEXT PRIMARY KEY, name TEXT, version TEXT
        );
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY, name TEXT, target_scope TEXT, target_id TEXT
        );
        CREATE TABLE preset_defaults (
            scope TEXT, scope_key TEXT, payload TEXT, updated_at TEXT,
            PRIMARY KEY (scope, scope_key)
        );
    """)
    return conn


def _seed(conn):
    conn.execute("INSERT INTO credentials (id, name, credential_type) VALUES (1,'a',?)", (OLD,))
    conn.execute("INSERT INTO credentials (id, name, credential_type) VALUES (2,'b','api_fofa')")
    conn.execute("INSERT INTO plugins VALUES (?, '百度人脸', '1.0.1')", (OLD,))
    conn.execute("INSERT INTO plugins VALUES (?, '百度 AI 开放平台', '1.1.0')", (NEW,))
    conn.execute("INSERT INTO schedules VALUES (1,'按类型','type',?)", (OLD,))
    conn.execute("INSERT INTO schedules VALUES (2,'按分组','group','3')")
    conn.execute("INSERT INTO preset_defaults VALUES ('plugin',?,?,CURRENT_TIMESTAMP)",
                 (OLD, json.dumps({"base_url": "https://aip.baidubce.com", "notes": "old"})))
    conn.execute("INSERT INTO preset_defaults VALUES ('plugin',?,?,CURRENT_TIMESTAMP)",
                 (NEW, json.dumps({"notes": "new"})))
    conn.commit()


def test_migration_renames_all_references():
    conn = _conn()
    _seed(conn)
    state = Path(tempfile.mkdtemp(prefix="kr-baidu-state-")) / ".state.json"
    state.write_text(json.dumps({"disabled": [OLD, "api_fofa", OLD]}), encoding="utf-8")
    old_state = mig._STATE_FILE
    mig._STATE_FILE = state
    try:
        preview = mig.preview(conn)
        assert preview["credentials"] == 1
        assert preview["schedules_type"] == 1
        assert preview["preset_plugin"] == 1
        json.dumps(preview)

        mig.apply(conn)
        mig.apply(conn)  # 幂等
        conn.commit()

        assert conn.execute(
            "SELECT credential_type FROM credentials WHERE id=1").fetchone()[0] == NEW
        assert conn.execute(
            "SELECT credential_type FROM credentials WHERE id=2").fetchone()[0] == "api_fofa"
        assert conn.execute(
            "SELECT COUNT(*) FROM plugins WHERE credential_type=?", (OLD,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM plugins WHERE credential_type=?", (NEW,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT target_id FROM schedules WHERE id=1").fetchone()[0] == NEW
        assert conn.execute(
            "SELECT target_id FROM schedules WHERE id=2").fetchone()[0] == "3"
        rows = conn.execute(
            "SELECT scope_key,payload FROM preset_defaults WHERE scope='plugin' "
            "ORDER BY scope_key").fetchall()
        assert [r[0] for r in rows] == [NEW]
        assert json.loads(rows[0][1]) == {"base_url": "https://aip.baidubce.com", "notes": "new"}

        disabled = json.loads(state.read_text(encoding="utf-8"))["disabled"]
        assert disabled == [NEW, "api_fofa"]
        assert json.loads(json.dumps(preview))
    finally:
        mig._STATE_FILE = old_state


def test_migration_renames_single_old_plugin_row():
    conn = _conn()
    conn.execute("INSERT INTO plugins VALUES (?, '百度人脸', '1.0.1')", (OLD,))
    conn.commit()
    state = Path(tempfile.mkdtemp(prefix="kr-baidu-state2-")) / ".state.json"
    old_state = mig._STATE_FILE
    mig._STATE_FILE = state
    try:
        mig.apply(conn)
        conn.commit()
        row = conn.execute(
            "SELECT credential_type,name,version FROM plugins").fetchone()
        assert row == (NEW, "百度 AI 开放平台", "1.1.0")
    finally:
        mig._STATE_FILE = old_state


def test_migration_is_noop_without_old_data():
    conn = _conn()
    state = Path(tempfile.mkdtemp(prefix="kr-baidu-state3-")) / ".state.json"
    old_state = mig._STATE_FILE
    mig._STATE_FILE = state
    try:
        mig.apply(conn)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0] == 0
    finally:
        mig._STATE_FILE = old_state


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
