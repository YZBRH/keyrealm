"""
pywebview JS-API 桥接层
所有前端调用的方法都在这里定义
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional, Union

from . import autostart
from . import backup
from . import data_upgrades
from . import database as db
from . import idle_lock
from . import logger
from . import mcp_server
from . import plugin_manager
from . import preset_store
from . import proxy as proxy_config
from . import proxy_store
from . import redact
from . import scheduler
from . import tasks
from . import tls_store
from . import version
from . import tray
from . import updater
from . import verify_options
from .crypto import (
    check_verify_token,
    create_verify_token,
    decrypt,
    decrypt_dict,
    derive_key,
    encrypt,
    encrypt_dict,
    generate_salt,
    generate_token,
    hash_token,
    mask_secret,
)

# 内存中的 AES 密钥（解锁后存在，锁定后清除）
_master_key: Optional[bytes] = None

# 数据格式升级（见 backend/data_upgrades.py）在本进程内是否已跑过——规则幂等，跑一次就够
_data_upgrades_done = False
_key_lock = threading.Lock()

# 哨兵：区分“未提供该参数”与“显式传 None（如清除分组）”
_UNSET = object()

# pywebview window 引用（由 main.py 注入）
_window = None

# 本模块 logger
_log = logger.get_logger(__name__)


def set_window(window):
    global _window
    _window = window


# 轻量模式：界面文档已被释放（由 main.py 换成占位页）期间置 True。
# 那时 evaluate_js 必然落在占位页上、必然报错，事件直接丢掉，
# 免得「释放期间每推一次事件就写一条 warning」把日志刷满。
_page_released = False


def mark_page_released(released: bool):
    """由 main.py 在释放 / 恢复界面文档时调用（见 main._release_page）。"""
    global _page_released
    _page_released = bool(released)


# 关闭窗口行为回调（由 main.py 注入）：分别执行"直接退出"与"隐藏到托盘"
_close_quit_cb = None
_close_hide_cb = None


def set_close_callbacks(quit_cb=None, hide_cb=None):
    """注入关闭行为回调：quit_cb=直接退出，hide_cb=隐藏到托盘。"""
    global _close_quit_cb, _close_hide_cb
    _close_quit_cb = quit_cb
    _close_hide_cb = hide_cb


def _get_key() -> Optional[bytes]:
    with _key_lock:
        return _master_key


def _set_key(key: Optional[bytes]):
    global _master_key
    with _key_lock:
        _master_key = key


def push_event(event_name: str, detail: Any = None):
    """向前端推送自定义事件"""
    if _window is None or _page_released:
        return
    try:
        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
        js = f"window.dispatchEvent(new CustomEvent('{event_name}', {{detail: {payload}}}));"
        _window.evaluate_js(js)
    except Exception as e:
        _log.warning("事件推送失败 %s: %s", event_name, e)


def get_tray_status() -> dict:
    return {
        "mcp_running": mcp_server.is_running(),
        "mcp_port": mcp_server.get_port(),
        "active_tokens": 1 if db.get_mcp_token_record() else 0,
        "is_locked": _get_key() is None,
    }


# ── 初始化回调注入 ────────────────────────────────────────

def init_callbacks():
    scheduler.set_callbacks(push_event, _get_key)
    mcp_server.set_callbacks(_get_key, push_event)
    # 空闲自动锁定：看门狗线程命中超时后回调 lock_vault；未解锁时不动它
    idle_lock.set_callbacks(lock_vault, lambda: _get_key() is not None)
    # 后台任务状态一变就推给前端（任务列表与侧栏角标据此刷新）
    tasks.set_notifier(lambda snapshot: push_event("vault:tasks", snapshot))
    # 恢复上次运行留下的任务记录（与日志同为普通文件，进程重启后仍可回看）。
    # 上次退出时仍在跑的一律改判「中断」——线程随进程没了，继续显示进行中是骗人。
    restored = tasks.load()
    if restored:
        _log.info("已恢复 %d 条历史后台任务记录", restored)


# ── Auth / Lock ───────────────────────────────────────────

def is_initialized() -> dict:
    verify_token = db.get_setting("verify_token")
    return {"initialized": bool(verify_token)}


def is_unlocked() -> dict:
    return {"unlocked": _get_key() is not None}


def setup_master_password(password: str) -> dict:
    if not password or len(password) < 6:
        return {"ok": False, "message": "密码至少 6 位"}
    if db.get_setting("verify_token"):
        return {"ok": False, "message": "主密码已设置，请使用解锁功能"}
    salt = generate_salt()
    key = derive_key(password, salt)
    verify_token = create_verify_token(key)
    db.set_setting("salt", salt.hex())
    db.set_setting("verify_token", verify_token)
    _set_key(key)
    idle_lock.touch()
    return {"ok": True, "message": "主密码设置成功"}


def unlock(password: str) -> dict:
    salt_hex = db.get_setting("salt")
    verify_token = db.get_setting("verify_token")
    if not salt_hex or not verify_token:
        return {"ok": False, "message": "尚未初始化，请先设置主密码"}
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return {"ok": False, "message": "数据损坏"}
    key = derive_key(password, salt)
    if not check_verify_token(key, verify_token):
        return {"ok": False, "message": "密码错误"}
    _set_key(key)
    idle_lock.touch()
    tray.update_tray()
    # 代码比库新（手动构建换了代码 / 导入过更老的库）时，**先把结构补到位**——
    # 迁移不需要主密码，放在这里正好与紧随其后的数据升级串成一条线：结构 → 数据。
    try:
        sc = ensure_schema_current()
        if sc.get("migrated"):
            _log.info("数据库结构已自动升级：%s", sc.get("message") or sc.get("applied"))
        elif not sc.get("ok"):
            _log.warning("数据库结构升级失败：%s", sc.get("message"))
    except Exception:  # noqa: BLE001 - 结构升级失败不该拖垮解锁（界面上会继续提示待迁移）
        _log.exception("数据库结构升级失败（不影响解锁）")
    # 换版 / 更新后：把库里遗留的**老格式凭据数据**升级到当前格式（幂等；失败不能拖垮解锁）
    try:
        up = run_data_upgrades()
        if up.get("upgraded"):
            _log.info("凭据数据升级完成：%s 条（v%s → v%s）",
                      up["upgraded"], up.get("from"), up.get("to"))
    except Exception:  # noqa: BLE001 - 数据升级失败不该影响解锁
        _log.exception("凭据数据升级失败（不影响解锁）")
    # 解锁后补跑一次「自动检测更新」——启动时应用还没解锁、访问令牌尚未注入，那次检查可能没成
    start_auto_update_check()
    return {"ok": True, "message": "解锁成功"}


def ensure_schema_current() -> dict:
    """把**磁盘库**的结构升到当前代码的 ``SCHEMA_VERSION``（幂等）。

    用在「**代码比库新**」的场合——手动构建换了代码、或导入了更老的库（结构版本只认磁盘上那一行，
    换代码不会自动改它）。逐级迁移由运行器负责，**每跳成功后**才推进 ``settings.schema_version``，
    所以版本号永远不会领先于实际结构。结构迁移**不需要主密码**，故在解锁时跑即可。
    """
    disk = (db.get_setting("schema_version", "") or "").strip()
    if disk == version.SCHEMA_VERSION:
        return {"ok": True, "migrated": False, "version": disk}
    _log.info("数据库结构落后于代码（磁盘 %s → 代码 %s），开始自动迁移",
              disk or "（空）", version.SCHEMA_VERSION)
    # guard=False：应用自己就在跑，不能（也不需要）去抢单实例锁
    res = updater.run_migrations(apply=True, guard=False, to=version.SCHEMA_VERSION)
    res["migrated"] = bool(res.get("ok") and (res.get("applied") or res.get("steps")))
    return res


def _sensitive_keys(credential_type: str) -> set:
    """该插件里声明为 sensitive 的字段名（与 create/update 拆 data_plain 的口径一致）。"""
    record = db.get_plugin_by_type(credential_type)
    return {f["key"] for f in (record or {}).get("fields_schema", []) if f.get("sensitive")}


def run_data_upgrades() -> dict:
    """把库里遗留的**老格式凭据数据**升级到当前格式（幂等；本进程只跑一次）。

    **必须在解锁后调用**：凭据的敏感字段是加密的，没有主密码读不出来——所以这一步做不到结构
    迁移 `m_*.py` 里（那些跑在更新交棒阶段、应用已退出）。

    **不记录"做到哪"**：规则本身幂等（同一个 `SCHEMA_VERSION` 就是同一套数据结构，见
    `backend/data_upgrades.py`），所以每次启动过一次即可，并且只处理**有升级规则的类型**——
    没有那类凭据就零成本跳过。导入备份在落库前走**同一套规则**，不依赖这里。
    """
    global _data_upgrades_done
    if _data_upgrades_done:
        return {"ok": True, "upgraded": 0, "skipped": True}
    types = data_upgrades.rule_types()
    if types is not None and not types:
        _data_upgrades_done = True
        return {"ok": True, "upgraded": 0}
    key = _get_key()
    if key is None:
        return {"ok": False, "message": "保险库未解锁，无法升级数据"}
    creds = db.list_credentials() if types is None else db.list_credentials(
        credential_types=sorted(types))
    upgraded = 0
    for cred in creds:
        ct = cred.get("credential_type") or ""
        blob = cred.get("data_encrypted")
        try:
            data = decrypt_dict(key, blob) if blob else dict(cred.get("data_plain") or {})
        except Exception:  # noqa: BLE001 - 单条解不开就跳过，别让整体失败
            continue
        new = data_upgrades.upgrade_credential(ct, data)
        if new == data:
            continue
        sensitive = _sensitive_keys(ct)
        db.update_credential(
            cred["id"],
            data_encrypted=encrypt_dict(key, new),
            data_plain={k: v for k, v in new.items() if k not in sensitive},
        )
        upgraded += 1
    _data_upgrades_done = True
    return {"ok": True, "upgraded": upgraded}


def lock_vault() -> dict:
    """清除内存中的解密密钥。所有加锁入口（界面按钮 / 托盘 / 空闲自动锁）都走这里，
    因此统一广播 `vault:locked`，前端据此清空明文并跳回解锁页。

    先广播再刷托盘：刷新托盘要碰 GUI 后端（pystray），万一它抛异常，界面也不该还停在
    「已解锁」的样子——密钥已经没了，前端必须知道。
    """
    _set_key(None)
    os.environ.pop(updater.ENV_TOKEN, None)   # 锁库即清令牌，别把 PAT 留在进程 env 里
    push_event("vault:locked")
    tray.update_tray()
    return {"ok": True}


def change_master_password(old_password: str, new_password: str) -> dict:
    key = _get_key()
    if key is None:
        return {"ok": False, "message": "请先解锁"}
    salt_hex = db.get_setting("salt")
    verify_token = db.get_setting("verify_token")
    try:
        old_salt = bytes.fromhex(salt_hex)
    except Exception:
        return {"ok": False, "message": "数据损坏"}
    old_key = derive_key(old_password, old_salt)
    if not check_verify_token(old_key, verify_token):
        return {"ok": False, "message": "原密码错误"}
    if not new_password or len(new_password) < 6:
        return {"ok": False, "message": "新密码至少 6 位"}

    # 重新加密所有凭据
    new_salt = generate_salt()
    new_key = derive_key(new_password, new_salt)
    creds = db.list_credentials()
    for cred in creds:
        try:
            data = decrypt_dict(old_key, cred["data_encrypted"])
            new_encrypted = encrypt_dict(new_key, data)
            db.update_credential(cred["id"], data_encrypted=new_encrypted)
        except Exception as e:
            _log.error("重新加密失败 cred_id=%s: %s", cred["id"], e)

    new_verify = create_verify_token(new_key)
    db.set_setting("salt", new_salt.hex())
    db.set_setting("verify_token", new_verify)
    _set_key(new_key)
    return {"ok": True, "message": "主密码修改成功"}


# ── Credentials ───────────────────────────────────────────

def list_credentials(
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    credential_types: Optional[list] = None,
) -> list:
    creds = db.list_credentials(group_id=group_id, credential_type=credential_type,
                                status=status, search=search,
                                credential_types=credential_types or None)
    # 附加敏感字段掩码（如 sk-123…abcd），供列表页直接展示部分凭据信息。
    # 仅在保险库已解锁时计算；锁定时返回空掩码。
    key = _get_key()
    if key:
        schema_map = {p["credential_type"]: p for p in db.list_plugins()}
        for cred in creds:
            cred["data_masked"] = _masked_fields(key, cred, schema_map.get(cred["credential_type"]))
    return creds


def _masked_fields(key: bytes, cred: dict, plugin_record: Optional[dict]) -> dict:
    sensitive = [
        f["key"] for f in (plugin_record or {}).get("fields_schema", []) if f.get("sensitive")
    ]
    if not sensitive:
        return {}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        return {}
    return {k: mask_secret(str(data[k])) for k in sensitive if data.get(k)}


def get_credential(credential_id: int) -> Optional[dict]:
    cred = db.get_credential(credential_id)
    if not cred:
        return None
    # GUI 编辑表单需要完整明文（含敏感字段）来预填，避免提交空值覆盖密钥。
    # 保险库已解锁（_get_key 可用）才返回明文；锁定时不附加 data 字段。
    key = _get_key()
    if key and cred.get("data_encrypted"):
        try:
            cred["data"] = decrypt_dict(key, cred["data_encrypted"])
        except Exception:
            cred["data"] = cred.get("data_plain", {})
    return cred


def create_credential(
    name: str,
    credential_type: str,
    data: dict,
    group_id: Optional[int] = None,
    auto_verify: bool = True,
) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked", "message": "保险库已锁定"}

    # 类别/插件级默认设置先补进来——默认值可能正好满足必填项，所以必须在校验之前
    data = preset_store.apply_defaults(credential_type, data)

    errors = plugin_manager.validate_credential(credential_type, data)
    if errors:
        return {"error": "validation_failed", "details": errors}

    plugin_record = db.get_plugin_by_type(credential_type)
    sensitive_keys = set()
    if plugin_record:
        for f in plugin_record.get("fields_schema", []):
            if f.get("sensitive"):
                sensitive_keys.add(f["key"])

    data_plain = {k: v for k, v in data.items() if k not in sensitive_keys}
    encrypted = encrypt_dict(key, data)

    cred = db.create_credential(
        name=name,
        credential_type=credential_type,
        data_encrypted=encrypted,
        data_plain=data_plain,
        group_id=group_id,
    )

    if auto_verify:
        scheduler.verify_single_credential(cred["id"], trigger_type="auto")
        cred = db.get_credential(cred["id"]) or cred

    push_event("vault:credentials_changed")
    return cred


def update_credential(
    credential_id: int,
    name: Optional[str] = None,
    group_id: Optional[int] = _UNSET,
    data: Optional[dict] = None,
    auto_verify: bool = False,
) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    # 显式传 None = 取消分组；省略（_UNSET）= 不修改分组
    if group_id is not _UNSET:
        kwargs["group_id"] = group_id
    if data is not None:
        cred = db.get_credential(credential_id)
        if not cred:
            return {"error": "not_found"}
        plugin_record = db.get_plugin_by_type(cred["credential_type"])
        sensitive_keys = set()
        if plugin_record:
            for f in plugin_record.get("fields_schema", []):
                if f.get("sensitive"):
                    sensitive_keys.add(f["key"])

        # 防御：敏感字段缺失或为空时保留已存明文，避免编辑表单未预填
        # 导致提交空值而覆盖密钥（凭据的敏感字段为空是无意义状态）。
        merged = dict(data)
        if sensitive_keys:
            try:
                existing_data = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                existing_data = {}
            for sk in sensitive_keys:
                if not merged.get(sk) and existing_data.get(sk):
                    merged[sk] = existing_data[sk]

        kwargs["data_encrypted"] = encrypt_dict(key, merged)
        kwargs["data_plain"] = {k: v for k, v in merged.items() if k not in sensitive_keys}

    updated = db.update_credential(credential_id, **kwargs)

    if auto_verify and data is not None:
        scheduler.verify_single_credential(credential_id, trigger_type="auto")
        updated = db.get_credential(credential_id) or updated

    push_event("vault:credentials_changed")
    return updated or {"error": "not_found"}


def delete_credential(credential_id: int) -> dict:
    db.delete_credential(credential_id)
    push_event("vault:credentials_changed")
    return {"ok": True}


def delete_credentials(credential_ids: list) -> dict:
    """批量删除（单事务），事件只推一次。

    与单条删除一致**不查 key**：UI 侧本就在解锁态之后才可能点到，且逐条删除同样不查。
    返回 `deleted` 是**实际**删除条数（可能小于请求数——期间有条目已被别处删掉）。
    """
    deleted = db.delete_credentials(credential_ids)
    push_event("vault:credentials_changed")
    return {"ok": True, "deleted": deleted}


def verify_credential(credential_id: int) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    return scheduler.verify_single_credential(credential_id, trigger_type="manual")


def batch_verify(
    scope: str = "all",
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    credential_ids: Optional[list] = None,
    concurrency=None,
    timeout=None,
) -> dict:
    """触发批量验证（后台并发执行）。

    `concurrency` / `timeout` 来自「批量验证」对话框里的两个输入框：传了就先落库再执行，
    下次打开对话框沿用；不传则用已保存的设置。

    返回的 `total` 取自 `scheduler.batch_verify` 的返回值，即**它真正会去验证的条数**
    （已排除禁用插件与 batch_verify=false 的插件）。不要在这里另查一遍凭据来数 —— 那会
    造出第二份筛选口径，与本函数返回给前端的数字、与进度条走的数字互相对不上。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    options = verify_options.save(concurrency, timeout)
    _log.info("批量验证参数：并发 %d，超时 %gs",
              options["concurrency"], options["timeout"])

    total = scheduler.batch_verify(scope=scope, group_id=group_id,
                                   credential_type=credential_type, trigger_type="manual",
                                   credential_ids=credential_ids,
                                   concurrency=options["concurrency"])
    return {"ok": True, "total": total,
            "concurrency": options["concurrency"], "timeout": options["timeout"]}


