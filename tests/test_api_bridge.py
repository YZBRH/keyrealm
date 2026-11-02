"""前端 ↔ 后端桥接面契约：``api/index.js`` 里 ``call('x')`` 的每个方法都必须在
``VaultApi`` 上存在，否则桌面 / Web 模式会报「后端未实现接口：x」。

运行：.venv/bin/python tests/test_api_bridge.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


def frontend_methods() -> set[str]:
    text = (ROOT / "frontend" / "src" / "api" / "index.js").read_text(encoding="utf-8")
    # 兼容单行与换行两种写法：call('x', …) / call(\n 'x', …)
    return set(re.findall(r"call\(\s*'([A-Za-z0-9_]+)'", text))


def mock_methods() -> set[str]:
    text = (ROOT / "frontend" / "src" / "api" / "mock.js").read_text(encoding="utf-8")
    return set(re.findall(r"async\s+([a-z0-9_]+)\s*\(", text))


def test_every_frontend_call_has_bridge_method():
    missing = sorted(
        name for name in frontend_methods()
        if not callable(getattr(main.VaultApi, name, None))
    )
    assert not missing, f"前端调用了 VaultApi 未实现的方法：{missing}"


def test_dev_mock_implements_every_frontend_method():
    """开发模式的内存 Mock 必须能冒充整个桥接面，缺方法时 `npm run dev` 会直接报「后端未实现接口」。"""
    missing = sorted(frontend_methods() - mock_methods())
    assert not missing, f"开发模式 Mock 缺失的方法：{missing}"


def test_ssh_file_methods_are_exposed():
    for name in ("ssh_files_create", "ssh_files_rename"):
        assert callable(getattr(main.VaultApi, name, None)), name


def main_run():
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
    sys.exit(main_run())
