"""
MCP 工具权限：全局开关 + 请求已鉴权标记（ContextVar）传播。

**全程沙盒**：DB 落到 ./tmp，绝不碰真实的 data/credentials.db。

运行：python tests/test_mcp_permissions.py
"""
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = ROOT / "tmp" / "test_mcp_permissions"

from backend import database as db

# 沙盒 DB（必须在任何连接建立之前改）
db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "probe.db"

from backend.mcp_server import _authenticated_var, _perm_denied  # noqa: E402


def setup_module():
    SANDBOX.mkdir(parents=True, exist_ok=True)
    db.init_db()


def teardown_module():
    try:
        if getattr(db, "_conn", None) is not None:
            db._conn.close()
            db._conn = None
    except Exception:  # noqa: BLE001
        pass
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)


def _in_thread(fn):
    """在**新线程**里跑 fn：ContextVar 在新线程里是默认值，验证不会串线程。"""
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _with_auth(fn, enabled: bool = True):
    token = _authenticated_var.set(enabled)
    try:
        return fn()
    finally:
        _authenticated_var.reset(token)


# ── ContextVar 传播 ──────────────────────────────────────

def test_default_is_unauthenticated_in_new_thread():
    assert _in_thread(lambda: _authenticated_var.get()) is False
    _authenticated_var.set(True)
    assert _in_thread(lambda: _authenticated_var.get()) is False
    _authenticated_var.set(False)


# ── 权限检查 ──────────────────────────────────────────────

def test_no_identity_is_denied():
    denied = _with_auth(lambda: _perm_denied("vault_list_credentials"), enabled=False)
    assert isinstance(denied, dict), denied
    assert denied["error"] == "permission_denied"
    assert denied["reason"] == "no_identity"


def test_enabled_tool_passes_when_authenticated():
    db.set_mcp_permission("vault_list_credentials", True)
    assert db.is_tool_enabled("vault_list_credentials")
    assert _with_auth(lambda: _perm_denied("vault_list_credentials")) is None


def test_global_switch_blocks_even_when_authenticated():
    db.set_mcp_permission("vault_get_secret", False)
    denied = _with_auth(lambda: _perm_denied("vault_get_secret"))
    assert denied and denied["reason"] == "global_disabled", denied


def test_permission_round_trip():
    db.set_mcp_permission("vault_list_credentials", True)
    assert db.is_tool_enabled("vault_list_credentials") is True
    db.set_mcp_permission("vault_list_credentials", False)
    assert db.is_tool_enabled("vault_list_credentials") is False
    record = next(p for p in db.list_mcp_permissions()
                  if p["tool_name"] == "vault_list_credentials")
    assert record["enabled"] == 0


if __name__ == "__main__":
    setup_module()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    teardown_module()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
