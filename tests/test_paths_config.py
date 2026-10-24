"""数据目录配置：优先级、相对路径、持久化、损坏配置、各模块落点。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import paths

_ORIGINAL_CONFIG: bytes | None = None


def setup_module():
    global _ORIGINAL_CONFIG
    cfg = paths.config_file()
    _ORIGINAL_CONFIG = cfg.read_bytes() if cfg.is_file() else None


def teardown_module():
    cfg = paths.config_file()
    try:
        if _ORIGINAL_CONFIG is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_bytes(_ORIGINAL_CONFIG)
    except OSError:
        pass
    paths.configure_data_dir(None)


def test_default_data_dir():
    cfg = paths.config_file()
    cfg.unlink(missing_ok=True)
    got = paths.configure_data_dir(None)
    assert got == paths.default_data_dir()
    assert paths.data_dir_source() == "default"


def test_argv_parsing_last_wins_and_missing():
    assert paths.argv_data_dir(["x", "--data-dir", "/a"]) == "/a"
    assert paths.argv_data_dir(["x", "--data-dir=/b"]) == "/b"
    assert paths.argv_data_dir(["x", "--data-dir", "/a", "--data-dir", "/b"]) == "/b"
    assert paths.argv_data_dir(["x"]) is None
    try:
        paths.argv_data_dir(["x", "--data-dir"])
    except ValueError:
        pass
    else:
        raise AssertionError("--data-dir 缺值时应报错")


def test_config_persist_and_relative_path():
    with tempfile.TemporaryDirectory(prefix="kr-paths-") as tmp:
        paths.config_file().unlink(missing_ok=True)
        target = Path(tmp) / "vault-data"
        written = paths.write_config_data_dir(str(target))
        assert written == target.resolve()
        assert target.is_dir()
        data = json.loads(paths.config_file().read_text(encoding="utf-8"))
        assert Path(data["data_dir"]).is_absolute()

        # 配置里的相对路径按入口目录解析
        paths.config_file().write_text(
            json.dumps({"data_dir": "tests-rel-data"}), encoding="utf-8")
        got = paths.configure_data_dir(None)
        assert got == (ROOT / "tests-rel-data").resolve()
        assert paths.data_dir_source() == "config"


def test_cli_arg_overrides_config_and_config_cleared():
    with tempfile.TemporaryDirectory(prefix="kr-paths-") as tmp:
        cfg_target = Path(tmp) / "from-config"
        arg_target = Path(tmp) / "from-arg"
        paths.write_config_data_dir(str(cfg_target))
        got = paths.configure_data_dir(["x", "--data-dir", str(arg_target)])
        assert got == arg_target.resolve()
        assert paths.data_dir_source() == "cli"
        # 不传命令行时回到配置文件
        got = paths.configure_data_dir(None)
        assert got == cfg_target.resolve()
        assert paths.clear_config_data_dir() is True
        assert not paths.config_file().exists()


def test_invalid_config_falls_back_and_clear_deletes_it():
    with tempfile.TemporaryDirectory(prefix="kr-paths-"):
        paths.config_file().write_text("{not-json", encoding="utf-8")
        got = paths.configure_data_dir(None)
        assert got == paths.default_data_dir()
        assert paths.data_dir_source() == "default"
        assert paths.config_error()
        assert paths.clear_config_data_dir() is True
        assert not paths.config_file().exists()


def test_cli_linux_asset_name_has_no_gui_flavor():
    """CLI 版没有界面层：Linux 发布资产应为 linux-<arch>-cli，而不是 linux-general/gtk。"""
    if not sys.platform.startswith("linux"):
        return
    from backend import updater

    old_frozen, old_cli = updater.is_frozen, updater._is_cli_build
    try:
        updater.is_frozen = lambda: True
        updater._is_cli_build = lambda: True
        assert updater.platform_pkg() == f"linux-{updater._machine_arch()}-cli"
        assert updater.release_asset_name("1.2.3").endswith(
            f"linux-{updater._machine_arch()}-cli.zip")
    finally:
        updater.is_frozen, updater._is_cli_build = old_frozen, old_cli


def test_modules_use_configured_dir():
    """database / logger / tasks / updater 的模块级路径必须跟着配置走。"""
    with tempfile.TemporaryDirectory(prefix="kr-paths-") as tmp:
        code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
sys.argv = ["x", "--data-dir", {tmp!r}]
from backend import paths
paths.configure_data_dir(sys.argv)
from backend import database, logger, tasks, updater
print(database.DB_PATH)
print(logger.LOG_FILE)
print(tasks._STORE)
print(updater._data_dir())
print(updater.backups_dir())
print(" ".join(updater._self_update_argv(updater.repo_root(), "formal")))
"""
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        assert out.returncode == 0, out.stderr
        lines = out.stdout.strip().splitlines()
        base = str(Path(tmp).resolve())
        assert lines[0] == f"{base}/credentials.db"
        assert lines[1] == f"{base}/logs/keyrealm.log"
        assert lines[2] == f"{base}/tasks.json"
        assert lines[3] == base
        assert lines[4] == f"{base}/backup"
        assert "--data-dir" in lines[5] and base in lines[5]


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
