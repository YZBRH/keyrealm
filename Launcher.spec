# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：Keyrealm **启动器**（onefile，只依赖标准库）。

产物与 ``Keyrealm.spec`` 的 onedir 运行时分开构建，由 ``scripts/build.py`` 组装成::

    Keyrealm(.exe)      ← 本文件
    runtime/Keyrealm    ← Keyrealm.spec 的 onedir 产物
    runtime/_internal/

``KEYREALM_CLI=1``（构建 CLI 版时由 build.py 设置）→ 控制台程序、名称 ``Keyrealm-CLI``。
启动器本身不含任何 backend / 第三方依赖，升级时由新版包整体替换。
"""
import os
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(SPECPATH).resolve()
_cli = os.environ.get("KEYREALM_CLI") == "1"
_NAME = "Keyrealm-CLI" if _cli else "Keyrealm"
_ICON_PATH = ROOT / "assets" / "icon.ico"
_icon = str(_ICON_PATH) if (_ICON_PATH.exists() and not _cli) else None

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["plugins", "data", "tests", "docs", "backend", "frontend",
              "PyInstaller", "pytest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=_NAME,
    icon=_icon,
    debug=False,
    # 启动器只负责等待子进程：SIGINT 由应用自己处理，bootloader 别抢先退出 / 清理。
    bootloader_ignore_signals=True,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=_cli,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
