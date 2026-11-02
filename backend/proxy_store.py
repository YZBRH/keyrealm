"""代理注册表与「按凭据解析代理」的逻辑。

背景
----
代理不能只靠一个全局字符串（``settings.verify_proxy``）给所有凭据共用，因此：

- **`proxies` 表**维护多个代理，各自可取名字；**其它地方一律按 id 引用**，不传名字、不传 URL。
- **全局默认**存 ``settings.global_proxy_id``（空 = 直连）。
- **每条凭据**用自己 ``data`` 里的两个保留键选择：
  ``_proxy_mode``（``global`` / ``proxy`` / ``direct``）+ ``_proxy_id``（mode 为 proxy 时有效）。
  放进 ``data`` 而不是新建表，是为了让四条验证路径（单条 / 批量 / MCP / 流式）**都不用改签名**
  ——``data`` 本来就会原样传到插件手里。

解析结果以 ``vault_proxy_ctx`` 的形式按调用生效（见该模块），因此批量并发时不同凭据
用不同代理也不会互相踩。
"""
from __future__ import annotations

import contextlib
import os
from typing import Optional, Union

from . import database as db
from . import proxy as proxy_config
from . import proxy_chain
from . import vault_proxy_ctx as ctx

# 凭据 data 里的两个保留键（约定：插件 manifest 的字段 key 不得以 "_" 开头）
KEY_MODE = "_proxy_mode"
KEY_ID = "_proxy_id"

MODE_PROXY = "proxy"
MODE_DIRECT = "direct"

SETTING_GLOBAL_ID = "global_proxy_id"
# 旧版单值：保留作为「没启用多代理」时的兜底，也是进程环境变量 VAULT_PROXY 的来源
SETTING_LEGACY_URL = "verify_proxy"


# ── 注册表 CRUD ──────────────────────────────────────────

def list_proxies() -> list[dict]:
    """列出全部代理与代理链（附解析后的 scheme，方便前端展示）。"""
    items = db.list_proxies()
    for item in items:
        item["scheme"] = proxy_config.scheme(item.get("url", ""))
        item["kind"] = "proxy"
    for chain in proxy_chain.list_chains():
        items.append({
            "id": chain["id"],
            "name": chain.get("name") or "",
            "url": chain.get("display") or "（成员缺失）",
            "scheme": "chain",
            "kind": "chain",
            "members": chain.get("members") or [],
            "member_names": chain.get("member_names") or [],
            "valid": chain.get("valid", True),
            "error": chain.get("error") or "",
            "sort_order": 10_000,
        })
    return items


def create_proxy(name: str, url: str, proxy_id: Optional[str] = None) -> dict:
    """新建代理；``proxy_id`` 只在**导入备份时**用来原样沿用源库的 UUID。"""
    name = (name or "").strip()
    url = proxy_config.normalize(url or "")
    if not name:
        return {"error": "invalid_name", "message": "代理名称不能为空"}
    if not proxy_config.is_valid(url):
        return {"error": "invalid_url", "message": f"代理地址不合法：{url}"}
    return db.create_proxy(name, url, proxy_id)


def update_proxy(proxy_id: int, name: Optional[str] = None, url: Optional[str] = None) -> dict:
    if url is not None:
        url = proxy_config.normalize(url)
        if not proxy_config.is_valid(url):
            return {"error": "invalid_url", "message": f"代理地址不合法：{url}"}
    if name is not None and not name.strip():
        return {"error": "invalid_name", "message": "代理名称不能为空"}
    updated = db.update_proxy(proxy_id, name=(name or "").strip() or None, url=url)
    if not updated:
        return {"error": "not_found", "message": "代理不存在"}
    return updated


def delete_proxy(proxy_id: int) -> dict:
    """删除代理。被代理链引用的成员拒绝删除；引用它的凭据会在下次解析时回退到全局。"""
    users = proxy_chain.chains_using(proxy_id)
    if users:
        names = "、".join(f"「{name}」" for name in users)
        return {"ok": False, "message": f"该代理被代理链{names}引用，请先修改或删除代理链"}
    if str(global_proxy_id() or "") == str(proxy_id or ""):
        set_global_proxy(None)
    removed = db.delete_proxy(proxy_id)
    return {"ok": removed, "id": proxy_id}


# ── 解析 ─────────────────────────────────────────────────