def get_batch_options() -> dict:
    """批量验证的运行参数（并发线程数 / 请求超时）与取值范围，供对话框回填。"""
    return {"ok": True, **verify_options.get_options()}



def check_duplicate_credential(credential_type: str, data: dict) -> list:
    """检测与新增数据重复的既有凭据：任一敏感字段值与既有凭据
    解密后任意字符串字段值相同即视为重复。返回匹配项摘要列表。"""
    key = _get_key()
    if key is None:
        return []

    plugin_record = db.get_plugin_by_type(credential_type)
    sensitive_keys = set()
    if plugin_record:
        for f in plugin_record.get("fields_schema", []):
            if f.get("sensitive"):
                sensitive_keys.add(f["key"])
    new_values = {str(data[k]).strip() for k in sensitive_keys if data.get(k)}
    if not new_values:
        return []

    matches = []
    for cred in db.list_credentials():
        try:
            existing = decrypt_dict(key, cred["data_encrypted"])
        except Exception:
            continue
        existing_values = {str(v).strip() for v in existing.values() if isinstance(v, str) and v}
        if new_values & existing_values:
            matches.append({
                "id": cred["id"],
                "name": cred["name"],
                "credential_type": cred["credential_type"],
            })
    return matches


# ── Groups ────────────────────────────────────────────────

