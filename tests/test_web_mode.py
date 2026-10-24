"""Web 模式：Token 鉴权、前端注入、RPC、SSE、备份、文件接口、数据目录配置。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testutil import MAIN, PYTHON, cli_env, free_port, port_open, run_cli, stop_process, wait_http  # noqa: E402

PASSWORD = "TestPass!123"
TOKEN = "integration-token"
_ORIGINAL_CONFIG: bytes | None = None


def setup_module():
    global _ORIGINAL_CONFIG
    cfg = ROOT / "keyrealm.conf.json"
    _ORIGINAL_CONFIG = cfg.read_bytes() if cfg.is_file() else None


def teardown_module():
    cfg = ROOT / "keyrealm.conf.json"
    try:
        if _ORIGINAL_CONFIG is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_bytes(_ORIGINAL_CONFIG)
    except OSError:
        pass


def _rpc(base: str, method: str, *args):
    resp = httpx.post(
        f"{base}/api/call",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"method": method, "args": list(args)},
        timeout=30,
    )
    assert resp.status_code == 200, f"{method}: HTTP {resp.status_code} {resp.text[:200]}"
    return resp.json()


def test_web_mode():
    with tempfile.TemporaryDirectory(prefix="kr-web-") as tmp:
        assert run_cli(tmp, "init", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "add", "--type", "api_fofa", "--name", "web-main",
                       "-f", "api_key=web-secret", "--no-verify",
                       password=PASSWORD).returncode == 0

        cfg = ROOT / "keyrealm.conf.json"
        cfg.write_text(json.dumps({"data_dir": tmp}), encoding="utf-8")

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        log = Path(tmp) / "web.log"
        import subprocess
        with open(log, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen(
                [PYTHON, str(MAIN), "--web", "--no-browser", "--port", str(port),
                 "--token", TOKEN],
                cwd=str(ROOT), env=cli_env(None), stdout=sink, stderr=subprocess.STDOUT,
            )
            try:
                assert wait_http(f"{base}/", timeout=30), log.read_text(encoding="utf-8", errors="replace")
                # 未带 token
                assert httpx.get(f"{base}/", timeout=5).status_code == 401
                index = httpx.get(f"{base}/?token={TOKEN}", timeout=5)
                assert index.status_code == 200
                assert "__KEYREALM_WEB__" in index.text and "web-bridge.js" in index.text
                bridge = httpx.get(f"{base}/web-bridge.js", timeout=5)
                assert bridge.status_code == 200 and "KEYREALM_WEB" in bridge.text

                assert _rpc(base, "is_initialized")["initialized"] is True
                bad = _rpc(base, "unlock", "wrong")
                assert bad.get("ok") is False
                good = _rpc(base, "unlock", PASSWORD)
                assert good.get("ok") is True

                creds = _rpc(base, "list_credentials", None, None, None, None, None)
                assert any(c["name"] == "web-main" for c in creds)
                created = _rpc(base, "create_credential", "rpc-made", "api_fofa",
                               {"api_key": "rpc-secret"}, None, False)
                assert created.get("id")
                creds = _rpc(base, "list_credentials", None, None, None, None, None)
                assert len(creds) == 2

                exported = httpx.post(
                    f"{base}/api/export_backup",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={"scope": "all", "encrypted": True}, timeout=30,
                )
                assert exported.status_code == 200
                doc = exported.json()
                assert doc.get("ok") is True and doc["document"]["counts"]["credentials"] == 2

                # 日志接口：拒绝路径穿越
                assert httpx.get(f"{base}/api/log_file?token={TOKEN}&name=../../credentials.db",
                                 timeout=5).status_code == 400

                # SSE 至少能建立并收到首帧
                with httpx.stream("GET", f"{base}/api/events?token={TOKEN}", timeout=5) as stream:
                    assert stream.status_code == 200
                    lines = []
                    for line in stream.iter_lines():
                        lines.append(line)
                        if len(lines) >= 2:
                            break
                    assert any(line.startswith(":") or line.startswith("data:")
                               for line in lines), lines
            finally:
                stop_process(proc)
                assert not port_open("127.0.0.1", port), "Web 进程退出后端口仍被占用"
                try:
                    cfg.unlink(missing_ok=True)
                except OSError:
                    pass


if __name__ == "__main__":
    setup_module()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    teardown_module()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
