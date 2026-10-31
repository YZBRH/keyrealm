"""构建版本变化时清理 WebView 缓存（backend/webview_cache.py）契约测试。

只删 Cache / Code Cache / GPUCache 等缓存目录，必须保留 Local Storage 等偏好数据。

运行：.venv/bin/python tests/test_webview_cache.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import webview_cache  # noqa: E402

SANDBOX = Path(tempfile.mkdtemp(prefix="keyrealm-webview-cache-"))


def _make_file(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_first_run_writes_marker_without_error():
    root = SANDBOX / "empty"
    assert webview_cache.clear_if_build_changed(root, "v1") is False
    assert (root / webview_cache.MARKER).read_text(encoding="utf-8").strip() == "v1"


def test_version_change_removes_only_cache_dirs():
    root = SANDBOX / "profile"
    cache = root / "EBWebView" / "Default" / "Cache"
    code_cache = root / "EBWebView" / "Default" / "Code Cache"
    gpu_cache = root / "EBWebView" / "Default" / "GPUCache"
    local = root / "EBWebView" / "Default" / "Local Storage" / "leveldb"
    cookies = root / "EBWebView" / "Default" / "Network"
    _make_file(cache / "data_0")
    _make_file(code_cache / "js")
    _make_file(gpu_cache / "blob")
    _make_file(local / "000003.log")
    _make_file(cookies / "Cookies")

    assert webview_cache.clear_if_build_changed(root, "v1") is True
    assert not cache.exists()
    assert not code_cache.exists()
    assert not gpu_cache.exists()
    assert local.is_dir() and (local / "000003.log").is_file()
    assert cookies.is_dir() and (cookies / "Cookies").is_file()
    assert (root / webview_cache.MARKER).read_text(encoding="utf-8").strip() == "v1"


def test_same_stamp_is_noop_and_new_stamp_clears_again():
    root = SANDBOX / "profile2"
    cache = root / "Cache"
    _make_file(cache / "payload")
    assert webview_cache.clear_if_build_changed(root, "v1") is True
    assert not cache.exists()

    _make_file(cache / "payload2")
    assert webview_cache.clear_if_build_changed(root, "v1") is False
    assert cache.exists()

    assert webview_cache.clear_if_build_changed(root, "v2") is True
    assert not cache.exists()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
        shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
