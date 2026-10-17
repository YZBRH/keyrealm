"""单实例闸门：同一台机器只允许跑一套 Keyrealm。

第二套进程**不创建界面**，而是把已在运行的那套窗口拉到前台，然后自己退出——
用户感知上就是「双击 exe 就出来了」，而不是莫名其妙多出一个托盘图标。

实现分平台：

- **Windows**：命名互斥体（``CreateMutexW``）。互斥体由内核在进程退出时自动释放，
  不会像锁文件那样在崩溃/被强杀后留下陈旧状态——这正是选它而不是 pid 文件的理由。
- **其它平台**：``data/.instance.lock`` 上 ``fcntl.flock``（同样随进程退出释放）。

拿不到锁/句柄时一律**放行**（宁可多开一个实例，也不能让应用起不来）。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .paths import app_dir

# 不加 "Global\" 前缀：那是内核对象命名空间，创建需要额外权限；
# 不带前缀落在当前会话命名空间，正好对应「用户双击 exe」的语义。
MUTEX_NAME = "Keyrealm_SingleInstance"
# pywebview 窗口标题（main.py 里 create_window(title=...) 用的就是这个字面量）
WINDOW_TITLE = "Keyrealm"

ERROR_ALREADY_EXISTS = 183

_mutex_handle: Optional[int] = None   # Windows：进程存活期间必须一直持有
_lock_file = None                     # 非 Windows：文件对象不能关，关了 flock 就释放


def _acquire_windows() -> bool:
    import ctypes
    from ctypes import wintypes

    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True  # 创建失败不拦
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def _acquire_posix() -> bool:
    import fcntl

    global _lock_file
    lock_dir = app_dir() / "data"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = open(lock_dir / ".instance.lock", "w", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    except Exception:
        return True  # 目录不可写之类的意外：不拦
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_file = handle
    return True


def acquire() -> bool:
    """尝试成为唯一实例。True = 可以继续启动；False = 已有实例在跑。"""
    if sys.platform == "win32":
        return _acquire_windows()
    return _acquire_posix()


def focus_existing() -> bool:
    """把已运行实例的主窗口拉到前台（仅 Windows 有效，失败返回 False）。

    ``SetForegroundWindow`` 有前台进程限制，可能被系统拒绝；此时至少已 ``ShowWindow``
    取消最小化，用户能在任务栏看到它。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        # SW_RESTORE 会取消最大化（窗口缩回普通尺寸）；只在确实最小化时用它，
        # 否则用 SW_SHOW 保持原有窗口状态（最大化 / 全屏不被改变）。
        SW_SHOW = 5
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def release() -> None:
    """主动释放已持有的单实例锁（**仅供更新器**在重启应用前调用）。

    正常进程存活期间靠内核/文件锁自动释放，不需要手动调；但更新器会先 `acquire()` 占住锁
    （保证更新期间没人能启动应用），到重启新应用之前必须**先释放**，否则新应用会把自己当成
    「第二实例」而退出。
    """
    global _mutex_handle, _lock_file
    if sys.platform == "win32":
        if _mutex_handle:
            try:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(_mutex_handle)
            except Exception:  # noqa: BLE001 - 释放失败也不该打断更新
                pass
            _mutex_handle = None
        return
    if _lock_file is not None:
        try:
            import fcntl

            fcntl.flock(_lock_file, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        try:
            _lock_file.close()
        except Exception:  # noqa: BLE001
            pass
        _lock_file = None
