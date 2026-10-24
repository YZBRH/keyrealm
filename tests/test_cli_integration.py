"""CLI 集成：初始化、增删改查、分组、代理/TLS、备份、去重、错误路径。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testutil import run_cli  # noqa: E402

PASSWORD = "TestPass!123"
_ORIGINAL_STATE: bytes | None = None


def setup_module():
    global _ORIGINAL_STATE
    state = ROOT / "plugins" / ".state.json"
    _ORIGINAL_STATE = state.read_bytes() if state.is_file() else None


def teardown_module():
    """备份导入会恢复插件启用状态，而 plugins/ 是仓库共享目录：测试后必须还原。"""
    state = ROOT / "plugins" / ".state.json"
    try:
        if _ORIGINAL_STATE is None:
            state.unlink(missing_ok=True)
        else:
            state.write_bytes(_ORIGINAL_STATE)
    except OSError:
        pass


def _json(res):
    assert res.returncode == 0, f"命令失败：{res.stderr}\n{res.stdout}"
    return json.loads(res.stdout)


def test_full_lifecycle():
    with tempfile.TemporaryDirectory(prefix="kr-cli-") as tmp:
        # 未初始化
        res = run_cli(tmp, "--json", "status")
        data = _json(res)
        assert data["initialized"] is False

        res = run_cli(tmp, "init", password=PASSWORD)
        assert res.returncode == 0 and "成功" in res.stdout

        res = run_cli(tmp, "--json", "list", password=PASSWORD)
        assert _json(res) == []

        res = run_cli(tmp, "add", "--type", "api_fofa", "--name", "main",
                      "-f", "api_key=secret-123", "-f", "notes=hello",
                      "--no-verify", password=PASSWORD)
        assert res.returncode == 0 and "已创建凭据 1" in res.stdout

        creds = _json(run_cli(tmp, "--json", "list", password=PASSWORD))
        assert len(creds) == 1 and creds[0]["name"] == "main"
        assert "secret-123" not in json.dumps(creds, ensure_ascii=False)

        shown = _json(run_cli(tmp, "--json", "show", "main", password=PASSWORD))
        assert "…" in shown["data"]["api_key"]
        revealed = _json(run_cli(tmp, "--json", "show", "main", "--reveal", password=PASSWORD))
        assert revealed["data"]["api_key"] == "secret-123"
        assert revealed["network"]["proxy_outlet"]

        # 分组
        assert run_cli(tmp, "group", "add", "测试组", "--description", "d", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "group", "update", "测试组", "--name", "改名组", password=PASSWORD).returncode == 0
        assert "改名组" in run_cli(tmp, "groups", password=PASSWORD).stdout

        # 更新（字段 + 分组 + TLS/代理策略）
        assert run_cli(tmp, "update", "main", "--group", "改名组", "-f", "notes=changed",
                       "--proxy-mode", "direct", "--tls", "1.2", password=PASSWORD).returncode == 0
        shown = _json(run_cli(tmp, "--json", "show", "main", password=PASSWORD))
        assert shown["data"]["notes"] == "changed"
        assert shown["network"]["proxy_mode"] == "direct"
        assert shown["network"]["tls_version"] == "1.2"
        assert shown["network"]["proxy_outlet"].startswith("直连")

        # 代理 / TLS 全局
        assert run_cli(tmp, "proxy", "add", "local", "http://127.0.0.1:18080", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "proxy", "use", "local", password=PASSWORD).returncode == 0
        proxy_data = _json(run_cli(tmp, "--json", "proxy", "list", password=PASSWORD))
        assert proxy_data["global_url"] == "http://127.0.0.1:18080"
        assert run_cli(tmp, "tls", "set", "1.3", password=PASSWORD).returncode == 0
        assert _json(run_cli(tmp, "--json", "tls", "show", password=PASSWORD))["global"] == "1.3"
        # 恢复
        assert run_cli(tmp, "proxy", "use", "none", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "tls", "set", "auto", password=PASSWORD).returncode == 0

        # 导出 / 导入（重复跳过）
        backup = Path(tmp) / "backup.json"
        assert run_cli(tmp, "export", str(backup), password=PASSWORD).returncode == 0
        assert backup.is_file()
        imported = _json(run_cli(tmp, "--json", "import", str(backup), password=PASSWORD))
        assert imported["skipped_duplicate"] == 1

        # 去重扫描 + 合并
        assert run_cli(tmp, "add", "--type", "api_fofa", "--name", "dup",
                       "-f", "api_key=secret-123", "--no-verify", password=PASSWORD).returncode == 0
        scan = _json(run_cli(tmp, "--json", "dedupe", password=PASSWORD))
        assert scan["group_count"] == 1 and scan["duplicate_count"] == 2
        merged = _json(run_cli(tmp, "--json", "dedupe", "--keep", "main", "--remove", "dup",
                               password=PASSWORD))
        assert merged["ok"] is True and merged["deleted"] == 1

        # 删除
        assert run_cli(tmp, "delete", "main", "--yes", password=PASSWORD).returncode == 0
        assert _json(run_cli(tmp, "--json", "list", password=PASSWORD)) == []

        # data-dir 只读命令
        shown_dir = _json(run_cli(tmp, "--json", "data-dir", "show", password=None))
        assert Path(shown_dir["data_dir"]) == Path(tmp).resolve()


def test_error_exit_codes():
    with tempfile.TemporaryDirectory(prefix="kr-cli-") as tmp:
        # 未初始化直接 list
        res = run_cli(tmp, "list", password=PASSWORD)
        assert res.returncode == 1 and "尚未初始化" in res.stderr

        # 非交互且没有密码
        res = run_cli(tmp, "status", password=None)
        assert res.returncode == 0  # status 不需要密码

        run_cli(tmp, "init", password=PASSWORD)
        res = run_cli(tmp, "list", password=None)
        assert res.returncode == 1 and "需要主密码" in res.stderr
        res = run_cli(tmp, "list", password="wrong-password")
        assert res.returncode == 1 and "密码错误" in res.stderr

        # 非法 --data-dir
        import subprocess
        from _testutil import MAIN, PYTHON, cli_env
        res = subprocess.run([PYTHON, str(MAIN), "--cli", "--data-dir"],
                             cwd=str(ROOT), env=cli_env(PASSWORD),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert res.returncode == 2 and "缺少路径" in res.stderr


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