def list_groups() -> list:
    return db.list_groups()


def create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
    return db.create_group(name, description, color)


def update_group(group_id: int, name: str = None, description: str = None, color: str = None) -> dict:
    kwargs = {k: v for k, v in {"name": name, "description": description, "color": color}.items() if v is not None}
    return db.update_group(group_id, **kwargs) or {"error": "not_found"}


def delete_group(group_id: int) -> dict:
    db.delete_group(group_id)
    return {"ok": True}


# ── Plugins ───────────────────────────────────────────────

def list_plugins() -> list:
    plugins = plugin_manager.list_installed_plugins()
    for record in plugins:
        record["icon_image"] = plugin_manager.get_icon_image(record)
        # 深色主题下这个 logo 要不要垫浅色底（判据见 plugin_manager.icon_needs_lift）。
        # 前端只给 icon_lift 为真的那些加 .icon-lift —— 彩色标垫了就是一圈白框。
        record["icon_lift"] = plugin_manager.icon_needs_lift(record)
        record["batch_verify"] = bool(record.get("batch_verify", 1))
        record["enabled"] = bool(record.get("enabled", 1))
        record["has_actions"] = plugin_manager.has_actions(record["credential_type"])
    return plugins


def credential_capabilities(credential_id: int) -> dict:
    """该凭据当前**真正能用**的操作（插件可选能力 `capabilities`）。

    与 `list_credential_actions` 分开是有意的：`actions()` 必须便宜且无副作用（菜单靠它渲染），
    而权限探测要联网、可能失败。分开了才能把三种状态说清楚：
    `actions 为空`（没有操作）/ `available 为空`（有操作但没权限）/ `探测失败`（全都展示）。
    """
    key = _get_key()
    if key is None:
        return {"available": None, "note": "", "error": True}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"available": None, "note": "", "error": True}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_capabilities(cred["credential_type"], data)


def pick_save_path(default_name: str = "", file_types: Optional[list] = None) -> dict:
    """弹系统「另存为」，返回用户选的路径。

    下载类操作（仓库打包、对象下载）**必须先经过这一步**：插件自己不该弹对话框
    （流式路径上插件跑在新建线程里，跨线程弹窗会卡界面），也不能把带鉴权头的下载 URL
    回传给前端——那等于把短期凭证交给 JS。正确顺序是：前端先拿到路径 → 路径塞进 params → 插件自己下载。
    """
    safe_name = _safe_filename(default_name)
    path = _file_dialog("save", default_name=safe_name, file_types=file_types)
    if not path:
        return {"ok": False, "message": "已取消"}
    return {"ok": True, "path": path}


def _safe_filename(name: str) -> str:
    """把用户/插件给的默认文件名洗干净：路径分隔符与控制字符不能进文件名。"""
    text = str(name or "").strip()
    for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        text = text.replace(ch, "_")
    text = text.replace("..", "_")
    return text[:120]


def list_credential_actions(credential_id: int) -> list:
    """某凭据对应插件声明的「简易操作」列表（可选能力）。"""
    key = _get_key()
    if key is None:
        return []
    cred = db.get_credential(credential_id)
    if not cred:
        return []
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_actions(cred["credential_type"], data)


def _credential_secrets(credential_type: str, data: dict) -> list:
    """返回该凭据所有敏感字段的明文值（用于输出净化）。

    实现住在 ``backend/redact.py``（verify 路径的净化也要用同一套逻辑，见
    ``plugin_manager.verify_credential``），这里只负责把 manifest 字段声明取出来。
    """
    record = db.get_plugin_by_type(credential_type) or {}
    return redact.secrets_of(record.get("fields_schema"), data, credential_type=credential_type)


def _declared_fields(credential_type: str) -> set[str]:
    """该插件在 manifest 里声明过的字段 key（用于限定「操作写回」能改哪些字段）。"""
    record = db.get_plugin_by_type(credential_type) or {}
    schema = record.get("fields_schema") or []
    return {str(f.get("key")) for f in schema if isinstance(f, dict) and f.get("key")}


def run_credential_action(credential_id: int, action_id: str, params: Optional[dict] = None) -> dict:
    """执行某凭据的插件操作（输出/错误统一做密钥净化）。

    插件可在结果里回传 ``updated_data``（``{字段key: 新值}``）把新值**写回本凭据**——
    用于「用 refresh_token 换一组新令牌」这类必须原子保存的操作（换令牌会轮换 refresh_token，
    不写回就等于把用户本机的登录态搞坏）。约束：
      - 只接受**该插件声明过**的字段，其它键一律丢弃（免得插件往库里塞野字段）；
      - ``updated_data`` 本身**绝不回给前端**（里面是新明文令牌），只回一个 ``data_updated``
        字段名列表，让界面刷新即可。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"ok": False, "error": "凭据不存在"}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    result = plugin_manager.run_plugin_action(
        cred["credential_type"], action_id, params or {}, data
    )
    # 写回：先摘掉 updated_data，再按声明字段过滤合并，最后重加密落库。
    updated = result.pop("updated_data", None) if isinstance(result, dict) else None
    if isinstance(updated, dict) and updated:
        declared = _declared_fields(cred["credential_type"])
        patch = {k: v for k, v in updated.items() if k in declared}
        if patch:
            merged = {**data, **patch}
            try:
                db.update_credential(credential_id, data_encrypted=encrypt_dict(key, merged))
                data = merged
                result["data_updated"] = sorted(patch)
                _log.info("操作 %s/%s 写回凭据 %s 字段：%s",
                          cred["credential_type"], action_id, credential_id, sorted(patch))
            except Exception as e:  # noqa: BLE001 - 写回失败不能吞掉操作本身的结论
                _log.exception("操作写回凭据失败 %s", credential_id)
                result["data_updated"] = []
                result["data_update_error"] = type(e).__name__
    secrets = _credential_secrets(cred["credential_type"], data)
    # 整份结果递归净化：列表类操作的 rows/columns 也在这里面
    result = redact.redact_deep(result, secrets)
    # 日志同样必须净化：插件回显的密钥不能落盘到日志文件
    ok = bool(isinstance(result, dict) and result.get("ok"))
    _log.info(
        "执行操作 %s/%s [%s]：%s",
        cred["credential_type"], action_id, cred.get("name", ""),
        "成功" if ok else "失败",
    )
    if not ok and isinstance(result, dict) and result.get("error"):
        _log.warning("操作 %s/%s 失败原因: %s", cred["credential_type"], action_id,
                     redact.redact_text(str(result["error"]), secrets))
    return result


def get_version() -> dict:
    """版本信息（「关于」页与启动日志用）。

    ``display`` 是给界面直接显示的一行：``1.0.0 (fe15e3b)`` —— 括号里是**构建时**的
    commit 短 hash（exe 由 scripts/build.py 注入；源码运行时现查 git）。
    """
    return {
        "version": version.APP_VERSION,
        "build": version.build_id(),
        "schema_version": version.SCHEMA_VERSION,
        "display": version.version_string(),
    }


def list_credential_models(credential_id: int, force: bool = False) -> list:
    """某凭据对应插件可用的模型列表（供 LLM「简单对话」选择模型）。

    ``force=True`` 是用户点「刷新模型列表」：穿透插件的模型清单缓存（24h）重新拉取。
    """
    key = _get_key()
    if key is None:
        return []
    cred = db.get_credential(credential_id)
    if not cred:
        return []
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_models(cred["credential_type"], data, force)


def list_draft_models(credential_type: str, data: Optional[dict] = None, force: bool = False) -> list:
    """尚未保存的（新增/编辑表单里的）凭据可用的模型列表。

    与 ``list_credential_models`` 的区别是数据来自表单而非库，因此新建凭据时也能
    拿到下拉候选；失败时插件返回空数组，前端回退为自由输入。
    ``force=True`` 同 ``list_credential_models``——用户手动刷新，穿透缓存。
    """
    if not credential_type:
        return []
    return plugin_manager.plugin_models(credential_type, data or {}, force)


def stream_credential_action(credential_id: int, action_id: str, params: Optional[dict] = None,
                             title: str = "") -> dict:
    """流式执行某凭据的插件操作：后台线程边收边推送 vault:action_stream 事件。

    事件 detail：
      - {"stream_id": ..., "delta": "…"}                        逐段增量
      - {"stream_id": ..., "progress": {current,total,text}}    进度（长操作）
      - {"stream_id": ..., "done": true, "ok": .., "output": .., "error": ..}  结束

    同时登记成一条**后台任务**（``backend/tasks.py``）：弹窗关掉后任务照跑，可以在「任务」页
    看进度、随时取消。``title`` 由前端传（操作的显示名 + 凭据名），缺省时退化成 action_id。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"ok": False, "error": "凭据不存在"}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}

    stream_id = str(uuid.uuid4())
    credential_type = cred["credential_type"]
    secrets = _credential_secrets(credential_type, data)
    task = tasks.create(
        "action",
        str(title or "").strip() or f"{action_id} · {cred.get('name', '')}",
        credential_id=credential_id,
        credential_type=credential_type,
        action_id=action_id,
    )
    task_id = task["id"]

    def _run():
        def on_delta(text: str) -> None:
            push_event("vault:action_stream", {
                "stream_id": stream_id,
                "delta": redact.redact_text(text, secrets),
            })

        def on_progress(current, total, text: str = "") -> None:
            """下载/打包这类长操作的进度：``total <= 0`` 表示总量未知（前端只显示已完成的量）。

            进度是**尽力而为**——数值先夹到合法区间，推送本身出错也绝不能把操作搞失败。
            """
            tasks.progress(task_id, current, total, text)
            try:
                cur = max(0, int(current))
                tot = max(0, int(total or 0))
            except (TypeError, ValueError):
                return
            push_event("vault:action_stream", {
                "stream_id": stream_id,
                "progress": {
                    "current": cur,
                    "total": tot,
                    "text": redact.redact_text(str(text or ""), secrets),
                },
            })

        result = plugin_manager.stream_plugin_action(
            credential_type, action_id, params or {}, data, on_delta, on_progress,
            tasks.should_cancel(task_id),
        )
        safe_output = redact.redact_text(result.get("output", ""), secrets)
        safe_error = redact.redact_text(result.get("error", ""), secrets)
        ok = bool(result.get("ok"))
        # 「是不是被取消的」由框架自己判断（它持有取消标志），不靠插件猜错误文案：
        # 只有确实按过取消、且这次没成功，才标成「已取消」——刚好在取消前跑完的仍算成功。
        canceled = tasks.should_cancel(task_id)() and not ok
        tasks.finish(task_id, ok=ok, output=safe_output, error=safe_error, canceled=canceled)
        push_event("vault:action_stream", {
            "stream_id": stream_id,
            "done": True,
            "ok": ok,
            "output": safe_output,
            "error": safe_error,
            "task_id": task_id,
        })
        _log.info("流式操作 %s/%s [%s]：%s", credential_type, action_id,
                  cred.get("name", ""), "成功" if ok else "失败")
        if not ok and safe_error:
            _log.warning("流式操作 %s/%s 失败原因: %s", credential_type, action_id, safe_error)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "stream_id": stream_id, "task_id": task_id}


