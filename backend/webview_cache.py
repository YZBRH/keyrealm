"""更新后清理 WebView 缓存（保留界面偏好）。

pywebview 用内置 HTTP 服务托管前端，WebView2 / WebEngine 会缓存 ``index.html``。
测试版更新替换了 runtime 里的前端资源，但旧页面仍可能从缓存加载，表现为
「版本号已是新 commit，界面却没有新功能」。

这里在**构建版本变化时**只删除浏览器缓存目录（Cache / Code Cache / GPUCache 等），
不动 Local Storage / IndexedDB / Cookies，因此主题、列顺序等偏好保留。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Optional

from . import database as db
from . import logger
from . import version
from .paths import resource_dir

_log = logger.get_logger(__name__)

MARKER = ".last-build"
# 各内核的磁盘缓存目录名；不包含 Local Storage / IndexedDB / Cookies / Network。
_CACHE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "GrShaderCache",
    "ShaderCache",
}


def _marker_path(webview_dir: Path) -> Path:
    return Path(webview_dir) / MARKER


def _frontend_fingerprint() -> str:
    """前端构建指纹：index.html 里带资源哈希，同一 commit 重新构建前端也能识别。"""
    index = Path(resource_dir()) / "frontend" / "dist" / "index.html"
    try:
        return hashlib.sha256(index.read_bytes()).hexdigest()[:16]
    except OSError:
        return "no-frontend"


def _stamp() -> str:
    return f"{version.APP_VERSION}|{version.build_id()}|{_frontend_fingerprint()}"


def clear_if_build_changed(webview_dir: Optional[Path] = None,
                           stamp: Optional[str] = None) -> bool:
    """构建版本变化时清理缓存目录；返回是否执行了清理。

    ``stamp`` 仅供测试覆盖；生产环境用 ``APP_VERSION + build_id``。
    """
    root = Path(webview_dir) if webview_dir is not None else (Path(db.DATA_DIR) / "webview")
    current = stamp or _stamp()
    marker = _marker_path(root)
    try:
        previous = marker.read_text(encoding="utf-8").strip()
    except OSError:
        previous = ""
    if previous == current:
        return False

    removed: list[str] = []
    failed = 0
    if root.is_dir():
        try:
            targets = [
                path for path in root.rglob("*")
                if path.is_dir() and path.name in _CACHE_DIRS
            ]
        except OSError as exc:
            _log.warning("枚举 WebView 缓存失败：%s", exc)
            targets = []
        # 浅层优先：父缓存删掉后，子目录已不存在，不再重复处理。
        targets.sort(key=lambda p: len(p.parts))
        for path in targets:
            if not path.exists():
                continue
            try:
                shutil.rmtree(path)
                removed.append(str(path.relative_to(root)))
            except OSError as exc:
                failed += 1
                _log.warning("清理 WebView 缓存失败（%s）：%s", path, exc)

    # 有删除失败就不推进标记：下次启动继续尝试，避免一次占用导致永远清不掉。
    if not failed:
        try:
            root.mkdir(parents=True, exist_ok=True)
            marker.write_text(current, encoding="utf-8")
        except OSError as exc:
            _log.warning("写入 WebView 缓存标记失败：%s", exc)

    if removed:
        _log.info("构建版本已变化，已清理 WebView 缓存 %d 项（保留界面偏好）", len(removed))
    elif previous:
        _log.info("构建版本已变化，未发现可清理的 WebView 缓存目录")
    return bool(removed)
