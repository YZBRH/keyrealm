"""MiniMax M Plan 套餐额度解析测试（不联网）。

运行：.venv/bin/python tests/test_minimax_plan.py
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "llm"))

_spec = importlib.util.spec_from_file_location(
    "minimax_plugin", ROOT / "plugins" / "llm" / "minimax" / "plugin.py")
mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mm)


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        self.urls.append(url)
        return self.response


def _plan_payload():
    return {
        "model_remains": [{
            "model_name": "general",
            "current_interval_remaining_percent": 56,
            "current_weekly_remaining_percent": 77,
        }],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def test_parse_plan_windows():
    plan = mm._parse_plan(_plan_payload())
    assert plan["套餐"] == "MiniMax M Plan"
    assert plan["五小时剩余"] == "56%"
    assert plan["周剩余"] == "77%"


def test_parse_plan_without_subscription():
    assert mm._parse_plan({
        "model_remains": None,
        "base_resp": {"status_code": 2062, "status_msg": "no active token plan subscription"},
    }) is None
    assert mm._parse_plan(None) is None


def test_fetch_plan_uses_origin_and_bearer():
    fake = _FakeClient(httpx.Response(200, json=_plan_payload()))
    with patch.object(mm, "make_client", lambda *a, **k: fake):
        plan = mm._fetch_plan({"api_key": "sk-cp-test", "base_url": "https://api.minimaxi.com/v1"})
    assert plan["五小时剩余"] == "56%"
    assert fake.urls == ["https://api.minimaxi.com/v1/token_plan/remains"]


def test_quota_probe_prefers_plan():
    with patch.object(mm, "_fetch_plan", lambda data: {"套餐": "MiniMax M Plan"}):
        assert mm.quota_probe({"api_key": "k"}) == {
            "account_info": {"套餐": "MiniMax M Plan"}}
    with patch.object(mm, "_fetch_plan", lambda data: None), \
            patch.object(mm, "_generic_quota_probe", lambda data: {"account_info": {"chat_probe": "可用"}}):
        assert mm.quota_probe({"api_key": "k"}) == {
            "account_info": {"chat_probe": "可用"}}


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
