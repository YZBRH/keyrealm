"""智谱 GLM 余额查询解析层测试（不联网）。

运行：.venv/bin/python tests/test_glm_balance.py
"""
import importlib.util
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "llm"))

_spec = importlib.util.spec_from_file_location("glm_plugin", ROOT / "plugins" / "llm" / "glm" / "plugin.py")
glm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(glm)


class _FakeClient:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        return self.response


def _fetch(url, payload, status=200):
    resp = httpx.Response(status, json=payload)
    original = glm.make_client
    glm.make_client = lambda: _FakeClient(resp)
    try:
        return glm._fetch_balance(url, "sk-test")
    finally:
        glm.make_client = original


def test_balance_origin_by_host():
    assert glm._balance_origin("https://open.bigmodel.cn/api/paas/v4") == (
        "https://open.bigmodel.cn", "CNY")
    assert glm._balance_origin("https://www.bigmodel.cn/api/paas/v4") == (
        "https://www.bigmodel.cn", "CNY")
    assert glm._balance_origin("https://api.z.ai/api/paas/v4") == (
        "https://api.z.ai", "USD")
    assert glm._balance_origin("https://relay.example.com/v1") == ("", "")


def test_protocol_detection():
    assert glm.protocol_for("https://open.bigmodel.cn/api/anthropic") == "anthropic"
    assert glm.protocol_for("https://open.bigmodel.cn/api/paas/v4") == "openai"
    assert glm.protocol_for("https://open.bigmodel.cn/api/coding/paas/v4") == "openai"
    assert glm.protocol_for("https://api.z.ai/api/anthropic") == "anthropic"
    assert glm.protocol_of({"base_url": "https://open.bigmodel.cn/api/anthropic"}) == "anthropic"


def test_fetch_balance_parses_available_balance():
    payload = {"code": 200, "success": True,
               "data": {"balance": 0E-9, "availableBalance": 27.502911760}}
    assert _fetch("https://open.bigmodel.cn/api/paas/v4", payload) == {
        "balance": 27.502911760, "currency": "CNY"}
    assert _fetch("https://api.z.ai/api/paas/v4", payload) == {
        "balance": 27.502911760, "currency": "USD"}


def test_fetch_balance_falls_back_to_balance_field():
    payload = {"success": True, "data": {"balance": 18.6}}
    assert _fetch("https://open.bigmodel.cn/api/paas/v4", payload) == {
        "balance": 18.6, "currency": "CNY"}


def test_fetch_balance_rejects_bad_responses():
    # 认证失败 / 未登录
    assert _fetch("https://open.bigmodel.cn/api/paas/v4",
                  {"code": 401, "success": False, "msg": "token expired"}) is None
    # HTTP 非 200
    assert _fetch("https://open.bigmodel.cn/api/paas/v4", {"success": True}, status=500) is None
    # 无 data
    assert _fetch("https://open.bigmodel.cn/api/paas/v4", {"success": True}) is None
    # 中转站哨兵金额（1e8）不可信
    assert _fetch("https://open.bigmodel.cn/api/paas/v4",
                  {"success": True, "data": {"availableBalance": 100000000}}) is None
    # 非官方站点不查询
    assert glm._fetch_balance("https://relay.example.com/v1", "sk-test") is None


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