# ── 后台任务（长操作的可见性与取消）──────────────────────

def list_tasks() -> list[dict]:
    """全部后台任务（进行中在前）。"""
    return tasks.list_tasks()


def cancel_task(task_id: str) -> dict:
    """请求取消一个正在跑的任务（协作式：插件在下一个检查点停下）。"""
    ok = tasks.cancel(task_id)
    return {"ok": ok} if ok else {"ok": False, "error": "任务不存在或已结束"}


def clear_finished_tasks() -> dict:
    """清掉已结束的任务记录，返回清掉的条数。"""
    return {"ok": True, "removed": tasks.clear_finished()}


def set_plugin_enabled(credential_type: str, enabled: bool) -> dict:
    """启用/禁用插件（前端调用）。

    只推 plugins_changed：禁用不动任何凭据，前端凭据行的置灰由插件状态派生，
    多推一次 credentials_changed 只会白白触发一次全量重拉。
    """
    result = plugin_manager.set_plugin_enabled(credential_type, bool(enabled))
    if result.get("ok"):
        push_event("vault:plugins_changed")
    return result


# ── MCP ───────────────────────────────────────────────────

MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 8765

# 上次的开关状态：启动时据此自动恢复（只有用户点「停止服务」才落 0）
MCP_ENABLED_KEY = "mcp_enabled"


def read_mcp_enabled() -> bool:
    return (db.get_setting(MCP_ENABLED_KEY, "0") or "").strip() == "1"


def _mcp_host() -> str:
    return db.get_setting("mcp_host", "").strip() or MCP_DEFAULT_HOST


def _mcp_port() -> int:
    try:
        port = int(db.get_setting("mcp_port", "").strip())
    except (TypeError, ValueError):
        return MCP_DEFAULT_PORT
    return port if 1 <= port <= 65535 else MCP_DEFAULT_PORT


