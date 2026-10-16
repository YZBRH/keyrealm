"""应用版本与**数据库结构版本** —— 全项目**唯一事实来源**。

版本规则
--------
- **只在用户明确要求时才提升版本号**——其余提交照常，只写 commit、**不动版本号**；
- **每次提升版本号都必须同步更新仓库根目录的 `CHANGELOG.md`**（独立更新日志文件）；
- **修 bug / 做优化** → 末位 +1（patch，如 1.0.0 → 1.0.1）
- **增加特性 / 功能** → 中位 +1（minor，如 1.0.0 → 1.1.0）
- **较大型的框架变动** → 首位 +1（major，如 1.0.0 → 2.0.0）

**数据库结构有变动时，必须在 `scripts/` 附一份迁移脚本**（`migrate_*.py`），并把
``SCHEMA_VERSION`` 一起提升；应用本身**不再新增**自动迁移——`database._migrate()` 里那些
``from_version < N`` 只服务 1.0 之前的旧库，已冻结。

> **凭据数据的格式变化也算「数据结构变化」**：例如某插件把两个字段合成一个、或拆成两个。
> 同样在 `scripts/migrations/` 留一跳脚本、同样提升 ``SCHEMA_VERSION``（哪怕表结构没动）。
> 区别只在**时机**：这类改动要解密凭据（敏感字段是加密的），而迁移脚本跑在更新交棒阶段
> （应用已退出、没有主密码），所以**数据部分**由 `backend/data_upgrades.py` 在**解锁之后**
> 补做（规则幂等，不另记进度）。**同一个 ``SCHEMA_VERSION`` 即同一套数据结构。**

展示格式：**``1.0.0 (fe15e3b)``** —— 括号里是**构建时**的 commit 短 hash
（由 `scripts/build.py` 注入到 ``backend/_build_info.py``；源码运行时现查 git）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import is_frozen

# 应用版本（界面 / 日志展示用）
#
# **只在用户明确要求时才改动版本号**（其余提交照常，只写 commit，不动版本）；每次改版本号
# **必须同时更新 `CHANGELOG.md`**（仓库根目录的独立更新日志文件）。
APP_VERSION = "1.4.5"

# **数据库结构版本**：与应用同号起步。
#
# 结构变动（建表 / 加列 / 改约束 / **删表**）时随迁移脚本一起提升；**凭据数据的格式变化也算**
# 结构变动（见文件头说明）——同样提升本版本号、同样留一跳迁移脚本（哪怕无表结构改动），好让更新
# 流程的迁移链完整。其中需要解密的那部分由 `backend/data_upgrades.py` 在**解锁后**补做（幂等）。
SCHEMA_VERSION = "1.2.2"

# 1.0 之前 schema_version 是整数（0..6）。读到**非整数**即说明是 1.0+ 的语义化版本，
# 那些旧的整数迁移一律视为已过（见 database._migrate）。
LEGACY_SCHEMA_MAX = 6

_ROOT = Path(__file__).resolve().parent.parent


def _git_commit() -> str:
    """现查 git 的 commit 短 hash；工作区脏时追加 ``-dirty``。取不到返回空串。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, cwd=str(_ROOT))
        if out.returncode != 0 or not out.stdout.strip():
            return ""
        value = out.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5, cwd=str(_ROOT))
        if dirty.returncode == 0 and dirty.stdout.strip():
            value += "-dirty"
        return value
    except Exception:  # noqa: BLE001 - 没装 git / 不是仓库
        return ""


def build_id() -> str:
    """构建标识。

    **源码运行**优先现查 git —— 构建时留下的 ``_build_info.py`` 记的是上一次打包的
    hash，不代表当前源码；**冻结的 exe 里没有 git**，才回落到构建时注入的值。
    """
    if not is_frozen():
        live = _git_commit()
        if live:
            return live
    try:
        from . import _build_info  # type: ignore[attr-defined]  # 由 scripts/build.py 生成
        value = str(getattr(_build_info, "COMMIT", "") or "").strip()
        if value:
            return value
    except Exception:  # noqa: BLE001 - 没生成过
        pass
    return _git_commit() or "unknown"


def version_string() -> str:
    """界面与日志统一用的一行：``1.0.0 (fe15e3b)``。"""
    return f"{APP_VERSION} ({build_id()})"
