"""把凭据类型 ``llm_doubao`` 迁移为 ``llm_ark``。

为什么需要它
------------
「字节豆包」插件已泛化为「**火山方舟**」（`plugins/llm/doubao/` → `plugins/llm/ark/`，
`credential_type` 由 ``llm_doubao`` 改为 ``llm_ark``）——因为方舟是字节的**模型托管平台**，
同一把 key 能调豆包 / DeepSeek / 智谱 GLM / 阿里 Qwen 等，叫「豆包」名不副实。

类型改名后，老库里 ``credential_type='llm_doubao'`` 的凭据会**找不到插件**（显示不出来、
也不能验证）。本项目「1.0 前不做自动兼容」的原则说的是**应用代码里不写迁移逻辑**；
本脚本是**一次性人工运维**，与那条原则不冲突。

改哪几处
--------
- ``credentials.credential_type``：`llm_doubao` → `llm_ark`（**主表**）
- ``plugins.credential_type``：插件注册镜像表。它是 ``UNIQUE``——若 ``llm_ark`` 行已存在
  （应用启动时 `sync_plugins()` 会建），则**删掉旧的 `llm_doubao` 行**，否则改名。
  该表本来就会在下次启动时按 manifest 重建，这里只是顺手清干净。
- ``preset_defaults``（`scope='plugin'` 的插件级默认设置）：`scope_key` 同样按类型存。
- ``plugins/.state.json`` 的 `disabled` 列表（若曾禁用过旧类型）。

用法
----
    python scripts/migrate_doubao_to_ark.py            # 只演练，不改任何东西（默认）
    python scripts/migrate_doubao_to_ark.py --apply    # 真正写库（写前自动备份）

**跑之前请完全退出应用**（脚本会先抢一次单实例锁来确认；抢不到就拒绝执行）。
只读演练不需要退出。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 输出里用了 ✓ / ✗ 等符号——不在 GBK 里，中文 Windows 控制台直接跑会 UnicodeEncodeError。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from backend import paths  # noqa: E402
paths.configure_data_dir()                   # 与桌面/CLI 使用同一份数据目录配置
from backend import database as db          # noqa: E402  (须在 sys.path 调整之后)
from backend import single_instance         # noqa: E402

OLD_TYPE = "llm_doubao"
NEW_TYPE = "llm_ark"


def _db_path(arg: str = "") -> Path:
    return Path(arg).expanduser().resolve() if arg else db.DB_PATH


def _state_file(db_path: Path) -> Path:
    """`plugins/.state.json` —— 与库同级的 BASE_DIR 下（源码树与 release/ 都适用）。"""
    return db_path.parent.parent / "plugins" / ".state.json"


def _survey(conn: sqlite3.Connection) -> dict:
    """盘点要改的东西（只读）。"""
    def one(sql: str, params=()):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0

    return {
        "creds_old": one("SELECT COUNT(*) FROM credentials WHERE credential_type=?", (OLD_TYPE,)),
        "creds_new": one("SELECT COUNT(*) FROM credentials WHERE credential_type=?", (NEW_TYPE,)),
        "plugin_rows": [r[0] for r in conn.execute(
            "SELECT credential_type FROM plugins WHERE credential_type IN (?,?)",
            (OLD_TYPE, NEW_TYPE)).fetchall()],
        "preset_old": one(
            "SELECT COUNT(*) FROM preset_defaults WHERE scope='plugin' AND scope_key=?", (OLD_TYPE,)),
        "preset_new": one(
            "SELECT COUNT(*) FROM preset_defaults WHERE scope='plugin' AND scope_key=?", (NEW_TYPE,)),
        "old_creds": [(r["id"], r["name"]) for r in conn.execute(
            "SELECT id, name FROM credentials WHERE credential_type=? ORDER BY id", (OLD_TYPE,)).fetchall()],
    }


def _migrate_state_file(path: Path, apply: bool) -> tuple[bool, list]:
    """迁移 .state.json 的 disabled 列表；返回 (是否需要改, 改后的列表)。"""
    if not path.is_file():
        return False, []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:  # noqa: BLE001 - 读不动就当没有
        return False, []
    disabled = data.get("disabled")
    if not isinstance(disabled, list) or OLD_TYPE not in disabled:
        return False, disabled if isinstance(disabled, list) else []
    new_list: list = []
    for item in disabled:
        # 旧类型改名；若新类型已在里面则去重
        mapped = NEW_TYPE if item == OLD_TYPE else item
        if mapped not in new_list:
            new_list.append(mapped)
    if apply:
        data["disabled"] = new_list
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)   # 原子替换，同 plugin_manager 的写法
    return True, new_list


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """用 SQLite 自己的 backup API 落一份一致性快照（WAL 下也安全）。

    **必须显式 close**：``with sqlite3.connect(...)`` 只是事务上下文、**不关连接**，
    连接还开着就删文件，Windows 上会 `WinError 32`。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(f"{db_path}.bak-llm-doubao-to-ark-{stamp}")
    bck = sqlite3.connect(str(dest))
    try:
        conn.backup(bck)
    finally:
        bck.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="把凭据类型 llm_doubao 迁移为 llm_ark（默认只演练）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只演练，不改任何东西）")
    ap.add_argument("--db", default="", help=f"指定库路径（默认 {db.DB_PATH}）")
    ap.add_argument("--no-backup", action="store_true", help="写库前不备份（不建议）")
    args = ap.parse_args()

    db_path = _db_path(args.db)
    if not db_path.is_file():
        print(f"✗ 找不到数据库：{db_path}", file=sys.stderr)
        return 2

    if not args.apply:
        print("== 演练模式（不会改动任何文件；要真正执行请加 --apply）==\n")
    else:
        # 写库前必须确认应用没在跑：它持有连接、且启动时会重建 plugins 表，边跑边改会互相打架。
        if not single_instance.acquire():
            print("✗ 检测到应用正在运行。请先**完全退出** Keyrealm，再执行本脚本。", file=sys.stderr)
            return 3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        before = _survey(conn)
        state_path = _state_file(db_path)
        state_dirty, state_new = _migrate_state_file(state_path, apply=False)

        print(f"数据库：{db_path}")
        print(f"  credentials       ：llm_doubao={before['creds_old']}  llm_ark={before['creds_new']}")
        print(f"  plugins(镜像)     ：{before['plugin_rows'] or '（无）'}")
        print(f"  preset_defaults   ：scope_key='llm_doubao'={before['preset_old']}  'llm_ark'={before['preset_new']}")
        print(f"  .state.json       ：{'disabled 含 llm_doubao，需改' if state_dirty else '无需改'}")
        if before["old_creds"]:
            print("\n待迁移的凭据：")
            for cid, name in before["old_creds"]:
                print(f"  #{cid}  {name}")
        else:
            print("\n待迁移的凭据：无")

        nothing = (before["creds_old"] == 0 and before["preset_old"] == 0
                   and OLD_TYPE not in before["plugin_rows"] and not state_dirty)
        if nothing:
            print("\n✓ 无需迁移（库与状态文件里都没有 llm_doubao）。")
            return 0

        if not args.apply:
            print("\n演练结束。加 --apply 才会真正写入（写前会自动备份数据库）。")
            return 0

        # ── 真正写入 ──
        if not args.no_backup:
            dest = _backup(conn, db_path)
            print(f"\n已备份数据库 → {dest}")

        with conn:  # 事务：异常自动回滚
            conn.execute("UPDATE credentials SET credential_type=? WHERE credential_type=?",
                         (NEW_TYPE, OLD_TYPE))
            # plugins 表有 UNIQUE(credential_type)：新行已存在就删旧行，否则改名
            has_new = conn.execute("SELECT 1 FROM plugins WHERE credential_type=?",
                                   (NEW_TYPE,)).fetchone()
            if has_new:
                conn.execute("DELETE FROM plugins WHERE credential_type=?", (OLD_TYPE,))
            else:
                conn.execute("UPDATE plugins SET credential_type=? WHERE credential_type=?",
                             (NEW_TYPE, OLD_TYPE))
            # preset_defaults 主键是 (scope, scope_key)：同样处理冲突
            has_new = conn.execute(
                "SELECT 1 FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
                (NEW_TYPE,)).fetchone()
            if has_new:
                conn.execute("DELETE FROM preset_defaults WHERE scope='plugin' AND scope_key=?",
                             (OLD_TYPE,))
            else:
                conn.execute("UPDATE preset_defaults SET scope_key=? WHERE scope='plugin' AND scope_key=?",
                             (NEW_TYPE, OLD_TYPE))

        if state_dirty:
            _migrate_state_file(state_path, apply=True)
            print(f"已更新 {state_path} 的 disabled 列表 → {state_new}")

        after = _survey(conn)
        print("\n== 迁移完成 ==")
        print(f"  credentials       ：llm_doubao={after['creds_old']}  llm_ark={after['creds_new']}")
        print(f"  plugins(镜像)     ：{after['plugin_rows'] or '（无）'}")
        ok = after["creds_old"] == 0 and after["preset_old"] == 0 and OLD_TYPE not in after["plugin_rows"]
        print("✓ 库里已无 llm_doubao" if ok else "✗ 仍有残留，请检查")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
