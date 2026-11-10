"""Gemini 模型列表过滤 / 默认排序 / 错误细分测试（不联网）。

运行：.venv/bin/python tests/test_gemini_models.py
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LLM_DIR = ROOT / "plugins" / "llm"
sys.path.insert(0, str(LLM_DIR))

import llm_protocols  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "gemini_plugin", ROOT / "plugins" / "llm" / "gemini" / "plugin.py")
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)


def _item(name, methods):
    return {"name": f"models/{name}", "supportedGenerationMethods": methods}


def test_text_model_filter():
    assert gemini._is_text_chat_model(
        _item("gemini-2.5-flash", ["generateContent", "countTokens"]))
    assert gemini._is_text_chat_model(
        _item("gemini-3.8-flash", ["generateContent"]))
    assert not gemini._is_text_chat_model(
        _item("gemini-embedding-001", ["embedContent"]))
    assert not gemini._is_text_chat_model(
        _item("gemini-2.5-flash-image", ["generateContent"]))
    assert not gemini._is_text_chat_model(
        _item("antigravity-preview-05-2026", ["generateContent"]))
    assert not gemini._is_text_chat_model(
        _item("gemini-omni-1.2-flash", ["generateContent"]))
    assert not gemini._is_text_chat_model(
        _item("gemini-3.8-flash-tts", ["generateContent"]))
    assert not gemini._is_text_chat_model(
        _item("deep-research-pro-preview-12-2025", ["generateContent"]))


def test_model_ordering_prefers_stable_aliases():
    names = [
        "antigravity-preview-05-2026",
        "gemini-2.5-flash",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-pro-latest",
        "gemini-3.8-flash",
    ]
    ordered = gemini._ordered_models(names)
    assert ordered[0] == "gemini-flash-lite-latest"
    assert ordered[1] == "gemini-flash-latest"
    assert ordered.index("gemini-3.8-flash") < ordered.index("gemini-pro-latest")
    assert ordered[-1] == "antigravity-preview-05-2026"


def test_chat_candidates_respect_explicit_model():
    assert gemini._chat_candidates({}, "gemini-2.5-pro") == ["gemini-2.5-pro"]
    with patch.object(gemini, "list_models", lambda data, force=False: ["a", "b", "c", "d", "e"]):
        assert gemini._chat_candidates({}, "") == ["a", "b", "c", "d"]


def test_model_level_error_detection():
    assert gemini._is_model_level_error("该模型已对当前项目退役（HTTP 404）")
    assert gemini._is_model_level_error("鉴权或请求错误 (HTTP 400): ...")
    assert gemini._is_model_level_error("HTTP 429")
    assert gemini._is_model_level_error("HTTP 503: high demand")
    assert not gemini._is_model_level_error("网络请求失败（ReadTimeout）")
    assert not gemini._is_model_level_error("请求超时（20 秒）")


def test_gemini_error_hints():
    def hint(status, message, text=""):
        return llm_protocols._gemini_error_hint(
            status, {"error": {"message": message}}, text)

    assert "退役" in hint(404, "This model models/gemini-2.5-pro is no longer available to new users.")
    assert "Interactions API" in hint(400, "This model only supports Interactions API.")
    assert "TTS" in hint(400, "The requested combination of response modalities (TEXT) is not supported by the model.")
    assert "免费层" in hint(429, "You exceeded your current quota",
                            '{"violations":[{"quotaMetric":"generate_content_free_tier_requests","limit":0}]}')
    assert hint(500, "internal error") == ""


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        self.requests.append(params or {})
        return self.responses.pop(0)


def test_gemini_model_pagination():
    page1 = httpx.Response(200, json={
        "models": [_item("a", ["generateContent"]), _item("b", ["generateContent"])],
        "nextPageToken": "tok",
    })
    page2 = httpx.Response(200, json={
        "models": [_item("c", ["generateContent"])],
    })
    fake = _FakeClient([page1, page2])
    with patch.object(llm_protocols, "make_client", lambda *a, **k: fake):
        items = llm_protocols._fetch_model_items("gemini", "https://example/v1beta", "k")
    assert [i["name"] for i in items] == ["models/a", "models/b", "models/c"]
    assert fake.requests[0].get("pageSize") == 1000
    assert "pageToken" not in fake.requests[0]
    assert fake.requests[1].get("pageToken") == "tok"


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
