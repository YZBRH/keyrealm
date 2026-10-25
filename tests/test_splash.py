"""开屏动画设置与「每进程只播一次」契约。

运行：python tests/test_splash.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = ROOT / "tmp" / "test_splash"
SANDBOX.mkdir(parents=True, exist_ok=True)

from backend import database as db

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "splash.db"
if db.DB_PATH.exists():
    db.DB_PATH.unlink()
db.init_db()

from backend import api  # noqa: E402


def test_default_enabled_and_single_claim():
    assert api.get_splash_disabled() == {"disabled": False, "always": False}
    first = api.claim_splash()
    assert first["show"] is True and first["reason"] == "startup"
    assert first["always"] is False
    second = api.claim_splash()
    assert second["show"] is False and second["reason"] == "already"


def test_disable_consumes_startup_slot():
    # 模拟「本次进程尚未认领过」：禁用状态下第一次 claim 也应消耗播放机会，
    # 之后重新启用不会在同一个进程里补播。
    api._splash_claimed = False
    assert api.set_splash_disabled(True)["ok"] is True
    assert api.get_splash_disabled() == {"disabled": True, "always": False}
    first = api.claim_splash()
    assert first["show"] is False and first["reason"] == "disabled"
    assert api.set_splash_disabled(False)["ok"] is True
    assert api.claim_splash()["show"] is False


def test_always_replays_and_disable_wins():
    api._splash_claimed = False
    assert api.set_splash_always(True)["ok"] is True
    assert api.get_splash_disabled() == {"disabled": False, "always": True}
    # 开启「总是展示」后，同一进程内每次都播放。
    first = api.claim_splash()
    assert first["show"] is True and first["reason"] == "always"
    second = api.claim_splash()
    assert second["show"] is True and second["reason"] == "always"
    # 禁用优先：即使「总是展示」还开着也不播。
    assert api.set_splash_disabled(True)["ok"] is True
    blocked = api.claim_splash()
    assert blocked["show"] is False and blocked["reason"] == "disabled"
    assert api.set_splash_disabled(False)["ok"] is True
    assert api.set_splash_always(False)["ok"] is True


def main():
    test_default_enabled_and_single_claim()
    test_disable_consumes_startup_slot()
    test_always_replays_and_disable_wins()
    print("3 passed, 0 failed")


if __name__ == "__main__":
    main()
