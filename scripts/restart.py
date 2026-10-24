#!/usr/bin/env python3
"""重启 Keyrealm（**可单独运行，无交互**）。

    python scripts/restart.py            # 直接重启（优先可执行文件，否则 python main.py --gui）
    python scripts/restart.py --wait     # 先等应用完全退出（并占锁）再重启

新进程与当前终端脱离，关掉本终端的窗口不影响它。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _logutil import tee_run_log  # noqa: E402
from backend import paths  # noqa: E402

paths.configure_data_dir()             # 与桌面/CLI 使用同一份数据目录配置
from backend import updater  # noqa: E402


def main() -> int:
    tee_run_log("restart")
    ap = argparse.ArgumentParser(description="重启 Keyrealm")
    ap.add_argument("--wait", action="store_true", help="先等应用完全退出再重启")
    args = ap.parse_args()

    if args.wait:
        if not updater.wait_for_app_exit():
            print("✗ 等待应用退出超时", file=sys.stderr)
            return 1
        from backend import single_instance  # 释放占住的锁，否则新实例会被判为第二实例
        single_instance.release()

    result = updater.relaunch_app()
    if result.get("ok"):
        print(f"✓ 已重启（pid={result.get('pid')}）")
        return 0
    print(f"✗ {result.get('message')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
