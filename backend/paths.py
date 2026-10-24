"""路径解析：区分「程序入口目录」「随包只读资源」与「可写数据目录」。

- resource_dir()：随包只读资源（frontend/dist 等）。冻结时 = sys._MEIPASS。
- app_dir()     ：程序入口所在目录（exe 同级 / 源码项目根）。插件、配置文件在这里。
- data_dir()    ：可写数据目录（数据库、日志、备份、任务记录）。默认
                  ``app_dir()/data``；可通过同目录配置文件或 CLI ``--data-dir`` 改到别处。

数据目录的解析必须在导入其它 backend 模块**之前**完成（database/logger/tasks 等在模块级
就把路径固化成常量），入口是 :func:`configure_data_dir`。

查找顺序（高 → 低）：
1. CLI 命令行 ``--data-dir PATH``（仅 CLI 模式；一次性，不写配置）
2. 入口目录下的 ``keyrealm.conf.json`` 里的 ``data_dir``（持久化）
3. 默认 ``<入口目录>/data``
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

CONFIG_FILENAME = "keyrealm.conf.json"
# onedir 发行布局：启动器在入口目录，应用本体在 `<入口>/runtime/`。
RUNTIME_DIR_NAME = "runtime"
APP_DIR_ENV = "KEYREALM_APP_DIR"

# 解析结果（由 configure_data_dir 在进程启动早期写入）
_data_dir_override: Optional[Path] = None
_data_dir_source = "default"      # default / config / cli
_config_error = ""


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结后的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


def _entry_dir() -> Path:
    if is_frozen():
        # onedir 布局：应用在 `<入口>/runtime/`，用户内容（plugins/data/配置）在入口目录。
        # 启动器会显式传 KEYREALM_APP_DIR；直接运行 runtime 里的 exe 时按父目录名回退。
        override = os.environ.get(APP_DIR_ENV, "").strip()
        if override:
            try:
                return Path(override).resolve()
            except OSError:
                pass
        exe = Path(sys.executable).resolve()
        # 只认 PyInstaller 6 的 onedir 布局（runtime/ 下有 _internal/）；否则一个碰巧
        # 一个碰巧叫 runtime 的普通目录会被误判成上了两层。
        if exe.parent.name == RUNTIME_DIR_NAME and (exe.parent / "_internal").is_dir():
            return exe.parent.parent
        return exe.parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """随包只读资源根目录（frontend/dist 等）。

    onefile 指向临时解压目录；onedir 指向 ``runtime/_internal``（PyInstaller 同样通过
    ``sys._MEIPASS`` 暴露）。源码运行指向项目根。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """程序入口目录（冻结 = exe 同级；源码 = 项目根）。插件与配置文件放这里。"""
    return _entry_dir()


# ── 数据目录配置 ──────────────────────────────────────────

def default_data_dir() -> Path:
    return app_dir() / "data"


def data_dir() -> Path:
    """当前生效的数据目录。其它模块应在模块级直接调用它。"""
    return _data_dir_override or default_data_dir()


def data_dir_source() -> str:
    """``cli`` / ``config`` / ``default``。"""
    return _data_dir_source


def config_file() -> Path:
    return app_dir() / CONFIG_FILENAME


def config_error() -> str:
    """上一次读取配置文件的错误（空 = 正常）。"""
    return _config_error


def _normalize(value: Any, base: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("数据目录不能为空")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def argv_data_dir(argv: Optional[list[str]] = None) -> Optional[str]:
    """从命令行提取 ``--data-dir`` / ``--data-dir=…``；没有返回 None。

    与 argparse 保持一致：重复出现时**最后一个**生效。
    """
    args = list(sys.argv if argv is None else argv)
    found: Optional[str] = None
    for index, item in enumerate(args):
        if item == "--data-dir":
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise ValueError("--data-dir 缺少路径")
            found = args[index + 1]
        elif item.startswith("--data-dir="):
            found = item.split("=", 1)[1]
    return found


def read_config() -> dict:
    global _config_error
    _config_error = ""
    path = config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置损坏时回退默认，错误留给界面/命令查看
        _config_error = f"{type(exc).__name__}: {exc}"
        return {}
    if not isinstance(data, dict):
        _config_error = "配置文件顶层必须是 JSON 对象"
        return {}
    return data


def configure_data_dir(argv: Optional[list[str]] = None) -> Path:
    """按「命令行 > 配置文件 > 默认」解析并记住数据目录。

    ``argv`` 为 None 时只读配置文件；传入列表才允许命令行覆盖（仅 CLI 入口这样调用）。
    配置里的相对路径以**配置文件所在目录**为基准；命令行相对路径以当前工作目录为基准。
    """
    global _data_dir_override, _data_dir_source
    cfg = read_config()
    cfg_value = cfg.get("data_dir")
    arg_value = argv_data_dir(argv) if argv is not None else None

    if arg_value is not None:
        _data_dir_override = _normalize(arg_value, Path.cwd())
        _data_dir_source = "cli"
    elif cfg_value:
        _data_dir_override = _normalize(cfg_value, app_dir())
        _data_dir_source = "config"
    else:
        _data_dir_override = None
        _data_dir_source = "default"
    return data_dir()


def write_config_data_dir(value: Any) -> Path:
    """把数据目录写入入口目录下的配置文件（绝对路径），并返回规范化结果。"""
    target = _normalize(value, Path.cwd())
    target.mkdir(parents=True, exist_ok=True)
    cfg = read_config()
    cfg["data_dir"] = str(target)
    path = config_file()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return target


def clear_config_data_dir() -> bool:
    """删除配置文件里的 ``data_dir``；文件没有其它键时直接删除文件。返回是否产生了改动。"""
    cfg = read_config()
    path = config_file()
    if not cfg and _config_error and path.is_file():
        # 配置文件本身坏了：clear 的语义就是“恢复默认”，直接删掉坏文件
        try:
            path.unlink()
        except OSError:
            pass
        return True
    if "data_dir" not in cfg:
        return False
    cfg.pop("data_dir", None)
    path = config_file()
    if cfg:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    return True
