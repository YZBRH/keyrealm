"""1.2.5 → 1.2.6 迁移：百度 AI 插件目录改名与旧目录清理（幂等）。"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIG_PATH = ROOT / "scripts" / "migrations" / "m_1.2.5__to_1.2.6.py"
_spec = importlib.util.spec_from_file_location("mig_baidu_dir_test", MIG_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE plugins (credential_type TEXT PRIMARY KEY, plugin_path TEXT)")
    conn.execute("INSERT INTO plugins VALUES ('api_baidu_ai','api/baidu_face')")
    conn.commit()
    return conn


def _tree(root: Path, old_manifest=None, new_manifest=None):
    old = root / "plugins" / "api" / "baidu_face"
    new = root / "plugins" / "api" / "baidu_ai"
    if old_manifest is not None:
        old.mkdir(parents=True)
        (old / "manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
    if new_manifest is not None:
        new.mkdir(parents=True)
        (new / "manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
    return old, new


def _with_dirs(root: Path, old: Path, new: Path):
    old_dir, new_dir = mig._OLD_DIR, mig._NEW_DIR
    mig._OLD_DIR, mig._NEW_DIR = old, new
    return old_dir, new_dir


def test_renames_path_and_removes_old_dir():
    root = Path(tempfile.mkdtemp(prefix="kr-baidu-dir-"))
    old, new = _tree(root,
                     {"credential_type": "api_baidu_face", "name": "百度人脸"},
                     {"credential_type": "api_baidu_ai", "name": "百度 AI 开放平台"})
    saved = _with_dirs(root, old, new)
    try:
        conn = _db()
        preview = mig.preview(conn)
        assert preview["plugin_path"] == 1
        assert preview["old_dir"] is True and preview["new_dir"] is True
        json.dumps(preview)

        mig.apply(conn)
        mig.apply(conn)  # 幂等
        conn.commit()
        assert conn.execute(
            "SELECT plugin_path FROM plugins WHERE credential_type='api_baidu_ai'"
        ).fetchone()[0] == "api/baidu_ai"
        assert not old.exists()
        assert new.is_dir()
    finally:
        mig._OLD_DIR, mig._NEW_DIR = saved


def test_keeps_old_dir_when_new_dir_missing():
    root = Path(tempfile.mkdtemp(prefix="kr-baidu-dir2-"))
    old, new = _tree(root, {"credential_type": "api_baidu_face"}, None)
    saved = _with_dirs(root, old, new)
    try:
        mig.apply(_db())
        assert old.is_dir()
    finally:
        mig._OLD_DIR, mig._NEW_DIR = saved


def test_keeps_unrelated_old_dir():
    root = Path(tempfile.mkdtemp(prefix="kr-baidu-dir3-"))
    old, new = _tree(root,
                     {"credential_type": "someone_else"},
                     {"credential_type": "api_baidu_ai"})
    saved = _with_dirs(root, old, new)
    try:
        mig.apply(_db())
        assert old.is_dir()
        assert new.is_dir()
    finally:
        mig._OLD_DIR, mig._NEW_DIR = saved


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
