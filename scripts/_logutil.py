"""CLI 脚本的**运行日志**：把 stdout/stderr 复制一份到 ``data/logs/<name>-<时间戳>.log``。

为什么做在 Python 侧、而不是 ``.sh``/``.bat`` 里：

- ``.bat`` **没有** ``tee``，Windows 上要用 ``Tee-Object`` 才勉强能同屏 + 落盘（未实测）；
- 写在各 ``.py`` 的入口，``.sh`` / ``.bat`` / 直接跑 ``.py`` **三种方式全覆盖**，跨平台一致，
  且不改 ``.sh`` 里 ``exec`` 的语义（退出码、信号都原样）。

用法：脚本 ``main()`` 一开头 ``from _logutil import tee_run_log; tee_run_log("db_update")``。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


class _Tee:
    """把写入同时转发到原流与日志文件。"""

    def __init__(self, stream, sink):
        self._stream = stream
        self._sink = sink

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:  # noqa: BLE001 - 原流失败不影响落盘
            pass
        try:
            self._sink.write(data)
        except Exception:  # noqa: BLE001
            pass
        return len(data)

    def flush(self):
        for s in (self._stream, self._sink):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def __getattr__(self, name):        # 其余属性（encoding / isatty / reconfigure …）委托给原流
        return getattr(self._stream, name)


def tee_run_log(name: str) -> "Path | None":
    """开始把本进程 stdout/stderr 落 ``<data_dir>/logs/<name>-<ts>.log``；返回日志路径（失败返回 None）。"""
    try:
        from backend import paths
        paths.configure_data_dir()          # 脚本入口同样遵循 keyrealm.conf.json
        log_dir = paths.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        sink = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:  # noqa: BLE001 - 记不了日志不该让脚本起不来
        return None
    sys.stdout = _Tee(sys.stdout, sink)
    sys.stderr = _Tee(sys.stderr, sink)
    try:
        print(f"=== {name} 开始（{time.strftime('%Y-%m-%d %H:%M:%S')}）argv={sys.argv[1:]} ===")
    except Exception:  # noqa: BLE001
        pass
    return path
