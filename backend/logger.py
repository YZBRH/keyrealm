"""统一日志管理。

- 输出：滚动文件（始终）+ 控制台（开发/非冻结时）。文件位于 <data_dir>/logs/，
  与数据库同属可写数据目录（默认 <入口目录>/data，可经 keyrealm.conf.json 或 CLI
  --data-dir 改到别处）。
- 级别：DEBUG/INFO/WARNING/ERROR/CRITICAL，可运行时调整，并持久化到 settings 表
  （key="log_level"），下次启动自动恢复。
- 第三方噪声：httpx/httpcore 的 INFO（每次请求一行）默认压到 WARNING，避免刷屏。

其余模块统一用 `logger.get_logger(__name__)` 取 logger，不要直接 print。
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import data_dir

# ── 常量 ──────────────────────────────────────────────────
LOG_DIR = data_dir() / "logs"
LOG_FILE = LOG_DIR / "keyrealm.log"
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_LEVEL = "INFO"

_MAX_BYTES = 1_000_000   # 单个日志文件上限 ~1MB
_BACKUP_COUNT = 5        # 保留 5 个轮转备份（.log.1 ~ .log.5）

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
# 匹配一条日志记录的起始行（用于把 traceback 等多行归并到同一条）
_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(\w+)\]\s")

_configured = False


def _norm(level) -> int:
    """把级别名/整数规整为 logging 级别整数，非法值回退 INFO。"""
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).strip().upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_level() -> str:
    return logging.getLevelName(logging.getLogger().level)


def set_level(level: str) -> None:
    """运行时调整根 logger 级别（不持久化，持久化由调用方写 settings）。"""
    logging.getLogger().setLevel(_norm(level))


def setup_logging(level: str = DEFAULT_LEVEL, console: bool | None = None) -> None:
    """初始化根 logger。幂等：重复调用只调整级别，不重复加 handler。

    console 为 None 时：冻结（GUI 打包）默认不接控制台，开发模式接 stdout。
    """
    global _configured
    if _configured:
        set_level(level)
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(_norm(level))
    fmt = logging.Formatter(_FMT, _DATEFMT)

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console is None:
        console = not getattr(sys, "frozen", False)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # 压制第三方 chatty logger（其 INFO/DEBUG 对用户无意义）。
    # PIL.PngImagePlugin 在每次 Image.open() 时会刷 STREAM b'IHDR' / b'IDAT' 一类的
    # 分块调试行，标识图处理（backend/icon_ink.py）每次启动都会触发，须一并压掉。
    for noisy in ("httpx", "httpcore", "PIL.PngImagePlugin"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def log_file_path() -> str:
    return str(LOG_FILE)


def read_logs(limit: int = 200, level: str | None = None,
              search: str | None = None) -> list[str]:
    """读取当前日志文件末尾若干条记录。

    - 以「时间戳起始行」为一条记录，后续无时间戳的行（如 traceback）并入同一条；
    - level 给定时只保留级别 >= 该阈值的记录；
    - search 给定时只保留**整条记录**（含 traceback 等续行）包含该子串的记录，
      大小写不敏感；多个空白分隔的关键词需全部命中（AND）。
    - 返回字符串列表，每项是一条完整记录（可能含换行）。
    """
    if not LOG_FILE.exists():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    records: list[list[str]] = []
    for ln in text.splitlines():
        if _LINE_RE.match(ln) or not records:
            records.append([ln])
        else:
            records[-1].append(ln)

    if level:
        threshold = _norm(level)
        kept = []
        for rec in records:
            m = _LINE_RE.match(rec[0])
            if m and getattr(logging, m.group(1).upper(), 0) >= threshold:
                kept.append(rec)
        records = kept

    terms = [t for t in (search or "").lower().split() if t]
    if terms:
        records = [
            rec for rec in records
            if all(t in "\n".join(rec).lower() for t in terms)
        ]

    limit = max(1, min(int(limit or 200), 2000))
    return ["\n".join(rec) for rec in records[-limit:]]


def clear_logs() -> None:
    """清空当前日志文件与全部轮转备份（显式枚举，不用通配符）。"""
    root = logging.getLogger()
    # 先摘掉并关闭文件 handler，避免 Windows 下句柄占用导致清空后写入错位
    file_handlers = [
        h for h in root.handlers
        if isinstance(h, RotatingFileHandler)
        and Path(h.baseFilename).resolve() == LOG_FILE.resolve()
    ]
    for h in file_handlers:
        h.close()
        root.removeHandler(h)

    try:
        LOG_FILE.write_text("", encoding="utf-8")
        for i in range(1, _BACKUP_COUNT + 1):
            backup = LOG_FILE.with_name(f"{LOG_FILE.name}.{i}")
            if backup.exists():
                backup.unlink()
    finally:
        # 重新挂回文件 handler，保持后续日志继续写入
        if file_handlers or _configured:
            fmt = logging.Formatter(_FMT, _DATEFMT)
            fh = RotatingFileHandler(
                LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
