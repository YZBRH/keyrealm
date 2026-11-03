"""国信证券插件：清单、签名算法与验证分支（不联网）。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLUGIN_PATH = ROOT / "plugins" / "exchange" / "guosen" / "plugin.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("guosen_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def test_manifest_identity():
    import json
    manifest = json.loads((PLUGIN_PATH.parent / "manifest.json").read_text())
    assert manifest["name"] == "国信证券"
    assert manifest["credential_type"] == "exchange_guosen"
    kinds = next(f for f in manifest["fields"] if f["key"] == "kind")
    values = [o["value"] for o in kinds["options"]]
    assert values == ["skill_gateway", "openapi_aksk"]
    sensitive = {f["key"] for f in manifest["fields"] if f.get("sensitive")}
    assert sensitive == {"api_key", "access_key", "secret_key"}


def test_openapi_signature_matches_official_vector():
    signature = plugin.openapi_signature(
        "GET", "/index.html", "name=james&age=36", "user-key", "my-secret-key",
        "Tue, 19 Jan 2021 11:33:20 GMT",
        {"User-Agent": "curl/7.29.0", "x-custom-a": "test"},
    )
    assert signature == "8XV1GB7Tq23OJcoz6wjqTs4ZLxr9DiLoY4PxzScWGYg="


def test_openapi_signature_sorts_query():
    a = plugin.openapi_signature("GET", "/x", "b=2&a=1", "ak", "sk", "date")
    b = plugin.openapi_signature("GET", "/x", "a=1&b=2", "ak", "sk", "date")
    assert a == b


def test_validate_requires_kind_specific_fields():
    assert plugin.validate({"kind": "skill_gateway"}) == [
        "GS_API_KEY 不能为空", "接入渠道（softName）不能为空",
    ]
    assert plugin.validate({"kind": "openapi_aksk"}) == [
        "Access Key（AK）不能为空", "Secret Key（SK）不能为空",
    ]
    assert plugin.validate({
        "kind": "skill_gateway", "api_key": "V2V-x", "soft_name": "tx_skills",
    }) == []
    assert plugin.validate({
        "kind": "openapi_aksk", "access_key": "ak", "secret_key": "sk",
    }) == []


def test_openapi_without_verify_url_is_unconfirmed():
    result = plugin.verify({"kind": "openapi_aksk", "access_key": "ak", "secret_key": "sk"})
    assert result.status == "error"
    assert "验证接口 URL" in result.message


def test_result_of_accepts_object_and_list():
    assert plugin._result_of({"result": {"code": 0, "msg": "ok"}})["code"] == 0
    assert plugin._result_of({"result": [{"code": 197001}]})["code"] == 197001
    assert plugin._result_of({"result": []}) == {}


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
