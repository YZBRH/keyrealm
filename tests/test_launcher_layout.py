"""启动器 + onedir 布局：安装、替换、路径解析。

这些用例全部在临时目录里跑，**不碰**仓库的 runtime/ 与 data/。
"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launcher
from backend import paths, updater

NAME = "Keyrealm.exe" if os.name == "nt" else "Keyrealm"


def _bundle(base: Path, tag: str) -> Path:
    """造一个完整发行目录：启动器 + runtime/ + plugins/。"""
    src = base / f"bundle-{tag}"
    (src / "runtime" / "_internal").mkdir(parents=True)
    (src / "runtime" / NAME).write_text(f"exe-{tag}", encoding="utf-8")
    (src / "runtime" / "_internal" / "lib.txt").write_text("new", encoding="utf-8")
    (src / NAME).write_text(f"launcher-{tag}", encoding="utf-8")
    plugin = src / "plugins" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(tag, encoding="utf-8")
    return src


def _app_root(base: Path) -> Path:
    root = base / "app"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / NAME).write_text("exe-old", encoding="utf-8")
    (root / "runtime" / "stale.txt").write_text("stale", encoding="utf-8")
    (root / NAME).write_text("launcher-old", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "credentials.db").write_text("user-data", encoding="utf-8")
    keep = root / "plugins" / "demo"
    keep.mkdir(parents=True)
    (keep / ".state.json").write_text('{"demo": true}', encoding="utf-8")
    return root


def test_launcher_install_swaps_runtime_and_keeps_user_data():
    with tempfile.TemporaryDirectory(prefix="kr-launcher-") as tmp:
        base = Path(tmp)
        root = _app_root(base)
        src = _bundle(base, "new")
        launcher.install_bundle(src, root, running_exe=root / NAME)

        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "exe-new"
        assert not (root / "runtime" / "stale.txt").exists(), "旧运行时残留文件必须被清掉"
        assert (root / NAME).read_text(encoding="utf-8") == "launcher-new"
        assert (root / "plugins" / "demo" / ".state.json").exists(), "插件启用状态不能被覆盖"
        assert (root / "plugins" / "demo" / "plugin.py").read_text(encoding="utf-8") == "new"
        assert (root / "data" / "credentials.db").read_text(encoding="utf-8") == "user-data"
        # 旧运行时保留为回滚副本，由启动器在新应用正常启动后删除
        assert (root / "runtime.old" / NAME).read_text(encoding="utf-8") == "exe-old"


def test_updater_install_bundle_same_layout():
    with tempfile.TemporaryDirectory(prefix="kr-updater-") as tmp:
        base = Path(tmp)
        root = _app_root(base)
        result = updater.install_bundle(_bundle(base, "v2"), root)
        assert result.get("ok"), result
        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "exe-v2"
        assert (root / "plugins" / "demo" / ".state.json").exists()
        assert (root / "data" / "credentials.db").read_text(encoding="utf-8") == "user-data"


def test_extract_release_zip_strips_top_dir_and_skips_data():
    import stat
    with tempfile.TemporaryDirectory(prefix="kr-zip-") as tmp:
        base = Path(tmp)
        root = _app_root(base)
        archive = base / "release.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"Keyrealm-1.5.0-demo/runtime/{NAME}", "exe-zip")
            zf.writestr("Keyrealm-1.5.0-demo/runtime/_internal/a", "a")
            zf.writestr(f"Keyrealm-1.5.0-demo/{NAME}", "launcher-zip")
            zf.writestr("Keyrealm-1.5.0-demo/plugins/demo/plugin.py", "zip")
            zf.writestr("Keyrealm-1.5.0-demo/data/credentials.db", "SHOULD-NOT-WIN")
            zf.writestr("Keyrealm-1.5.0-demo/.update-meta/version.json", "{}")
            link = zipfile.ZipInfo("Keyrealm-1.5.0-demo/runtime/_internal/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, "a")
            # zip-slip：绝对路径与 .. 都不能逃出安装目录
            zf.writestr("../kr-slip-outside.txt", "evil")
            zf.writestr("/kr-slip-absolute.txt", "evil")
            # Windows 驱动器相对路径既非绝对路径、也不含 ..：同样必须丢弃
            zf.writestr("C:kr-slip-drive.txt", "evil")

        result = updater.extract_release_zip(archive, root)
        assert result.get("ok"), result
        assert (root / "runtime" / "_internal" / "link").is_symlink()
        assert os.readlink(root / "runtime" / "_internal" / "link") == "a"
        assert not (base.parent / "kr-slip-outside.txt").exists()
        assert not Path("/kr-slip-absolute.txt").exists()
        assert not (root / "C:kr-slip-drive.txt").exists()
        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "exe-zip"
        assert (root / NAME).read_text(encoding="utf-8") == "launcher-zip"
        assert (root / "plugins" / "demo" / "plugin.py").read_text(encoding="utf-8") == "zip"
        assert (root / "data" / "credentials.db").read_text(encoding="utf-8") == "user-data"
        assert not (root / ".update-new").exists()


def test_update_lock_is_fail_closed():
    """取不到锁（数据目录不可写/不可用）时必须当作没拿到，不能放行更新。"""
    with tempfile.TemporaryDirectory(prefix="kr-lock-fail-") as tmp:
        root = Path(tmp)
        (root / "data").write_text("not a directory", encoding="utf-8")
        try:
            assert launcher._acquire_update_lock(root) is False
        finally:
            launcher._release_update_lock()


def test_request_ttl():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    assert launcher._request_fresh({"requested_at": now.isoformat()})
    old = (now - timedelta(days=2)).isoformat()
    assert not launcher._request_fresh({"requested_at": old})
    assert not launcher._request_fresh({})


def test_install_failure_restores_old_launcher():
    """运行时替换失败时，已经换上的新启动器必须退回旧版，不能留下混合目录。"""
    with tempfile.TemporaryDirectory(prefix="kr-install-fail-") as tmp:
        base = Path(tmp)
        root = _app_root(base)
        src = _bundle(base, "broken")
        failed_src = src / "runtime"
        real_replace = launcher.os.replace

        def fake_replace(a, b):
            if Path(a) == failed_src:
                raise OSError("simulated runtime swap failure")
            return real_replace(a, b)

        launcher.os.replace = fake_replace
        try:
            try:
                launcher.install_bundle(src, root, running_exe=root / NAME)
            except OSError:
                pass
            else:
                raise AssertionError("运行时替换失败时 install_bundle 应抛错")
        finally:
            launcher.os.replace = real_replace
        assert (root / NAME).read_text(encoding="utf-8") == "launcher-old"
        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "exe-old"
        assert not (root / (NAME + ".new")).exists()


def test_launcher_extract_rejects_unsafe_names():
    with tempfile.TemporaryDirectory(prefix="kr-launcher-zip-") as tmp:
        base = Path(tmp)
        dest = base / "dest"
        dest.mkdir()
        archive = base / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"pkg/runtime/{NAME}", "exe")
            zf.writestr("../outside.txt", "evil")
            zf.writestr("C:evil.txt", "evil")
        launcher._extract_zip(archive, dest)
        assert (dest / "runtime" / NAME).read_text(encoding="utf-8") == "exe"
        assert not (base / "outside.txt").exists()
        assert not (dest / "C:evil.txt").exists()


def test_finish_update_keeps_old_runtime_for_rollback():
    import json
    with tempfile.TemporaryDirectory(prefix="kr-finish-") as tmp:
        root = Path(tmp)
        data = root / "data"
        data.mkdir()
        (root / "runtime").mkdir()
        (root / "runtime.old").mkdir()
        (root / "runtime.old" / "old.txt").write_text("old", encoding="utf-8")
        data.joinpath(".update-request.json").write_text(json.dumps({
            "kind": "formal", "restart": False, "requested_at": updater._now(),
        }), encoding="utf-8")
        data.joinpath(".update-prepared.json").write_text(json.dumps({
            "kind": "formal", "version": "9.9.9", "from_version": "9.9.8",
            "schema_version": "",
        }), encoding="utf-8")

        result = None
        old_acquire, old_release = updater.single_instance.acquire, updater.single_instance.release
        updater.single_instance.acquire = lambda: True
        updater.single_instance.release = lambda: None
        try:
            result = updater.finish_update("formal", root=root, log=lambda _msg: None)
        finally:
            updater.single_instance.acquire = old_acquire
            updater.single_instance.release = old_release
        assert result.get("ok"), result
        # 启动器在应用正常跑起来之后才删；收尾阶段必须保留回滚副本
        assert (root / "runtime.old").is_dir()
        assert not (data / ".update-prepared.json").exists(), "收尾后应清掉准备清单"


def test_renamed_launcher_finds_runtime_and_rewrites_itself():
    """启动器被改名：仍能找到 runtime/ 里的应用，更新时新启动器装回**当前名字**。"""
    old_name = launcher.app_name
    launcher.app_name = lambda: "renamed-launcher"
    try:
        with tempfile.TemporaryDirectory(prefix="kr-rename-") as tmp:
            base = Path(tmp)
            root = base / "app"
            (root / "runtime").mkdir(parents=True)
            (root / "runtime" / NAME).write_text("exe", encoding="utf-8")
            (root / "runtime" / NAME).chmod(0o755)
            (root / "renamed-launcher").write_text("old-launcher", encoding="utf-8")
            assert launcher.app_exe(root) == root / "runtime" / NAME

            src = _bundle(base, "renamed")
            launcher.install_bundle(src, root, running_exe=root / "renamed-launcher")
            assert (root / "renamed-launcher").read_text(encoding="utf-8") == "launcher-renamed"
            assert not (root / "Keyrealm").exists(), "不应额外留下旧名字的启动器"
    finally:
        launcher.app_name = old_name


def test_failure_status_is_visible_to_ui():
    with tempfile.TemporaryDirectory(prefix="kr-failstatus-") as tmp:
        root = Path(tmp)
        data = root / "data"
        launcher._fail_status(root, data, "formal", "模拟失败")
        import json
        status = json.loads((data / ".update-status.json").read_text(encoding="utf-8"))
        assert status["ok"] is False and status["phase"] == "failed"
        assert status.get("finished_at"), "界面靠 finished_at 判断是否显示/通知"
        assert status.get("log") and Path(status["log"]).is_file()


def test_restore_backup_swaps_old_runtime():
    with tempfile.TemporaryDirectory(prefix="kr-restore-") as tmp:
        root = Path(tmp)
        (root / "runtime").mkdir()
        (root / "runtime" / "new.txt").write_text("new", encoding="utf-8")
        (root / "runtime.old").mkdir()
        (root / "runtime.old" / NAME).write_text("old-exe", encoding="utf-8")
        archive = root / "backup.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("plugins/demo/plugin.py", "restored")
            zf.writestr("data/credentials.db", "db")

        result = updater.restore_backup(archive, root=root)
        assert result.get("ok"), result
        assert result.get("runtime_restored") is True
        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "old-exe"
        assert not (root / "runtime" / "new.txt").exists()
        assert (root / "plugins" / "demo" / "plugin.py").read_text(encoding="utf-8") == "restored"
        assert (root / "data" / "credentials.db").read_text(encoding="utf-8") == "db"


def test_extract_zip_preserves_symlinks():
    """onedir 里的 .so 版本链接必须按符号链接还原，而不是复制目标文件。"""
    import stat
    with tempfile.TemporaryDirectory(prefix="kr-symlink-") as tmp:
        base = Path(tmp)
        pkg = base / "pkg"
        (pkg / "runtime").mkdir(parents=True)
        (pkg / "runtime" / "libreal.so.1").write_text("content", encoding="utf-8")
        os.symlink("libreal.so.1", pkg / "runtime" / "libreal.so")
        archive = base / "release.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for p in sorted(pkg.rglob("*")):
                arc = f"top/{p.relative_to(pkg).as_posix()}"
                if p.is_symlink():
                    info = zipfile.ZipInfo(arc)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(info, os.readlink(p))
                elif p.is_file():
                    zf.write(p, arc)

        dest = base / "out"
        launcher._extract_zip(archive, dest)
        link = dest / "runtime" / "libreal.so"
        assert link.is_symlink(), "符号链接应保持为链接"
        assert os.readlink(link) == "libreal.so.1"
        assert (dest / "runtime" / "libreal.so.1").read_text(encoding="utf-8") == "content"

        # 指向包外的链接一律拒绝
        evil = base / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            info = zipfile.ZipInfo("top/runtime/evil")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "../../etc/passwd")
        dest2 = base / "out2"
        launcher._extract_zip(evil, dest2)
        assert not (dest2 / "runtime" / "evil").exists() and not (dest2 / "runtime" / "evil").is_symlink()


def test_recover_runtime_after_interrupted_swap():
    with tempfile.TemporaryDirectory(prefix="kr-recover-") as tmp:
        root = Path(tmp)
        # 模拟两次 rename 之间中断：runtime 目录不完整，runtime.old 完好
        (root / "runtime").mkdir()
        (root / "runtime" / "partial.txt").write_text("partial", encoding="utf-8")
        (root / "runtime.old").mkdir()
        (root / "runtime.old" / NAME).write_text("old-exe", encoding="utf-8")
        (root / "runtime.old" / NAME).chmod(0o755)
        assert launcher._recover_runtime(root) is True
        assert (root / "runtime" / NAME).read_text(encoding="utf-8") == "old-exe"
        assert not (root / "runtime" / "partial.txt").exists()
        assert not (root / "runtime.old").exists()
        # 没有可用的旧运行时 → 不动作
        assert launcher._recover_runtime(root) is False


def test_restore_backup_honors_custom_data_dir():
    with tempfile.TemporaryDirectory(prefix="kr-restore-data-") as tmp:
        base = Path(tmp)
        root = base / "app"
        root.mkdir()
        custom = base / "custom-data"
        archive = base / "backup.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("data/credentials.db", "db")
            zf.writestr("plugins/demo/plugin.py", "p")

        old_data_dir = updater._data_dir
        updater._data_dir = lambda root=None: custom  # noqa: ARG005
        try:
            result = updater.restore_backup(archive, root=root)
        finally:
            updater._data_dir = old_data_dir
        assert result.get("ok"), result
        assert (custom / "credentials.db").read_text(encoding="utf-8") == "db"
        assert not (root / "data").exists(), "备份里的 data/ 不能写回入口目录"
        assert (root / "plugins" / "demo" / "plugin.py").exists()


def test_materialize_symlinks_from_manifest():
    """旧更新器把链接存成普通文件；新版启动器据清单还原为链接。"""
    import json
    with tempfile.TemporaryDirectory(prefix="kr-linkmanifest-") as tmp:
        root = Path(tmp)
        runtime_internal = root / "runtime" / "_internal"
        runtime_internal.mkdir(parents=True)
        (runtime_internal / "libreal.so.1").write_text("content", encoding="utf-8")
        (runtime_internal / "libreal.so").write_text("content", encoding="utf-8")
        (runtime_internal / "evil").write_text("copy", encoding="utf-8")
        meta = root / ".update-meta"
        meta.mkdir()
        (meta / "symlinks.json").write_text(json.dumps({
            "runtime/_internal/libreal.so": "libreal.so.1",
            "runtime/_internal/evil": "../../etc/passwd",
            "keyrealm.conf.json": "outside",            # 非 runtime 条目必须忽略
        }), encoding="utf-8")

        launcher._materialize_symlinks(root)

        link = runtime_internal / "libreal.so"
        assert link.is_symlink() and os.readlink(link) == "libreal.so.1"
        assert not (runtime_internal / "evil").is_symlink(), "越界目标不能还原"
        assert (runtime_internal / "evil").read_text(encoding="utf-8") == "copy"
        assert not (root / "keyrealm.conf.json").exists()
        assert not (meta / "symlinks.json").exists(), "处理完应删除清单"


def test_install_bundle_materializes_symlink_manifest():
    import json
    with tempfile.TemporaryDirectory(prefix="kr-install-links-") as tmp:
        base = Path(tmp)
        root = _app_root(base)
        src = _bundle(base, "links")
        internal = src / "runtime" / "_internal"
        (internal / "libreal.so.1").write_text("content", encoding="utf-8")
        (internal / "libreal.so").write_text("content", encoding="utf-8")
        meta = src / ".update-meta"
        meta.mkdir()
        (meta / "symlinks.json").write_text(json.dumps({
            "runtime/_internal/libreal.so": "libreal.so.1",
        }), encoding="utf-8")

        launcher.install_bundle(src, root, running_exe=root / NAME)
        link = root / "runtime" / "_internal" / "libreal.so"
        assert link.is_symlink() and os.readlink(link) == "libreal.so.1"
        assert not (root / ".update-meta" / "symlinks.json").exists()


def test_update_lock_excludes_second_holder():
    with tempfile.TemporaryDirectory(prefix="kr-lock-") as tmp:
        root = Path(tmp)
        assert launcher._acquire_update_lock(root) is True
        try:
            assert launcher._acquire_update_lock(root) is False, "同一把锁不能被拿两次"
        finally:
            launcher._release_update_lock()
        assert launcher._acquire_update_lock(root) is True
        launcher._release_update_lock()


def test_app_dir_env_and_runtime_fallback():
    old_frozen, old_exe = paths.is_frozen, paths.sys.executable
    paths.is_frozen = lambda: True
    try:
        with tempfile.TemporaryDirectory(prefix="kr-appdir-") as tmp:
            root = Path(tmp)
            os.environ[paths.APP_DIR_ENV] = str(root)
            try:
                assert paths.app_dir() == root.resolve()
            finally:
                os.environ.pop(paths.APP_DIR_ENV, None)
            # 没有环境变量时：`<root>/runtime/Keyrealm`（且 runtime/ 有 _internal）→ `<root>`
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            (runtime_dir / "_internal").mkdir()
            paths.sys.executable = str(runtime_dir / NAME)
            assert paths.app_dir() == root.resolve()
            # 旧 onefile 碰巧放在名为 runtime 的目录：不能误上跳
            legacy = root / "legacy" / "runtime"
            legacy.mkdir(parents=True)
            paths.sys.executable = str(legacy / NAME)
            assert paths.app_dir() == legacy.resolve()
            # 旧 onefile：exe 同级
            paths.sys.executable = str(root / NAME)
            assert paths.app_dir() == root.resolve()
    finally:
        paths.is_frozen, paths.sys.executable = old_frozen, old_exe


def test_updater_detects_launcher_layout():
    old_frozen, old_name = updater.is_frozen, updater._exe_name
    updater.is_frozen = lambda: True
    updater._exe_name = lambda: NAME
    try:
        with tempfile.TemporaryDirectory(prefix="kr-detect-") as tmp:
            root = Path(tmp)
            (root / NAME).write_text("l", encoding="utf-8")
            (root / "runtime").mkdir()
            (root / "runtime" / NAME).write_text("a", encoding="utf-8")
            assert updater._launcher_path(root) == root / NAME
            (root / "runtime" / NAME).unlink()
            assert updater._launcher_path(root) is None
    finally:
        updater.is_frozen, updater._exe_name = old_frozen, old_name


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
