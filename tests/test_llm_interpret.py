"""LLM 探针判定：402 / 无订阅 / 404 不应误判成 invalid。"""
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LLM_DIR = ROOT / "plugins" / "llm"
sys.path.insert(0, str(LLM_DIR))

import llm_protocols  # noqa: E402


def _resp(status, body):
    return httpx.Response(status, text=body, request=httpx.Request("GET", "http://relay.example"))


def test_402_is_valid_but_unpaid():
    result = llm_protocols._interpret_simple(_resp(
        402, '{"error":{"code":"INSUFFICIENT_BALANCE","message":"Insufficient account balance"}}'))
    assert result.status == "valid"
    assert "欠费" in result.message or "额度" in result.message


def test_subscription_missing_is_valid():
    result = llm_protocols._interpret_simple(_resp(
        403, '{"code":"SUBSCRIPTION_NOT_FOUND","message":"No active subscription found for this group"}'))
    assert result.status == "valid"
    assert "订阅" in result.message


def test_insufficient_in_403_is_valid():
    result = llm_protocols._interpret_simple(_resp(
        403, '{"code":"INSUFFICIENT_BALANCE","message":"Insufficient account balance"}'))
    assert result.status == "valid"


def test_glm_coding_plan_expired_is_subscription_not_rate_limit():
    result = llm_protocols._interpret_chat(_resp(
        429, '{"type":"error","error":{"type":"rate_limit_error","code":"1309",'
             '"message":"[1309][您的GLM Coding Plan套餐已到期，暂无法使用]"}}'))
    assert result.status == "valid"
    assert "套餐" in result.message or "订阅" in result.message
    assert "速率限制" not in result.message


def test_invalid_key_still_invalid():
    result = llm_protocols._interpret_simple(_resp(
        401, '{"error":{"message":"Invalid token","type":"authentication_error"}}'))
    assert result.status == "invalid"


def test_models_404_is_error_not_invalid():
    result = llm_protocols._interpret_simple(_resp(404, '{"message":"not found"}'))
    assert result.status == "error"


def test_quota_probe_subscription_is_valid():
    def fake_chat(*args, **kwargs):
        return {"ok": False, "error": '鉴权或请求错误 (HTTP 403): '
                                      '{"code":"SUBSCRIPTION_NOT_FOUND","message":"No active subscription"}'}

    with patch.object(llm_protocols, "_probe_model_candidates", lambda *a, **k: ["m1"]), \
            patch.object(llm_protocols, "chat_completion", fake_chat):
        result = llm_protocols.quota_chat_probe("http://relay.example/v1", "sk-test")
    assert "message" in result
    assert "订阅" in result["message"]
    assert "status" not in result


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