def _valid_mcp_host(host: str) -> bool:
    """只接受合法 IP 或 localhost —— 别让人一不小心把凭据服务挂到公网。"""
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _wait_mcp_stopped(timeout: float = 3.0) -> bool:
    """等服务真正停下来（uvicorn 退出后端口才释放，紧接着启动会撞上自己）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mcp_server.is_running():
            return True
        time.sleep(0.1)
    return not mcp_server.is_running()


def get_mcp_status() -> dict:
    running = mcp_server.is_running()
    return {
        "running": running,
        "host": mcp_server.get_host(),
        "port": mcp_server.get_port(),
        "active_tokens": 1 if db.get_mcp_token_record() else 0,
        "url": mcp_server.get_url() if running else "",
    }


def get_mcp_options() -> dict:
    """MCP 监听地址（供设置页回填）。"""
    return {"ok": True, "host": _mcp_host(), "port": _mcp_port(),
            "defaults": {"host": MCP_DEFAULT_HOST, "port": MCP_DEFAULT_PORT},
            "running": mcp_server.is_running()}


def set_mcp_options(host: str, port) -> dict:
    """保存监听地址；服务在运行则按新地址重启。

    端口被占用时**不改动已保存的值**，并把原地址的服务起回去——不能把用户留在「改完啥都没了」的状态。
    """
    host = (host or "").strip() or MCP_DEFAULT_HOST
    if not _valid_mcp_host(host):
        return {"ok": False, "message": f"监听地址不合法：{host}（只允许 IP 或 localhost）"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "message": "端口必须是数字"}
    if not (1 <= port <= 65535):
        return {"ok": False, "message": "端口需在 1–65535 之间"}

    old_host, old_port = _mcp_host(), _mcp_port()
    was_running = mcp_server.is_running()
    if was_running:
        mcp_server.stop_server()
        _wait_mcp_stopped()

    if was_running:
        ok, actual_port, url = mcp_server.start_server(port, host)
        if not ok:
            mcp_server.start_server(old_port, old_host)   # 回滚：服务照旧跑着
            _push_mcp_status()
            return {"ok": False, "message": f"端口 {port} 已被占用，配置未保存（服务已按原地址恢复）"}
    else:
        actual_port, url = port, ""

    db.set_setting("mcp_host", host)
    db.set_setting("mcp_port", str(port))
    _push_mcp_status()
    _log.info("MCP 监听地址已更新为 %s:%s", host, port)
    return {"ok": True, "host": host, "port": actual_port, "restarted": was_running,
            "url": url or f"http://{host}:{port}/sse"}


def start_mcp() -> dict:
    host, port = _mcp_host(), _mcp_port()
    ok, actual_port, url = mcp_server.start_server(port, host)
    _push_mcp_status()
    if not ok:
        return {"ok": False, "message": f"端口 {port} 已被占用，请到设置里换一个端口"}
    db.set_setting(MCP_ENABLED_KEY, "1")
    return {"ok": True, "host": host, "port": actual_port, "url": url}


def stop_mcp() -> dict:
    mcp_server.stop_server()
    db.set_setting(MCP_ENABLED_KEY, "0")
    _push_mcp_status()
    return {"ok": True}


def autostart_mcp() -> bool:
    """启动时按上次的开关状态恢复 MCP；没开过就什么都不做。

    端口被占用时只记日志、**不改开关**——下次启动照旧会试。
    """
    if not read_mcp_enabled():
        return False
    host, port = _mcp_host(), _mcp_port()
    ok, actual_port, _url = mcp_server.start_server(port, host)
    if ok:
        _log.info("MCP 已按上次状态自动启动（%s:%s）", host, actual_port)
    else:
        _log.warning("MCP 自动启动失败：端口 %s 被占用（开关保持开启，下次启动会再试）", port)
    return ok


def list_mcp_permissions() -> list:
    return db.list_mcp_permissions()


def set_mcp_permission(tool_name: str, enabled: bool) -> dict:
    db.set_mcp_permission(tool_name, enabled)
    return {"ok": True}


def _new_mcp_token() -> dict:
    """生成并保存新的 MCP Token（全局只有一个；只存 hash 与前缀，不记时间）。"""
    token = generate_token()
    token_prefix = token[:8]
    db.set_mcp_token(hash_token(token), token_prefix)
    return {"token": token, "token_prefix": token_prefix}


def ensure_mcp_token() -> Optional[dict]:
    """确保存在一个 Token（无则创建），返回记录（不含明文）。"""
    record = db.get_mcp_token_record()
    if record:
        return record
    _new_mcp_token()
    return db.get_mcp_token_record()


def get_mcp_token() -> dict:
    """返回当前 Token 的元数据（不含明文）；无则自动创建一个。"""
    record = ensure_mcp_token()
    if not record:
        return {"exists": False}
    return {"exists": True, "token_prefix": record["token_prefix"]}


def _push_mcp_status():
    """推送 MCP 状态变化事件（供前端与托盘刷新）。"""
    tray.update_tray()
    push_event("vault:mcp_status", {
        "running": mcp_server.is_running(),
        "host": mcp_server.get_host(),
        "port": mcp_server.get_port(),
        "active_tokens": 1 if db.get_mcp_token_record() else 0,
    })


def rotate_mcp_token() -> dict:
    """轮换 MCP Token：签发新的覆盖旧的（明文仅返回一次）。"""
    created = _new_mcp_token()
    _push_mcp_status()
    _log.info("已轮换 MCP Token（前缀 %s…）", created["token_prefix"])
    return {
        "token": created["token"],          # 明文仅此一次
        "token_prefix": created["token_prefix"],
    }


# ── Schedules ─────────────────────────────────────────────

def list_schedules() -> list:
    return db.list_schedules()


def create_schedule(
    name: str,
    trigger_type: str,
    trigger_args: dict,
    target_scope: str = "all",
    target_id: Optional[Union[int, str]] = None,
    credential_ids: Optional[list] = None,
) -> dict:
    """新建定时计划。`target_scope="credentials"` 时由 `credential_ids` 指定**具体凭据**
    （前端用目录树勾选，与备份页「仅选中的凭据」同一个组件），此时 `target_id` 为空。"""
    sched = db.create_schedule(name, trigger_type, trigger_args, target_scope, target_id,
                               credential_ids)
    scheduler.reload_schedules()
    return sched


def update_schedule(schedule_id: int, payload: dict = None) -> dict:
    # pywebview 桥接仅支持位置参数，前端以 (id, payload_dict) 形式调用，
    # 因此这里接收字典再解包，而非使用 **kwargs（JS 无法触达）。
    db.update_schedule(schedule_id, **(payload or {}))
    scheduler.reload_schedules()
    return db.list_schedules()


def delete_schedule(schedule_id: int) -> dict:
    db.delete_schedule(schedule_id)
    scheduler.reload_schedules()
    return {"ok": True}


# ── Tray ──────────────────────────────────────────────────

def minimize_to_tray() -> dict:
    """最小化到托盘。**不弹系统通知**（缩到托盘是常规操作，每次提示只是噪音）。

    有注入的隐藏回调时优先走回调（main._hide_to_tray），而不是在这里直接 hide：
    "隐藏"在轻量模式下还附带释放界面文档，那段逻辑只应该在主程序一侧有一份。
    """
    if _close_hide_cb is not None:
        try:
            _close_hide_cb()
        except Exception as e:
            _log.warning("隐藏窗口失败: %s", e)
        return {"ok": True}
    if _window:
        try:
            _window.hide()
        except Exception as e:
            _log.warning("隐藏窗口失败: %s", e)
    return {"ok": True}


def log_frontend_error(message: str = "") -> dict:
    """前端把**用户看不见的失败**回传到后端日志，便于排障。

    前端跑在 WebView 里，`console.error` 用户看不到、事后也无从查起（比如「读初始化状态
    失败」这种：现象是界面跳回注册页，日志里一个字都没有）。故留这个后门。
    """
    _log.error("前端报错：%s", str(message or "").strip()[:2000])
    return {"ok": True}


def get_tray_status_api() -> dict:
    return get_tray_status()


# ── 关闭窗口行为 ──────────────────────────────────────────
# 用户点击窗口关闭按钮时可选择：每次询问 / 直接退出 / 后台运行（最小化到托盘）。
# 选择持久化于 settings 表键 close_action，可在"系统设置"中二次调整。

CLOSE_ACTIONS = ("ask", "quit", "background")
_CLOSE_ACTION_DEFAULT = "ask"
_CLOSE_ACTION_OPTIONS = [
    {"value": "ask", "label": "每次询问我"},
    {"value": "quit", "label": "直接退出程序"},
    {"value": "background", "label": "后台运行（最小化到托盘）"},
]


def _read_close_action() -> str:
    action = (db.get_setting("close_action", _CLOSE_ACTION_DEFAULT) or "").strip().lower()
    return action if action in CLOSE_ACTIONS else _CLOSE_ACTION_DEFAULT


def get_close_action() -> dict:
    """返回当前关闭行为与可选值（供"系统设置"展示）。"""
    return {"action": _read_close_action(), "options": _CLOSE_ACTION_OPTIONS}


def set_close_action(action: str) -> dict:
    """持久化关闭行为设置。"""
    action = (action or "").strip().lower()
    if action not in CLOSE_ACTIONS:
        return {"ok": False, "message": f"无效的关闭行为：{action or '(空)'}"}
    db.set_setting("close_action", action)
    _log.info("关闭窗口行为已设置为 %s", action)
    return {"ok": True, "action": action}


def confirm_close_action(action: str, remember: bool = False) -> dict:
    """前端在关闭确认弹窗中做出选择后回调（action: 'quit' | 'background'）。

    remember 为真时把该选择记为默认（下次不再询问）。
    """
    action = (action or "").strip().lower()
    if action not in ("quit", "background"):
        return {"ok": False, "message": f"无效的选择：{action or '(空)'}"}
    if remember:
        db.set_setting("close_action", action)
        _log.info("已记住关闭窗口行为：%s", action)
    if action == "quit":
        if _close_quit_cb:
            _close_quit_cb()
    else:
        if _close_hide_cb:
            _close_hide_cb()
    return {"ok": True, "action": action}


# ── 轻量模式 ──────────────────────────────────────────────
# "最小化到托盘时释放界面文档"以省内存。开关存 settings 表键 light_mode，默认关闭。
# 释放/恢复的实际动作在 main.py（那边才有 window 引用）；这里只负责读写设置。

LIGHT_MODE_KEY = "light_mode"


def read_light_mode() -> bool:
    """读轻量模式开关（供 main.py 在隐藏窗口前判断）。值异常时按"关闭"处理。"""
    return (db.get_setting(LIGHT_MODE_KEY, "0") or "").strip() == "1"


def get_light_mode() -> dict:
    """返回轻量模式开关（供"系统设置"展示）。"""
    return {"enabled": read_light_mode()}


def set_light_mode(enabled) -> dict:
    """持久化轻量模式开关。

    只改设置，不动当前已释放/未释放的页面——下次最小化到托盘时生效。
    """
    value = bool(enabled)
    db.set_setting(LIGHT_MODE_KEY, "1" if value else "0")
    _log.info("轻量模式已%s", "开启" if value else "关闭")
    return {"ok": True, "enabled": value}


# ── 空闲自动锁定 ──────────────────────────────────────────
# "一段时间未操作后自动锁定数据库"。设置项 auto_lock_minutes 存 settings 表（0 = 关闭）；
# 判定与看门狗线程在 backend/idle_lock.py，这里只负责读写设置、接收前端活动上报。

def get_auto_lock() -> dict:
    """返回当前空闲锁定分钟数与可选档位（供"系统设置"展示）。"""
    return {"minutes": idle_lock.read_minutes(), "options": idle_lock.OPTIONS}


def set_auto_lock(minutes) -> dict:
    """持久化空闲自动锁定设置（0 = 关闭）。"""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"无效的分钟数：{minutes!r}"}
    if value not in idle_lock.ALLOWED_MINUTES:
        return {"ok": False, "message": f"不支持的档位：{value} 分钟"}
    db.set_setting(idle_lock.SETTING_KEY, str(value))
    idle_lock.touch()  # 改设置时重置计时，免得刚设完就被锁
    _log.info("空闲自动锁定已设置为 %s 分钟", value)
    return {"ok": True, "minutes": value}


def touch_activity() -> dict:
    """前端在一段时间内有用户操作时（节流）上报，重置空闲计时。"""
    idle_lock.touch()
    return {"ok": True}


# ── 开机自启动 ────────────────────────────────────────────
# 读写的是**操作系统**的自启条目（Windows 注册表 Run / Linux XDG autostart / macOS
# LaunchAgent），**不落 settings** —— 系统侧即唯一事实来源，理由见 backend/autostart.py。

def get_autostart() -> dict:
    """返回自启开关现状与落点（供"系统设置"展示）。"""
    return autostart.status()


def set_autostart(enabled) -> dict:
    """开启 / 关闭开机自启动，直接增删系统条目。"""
    try:
        autostart.set_enabled(bool(enabled))
    except autostart.AutostartError as exc:
        _log.warning("设置开机自启动失败：%s", exc)
        return {"ok": False, "message": str(exc)}
    # 回读系统侧，把真实结果告诉前端（而不是假定的目标值）
    return {"ok": True, "enabled": autostart.is_enabled()}


# ── 凭据目录树顺序 ────────────────────────────────────────
# 用户可在「凭据目录」里拖拽调整「类别」与「插件」两层的先后顺序，顺序以 JSON 存 settings
# 表键 tree_order，形如 {"categories": ["llm", ...], "plugins": ["llm_deepseek", ...]}。
#
# 存的是**顺序清单**而不是全量快照：没被列到的类别 / 插件（例如新装进来的插件）由前端按
# 自然顺序追加到末尾。这样新增或改名插件都不会让已存顺序失效，也不需要迁移脚本。
# 顺序只影响展示，不影响任何筛选、验证或存储行为。

TREE_ORDER_KEY = "tree_order"
_TREE_ORDER_MAX = 500  # 顺序清单的长度上限（远超真实插件数，纯属防御性上限）


def _clean_order_list(value) -> list:
    """把前端传来的顺序数组规整为去重后的字符串列表（保序）。

    空字符串是**合法**条目：目录树里「没有类别的插件」自成一个桶，其键就是空串，
    它同样可以被拖到任意位置。仅丢弃非字符串/数字的杂项。
    """
    if not isinstance(value, (list, tuple)):
        return []
    seen: set = set()
    result: list = []
    for item in value:
        if not isinstance(item, (str, int)):
            continue
        key = str(item).strip()
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= _TREE_ORDER_MAX:
            break
    return result


def get_tree_order() -> dict:
    """返回用户自定义的目录树顺序（未设置过时两层都为空列表，前端按默认顺序展示）。"""
    raw = (db.get_setting(TREE_ORDER_KEY, "") or "").strip()
    if not raw:
        return {"ok": True, "categories": [], "plugins": []}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # 存储值损坏（例如用户手改过库）时按「未设置」处理，不阻塞界面
        _log.warning("目录树顺序存储值无法解析，已按默认顺序处理")
        return {"ok": True, "categories": [], "plugins": []}
    if not isinstance(parsed, dict):
        return {"ok": True, "categories": [], "plugins": []}
    return {
        "ok": True,
        "categories": _clean_order_list(parsed.get("categories")),
        "plugins": _clean_order_list(parsed.get("plugins")),
    }


def set_tree_order(categories=None, plugins=None) -> dict:
    """持久化目录树顺序。传空数组即表示该层恢复默认（自然）顺序。"""
    payload = {
        "categories": _clean_order_list(categories),
        "plugins": _clean_order_list(plugins),
    }
    db.set_setting(TREE_ORDER_KEY, json.dumps(payload, ensure_ascii=False))
    _log.info(
        "目录树顺序已更新：%d 个类别 / %d 个插件",
        len(payload["categories"]),
        len(payload["plugins"]),
    )
    return {"ok": True, **payload}


# ── 验证代理（注册表 + 全局默认） ───────────────────────────
# 一律按 id 引用；取名字只用于展示。每条凭据用哪个代理由凭据自己的字段决定，见 proxy_store。

def list_proxies() -> dict:
    """全部代理 + 当前全局默认，供「系统设置」渲染。"""
    return {
        "ok": True,
        "items": proxy_store.list_proxies(),
        "global_id": proxy_store.global_proxy_id(),
        "global_url": proxy_store.global_url() or "",
    }


def _apply_global_after_change() -> None:
    """代理表有变动后，按当前全局设置重新写一次环境变量（删掉的那个可能正被全局引用）。"""
    proxy_config.apply_proxy(proxy_store.global_url() or "")


def create_proxy(name: str, url: str) -> dict:
    result = proxy_store.create_proxy(name, url)
    if result.get("error"):
        return {"ok": False, "message": result.get("message") or "新增代理失败"}
    _log.info("已新增代理 #%s（%s）", result.get("id"), name)
    push_event("vault:proxies_changed")
    return {"ok": True, **list_proxies()}


def update_proxy(proxy_id: int, name=None, url=None) -> dict:
    result = proxy_store.update_proxy(proxy_id, name, url)
    if result.get("error"):
        return {"ok": False, "message": result.get("message") or "修改代理失败"}
    _apply_global_after_change()
    _log.info("已修改代理 #%s", proxy_id)
    push_event("vault:proxies_changed")
    return {"ok": True, **list_proxies()}


def delete_proxy(proxy_id: int) -> dict:
    result = proxy_store.delete_proxy(proxy_id)
    if not result.get("ok"):
        return {"ok": False, "message": "代理不存在"}
    _apply_global_after_change()
    _log.info("已删除代理 #%s", proxy_id)
    push_event("vault:proxies_changed")
    return {"ok": True, **list_proxies()}


def set_global_proxy(proxy_id=None) -> dict:
    """设置全局默认代理；``None`` / ``0`` / 空串 = 直连。"""
    if proxy_id in (None, "", 0, "0"):
        result = proxy_store.set_global_proxy(None)
        _log.info("全局代理已设为直连")
    else:
        # 代理 id 是 UUID 字符串，**不要**转 int（一转换就成了一个不存在的代理）
        result = proxy_store.set_global_proxy(str(proxy_id))
        if not result.get("ok"):
            return {"ok": False, "message": result.get("message") or "设置失败"}
        _log.info("全局代理已设为 #%s", proxy_id)
    push_event("vault:proxies_changed")
    return {"ok": True, **list_proxies()}


def test_proxy_id(proxy_id=None) -> dict:
    """对照测试某个代理（``None`` = 当前全局生效的那个）。"""
    result = proxy_store.test_proxy_by_id(proxy_id)
    verdict = result.get("verdict", {})
    _log.info("代理测试完成（%s）：%s", result.get("proxy_source", ""), verdict.get("headline", ""))
    return result


# ── TLS 版本策略（框架级） ───────────────────────────────────
# 与代理同一套逐级继承：凭据 > 插件预设 > 类别预设 > 全局默认（auto）。

def get_tls_options() -> dict:
    """全局默认 TLS 版本 + 可选值，供「系统设置」渲染。"""
    return {"ok": True, **tls_store.options()}


def set_tls_version(value="") -> dict:
    """设置全局默认 TLS 版本（``auto`` / ``1.0`` / ``1.1`` / ``1.2`` / ``1.3``）。"""
    result = tls_store.set_global(value)
    _log.info("全局 TLS 版本已设为 %s", result.get("tls_version"))
    return {"ok": True, **tls_store.options()}


# ── 类别级 / 插件级默认设置 ─────────────────────────────────
# 框架只提供「存储 + 继承 + 注入」；哪些字段可预设由插件在 manifest 里自行声明
# （默认全部非敏感字段可预设，加 "preset": false 可排除）。敏感字段永远不可预设。

def get_credential_defaults(credential_type: str) -> dict:
    """该插件新建凭据时应带的默认值（插件级覆盖类别级）。新增表单打开时调它。"""
    return {
        "ok": True,
        "values": preset_store.defaults_for(credential_type),
        "uses_proxy": plugin_manager.plugin_uses_proxy(credential_type),
        # 给「使用代理」下拉里那项做括注：该插件选「跟随上层设置」时实际会走哪个出口
        "inherited_proxy": proxy_store.describe_inherited(credential_type),
        # 同上，给「TLS 版本」下拉里「跟随上层」那项做括注
        "inherited_tls": tls_store.describe_inherited(credential_type),
        "tls_versions": list(tls_store.VERSIONS),
    }


def get_scope_options(scope: str, scope_key: str, credential_type: str = "") -> dict:
    """某个作用域的默认设置界面所需数据（当前值 + 可预设字段列表）。"""
    data = preset_store.scope_options(scope, scope_key, credential_type)
    # 该作用域下有没有「走代理」的插件——没有就不给用户显示代理项，免得白设
    if scope == preset_store.SCOPE_PLUGIN:
        data["uses_proxy"] = plugin_manager.plugin_uses_proxy(credential_type or scope_key)
    else:
        data["uses_proxy"] = any(
            plugin_manager.plugin_uses_proxy(p["credential_type"])
            for p in db.list_plugins()
            if (p.get("category") or "") == scope_key
        )
    data["tls_versions"] = list(tls_store.VERSIONS)
    return {"ok": True, **data}


def set_scope_options(scope: str, scope_key: str, values: dict,
                      credential_type: str = "") -> dict:
    result = preset_store.set_scope_defaults(scope, scope_key, values or {}, credential_type)
    if result.get("ok"):
        _log.info("已保存 %s 默认设置 %s（%d 项）", scope, scope_key,
                  len(result.get("values") or {}))
    return result


def clear_scope_options(scope: str, scope_key: str) -> dict:
    _log.info("已清空 %s 默认设置 %s", scope, scope_key)
    return preset_store.clear_scope_defaults(scope, scope_key)


# ── 备份 / 恢复 ────────────────────────────────────────────

def _file_dialog(kind: str, default_name: str = "",
                 file_types: Optional[list] = None) -> Optional[str]:
    """打开系统文件对话框，返回所选路径（取消或无窗口时返回 None）。

    ``file_types`` 默认只给 JSON（备份用）；通用的「另存为」（仓库打包、对象下载）要自己传，
    否则用户面对一个写死的 `*.json` 过滤器会存错扩展名。
    """
    if _window is None:
        return None
    try:
        import webview

        # pywebview 5.x 起把 webview.SAVE_DIALOG / OPEN_DIALOG 收敛成了
        # webview.FileDialog 枚举，旧的模块级常量仍能用但会打印弃用警告；
        # 这里优先用新枚举，旧版本回退到常量。
        dialog_enum = getattr(webview, "FileDialog", None)
        if dialog_enum is not None:
            dialog_type = dialog_enum.SAVE if kind == "save" else dialog_enum.OPEN
        else:
            dialog_type = webview.SAVE_DIALOG if kind == "save" else webview.OPEN_DIALOG

        kwargs: dict = {"file_types": tuple(file_types or ("JSON 备份 (*.json)", "所有文件 (*.*)"))}
        if kind == "save":
            kwargs["save_filename"] = default_name
        result = _window.create_file_dialog(dialog_type, **kwargs)
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result
    except Exception as e:
        _log.warning("文件对话框打开失败: %s", e)
        return None


def build_backup_doc(scope: str = "all", encrypted: bool = True,
                     credential_ids: Optional[list] = None) -> dict:
    """构建备份文档（不弹对话框、不落盘）——GUI 导出与 MCP 工具共用。

    返回 ``{"ok": True, "document": {...}}``，或 ``{"error": "vault_locked"}`` /
    ``{"ok": False, "message": ...}``。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    if scope not in ("all", "credentials"):
        return {"ok": False, "message": f"无效范围：{scope}"}
    if credential_ids is not None and not credential_ids:
        return {"ok": False, "message": "未选择任何凭据"}

    doc = backup.build_backup(key, scope=scope, encrypted=encrypted,
                              credential_ids=credential_ids)
    return {"ok": True, "document": doc}


