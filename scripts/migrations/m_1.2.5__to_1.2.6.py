"""1.2.5 → 1.2.6：百度 AI 插件目录 api/baidu_face 改名为 api/baidu_ai。

1.2.5 已把 credential_type 改成 api_baidu_ai，但目录名仍是 baidu_face。更新器对 plugins/
是合并安装，不会删除旧目录；这里在确认新目录已就位、旧目录确实是退役插件后删除旧目录，
并把插件镜像表的 plugin_path 更新为新路径。旧目录校验失败时只告警，不影响其它迁移。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

FROM_VERSION = "1.2.5"
TO_VERSION = "1.2.6"

ROOT = Path(__file__).resolve().parent.parent.parent
_OLD_DIR = ROOT / "plugins" / "api" / "baidu_face"
_NEW_DIR = ROOT / "plugins" / "api" / "baidu_ai"
_OLD_TYPE = "api_baidu_face"
_NEW_TYPE = "api_baidu_ai"
_log = logging.getLogger(__name__)


def _count(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _manifest_type(directory: Path) -> str:
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("credential_type") or "") if isinstance(data, dict) else ""


def preview(conn) -> dict:
    return {
        "plugin_path": _count(
            conn,
            "SELECT COUNT(*) FROM plugins WHERE credential_type=? AND plugin_path=?",
            (_NEW_TYPE, "api/baidu_face")),
        "old_dir": _OLD_DIR.is_dir(),
        "new_dir": _NEW_DIR.is_dir(),
        "old_dir_type": _manifest_type(_OLD_DIR) if _OLD_DIR.is_dir() else "",
        "new_dir_type": _manifest_type(_NEW_DIR) if _NEW_DIR.is_dir() else "",
    }


def _remove_old_dir() -> None:
    if not _OLD_DIR.is_dir() or _OLD_DIR.is_symlink():
        return
    if _manifest_type(_OLD_DIR) != _OLD_TYPE:
        _log.warning("旧百度插件目录的 manifest 不是 %s，保留不动：%s", _OLD_TYPE, _OLD_DIR)
        return
    if not _NEW_DIR.is_dir() or _manifest_type(_NEW_DIR) != _NEW_TYPE:
        _log.warning("新百度插件目录未就位，暂不删除旧目录：%s", _OLD_DIR)
        return
    shutil.rmtree(_OLD_DIR)
    _log.info("已删除退役的百度插件目录：%s", _OLD_DIR)


def apply(conn) -> None:
    conn.execute(
        "UPDATE plugins SET plugin_path=? "
        "WHERE credential_type=? AND plugin_path=?",
        ("api/baidu_ai", _NEW_TYPE, "api/baidu_face"),
    )
    try:
        _remove_old_dir()
    except Exception as exc:  # noqa: BLE001 - 文件清理失败不拖垮 DB 迁移
        _log.warning("删除旧百度插件目录失败（可手动删除）：%s", exc)
