"""CLI MCP：前台启动、Token、鉴权、SSE、完整 MCP 调用、优雅停止、状态。"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testutil import MAIN, PYTHON, cli_env, free_port, port_open, run_cli, stop_process, wait_port  # noqa: E402

PASSWORD = "TestPass!123"


def _json(res):
    assert res.returncode == 0, f"命令失败：{res.stderr}\n{res.stdout}"
    return json.loads(res.stdout)


def _wait_token(log: Path, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        match = re.search(r"kr_[0-9a-f]+", text)
        if match:
            return match.group(0)
        time.sleep(0.1)
    raise AssertionError("未在日志中找到 MCP Token")


async def _mcp_round_trip(url: str, token: str) -> tuple[int, int]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("vault_list_credentials", {})
            text = " ".join(
                getattr(item, "text", "") for item in (result.content or [])
            )
            assert "web-main" in text or "credential_type" in text, text[:200]
            return len(tools.tools), len(result.content or [])


def test_mcp_lifecycle():
    with tempfile.TemporaryDirectory(prefix="kr-mcp-") as tmp:
        assert run_cli(tmp, "init", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "add", "--type", "api_fofa", "--name", "web-main",
                       "-f", "api_key=mcp-secret", "--no-verify", password=PASSWORD).returncode == 0
        assert run_cli(tmp, "mcp", "permission", "vault_list_credentials", "on",
                       password=PASSWORD).returncode == 0
        unknown = run_cli(tmp, "mcp", "permission", "not_a_tool", "on", password=PASSWORD)
        assert unknown.returncode == 1 and "未知 MCP 工具" in unknown.stderr

        port = free_port()
        log = Path(tmp) / "mcp.log"
        with open(log, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen(
                [PYTHON, str(MAIN), "--cli", "--data-dir", tmp,
                 "mcp", "start", "--host", "127.0.0.1", "--port", str(port), "--new-token"],
                cwd=str(ROOT), env=cli_env(PASSWORD), stdout=sink, stderr=subprocess.STDOUT,
            )
            try:
                assert wait_port("127.0.0.1", port, timeout=30), log.read_text(encoding="utf-8", errors="replace")
                token = _wait_token(log)
                base = f"http://127.0.0.1:{port}"

                assert httpx.get(f"{base}/sse", timeout=5).status_code == 401
                status = _json(run_cli(tmp, "--json", "mcp", "status", password=PASSWORD))
                assert status["port_open"] is True
                assert status["host"] == "127.0.0.1" and status["port"] == port
                assert status["token_prefix"] == token[:8]

                with httpx.stream("GET", f"{base}/sse",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=5) as stream:
                    assert stream.status_code == 200
                    lines = []
                    for line in stream.iter_lines():
                        lines.append(line)
                        if len(lines) >= 2:
                            break
                    assert any(line.startswith("event: endpoint") for line in lines), lines

                tools, contents = asyncio.run(_mcp_round_trip(f"{base}/sse", token))
                assert tools >= 20 and contents >= 1
            finally:
                stop_process(proc)
                assert not port_open("127.0.0.1", port), "MCP 停止后端口仍被占用"

        status = _json(run_cli(tmp, "--json", "mcp", "status", password=PASSWORD))
        assert status["running_in_this_process"] is False
        assert status["port_open"] is False
        assert status["enabled_setting"] is False


if __name__ == "__main__":
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
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