def _resolve_decrypt_key(doc: dict, key: bytes, orig_password: Optional[str]) -> tuple:
    """决定**解开这份文档密文**该用哪把 key。返回 ``(decrypt_key | None, 需要密码时的提示)``。

    v2 起文档带 ``vault.salt``：文档来自**别的保险库**时，密文是用「原主密码 + 那份 salt」
    派生的 key 加密的——只有拿到原主密码才能解开，解完再用**当前库的 key** 重新加密写回。
    """
    vault = doc.get("vault") if isinstance(doc.get("vault"), dict) else {}
    doc_salt = str((vault or {}).get("salt") or "").strip()
    cur_salt = (db.get_setting("salt", "") or "").strip()
    if not doc.get("encrypted") or not doc_salt or doc_salt == cur_salt:
        return key, ""                       # 明文 / 无 salt 信息 / 同一保险库
    if not orig_password:
        return None, "这份备份来自另一个保险库，请输入它当时的主密码后重试"
    try:
        salt = bytes.fromhex(doc_salt)
    except ValueError:
        return None, "备份里的 salt 不合法，无法还原"
    derived = derive_key(orig_password, salt)
    verify_token = str((vault or {}).get("verify_token") or "").strip()
    if verify_token and not check_verify_token(derived, verify_token):
        return None, "主密码不正确（与这份备份的保险库不匹配）"
    return derived, ""