def get_proxy_url(proxy_id) -> Optional[str]:
    """id → 代理地址。这是「取用代理」的唯一入口（绝不按名字/裸值传）。

    id 是 UUID 字符串。这里**不做 int 转换**——旧实现 `int(uuid)` 会抛异常被吞掉、
    返回 None，那正是「配了代理却静默直连」的成因之一。
    """
    pid = str(proxy_id or "").strip()
    if not pid:
        return None
    if proxy_chain.is_chain_id(pid):
        return proxy_chain.ensure_url(pid)
    record = db.get_proxy(pid)
    return (record or {}).get("url") or None


def global_proxy_id() -> Optional[str]:
    """全局默认代理的 id（UUID 字符串）；未设置返回 None。"""
    return db.get_setting(SETTING_GLOBAL_ID, "").strip() or None


def describe_id(proxy_id) -> str:
    """代理 id 的人类可读描述（不启动代理链转发器）。"""
    pid = str(proxy_id or "").strip()
    if not pid:
        return ""
    if proxy_chain.is_chain_id(pid):
        chain = proxy_chain.get_chain(pid)
        if chain:
            names = []
            for member in chain.get("members") or []:
                record = db.get_proxy(str(member)) or {}
                names.append(str(record.get("name") or record.get("url") or member))
            return f"代理链 {chain.get('name') or pid}（{' → '.join(names)}）"
        return "代理链（已失效）"
    record = db.get_proxy(pid) or {}
    return str(record.get("url") or "")


def global_display() -> str:
    """全局默认代理的展示文本（代理链显示名称与成员，而不是本地转发端口）。"""
    pid = global_proxy_id()
    if pid is not None:
        text = describe_id(pid)
        if text:
            return text
    return db.get_setting(SETTING_LEGACY_URL, "").strip() or ""


def global_url() -> Optional[str]:
    """全局代理地址：有 global_proxy_id 时只按 id 解析（失效即直连）；否则回落旧版单值。"""
    pid = global_proxy_id()
    if pid is not None:
        return get_proxy_url(pid)
    return db.get_setting(SETTING_LEGACY_URL, "").strip() or None


def apply_global() -> Optional[str]:
    """把当前全局代理解析并写入环境变量（启动时调用；代理链会拉起本地转发器）。"""
    url = global_url() or ""
    proxy_config.apply_proxy(url)
    return url or None


def set_global_proxy(proxy_id: Optional[str]) -> dict:
    """设置全局默认代理（``None`` = 直连），并立即把值同步进 ``VAULT_PROXY``。

    ``verify_proxy`` 一并写好：进程环境变量那条老通道（以及没走 ctx 的场景）仍然依赖它。
    """
    if proxy_id is None:
        db.set_setting(SETTING_GLOBAL_ID, "")
        db.set_setting(SETTING_LEGACY_URL, "")
        proxy_config.apply_proxy("")
        return {"ok": True, "proxy_id": None, "url": ""}

    url = get_proxy_url(proxy_id)
    if not url:
        return {"ok": False, "message": "代理不存在"}
    db.set_setting(SETTING_GLOBAL_ID, str(proxy_id))
    db.set_setting(SETTING_LEGACY_URL, url)
    proxy_config.apply_proxy(url)
    return {"ok": True, "proxy_id": str(proxy_id), "url": url}


def _resolve_spec(mode, proxy_id) -> Union[object, str, None]:
    """把一对 ``(_proxy_mode, _proxy_id)`` 解析成 ``ctx.DIRECT`` / 代理 URL / ``None``。

    ``None`` 表示**这一层没给出可用的指定**（没填、或者是「跟随上层」、又或者引用的代理已被删）
    —— 交给调用方继续往上一层找。
    """
    text = str(mode or "").strip().lower()
    if text == MODE_DIRECT:
        return ctx.DIRECT
    if text == MODE_PROXY:
        return get_proxy_url(proxy_id)      # 悬空 id 返回 None → 继续往上层找，而不是直接掉到全局
    return None


def inherited_proxy(credential_type: str):
    """该插件继承到的代理设置（**子模块预设 > 上层模块预设**，``preset_store`` 已按此顺序合并）。

    返回 ``(mode, proxy_id)``；没设过就是 ``("", None)``。
    """
    if not credential_type:
        return "", None
    # 延迟导入：preset_store 依赖本模块的两个保留键常量，模块级导入会成环
    from . import preset_store

    values = preset_store.defaults_for(credential_type) or {}
    return values.get(KEY_MODE) or "", values.get(KEY_ID)


