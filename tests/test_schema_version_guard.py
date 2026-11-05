"""schema_version 守卫：init_db 不替迁移运行器推进已有语义化版本。"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402
from backend.version import SCHEMA_VERSION  # noqa: E402


def _fresh_sandbox():
    sandbox = Path(tempfile.mkdtemp(prefix="keyrealm-schema-guard-"))
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    db.DATA_DIR = sandbox
    db.DB_PATH = sandbox / "credentials.db"
    db.init_db()
    return sandbox


def _set_disk_version(value: str) -> None:
    db._conn.close()
    db._conn = None
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.execute("UPDATE settings SET value=? WHERE key='schema_version'", (value,))
    conn.commit()
    conn.close()
    db.init_db()


def test_existing_semver_is_not_advanced():
    sandbox = _fresh_sandbox()
    try:
        _set_disk_version("1.2.3")
        assert db.get_setting("schema_version") == "1.2.3"
        assert db.get_setting("schema_version") != SCHEMA_VERSION
    finally:
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        shutil.rmtree(sandbox, ignore_errors=True)


def test_legacy_integer_is_stamped_to_current():
    sandbox = _fresh_sandbox()
    try:
        _set_disk_version("6")
        assert db.get_setting("schema_version") == SCHEMA_VERSION
    finally:
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        shutil.rmtree(sandbox, ignore_errors=True)


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
