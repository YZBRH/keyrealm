"""帮助与参数可达性：所有子命令 --help、关键参数、模式/数据目录约束。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testutil import MAIN, PYTHON, cli_env  # noqa: E402


def _run(*args):
    return subprocess.run(
        [PYTHON, str(MAIN), *args], cwd=str(ROOT), env=cli_env(None),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def test_all_subcommand_help():
    commands = [
        "init", "status", "list", "show", "add", "update", "delete", "verify",
        "groups", "group", "plugins", "plugin", "mcp", "data-dir", "proxy", "tls",
        "actions", "action", "export", "import", "dedupe", "self-update", "logs", "version",
    ]
    nested = [
        ("group", "add"), ("group", "update"), ("group", "rm"),
        ("plugin", "enable"), ("plugin", "disable"),
        ("mcp", "status"), ("mcp", "start"), ("mcp", "token"),
        ("mcp", "permissions"), ("mcp", "permission"),
        ("data-dir", "show"), ("data-dir", "set"), ("data-dir", "clear"),
        ("proxy", "list"), ("proxy", "add"), ("proxy", "update"), ("proxy", "rm"),
        ("proxy", "use"), ("proxy", "test"),
        ("tls", "show"), ("tls", "set"),
        ("self-update", "check"), ("self-update", "status"), ("self-update", "install"),
    ]
    for cmd in commands:
        res = _run("--cli", cmd, "--help")
        assert res.returncode == 0, f"{cmd} --help: {res.stderr}"
    for parent, child in nested:
        res = _run("--cli", parent, child, "--help")
        assert res.returncode == 0, f"{parent} {child} --help: {res.stderr}"


def test_key_options_discoverable():
    show_help = _run("--cli", "show", "--help").stdout
    assert "--reveal" in show_help
    add_help = _run("--cli", "add", "--help").stdout
    for flag in ("--proxy", "--proxy-mode", "--tls", "--data-json", "-f"):
        assert flag in add_help, flag
    mcp_help = _run("--cli", "mcp", "--help").stdout
    for word in ("status", "start", "token", "permissions", "permission"):
        assert word in mcp_help, word
    top = _run("--cli", "--help").stdout
    assert "data-dir" in top and "self-update" in top and "show --reveal" in top


def test_modes_help_and_version():
    for args in (("--help",), ("--version",)):
        res = _run(*args)
        assert res.returncode == 0, res.stderr
    assert "Keyrealm 启动方式" in _run().stdout
    assert "1.4.7" in _run("--version").stdout


def test_data_dir_restriction():
    # 源码的桌面模式（--web / 无模式）不接受 --data-dir
    res = _run("--web", "--data-dir", "/tmp/x")
    assert res.returncode == 2 and "仅支持 CLI" in res.stderr
    res = _run("--data-dir", "/tmp/x")
    assert res.returncode == 2 and "仅支持 CLI" in res.stderr
    # CLI 模式接受；--data-dir 缺值报错
    res = _run("--cli", "--data-dir", "/tmp/x", "data-dir", "show")
    assert res.returncode == 0 and "/tmp/x" in res.stdout
    res = _run("--cli", "--data-dir")
    assert res.returncode == 2 and "缺少路径" in res.stderr


if __name__ == "__main__":
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
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
