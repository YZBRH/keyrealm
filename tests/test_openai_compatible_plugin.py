"""兼容接口插件的 Responses / Anthropic 回退与额外请求头。"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LLM_DIR = ROOT / "plugins" / "llm"
sys.path.insert(0, str(LLM_DIR))

import llm_protocols  # noqa: E402
from backend.plugin_sdk import VerifyResult  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "compat_plugin_test", LLM_DIR / "openai_compatible" / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body
        self._payload = None

    def json(self):
        if self._payload is not None:
            return self._payload
        import json
        try:
            self._payload = json.loads(self.text)
        except Exception:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.last_headers = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.last_headers = kwargs.get("headers")
        return self.response


def test_extra_headers_parsing():
    headers = plugin._extra_headers({
        "extra_headers": "# comment\nx-opencode-session: abc\nX-Test: 1:2\nbad line\n",
    })
    assert headers == {"x-opencode-session": "abc", "X-Test": "1:2"}


def test_responses_style_needs_model():
    result = plugin.verify({
        "api_style": "openai-responses", "base_url": "https://relay.example/v1",
        "api_key": "sk-test",
    })
    assert result.status == "error"
    assert "模型" in result.message


def test_responses_probe_sends_session_header_and_valid():
    client = FakeClient(FakeResponse(200, '{"id":"resp_1","status":"incomplete"}'))
    with patch.object(llm_protocols, "make_client", lambda *a, **k: client):
        result = plugin.verify({
            "api_style": "openai-responses", "base_url": "https://relay.example/v1",
            "api_key": "sk-test", "probe_model": "deepseek-v4-pro",
            "extra_headers": "x-opencode-session: session-1",
        })
    assert result.status == "valid"
    assert client.last_headers["x-opencode-session"] == "session-1"
    assert client.last_headers["Authorization"] == "Bearer sk-test"


def test_responses_probe_insufficient_is_valid():
    client = FakeClient(FakeResponse(402, '{"code":"INSUFFICIENT_BALANCE"}'))
    with patch.object(llm_protocols, "make_client", lambda *a, **k: client):
        result = plugin.verify({
            "api_style": "openai-responses", "base_url": "https://relay.example/v1",
            "api_key": "sk-test", "probe_model": "deepseek-v4-pro",
        })
    assert result.status == "valid"
    assert "欠费" in result.message or "额度" in result.message


def test_anthropic_falls_back_to_messages_when_models_missing():
    fallback = Mock(return_value=VerifyResult(status="valid", message="凭证有效（deepseek-chat）"))
    with patch.object(plugin, "probe_anthropic_models",
                      lambda *a, **k: VerifyResult(status="error", message="无法确认凭证状态 (HTTP 404)")), \
            patch.object(plugin, "probe_anthropic_messages", fallback):
        result = plugin.verify({
            "api_style": "anthropic", "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-test",
        })
    assert result.status == "valid"
    assert "deepseek-chat" in result.message
    assert fallback.called


def test_actions_hidden_for_responses():
    assert plugin.actions({
        "api_style": "openai-responses", "api_key": "sk-test"}) == []


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
