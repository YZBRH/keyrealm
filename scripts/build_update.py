#!/usr/bin/env python3
"""Keyrealm 一键更新：**等应用退出 → 备份 → 取源码 → 编译 → 数据库迁移 → 重启**。

**可单独运行，无交互**，应用内「立即更新」就是它被起成脱离进程后跑这套流程。

    python scripts/build_update.py                 # 完整更新
    python scripts/build_update.py --dry-run       # 只打印将执行的步骤
    python scripts/build_update.py --no-restart    # 到迁移为止，不自动重启
    python scripts/build_update.py --no-frontend   # 跳过 npm 前端构建
    python scripts/build_update.py --rollback      # 从最近一次备份恢复
    python scripts/build_update.py --rollback <zip>

顺序与安全：先备份；``git fetch --depth 1`` + ``merge --ff-only``（工作区脏则中止，``--force``
才先 stash）；编译失败不覆盖旧产物；数据库只走「逐级」迁移。
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
    tee_run_log("build_update")
    ap = argparse.ArgumentParser(description="Keyrealm 一键更新（备份→编译→迁移→重启）")
    ap.add_argument("--kind", choices=list(updater.CHANNELS), default="formal")
    ap.add_argument("--no-frontend", action="store_true", help="跳过 npm run build")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-migrate", action="store_true", help="跳过数据库逐级迁移")
    ap.add_argument("--no-restart", action="store_true", help="完成后不重启")
    ap.add_argument("--force", action="store_true", help="工作区有改动时先 git stash 再合并")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的步骤")
    ap.add_argument("--rollback", nargs="?", const="", default=None,
                    metavar="ZIP", help="从备份恢复（省略路径则用最近一次备份）")
    args = ap.parse_args()

    if args.rollback is not None:
        zip_path = Path(args.rollback) if args.rollback else updater.latest_backup()
        if not zip_path:
            print("✗ 没有可用的备份", file=sys.stderr)
            return 2
        result = updater.restore_backup(zip_path)
        print(("✓ " if result.get("ok") else "✗ ") + str(result.get("message", "")))
        return 0 if result.get("ok") else 1

    result = updater.apply_update(
        args.kind,
        frontend=not args.no_frontend,
        do_backup=not args.no_backup,
        do_migrate=not args.no_migrate,
        do_restart=not args.no_restart,
        force=args.force,
        dry_run=args.dry_run,
    )
    ok = bool(result.get("ok"))
    print(("✓ " if ok else "✗ ") + str(result.get("message", "")))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
