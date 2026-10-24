"""集成测试共用的小工具（不参与 pytest 收集）。"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.1)
    return False


def wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=0.5)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return False


def cli_env(password: str | None = "TestPass!123") -> dict:
    env = os.environ.copy()
    env.pop("KEYREALM_MASTER_PASSWORD", None)
    env.pop("VAULT_PROXY", None)
    env.pop("VAULT_TLS", None)
    for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env.pop(key, None)
    if password is not None:
        env["KEYREALM_MASTER_PASSWORD"] = password
    return env


def run_cli(data_dir: str | Path, *args: str, password: str | None = "TestPass!123",
            timeout: float = 90, with_data_dir: bool = True) -> subprocess.CompletedProcess:
    cmd = [PYTHON, str(MAIN), "--cli"]
    if with_data_dir:
        cmd += ["--data-dir", str(data_dir)]
    cmd += list(args)
    return subprocess.run(
        cmd, cwd=str(ROOT), env=cli_env(password), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def stop_process(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
