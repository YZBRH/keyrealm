#!/usr/bin/env python3
"""数据库结构**逐级**迁移（默认只演练；``--apply`` 才写库）。**可单独运行，无交互**。

    python scripts/db_update.py            # 演练：只打印将执行的迁移链
    python scripts/db_update.py --apply    # 逐级写库（每跳前留 SQLite 快照）
    python scripts/db_update.py --list     # 只列当前/目标版本与迁移链

逐级约定见 ``scripts/migrations/README.md``：只提供「低一级 → 高一级」的脚本，运行器把它们
串起来一层层升，**绝不跨级**。``--apply`` 前会抢一次单实例锁，应用在跑就拒绝执行。
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
    tee_run_log("db_update")
    ap = argparse.ArgumentParser(description="逐级迁移 Keyrealm 数据库结构（默认只演练）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只演练，不改任何东西）")
    ap.add_argument("--list", action="store_true", help="只列出版本与迁移链")
    args = ap.parse_args()

    status = updater.db_status()
    if not status.get("ok"):
        print(f"✗ {status.get('message')}", file=sys.stderr)
        return 1

    if args.list:
        print(f"磁盘库版本 : {status.get('disk') or '（尚未创建数据库）'}")
        print(f"源码版本   : {status.get('app')}")
        chain = status.get("chain") or []
        print("迁移链     : " + (" → ".join(chain) if chain else "（无需迁移）"))
        return 0

    result = updater.run_migrations(apply=args.apply)
    ok = bool(result.get("ok"))
    print(("✓ " if ok else "✗ ") + str(result.get("message", "")))
    steps = result.get("steps") or result.get("applied") or []
    if steps:
        print("  步骤: " + " , ".join(steps))
    if not args.apply and ok and steps:
        print("\n（演练结束。加 --apply 才会真正写入；每跳前会自动留数据库快照。）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
