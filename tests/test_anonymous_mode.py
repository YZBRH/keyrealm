"""匿名模式契约测试（独立沙箱，不联网）。

覆盖：
1. 开启后，未配置代理的验证 / 操作 / 能力探测 / 模型列表在发起前被拒绝；
2. 显式直连即使有环境代理也被拒绝；
3. 环境里有代理时不再拦截；
4. 关闭后恢复。

运行：.venv/bin/python tests/test_anonymous_mode.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-anon-"))

from backend import database as db  # noqa: E402

db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "credentials.db"
db.init_db()

from backend import anonymous, api, plugin_manager, proxy_store  # noqa: E402
from backend import vault_proxy_ctx as ctx  # noqa: E402

_PROXY_ENV = ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")


def _clear_env():
    for key in _PROXY_ENV:
        os.environ.pop(key, None)


def setup_module():
    _clear_env()


def teardown_module():
    _clear_env()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def test_anonymous_blocks_direct_verify_and_action():
    _clear_env()
    assert api.set_anonymous_mode(True)["enabled"] is True

    reason = anonymous.blocked_reason({"api_key": "x"}, "llm_openai")
    assert "匿名模式" in reason

    verify = plugin_manager.verify_credential("llm_openai", {
        "base_url": "https://example.com/v1", "api_key": "x",
    })
    assert verify.status == "error"
    assert "匿名模式" in verify.message

    action = plugin_manager.run_plugin_action("llm_openai", "chat", {}, {
        "base_url": "https://example.com/v1", "api_key": "x",
    })
    assert action.get("ok") is False
    assert "匿名模式" in action.get("error", "")

    caps = plugin_manager.plugin_capabilities("llm_openai", {"api_key": "x"})
    assert caps["error"] is True and caps["available"] == []
    assert "匿名模式" in caps["note"]

    assert plugin_manager.plugin_models("llm_openai", {"api_key": "x"}) == []


def test_explicit_direct_is_blocked_even_with_env_proxy():
    _clear_env()
    api.set_anonymous_mode(True)
    os.environ["VAULT_PROXY"] = "socks5://127.0.0.1:9"
    try:
        data = {"_proxy_mode": "direct"}
        assert anonymous.blocked_reason(data, "llm_openai")
        # 未显式直连的凭据会跟随环境代理，不应被拦截
        assert anonymous.blocked_reason({}, "llm_openai") == ""
        assert proxy_store.effective_url(ctx.UNSET) == "socks5://127.0.0.1:9"
    finally:
        _clear_env()


def test_global_proxy_in_db_is_used_even_without_env_sync():
    """库里配了全局代理、进程环境变量没同步（MCP / 子进程 / 改完设置未同步）时不能误判直连。"""
    _clear_env()
    api.set_anonymous_mode(True)
    created = proxy_store.create_proxy("anon-global", "socks5://127.0.0.1:1080")
    pid = created["id"]
    proxy_store.set_global_proxy(pid)
    # 模拟「另一个进程」：设置写进了共享库，但本进程环境里没有 VAULT_PROXY
    _clear_env()
    try:
        assert proxy_store.global_proxy_id() == str(pid)
        assert anonymous.blocked_reason({}, "llm_openai") == ""
        # 真正发请求的上下文必须是同一个出口（否则「检查通过」但实际直连）
        with proxy_store.credential_proxy_scope({}, "llm_openai") as outlet:
            assert outlet == "socks5://127.0.0.1:1080"
            assert ctx.current() == "socks5://127.0.0.1:1080"
    finally:
        proxy_store.set_global_proxy(None)
        _clear_env()


def test_per_credential_proxy_is_not_blocked():
    _clear_env()
    api.set_anonymous_mode(True)
    created = proxy_store.create_proxy("anon-own", "socks5://127.0.0.1:1081")
    try:
        data = {"_proxy_mode": "proxy", "_proxy_id": created["id"]}
        assert anonymous.blocked_reason(data, "llm_openai") == ""
        with proxy_store.credential_proxy_scope(data, "llm_openai") as outlet:
            assert outlet == "socks5://127.0.0.1:1081"
    finally:
        proxy_store.delete_proxy(created["id"])
        _clear_env()


def test_ssh_console_and_files_are_blocked():
    _clear_env()
    api.set_anonymous_mode(True)
    plugin_manager.sync_plugins()
    assert api.setup_master_password("TestPass!123")["ok"] is True
    cred = api.create_credential(
        name="anon-ssh", credential_type="remote_ssh",
        data={"host": "192.0.2.1", "port": "22", "username": "root",
              "auth_type": "password", "password": "x"},
        auto_verify=False,
    )
    for opened in (api.ssh_open(cred["id"]), api.ssh_files_open(cred["id"])):
        assert opened.get("ok") is False
        assert "匿名模式" in opened.get("error", "")
    api.lock_vault()


def test_disable_restores_direct():
    _clear_env()
    assert api.set_anonymous_mode(False)["enabled"] is False
    assert anonymous.blocked_reason({}, "llm_openai") == ""


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    setup_module()
    passed = failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  PASS  {test.__name__}")
                passed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {test.__name__}: {exc}")
                failed += 1
    finally:
        teardown_module()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