def apply_backup_doc(doc: dict, dedupe: bool = True, source: str = "",
                     orig_password: Optional[str] = None) -> dict:
    """把备份文档并入当前库（不弹对话框）——GUI 导入与 MCP 工具共用。

    ``source`` 只写进导入日志抬头，标明这份备份的来源文件。
    ``orig_password``：文档来自**别的保险库**（带 ``vault.salt``）时，用它来解开密文；
    缺省/不对时返回 ``{"ok": False, "need_password": True}``，由界面补问后重试。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    # 导入按「当前插件字段表」填充，所以**先**把库结构补到当前版本——否则结构对不上会填错/填不进去。
    # （版本号由迁移运行器在每跳成功后推进，不会抢跑。）
    sc = ensure_schema_current()
    if not sc.get("ok"):
        return {"ok": False,
                "message": f"数据库结构升级失败，已中止导入：{sc.get('message')}"}
    decrypt_key, need_msg = _resolve_decrypt_key(doc, key, orig_password)
    if decrypt_key is None:
        return {"ok": False, "need_password": True, "message": need_msg}
    try:
        result = backup.import_backup(key, doc, dedupe=dedupe, source=source,
                                      decrypt_key=decrypt_key)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    # 备份里的代理设置若被导入，立即生效（全局默认可能是按 id 引用的，要用解析后的地址）
    proxy_config.apply_proxy(proxy_store.global_url() or "")
    push_event("vault:credentials_changed")
    push_event("vault:plugins_changed")
    push_event("vault:proxies_changed")
    _log.info("已导入备份：成功 %d 条，失败 %d 条，重复跳过 %d 条（日志：%s）",
              result["imported"], result.get("failed_count", 0),
              result.get("skipped_duplicate", 0), result.get("log_file") or "未生成")
    return result


def export_backup(scope: str = "all", encrypted: bool = True,
                  credential_ids: Optional[list] = None) -> dict:
    """导出备份到用户选择的文件。

    - ``encrypted=True``：敏感值沿用密文（需同一主密码导入）
    - ``credential_ids``：只导出这些凭据（省略或 None = 全部）
    """
    built = build_backup_doc(scope, encrypted, credential_ids)
    if built.get("error") or built.get("ok") is False:
        return built
    doc = built["document"]

    path = _file_dialog(
        "save", default_name=f"keyrealm-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    if not path:
        return {"ok": False, "message": "已取消"}

    try:
        backup.write_backup_file(path, doc)
    except Exception as e:
        return {"ok": False, "message": f"写入失败: {e}"}

    _log.info("已导出备份 %s（%d 条凭据，范围=%s，加密=%s）",
              path, doc["counts"]["credentials"], scope, encrypted)
    return {
        "ok": True,
        "path": path,
        "count": doc["counts"]["credentials"],
        "scope": scope,
        "selected": credential_ids is not None,
        "encrypted": bool(encrypted),
    }


def import_backup(orig_password: Optional[str] = None, path: Optional[str] = None) -> dict:
    """从备份文件导入（自动识别加密/明文）。

    ``path`` 传了就不弹文件框——界面在「这份备份来自别的保险库、请补主密码」后会带着
    **同一个文件**再调一次，免得让用户重选一遍。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    if not path:
        path = _file_dialog("open")
        if not path:
            return {"ok": False, "message": "已取消"}

    try:
        doc = backup.read_backup_file(path)
    except Exception as e:
        return {"ok": False, "message": f"读取失败: {e}"}

    result = apply_backup_doc(doc, source=path, orig_password=orig_password)
    if result.get("ok"):
        result["path"] = path
    elif result.get("need_password"):
        result["path"] = path      # 带上同一个文件，界面补问主密码后直接重试，不必重选
    return result


# ── 程序内更新（从源码编译）─────────────────────────────────
# 实现在 backend/updater.py（应用内与四个独立脚本共用同一份）。这里只做薄转发 +
# 应用内专属的「交棒」：起更新进程后，经 _close_quit_cb 关闭程序。

def update_check(kind: str = "formal") -> dict:
    """检查更新（只读，不改工作区）。正式版看版本号，测试版看版本号 + commit。"""
    try:
        _sync_update_token()
        return updater.check_update(kind)
    except Exception as e:  # noqa: BLE001 - 检查边界兜底，不把栈甩给前端
        _log.exception("检查更新失败")
        return {"ok": False, "message": f"检查更新失败：{type(e).__name__}"}


def update_status() -> dict:
    """上一次更新的结果（读 data/.update-status.json；无则返回 {}）。"""
    try:
        return updater.read_status()
    except Exception:  # noqa: BLE001
        return {}


def db_migration_status() -> dict:
    """磁盘数据库结构版本与源码版本的差异（是否需要逐级迁移）。"""
    try:
        return updater.db_status()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"读取数据库版本失败：{type(e).__name__}"}


def get_update_channel() -> dict:
    return {"channel": updater.get_channel()}


def set_update_channel(channel: str) -> dict:
    return updater.set_channel(channel)


def get_update_proxy() -> dict:
    """更新走哪个代理（跟随全局默认 / 直连 / 指定某个代理）与解析后的地址。

    与应用内每凭据的「使用代理」同一套语义，但这里是**全局**的（更新对所有凭据一视同仁）。
    """
    try:
        return {"ok": True, **updater.get_update_proxy()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"读取更新代理失败：{type(e).__name__}"}


def set_update_proxy(mode: str, proxy_id: str = "") -> dict:
    return updater.set_update_proxy(mode, proxy_id)


# ── 更新访问令牌（可选）───────────────────────────────────
# 本仓库已公开，匿名也能检查与更新；填令牌是为认证 GitHub API（提速率上限）与「仓库又转回私有」的情形。
# 令牌用**主密码加密**后存 settings（与凭据同口径）；明文只在本进程 env 与内存里。
# 一旦注入 env，updater 的 git / 下载，以及「交棒」起的更新子进程都会自动带上（见 updater.ENV_TOKEN）。

def _sync_update_token(force: bool = False) -> str:
    """把已存的令牌解密后注入本进程 env（幂等）；返回当前生效的令牌（无则 ``""``）。

    ``force=True`` 时忽略 env 里的现值，重新按 settings 解析（保存/清除令牌后调用）。
    未解锁 / 未配置 / 解密失败都不抛错——更新退化为「匿名访问」，对公开仓库照常可用（只受匿名限流）。
    """
    if not force:
        cur = os.environ.get(updater.ENV_TOKEN, "")
        if cur:
            return cur
    enc = db.get_setting(updater.SETTING_TOKEN, "")
    if not enc:
        os.environ.pop(updater.ENV_TOKEN, None)
        return ""
    key = _get_key()
    if key is None:
        return ""
    try:
        token = (decrypt(key, enc) or "").strip()
    except Exception as e:  # noqa: BLE001 - 解密失败按「未配置」处理，别拖垮更新入口
        _log.warning("解密更新访问令牌失败：%s", type(e).__name__)
        return ""
    if token:
        os.environ[updater.ENV_TOKEN] = token
    else:
        os.environ.pop(updater.ENV_TOKEN, None)
    return token


def get_update_token() -> dict:
    """更新访问令牌状态：是否已配置 + 掩码展示（**绝不**回明文）。"""
    token = _sync_update_token()
    return {"ok": True, "configured": bool(token), "mask": mask_secret(token)}


def set_update_token(token: str) -> dict:
    """保存 / 清除更新访问令牌（主密码加密后落库，并即时注入 env）。空串 = 清除。"""
    token = (token or "").strip()
    if not token:
        db.set_setting(updater.SETTING_TOKEN, "")
        os.environ.pop(updater.ENV_TOKEN, None)
        _log.info("已清除更新访问令牌")
        return {"ok": True, "configured": False, "mask": ""}
    key = _get_key()
    if key is None:
        return {"ok": False, "message": "请先解锁后再保存访问令牌"}
    db.set_setting(updater.SETTING_TOKEN, encrypt(key, token))
    os.environ[updater.ENV_TOKEN] = token
    _log.info("已保存更新访问令牌（%s）", mask_secret(token))
    return {"ok": True, "configured": True, "mask": mask_secret(token)}


# 「立即更新」的准备任务 id（同一时刻只允许一个在跑）
_update_task_id: Optional[str] = None


# ── 自动检测更新（启动时查一次）───────────────────────────
# 「自动检测更新」默认开启：应用启动（以及首次解锁）后**在后台查一次**，发现新版本就通知前端。
# 未解锁时拿不到访问令牌；公开仓库即便没有令牌也能查，但解锁后（令牌注入）再补一次更稳，故此时**不标记完成**。

# ── 检查更新（后台任务 + 结果持久化）────────────────────────
# 「检查更新」与「自动检测更新」共用同一台机器：**同一时刻只跑一个**；结果写
# data/.update-check.json 持久化，界面换页 / 重启后仍能显示，**直到被新结果替换**。
# - 手动检查（点按钮）→ 登记成**后台任务**（在「任务」页可见）；
# - 自动检查（启动 / 解锁）→ 静默，**只在成功时**持久化（免得启动时未解锁的失败盖掉好结果）。

_check_lock = threading.Lock()
_check_state: dict = {"running": False, "task_id": None, "auto_done": False, "notice": None}


def get_update_auto_check() -> dict:
    return {"enabled": updater.get_auto_check()}


def set_update_auto_check(enabled) -> dict:
    result = updater.set_auto_check(enabled)
    if result.get("enabled"):
        # 重新开启 → 清掉「本会话已自动查过」的标记并立刻再查一次（给用户即时反馈）
        with _check_lock:
            _check_state["auto_done"] = False
            _check_state["notice"] = None
        start_auto_update_check()
    return result


def get_update_check_result() -> dict:
    """最近一次「检查更新」的结果（**持久化**）；从未查过返回 ``{}``。"""
    return updater.read_last_check()


