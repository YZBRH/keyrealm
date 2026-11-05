"""read_logs 解析缓存：内容变化后必须失效，过滤语义保持不变。"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import logger  # noqa: E402

LINES = [
    "2026-11-06 12:00:00 [INFO] backend.api: 第一条\n",
    "2026-11-06 12:00:01 [ERROR] backend.api: 第二条\n",
    "Traceback (most recent call last):\n    File \"x.py\", line 1\n",
    "2026-11-06 12:00:02 [WARNING] backend.api: 第三条\n",
]


def _reset(p: Path):
    logger.LOG_FILE = p
    logger._records_cache_key = None
    logger._records_cache = []


def test_cache_reflects_appends_and_filters():
    sandbox = Path(tempfile.mkdtemp(prefix="keyrealm-log-cache-"))
    p = sandbox / "k.log"
    _reset(p)
    try:
        p.write_text(LINES[0] + LINES[1] + LINES[2], encoding="utf-8")
        rows = logger.read_logs(10)
        assert len(rows) == 2  # traceback 并入上一条
        assert "Traceback" in rows[1]

        # 文件变化（追加）后缓存必须失效
        with p.open("a", encoding="utf-8") as fh:
            fh.write(LINES[3])
        assert len(logger.read_logs(10)) == 3

        # 搜索与级别过滤仍按整条记录工作
        assert len(logger.read_logs(10, search="第二条")) == 1
        assert len(logger.read_logs(10, level="WARNING")) == 2
        assert len(logger.read_logs(10, level="INFO")) == 3

        # 清空后立即读到空，不残留缓存
        logger.clear_logs()
        assert logger.read_logs(10) == []
    finally:
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
