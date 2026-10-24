#!/usr/bin/env python3
"""检查 Keyrealm 是否有新版本（**可单独运行，无交互**）。

    python scripts/check_version.py --kind formal
    python scripts/check_version.py --kind test --json

只读：会用 ``git clone --depth 1`` 到临时目录读远端版本，**不改动当前工作区**。
远端地址可用环境变量 ``KEYREALM_UPDATE_REMOTE`` 覆盖（离线自测用）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 中文 Windows 控制台默认 GBK，输出 ✓/✗/中文会 UnicodeEncodeError
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
    tee_run_log("check_version")
    ap = argparse.ArgumentParser(description="检查 Keyrealm 是否有新版本（默认正式版口径）")
    ap.add_argument("--kind", choices=list(updater.CHANNELS), default="formal",
                    help="formal=正式版（看版本号）/ test=测试版（看版本号+commit）")
    ap.add_argument("--branch", default=updater.DEFAULT_BRANCH)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    args = ap.parse_args()

    result = updater.check_update(args.kind, branch=args.branch, timeout=args.timeout)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"✗ 检查更新失败：{result.get('message') or result.get('error')}", file=sys.stderr)
        return 1

    cur, latest = result["current"], result["latest"]
    print(f"渠道     : {'测试版' if args.kind == 'test' else '正式版'}")
    print(f"当前     : {cur['version']} ({cur['commit']})")
    print(f"远端     : {latest['version']} ({latest['commit']})")
    if result.get("db_change"):
        print(f"数据库   : 将逐级迁移 {' → '.join(result.get('db_chain') or [])}")
    if result["has_update"]:
        print(f"✓ 有更新：{result['reason']}")
    else:
        print(f"✓ 无需更新：{result['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