def describe_inherited(credential_type: str) -> str:
    """给界面看的一句话：该插件若选「跟随上层设置」，最终会走哪个出口。

    用于「使用代理」下拉里那项的括注，免得用户对着「跟随上层」猜到底是哪个代理。
    """
    mode, proxy_id = inherited_proxy(credential_type)
    text = str(mode or "").strip().lower()
    if text == MODE_DIRECT:
        return "直连"
    if text == MODE_PROXY:
        value = describe_id(proxy_id)
        if value:
            return value
    return global_display() or "直连"


def resolve_credential_proxy(data: dict, credential_type: str = "") -> Union[object, str]:
    """解析本次调用该用哪个代理，**按优先级逐层找**：

    1. **条目自己**的设置（``_proxy_mode`` / ``_proxy_id``）
    2. **子模块预设**（该 credential_type 的默认设置）
    3. **上层模块预设**（该类别目录的默认设置）
    4. **全局设置** —— 用 ``ctx.UNSET`` 表达，让 ``proxy()`` 回落到 ``VAULT_PROXY`` 环境变量
       （它由全局默认代理写入，见 ``set_global_proxy``）

    返回 ``ctx.DIRECT``（直连）/ 代理 URL / ``ctx.UNSET``（继续用环境变量那条路）。
    """
    data = data or {}
    # 1) 条目自身
    own = _resolve_spec(data.get(KEY_MODE), data.get(KEY_ID))
    if own is not None:
        return own
    # 2) + 3) 子模块预设、上层模块预设（preset_store 已按「插件 > 类别」合并好）
    mode, proxy_id = inherited_proxy(credential_type)
    if mode or proxy_id:
        inherited = _resolve_spec(mode, proxy_id)
        if inherited is not None:
            return inherited
    # 4) 全局
    return ctx.UNSET


@contextlib.contextmanager
def credential_proxy_scope(data: dict, credential_type: str = ""):
    """在本次调用期间生效「该凭据的代理」，退出时按 token 还原（支持嵌套、线程安全）。

    ``credential_type`` 是继承链的关键——没有它就只能看条目自己的设置，找不到子模块/类别预设。

    **yield 出本次解析结果**（``ctx.DIRECT`` / 代理 URL / ``ctx.UNSET``），供调用方记日志：
    「配了代理却没走」这类问题在界面上完全看不出来，唯一能查的就是这行日志。
    """
    resolved = resolve_credential_proxy(data, credential_type)
    token = ctx.begin(resolved)
    try:
        yield resolved
    finally:
        ctx.end(token)


def describe_outlet(resolved: Union[object, str]) -> str:
    """把解析结果说成人话，给日志用（把「继续回退环境变量」也展开成实际值）。"""
    value = effective_url(resolved)
    if value:
        return value
    if resolved is ctx.DIRECT:
        return "直连（该凭据显式选了直连）"
    return "直连（未配置代理）"


def effective_url(resolved: Union[object, str]) -> Optional[str]:
    """解析结果最终会用的代理 URL；``None`` 表示实际是直连。

    ``ctx.UNSET`` 表示继续回退环境变量，因此这里与插件 SDK 使用同一组变量。
    """
    if resolved is ctx.DIRECT:
        return None
    if resolved is not ctx.UNSET:
        return str(resolved) or None
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def missing_proxy_ref(data: dict, credential_type: str = ""):
    """本次**本应走某个代理、但那个 id 已经不存在** → 返回该 id；否则 ``None``。

    为什么专门查这个：悬空引用会被 `_resolve_spec` **静默降级成直连**，而界面上
    「使用代理」下拉照样好好显示着代理名——用户以为在走代理，实际是直连，
    表现就是「配了代理还是不生效」，且查不到任何线索。**这条必须能被看见。**
    """
    data = data or {}
    candidates = [(data.get(KEY_MODE), data.get(KEY_ID))]
    inherited_mode, inherited_id = inherited_proxy(credential_type)
    candidates.append((inherited_mode, inherited_id))
    for mode, proxy_id in candidates:
        if str(mode or "").strip().lower() != MODE_PROXY or not proxy_id:
            continue
        if get_proxy_url(proxy_id) is None:
            return proxy_id
    return None


# ── 连通性测试 ───────────────────────────────────────────

def test_proxy_by_id(proxy_id=None) -> dict:
    """测试指定代理（``None`` = 测全局当前生效的那个）。"""
    from . import proxy_test

    if proxy_id is None:
        url = global_url() or ""
        source = "global"
    else:
        url = get_proxy_url(proxy_id)
        if not url:
            return {"ok": False, "message": "代理不存在"}
        source = f"proxy:{proxy_id}"
    result = proxy_test.test_proxy(url)
    result["proxy_source"] = source
    return result