def start_update_check() -> dict:
    """「检查更新」——**后台任务**：结果持久化，换页 / 重启后仍在，直到被新结果替换。"""
    return _start_check("manual")


def start_auto_update_check() -> dict:
    """启动 / 解锁后自动查一次（**静默**，失败不打扰；发现新版本弹通知）。"""
    return _start_check("auto")


def _start_check(source: str) -> dict:
    if source == "auto" and not updater.get_auto_check():
        return {"ok": False, "skipped": True, "reason": "disabled"}
    with _check_lock:
        if _check_state["running"]:
            # 已有检查在跑（多半是刚启动的自动检查）：搭车，不重复跑
            return {"ok": True, "running": True, "task_id": _check_state["task_id"]}
        if source == "auto" and _check_state["auto_done"]:
            return {"ok": False, "skipped": True, "reason": "done"}
        task = tasks.create("update_check", "检查更新") if source == "manual" else None
        _check_state["running"] = True
        _check_state["task_id"] = task["id"] if task else None
    threading.Thread(target=_run_check, args=(source,), daemon=True).start()
    return {"ok": True, "task_id": task["id"] if task else None}


def _run_check(source: str) -> None:
    with _check_lock:
        tid = _check_state["task_id"]
    try:
        _sync_update_token()          # 注入令牌（走认证请求；解锁后才拿得到）
        res = updater.check_update(updater.get_channel())
    except Exception as exc:  # noqa: BLE001 - 检查边界兜底，绝不抛
        _log.warning("检查更新异常：%s", type(exc).__name__)
        res = {"ok": False, "error": "error", "message": f"检查更新异常：{type(exc).__name__}"}
    ok = bool(res.get("ok"))

    # 手动检查总是留痕（含失败）；自动检查只在成功时落盘
    if source == "manual" or ok:
        try:
            updater.write_last_check(res)
        except Exception as exc:  # noqa: BLE001
            _log.warning("写检查结果失败：%s", type(exc).__name__)

    if tid:
        if ok:
            tasks.finish(tid, ok=True, output=_check_summary(res))
        else:
            tasks.finish(tid, ok=False, error=res.get("message") or res.get("error") or "检查失败")
    elif not ok:
        _log.info("自动检查更新未完成（解锁后会再试一次）：%s",
                  res.get("message") or res.get("error") or "")

    if ok:
        with _check_lock:
            _check_state["auto_done"] = True
        if res.get("has_update"):
            latest = res.get("latest") or {}
            _log.info("检查更新：发现新版本 %s（当前 %s）",
                      latest.get("version"), (res.get("current") or {}).get("version"))
            notice = {k: res.get(k) for k in
                      ("kind", "current", "latest", "reason", "db_change", "db_chain", "asset")}
            notice["has_update"] = True
            with _check_lock:
                _check_state["notice"] = notice
            push_event("vault:update_available", notice)

    push_event("vault:update_checked", res)     # 界面据此刷新「检查更新」卡片
    with _check_lock:
        _check_state["running"] = False
        _check_state["task_id"] = None


def _check_summary(res: dict) -> str:
    latest = res.get("latest") or {}
    cur = res.get("current") or {}
    if res.get("has_update"):
        v = latest.get("version") or ""
        c = latest.get("commit") or ""
        return f"发现新版本 {v}" + (f"（{c}）" if c else "")
    return f"已是最新（当前 {cur.get('version') or ''}）"


def take_auto_update_notice() -> dict:
    """取出「自动检测发现的更新」通知（**取出即清除**，避免重复弹）。无则返回 ``{}``。"""
    with _check_lock:
        notice = _check_state.get("notice")
        _check_state["notice"] = None
    return notice or {}


def _full_export_doc() -> Optional[dict]:
    """给更新前的备份构一份**全量导出文档**；拿不到（未解锁等）就返回 ``None``——不影响更新。"""
    try:
        built = build_backup_doc("all", True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("生成全量导出失败：%s", type(exc).__name__)
        return None
    if not built.get("ok"):
        _log.info("跳过随包全量导出：%s", built.get("message") or built.get("error") or "")
        return None
    return built["document"]


def start_update(kind: str = "formal", restart: bool = True, terminal: str = "auto") -> dict:
    """「立即更新」：**应用内先准备**（备份 + 下载 / 拉源码 / 装依赖 / 构建，带进度、可取消），
    准备成功后再**交棒**给脱离进程（替换 / 迁移 / 重启）并退出程序。

    与旧实现的关键区别：**不再一点就退**——最耗时的准备阶段留在应用内，用户看得到进度。
    准备失败 / 取消 / 无更新一律**不退出应用**。退出走 `_close_quit_cb`（= main._quit_app），
    与「关闭窗口 → 直接退出」同一条路径。
    """
    global _update_task_id
    kind = kind if kind in updater.CHANNELS else "formal"
    if kind == "test" and not updater.git_available():
        return {"ok": False, "message": "测试版需要从源码构建：本机未安装 git，无法拉取源码。"}
    if _update_task_id and (tasks.get(_update_task_id) or {}).get("status") == tasks.RUNNING:
        return {"ok": False, "message": "已有更新准备在进行中，请稍候或先取消"}

    _sync_update_token()   # 注入令牌（注入 env，交棒子进程会继承）

    label = "正式版" if kind == "formal" else "测试版"
    task = tasks.create("update", f"准备更新（{label}）")
    tid = task["id"]
    _update_task_id = tid

    def _run() -> None:
        try:
            res = updater.prepare_update(
                kind,
                progress_cb=lambda c, t, s: tasks.progress(tid, c, t, s),
                should_cancel=tasks.should_cancel(tid),
                export_doc=_full_export_doc(),      # 全量导出随备份落盘（原库坏了也能还原）
                log=lambda m: _log.info("更新准备：%s", m),
            )
        except Exception as exc:  # noqa: BLE001 - 准备边界兜底，不退出应用
            _log.exception("准备更新异常")
            res = {"ok": False, "message": f"准备更新异常：{type(exc).__name__}"}

        if res.get("canceled"):
            tasks.finish(tid, ok=False, canceled=True, error="已取消")
            _log.info("更新准备已取消（渠道=%s）", kind)
            return
        if not res.get("ok"):
            msg = res.get("message", "准备失败")
            tasks.finish(tid, ok=False, error=msg)
            push_event("vault:update_failed", {"kind": kind, "message": msg})
            _log.warning("更新准备失败：%s", msg)
            return
        if not res.get("has_update"):
            tasks.finish(tid, ok=True, output=res.get("message", "已是最新"))
            return

        # 准备成功 → 交棒（起脱离进程 + 关闭应用）
        hs = updater.begin_handoff(kind, restart=restart, terminal=terminal,
                                   quit_cb=_close_quit_cb, prepared=res.get("prepared"))
        if hs.get("ok"):
            tasks.finish(tid, ok=True, output=hs.get("message", "已开始更新"))
            _log.info("已发起程序内更新（渠道=%s，已准备）", kind)
        else:
            msg = hs.get("message", "交棒失败")
            tasks.finish(tid, ok=False, error=msg)
            push_event("vault:update_failed", {"kind": kind, "message": msg})
            _log.warning("交棒失败：%s", msg)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "task_id": tid, "preparing": True,
            "message": "已开始准备更新（准备完成后程序将自动关闭并重启）"}


# ── Logging ───────────────────────────────────────────────

def get_log_config() -> dict:
    """返回当前日志级别、可选级别列表与日志文件路径。"""
    return {
        "level": logger.get_level(),
        "levels": logger.LEVELS,
        "log_file": logger.log_file_path(),
    }


def set_log_level(level: str) -> dict:
    """设置日志级别：立即生效并持久化到 settings（下次启动恢复）。"""
    level = (level or "").strip().upper()
    if level not in logger.LEVELS:
        return {"ok": False, "message": f"无效日志级别：{level or '(空)'}"}
    logger.set_level(level)
    db.set_setting("log_level", level)
    _log.info("日志级别已设置为 %s", level)
    return {"ok": True, "level": level}


def get_logs(limit: int = 200, level: str = "", search: str = "") -> dict:
    """读取日志文件末尾若干条记录，可按最低级别过滤 + 关键词搜索。"""
    try:
        limit = max(1, min(int(limit or 200), 2000))
    except (TypeError, ValueError):
        limit = 200
    level = (level or "").strip().upper()
    if level and level not in logger.LEVELS:
        level = ""
    search = (search or "").strip()
    entries = logger.read_logs(limit, level or None, search or None)
    return {
        "ok": True,
        "entries": entries,
        "count": len(entries),
        "log_file": logger.log_file_path(),
    }


def clear_logs() -> dict:
    """清空日志文件与轮转备份。"""
    logger.clear_logs()
    _log.info("日志已清空")
    return {"ok": True, "message": "日志已清空"}


def open_log_file(name: str) -> dict:
    """用系统默认程序打开 `data/logs/` 下的日志文件（导入失败日志的「打开」按钮用）。

    只接受**单个文件名**：这些文件的名字由后端生成、前端只是原样回传，没有任何理由
    允许路径分隔符——挡住了拼接穿越，也就不必再谈白名单。
    """
    name = (name or "").strip()
    if not name or os.path.basename(name) != name or name in (".", ".."):
        return {"ok": False, "message": "非法的日志文件名"}
    path = logger.LOG_DIR / name
    if not path.is_file():
        return {"ok": False, "message": f"日志文件不存在：{name}"}
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]  # 仅 Windows 存在
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:  # noqa: BLE001 - 打不开是环境问题，如实回报即可
        _log.warning("打开日志文件失败 %s: %s", name, e)
        return {"ok": False, "message": f"打开失败：{e}"}
    _log.info("已用系统默认程序打开日志文件 %s", name)
    return {"ok": True, "path": str(path)}
