"""对话实测（quota_probe）：余额可用但对话失效时不能仍判 valid。"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LLM_DIR = ROOT / "plugins" / "llm"
sys.path.insert(0, str(LLM_DIR))

# 插件目录里的模块以裸名互相 import（llm_common / llm_protocols），按真实插件加载方式引入。
import llm_protocols  # noqa: E402

from backend import plugin_manager  # noqa: E402
from backend.plugin_sdk import VerifyResult  # noqa: E402


def _probe(chat_results):
    results = list(chat_results)

    def fake_chat(*args, **kwargs):
        return results.pop(0)

    with patch.object(llm_protocols, "_probe_model_candidates", lambda *a, **k: ["m1", "m2"]), \
            patch.object(llm_protocols, "chat_completion", fake_chat):
        return llm_protocols.quota_chat_probe("http://relay.example/v1", "sk-test")


def test_auth_failure_marks_invalid():
    result = _probe([
        {"ok": False, "error": "鉴权或请求错误 (HTTP 401): Authentication Fails, api key invalid"},
        {"ok": False, "error": "鉴权或请求错误 (HTTP 401): Authentication Fails, api key invalid"},
    ])
    assert result["status"] == "invalid"
    assert "鉴权" in result["message"]


def test_no_channel_marks_error():
    result = _probe([
        {"ok": False, "error": "HTTP 503: 无可用渠道"},
        {"ok": False, "error": "HTTP 503: 无可用渠道"},
    ])
    assert result["status"] == "error"
    assert "不可用" in result["message"]


def test_rate_limit_stays_unconfirmed():
    result = _probe([
        {"ok": False, "error": "HTTP 429: rate limited"},
        {"ok": False, "error": "请求超时（20 秒）"},
    ])
    assert result is None


def test_success_marks_chat_probe_available():
    result = _probe([{"ok": True, "output": "hi"}])
    assert result == {"account_info": {"chat_probe": "可用"}}


def test_probe_runs_even_when_balance_known():
    module = SimpleNamespace(
        quota_probe=lambda data: {"status": "invalid", "message": "对话鉴权失败"})
    result = VerifyResult(status="valid", message="凭证有效",
                          account_info={"balance": 100, "currency": "USD"})
    plugin_manager._apply_quota_probe("llm_test", module, {}, result)
    assert result.status == "invalid"
    assert result.message == "对话鉴权失败"
    assert result.account_info["balance"] == 100


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
