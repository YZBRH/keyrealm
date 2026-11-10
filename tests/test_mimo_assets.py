"""MiMo 余额 / Token Plan 解析测试（不联网）。

运行：.venv/bin/python tests/test_mimo_assets.py
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
    "mimo_plugin", ROOT / "plugins" / "llm" / "mimo" / "plugin.py")
mimo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mimo)


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


def test_parse_balance_cash_plus_gift():
    info, available = mimo._parse_balance({
        "object": "balance",
        "is_available": True,
        "balance_infos": [{
            "currency": "CNY",
            "cash_balance": 12.5,
            "gift_balance": 3.5,
            "total_available": 20.0,
        }],
    })
    assert available is True
    assert info == {"balance": 16.0, "currency": "CNY"}


def test_parse_balance_fallback_total_available():
    info, _ = mimo._parse_balance({
        "balance_infos": [{"currency": "USD", "total_available": 7.25}],
    })
    assert info == {"balance": 7.25, "currency": "USD"}
    assert mimo._parse_balance({"balance_infos": []}) == (None, None)


def test_parse_plan_fields():
    plan = mimo._parse_plan({
        "plan_code": "mimo-token-plan-mini",
        "period": {"start_at": "2026-11-01T00:00:00Z", "end_at": "2026-12-01T00:00:00Z"},
        "quota": {"total_credits": 1000, "used_credits": 250, "remaining_credits": 750},
    })
    assert plan["套餐"] == "mimo-token-plan-mini"
    assert plan["套餐剩余"] == "750/1000"
    assert plan["套餐到期"] == "2026-12-01"
    assert mimo._parse_plan({}) is None


def test_fetch_assets_routes_by_key_prefix():
    balance_resp = httpx.Response(200, json={
        "is_available": True,
        "balance_infos": [{"currency": "CNY", "cash_balance": 1, "gift_balance": 0}],
    })
    fake = _FakeClient(balance_resp)
    with patch.object(mimo, "make_client", lambda *a, **k: fake):
        info, available = mimo._fetch_assets({"api_key": "sk-test"})
    assert info["balance"] == 1.0 and available is True
    assert fake.urls == [mimo.BALANCE_URL]

    plan_resp = httpx.Response(200, json={
        "plan_code": "mini", "quota": {"total_credits": 10, "remaining_credits": 8},
    })
    fake = _FakeClient(plan_resp)
    with patch.object(mimo, "make_client", lambda *a, **k: fake):
        info, available = mimo._fetch_assets({
            "api_key": "tp-test",
            "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        })
    assert info["套餐剩余"] == "8/10" and available is None
    assert fake.urls == ["https://token-plan-cn.xiaomimimo.com/v1/platform/token_plan"]


def test_quota_probe_prefers_assets():
    with patch.object(mimo, "_fetch_assets", lambda data: ({"balance": 1.0, "currency": "CNY"}, True)):
        assert mimo.quota_probe({"api_key": "sk-test"}) == {
            "account_info": {"balance": 1.0, "currency": "CNY"}}
    with patch.object(mimo, "_fetch_assets", lambda data: (None, None)), \
            patch.object(mimo, "_generic_quota_probe", lambda data: {"account_info": {"chat_probe": "可用"}}):
        assert mimo.quota_probe({"api_key": "sk-test"}) == {
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
