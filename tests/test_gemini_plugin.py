"""Gemini 错误分类：Key 有效但被 API 限制时不应笼统报「无法确认」。"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PLUGIN_DIR = ROOT / "plugins" / "llm"
sys.path.insert(0, str(PLUGIN_DIR))
PLUGIN_PATH = PLUGIN_DIR / "gemini" / "plugin.py"

spec = importlib.util.spec_from_file_location("gemini_plugin_test", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return self._response


def _probe(status, payload):
    response = FakeResponse(status, payload)
    with patch.object(plugin, "make_client", lambda: FakeClient(response)):
        return plugin._probe("https://generativelanguage.googleapis.com/v1beta", "test-key")


def test_service_blocked_is_valid_but_restricted():
    result = _probe(403, {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "Requests to this API are blocked.",
            "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}],
        }
    })
    assert result.status == "error"
    assert "API Key 有效" in result.message
    assert "API_KEY_SERVICE_BLOCKED" in result.message


def test_api_key_invalid_is_invalid():
    result = _probe(400, {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "API key not valid. Please pass a valid API key.",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    })
    assert result.status == "invalid"


def test_success_is_valid():
    result = _probe(200, {"models": [{"name": "models/gemini-1.5"}]})
    assert result.status == "valid"


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
