"""
插件管理器：plugins/ 目录两层扫描，自动加载

目录结构（一服务一插件，同类插件可按类别目录归组）：

    plugins/
      .state.json             # 启用/禁用状态（{"disabled": [credential_type, ...]}），缺失=全启用
      llm/                    # 类别目录（对应左侧树的一层）
        category.json         # 可选：{"name": "LLM 模型", "icon": "🤖"}
        llm_common.py         # 可选：子插件共享代码
        deepseek/             # 独立子插件
          manifest.json
          plugin.py
        openai/...
      some_plugin/            # 也支持根级平铺插件（无类别）
        manifest.json
        plugin.py

放入插件目录即自动加载、删除目录即在下次扫描时自动卸载，均无需手动管理；启用/禁用只写在
plugins/.state.json 里（禁用即卸载模块，凭据与插件目录原样保留）。
"""
from __future__ import annotations

import base64
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import database as db
from . import icon_ink
from . import logger
from . import proxy_store
from . import redact
from . import tls_store
from .paths import app_dir
from .plugin_base import (
    VerifyResult,
    call_actions,
    call_capabilities,
    call_display_summary,
    call_list_models,
    call_run_action,
    call_stream_action,
    call_validate,
    call_verify,
    load_manifest,
    load_plugin_module,
)

_log = logger.get_logger(__name__)

# 应用根目录：开发时=项目根；冻结后=exe 同级目录。
# plugins/ 是插件的【唯一事实来源】，不打包进 exe，随 exe 同级以外部目录形式提供。
BASE_DIR = app_dir()
PLUGINS_DIR = BASE_DIR / "plugins"
# 启用/禁用状态的**唯一事实来源**：plugins/ 下的一个点文件。
# {"disabled": ["llm_groq", "api_shodan"]}（键是 credential_type）。
# 文件不存在 = 全部启用；文件损坏也退化为全部启用（见 _read_disabled）。
# 插件目录本身仍是唯一事实来源——这里只记「关掉了哪几个」，不记插件清单。
STATE_FILE = PLUGINS_DIR / ".state.json"

# 已禁用的 credential_type 缓存（按 mtime 失效）：批量验证会对每条凭据调一次
# is_plugin_enabled，不缓存的话每个凭据都要 stat + 读一次文件。
_state_cache: Optional[set[str]] = None
_state_mtime: Optional[float] = None

# 内存中的插件模块缓存  credential_type -> module
_loaded_modules: dict[str, Any] = {}

# 每个凭据类型最近一次加载失败的原因（成功则清除），用于把笼统的
# 「插件未安装或加载失败」补成带真实缘由的提示（见 verify_credential / run_credential_action）。
_load_errors: dict[str, str] = {}

# manifest icon 字段支持图片文件名（相对于插件目录），解析为 data URL 后的缓存
_ICON_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".gif": "image/gif",
}
_icon_image_cache: dict[str, Optional[str]] = {}

# 深色主题下「这个 logo 要不要垫浅色底」的缓存（credential_type -> bool），
# 与 _icon_image_cache 同生命周期：同一个 logo 只分析一次，清缓存只在扫描插件时。
_icon_lift_cache: dict[str, bool] = {}

# 类别目录 category.json 的 icon 缓存（category_folder -> icon）
_category_icon_cache: dict[str, Optional[str]] = {}


def _read_disabled() -> set[str]:
    """读 .state.json 的 disabled 集合（按 mtime 缓存）。

    **任何异常都退化为空集（= 全部启用）**：一个坏掉的状态文件绝不能让所有插件在界面上
    集体消失——那是最糟的失败模式（用户会以为插件全丢了）。
    """
    global _state_cache, _state_mtime
    try:
        mtime = STATE_FILE.stat().st_mtime
    except OSError:
        # 文件不存在 = 全部启用（出厂默认）
        _state_cache, _state_mtime = set(), None
        return set()
    if _state_cache is not None and _state_mtime == mtime:
        return _state_cache
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        raw = data.get("disabled") if isinstance(data, dict) else None
        disabled = {x for x in raw if isinstance(x, str)} if isinstance(raw, list) else set()
        if raw is not None and not isinstance(raw, list):
            _log.warning("插件状态文件的 disabled 不是数组（%s），按全部启用处理", type(raw).__name__)
    except Exception as e:  # noqa: BLE001 - 坏 JSON / 非对象 / 编码错，一律回落全启用
        _log.warning("插件状态文件不可读，按全部启用处理: %s", e)
        disabled = set()
    _state_cache, _state_mtime = disabled, mtime
    return disabled


def _write_disabled(disabled: set[str]) -> None:
    """原子写 .state.json：先写同目录的 .tmp 再 Path.replace，避免读到半截文件。"""
    global _state_cache, _state_mtime
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(
        json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)
    try:
        _state_mtime = STATE_FILE.stat().st_mtime
    except OSError:
        _state_mtime = None
    _state_cache = set(disabled)


def is_plugin_enabled(credential_type: str) -> bool:
    """该 credential_type 的插件当前是否启用（禁用 = 功能闸门关闭）。"""
    return credential_type not in _read_disabled()


def disabled_types() -> list:
    """当前被禁用的 credential_type 列表（供**全量备份**导出）。"""
    return sorted(_read_disabled())


def set_disabled(types) -> None:
    """整体覆盖禁用集合（供**全量备份**导入；随后 sync 一次即生效）。"""
    _write_disabled({str(x) for x in (types or []) if str(x).strip()})
    sync_plugins()


def batchable_types() -> set[str]:
    """可参与「批量验证」的 credential_type 全集（**唯一的判定处**）。

    只有**同时**满足两条的插件才算数：

    - manifest 未声明 ``batch_verify=false``（那类插件无法自动化验证，或有写副作用）；
    - 当前处于启用状态（`plugins/.state.json`，即 `is_plugin_enabled` 的事实来源）。

    `scheduler.batch_verify` 用它挑要验的凭据、`api.batch_verify` 用它算返回给前端的
    ``total`` —— 两边各写一份筛选逻辑，正是「total 与实际执行数口径不一致」的成因。

    这是**白名单**语义：插件表为空（尚未 `sync_plugins`）时返回空集，等于「一条都不批量
    验证」。失败方向刻意选安全的那个 —— 反向默认会去跑那些「有写副作用、已标注不可批量」
    的插件。
    """
    disabled = _read_disabled()
    return {
        p["credential_type"] for p in db.list_plugins()
        if p.get("batch_verify", 1) and p["credential_type"] not in disabled
    }


def get_category_icon(category: str) -> Optional[str]:
    """类别图标：读取 <类别目录>/category.json 的 icon 字段。

    类别节点与插件节点图标相互独立，避免类别借用某个插件的 icon。
    未配置时返回 None，前端回退到默认文件夹图标。
    """
    if not category:
        return None
    if category in _category_icon_cache:
        return _category_icon_cache[category]
    result: Optional[str] = None
    try:
        cat_file = PLUGINS_DIR / category / "category.json"
        if cat_file.is_file():
            with open(cat_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("icon"):
                result = str(data["icon"])
    except Exception as e:
        _log.warning("类别图标读取失败 %s: %s", category, e)
    _category_icon_cache[category] = result
    return result


def _plugin_rel_path(plugin_path: str) -> str:
    """把插件目录转成**相对 ``plugins/`` 的路径**（如 ``llm/deepseek``）——写库与展示都用它。

    ``plugins.plugin_path`` 一律存相对值：绝对路径会把程序目录钉死，用户换一次目录就全部失效
    （早期版本正是这么存的，``_resolve_plugin_path`` 里还留着兜底重定位）。
    """
    if not plugin_path:
        return ""
    try:
        return (
            Path(plugin_path).resolve()
            .relative_to(PLUGINS_DIR.resolve())
            .as_posix()
        )
    except Exception:
        return Path(plugin_path).name


def _icon_path(record: dict) -> Optional[Path]:
    """manifest 的 icon 指向的插件目录内图片文件；不是图片文件名（如 emoji）则 None。

    防目录穿越：图标文件必须位于插件目录内。
    """
    icon = (record.get("icon") or "").strip()
    if not icon or Path(icon).suffix.lower() not in _ICON_IMAGE_MIME:
        return None
    try:
        plugin_dir = _resolve_plugin_path(record).resolve()
        path = (plugin_dir / icon).resolve()
    except Exception:  # noqa: BLE001 —— 路径不可解析，等同「没有图标」
        return None
    return path if path.is_relative_to(plugin_dir) and path.is_file() else None


def get_icon_image(record: dict) -> Optional[str]:
    """插件自定义图标：manifest 的 icon 为插件目录内图片文件时读取为 data URL。

    icon 不是图片文件名（如 emoji）时返回 None，前端回退到默认展示。
    """
    ext = Path((record.get("icon") or "").strip()).suffix.lower()
    ct = record.get("credential_type", "")
    if ct in _icon_image_cache:
        return _icon_image_cache[ct]
    result: Optional[str] = None
    path = _icon_path(record)
    if path is not None:
        try:
            b64 = base64.b64encode(path.read_bytes()).decode()
            result = f"data:{_ICON_IMAGE_MIME[ext]};base64,{b64}"
        except Exception as e:
            _log.warning("插件图标读取失败 %s: %s", ct, e)
    _icon_image_cache[ct] = result
    return result


def icon_needs_lift(record: dict) -> bool:
    """该插件 logo 在深色主题下要不要垫一层浅色底（判据见 icon_ink 模块）。

    icon 不是图片文件名（emoji 等）时恒为 False —— 那种情况前端根本不走 <img>。
    分析失败时返回 True：垫了只是不好看，不垫就直接看不见。
    """
    ct = record.get("credential_type", "")
    if ct in _icon_lift_cache:
        return _icon_lift_cache[ct]
    need = False
    path = _icon_path(record)
    if path is not None:
        try:
            need = icon_ink.needs_lift(path)
        except Exception as e:
            _log.warning("插件图标可见度分析失败（按需要垫底处理） %s: %s", ct, e)
            need = True
    _icon_lift_cache[ct] = need
    return need


def _scan_plugin_dirs() -> list[tuple[Path, str, dict]]:
    """扫描 plugins/ 两层结构。

    返回 [(plugin_dir, category_folder, category_meta)]：
    - 根级含 manifest.json 的目录为平铺插件（category 为空）；
    - 否则视为类别目录，其下含 manifest.json 的子目录为子插件，
      category.json 提供类别显示名/图标。
    """
    results: list[tuple[Path, str, dict]] = []
    root = PLUGINS_DIR
    if not root.exists():
        return results
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        if (entry / "manifest.json").exists():
            results.append((entry, "", {}))
            continue
        cat_meta: dict = {}
        cat_file = entry / "category.json"
        if cat_file.exists():
            try:
                with open(cat_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cat_meta = data
            except Exception as e:
                _log.warning("类别元数据读取失败 %s: %s", cat_file, e)
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir() or sub.name.startswith(("_", ".")):
                continue
            if (sub / "manifest.json").exists():
                results.append((sub, entry.name, cat_meta))
    return results


def _category_display(category: str, meta: dict) -> str:
    return str(meta.get("name") or category or "未分类")


# 「像密钥」的字段名提示：只用于启动时的 lint 判断（判不准也不影响任何功能）
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "pass", "auth",
                 "credential", "sign", "webhook", "cookie", "session")


def _looks_like_secret_field(field) -> bool:
    """字段看起来是不是「密钥」：``type=password``，或名字里含密钥类词。"""
    if str(getattr(field, "type", "") or "").lower() == "password":
        return True
    key = str(getattr(field, "key", "") or "").lower()
    return any(hint in key for hint in _SECRET_HINTS)


def sync_plugins():
    """扫描 plugins/ 目录，同步插件注册表、DB 的 enabled 镜像与已加载模块。

    - 新目录 → 注册并加载（放入即生效）；
    - 已存在目录 → 更新记录并重载模块（改完重启/刷新即生效）；
    - 被禁用的插件 → 卸载模块（功能闸门随之关闭），记录里 enabled 记 0；
    - 磁盘上已消失的目录 → 移除模块、删除数据库记录、清理状态文件里的残余键
      （删除即卸载，凭据保留）。

    启用/禁用的唯一事实来源是 plugins/.state.json；DB 的 plugins.enabled 列只是它的
    **只读镜像**，不参与任何功能判定（真正的闸门是 get_module）。
    """
    _icon_image_cache.clear()
    _icon_lift_cache.clear()
    _category_icon_cache.clear()
    disabled = _read_disabled()
    found: set[str] = set()
    for plugin_dir, category, cat_meta in _scan_plugin_dirs():
        try:
            manifest = load_manifest(plugin_dir / "manifest.json")
        except Exception as e:
            _log.error("插件 manifest 无效 %s: %s", plugin_dir, e)
            continue
        ct = manifest.credential_type
        found.add(ct)
        # lint：声明了字段却一个 sensitive 都没有 → 该插件的密钥完全不参与输出净化。
        # 这是**静默失效**的一种（用户与作者都看不出区别），加载时点出来。
        # **只在确实存在「像密钥」的字段时才报**：像「匿名对象存储」那样全字段都非机密的插件
        # （只有桶地址 / 备注）本来就没有可净化的东西，按旧条件会每次启动都误报。
        if manifest.fields and not any(f.sensitive for f in manifest.fields):
            suspect = [f.key for f in manifest.fields if _looks_like_secret_field(f)]
            if suspect:
                _log.warning(
                    "插件 %s 声明了 %d 个字段，但没有任何 sensitive:true —— "
                    "其密钥（%s）不会被输出净化（错误消息里的密钥会落日志/落库）",
                    ct, len(manifest.fields), "、".join(suspect),
                )
        is_on = ct not in disabled
        try:
            db.upsert_plugin(
                name=manifest.name,
                version=manifest.version,
                credential_type=ct,
                fields_schema=manifest.fields_schema_json(),
                plugin_path=_plugin_rel_path(str(plugin_dir)),
                author=manifest.author,
                description=manifest.description,
                icon=manifest.icon,
                category=category,
                category_name=_category_display(category, cat_meta),
                batch_verify=manifest.batch_verify,
                enabled=is_on,
                commit=False,
            )
            if is_on:
                load_plugin(ct)
            else:
                # 禁用的插件不该占着内存；顺带清掉加载错误，免得重新启用后还带着旧提示
                _unload_plugin(ct)
                _load_errors.pop(ct, None)
        except Exception as e:
            _log.error("插件加载失败 %s (%s): %s", plugin_dir, ct, e)

    db.commit()          # 批量 upsert 一次提交；逐插件 commit 会让启动多花数百毫秒
    for record in db.list_plugins():
        ct = record["credential_type"]
        if ct in found:
            continue
        _unload_plugin(ct)
        db.delete_plugin_record(ct)
        _log.info("插件已移除（目录不存在）: %s", ct)

    # 目录都没了，状态文件里对应的键也该清掉，否则会无限累积
    stale = disabled - found
    if stale:
        _write_disabled(disabled - stale)
        _log.info("清理状态文件中已失效的插件: %s", ", ".join(sorted(stale)))


def _unload_plugin(credential_type: str):
    _loaded_modules.pop(credential_type, None)
    sys.modules.pop(f"_vault_plugin_{credential_type}", None)


def set_plugin_enabled(credential_type: str, enabled: bool) -> dict:
    """启用/禁用插件。

    只写 .state.json（唯一事实来源），随后交给 sync_plugins() 去镜像 DB 的 enabled 列
    并加载/卸载模块。禁用**不动**插件的任何凭据，也不动插件目录本身——要物理移除插件，
    由用户自己删掉 plugins/<类别>/<服务>/ 目录（下次扫描即卸载，凭据保留）。
    """
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return {"ok": False, "message": "插件不存在"}
    disabled = set(_read_disabled())
    if enabled:
        disabled.discard(credential_type)
    else:
        disabled.add(credential_type)
    _write_disabled(disabled)
    sync_plugins()
    _log.info("插件已%s: %s", "启用" if enabled else "禁用", credential_type)
    return {
        "ok": True,
        "name": record.get("name") or credential_type,
        "credential_type": credential_type,
        "enabled": bool(enabled),
    }


def _module_missing_error(credential_type: str) -> str:
    """插件模块不可用时的用户可见提示；带上最近一次加载失败的真实缘由。

    禁用与「未安装/加载失败」是两回事，提示必须分开——否则用户看到「插件未安装」会去
    翻插件目录，而其实只是被自己关掉了。
    """
    if not is_plugin_enabled(credential_type):
        return f"插件已禁用: {credential_type}"
    reason = _load_errors.get(credential_type)
    base = f"插件未安装或加载失败: {credential_type}"
    return f"{base}（{reason}）" if reason else base


def _resolve_plugin_path(record: dict) -> Path:
    """取插件目录。

    记录里的 ``plugin_path`` 是**相对 ``plugins/`` 的路径**（如 ``llm/deepseek``）——存绝对路径
    会把程序目录钉死，用户把目录搬到别处就全部失效。为兼容**早期写过绝对路径**的老记录，
    这里：相对值直接拼 ``PLUGINS_DIR``；绝对值仍照用，失效时按「类别 / 服务目录名」重定位
    （老库由迁移脚本 ``m_1.1.0__to_1.2.0.py`` 统一改成相对）。
    """
    raw = str(record.get("plugin_path") or "").strip()
    if not raw:
        return PLUGINS_DIR
    path = Path(raw)
    if not path.is_absolute():
        return PLUGINS_DIR / path
    # —— 老记录：绝对值（迁移前遗留）——
    if (path / "plugin.py").exists():
        return path
    service = path.name
    category = record.get("category") or ""
    if service and category:
        candidate = PLUGINS_DIR / category / service
        if (candidate / "plugin.py").exists():
            _log.warning("插件路径已失效，按类别/服务重定位 %s: %s → %s",
                         record.get("credential_type"), path, candidate)
            return candidate
    return path


def load_plugin(credential_type: str) -> Optional[Any]:
    """加载插件模块到内存（被禁用的插件不加载）"""
    if not is_plugin_enabled(credential_type):
        return None
    record = db.get_plugin_by_type(credential_type)
    if not record:
        _load_errors[credential_type] = "数据库中没有该插件的记录"
        return None
    plugin_path = _resolve_plugin_path(record)
    try:
        module = load_plugin_module(plugin_path, credential_type)
        _loaded_modules[credential_type] = module
        _load_errors.pop(credential_type, None)
        return module
    except Exception as e:
        # 记下具体原因，好让「插件未安装或加载失败」这条前端提示能带上真实缘由，
        # 否则用户只看到一句无从下手的笼统报错。
        _load_errors[credential_type] = f"{type(e).__name__}: {e}"
        _log.error("加载插件失败 %s (%s): %s", credential_type, plugin_path, e)
        return None


def get_module(credential_type: str) -> Optional[Any]:
    """获取已加载的插件模块（懒加载）。

    **这是唯一的「功能闸门」**：插件被禁用时恒返回 None。下游无需各自判断——
    has_actions→False、plugin_actions/plugin_models→[]、plugin_capabilities→
    {"available": None, "note": "", "error": True}、run/stream_plugin_action→
    {"ok": False, "error": ...}、get_display_summary→""、plugin_uses_proxy→False、
    verify_credential→status="error"（message 为「插件已禁用: …」）。
    """
    if not is_plugin_enabled(credential_type):
        return None
    if credential_type not in _loaded_modules:
        return load_plugin(credential_type)
    return _loaded_modules[credential_type]


def _log_proxy_outlet(what: str, credential_type: str, resolved, data: dict = None) -> None:
    """记下本次调用**实际生效的出口**（直连 / 哪个代理）。

    为什么值得专门记一行：凭据里写着「用代理 #1」、实际却直连，这种错配在界面上
    **完全看不出来**（下拉里照样显示着代理名）。排查「配了代理还是不生效」时，
    这行日志是唯一的抓手。``resolved`` 就是 ``credential_proxy_scope`` yield 出来的解析结果。

    引用的代理**已不存在**时升级成 WARNING——那是静默降级成直连的最常见原因。
    """
    try:
        missing = proxy_store.missing_proxy_ref(data or {}, credential_type)
        if missing is not None:
            _log.warning("%s %s：引用的代理 #%s **已不存在**，已回落直连"
                         "（请到「系统设置 → 网络与代理」重新选择，或改预设）",
                         what, credential_type, missing)
            return
        _log.info("%s %s：代理出口 = %s", what, credential_type,
                  proxy_store.describe_outlet(resolved))
    except Exception:  # noqa: BLE001 - 记日志绝不能影响调用本身
        pass


def _redact_result(credential_type: str, data: dict, result: VerifyResult) -> VerifyResult:
    """净化 verify 结果里回显的敏感字段值（就地改 message / account_info）。

    这是**唯一**的收口点：单条 / 批量 / MCP / 定时四条验证路径都经 ``verify_credential``，
    而下游 ``backend/scheduler.py`` 会把 ``result.message`` **落日志 + 落 DB 的
    ``status_detail``**——不在这里净化，插件经 ``body_snippet`` / ``str(exc)`` 原样带出的
    上游响应（部分服务的密钥就在 URL query 里）就会持久化到磁盘。
    """
    record = db.get_plugin_by_type(credential_type) or {}
    secrets = redact.secrets_of(record.get("fields_schema"), data, credential_type=credential_type)
    if not secrets:
        return result
    result.message = redact.redact_text(result.message, secrets)
    result.account_info = redact.redact_deep(result.account_info, secrets)
    return result


def _log_tls_policy(what: str, credential_type: str, resolved) -> None:
    """记下本次调用显式指定的 TLS 版本（跟随全局默认时不记）。"""
    try:
        if resolved is tls_store.UNSET:
            return
        _log.info("%s %s：TLS 版本 = %s", what, credential_type, resolved)
    except Exception:  # noqa: BLE001 - 记日志绝不能影响调用本身
        pass


@contextlib.contextmanager
def _credential_scopes(credential_type: str, data: dict, what: str):
    """代理 + TLS 版本两个作用域收在一处并统一记日志。会联网的插件调用都应包在这里。"""
    with proxy_store.credential_proxy_scope(data, credential_type) as outlet:
        with tls_store.credential_tls_scope(data, credential_type) as tls_ver:
            _log_proxy_outlet(what, credential_type, outlet, data)
            _log_tls_policy(what, credential_type, tls_ver)
            yield


def verify_credential(credential_type: str, data: dict) -> VerifyResult:
    """调用插件验证凭据（含「按凭据解析代理 / TLS 版本」与「欠费探测」）"""
    module = get_module(credential_type)
    if module is None:
        return VerifyResult(status="error", message=_module_missing_error(credential_type))
    # 作用域收口：单条 / 批量 / MCP / 定时 四条路径都经此函数
    with _credential_scopes(credential_type, data, "验证"):
        result = call_verify(module, data)
        _apply_quota_probe(credential_type, module, data, result)
    return _redact_result(credential_type, data, result)


def plugin_uses_proxy(credential_type: str) -> bool:
    """该插件的请求是否走代理（决定前端要不要给它显示「使用代理」）。

    **默认 True**：绝大多数插件经 httpx 发请求（`make_client` 读 `VAULT_PROXY` /
    `vault_proxy_ctx` 注入的出口），「用哪个代理」是每个凭据的通用设置，理应都有。

    不能用「模块里有没有 `make_client`」判定——那会漏掉**间接**发请求的插件：例如经
    `llm_protocols` 的 `probe_openai_chat` / `chat_completion` 发请求的，会被误判为
    「不走代理」而隐藏该选项。同理，走裸 TCP 的邮箱（`smtplib` / `imaplib`）**也仍走代理**：
    它用 `mail_common` 的自建 socket 读 `plugin_sdk.proxy()`、穿 SOCKS / HTTP CONNECT 隧道。

    真正**无出网请求**的插件在**模块级显式**声明 `USES_PROXY = False` 即可排除
    （见 `docs/PLUGIN_GUIDE.md` §2.5）。**目前仓库里没有任何插件这样做**——别照着旧版本
    的注释去给邮箱 / 微信 iLink 加这个声明，那会把有用的「使用代理」选项藏掉。
    """
    module = get_module(credential_type)
    if module is None:
        return False
    return bool(getattr(module, "USES_PROXY", True))


def _apply_quota_probe(credential_type: str, module, data: dict, result: VerifyResult) -> None:
    """余额拿不到时的「欠费探测」后置检查（原地修改 result）。

    只在**验证通过**且**没有余额/使用率**时才给插件机会——有余额就没必要再花一发请求。
    开关与模型都在凭据自己的字段里（``probe_chat`` / ``probe_model``），未接入探测的
    插件没有 ``quota_probe``，直接跳过。
    """
    if result.status != "valid":
        return
    info = result.account_info or {}
    if info.get("balance") is not None or info.get("usage_rate") is not None:
        return
    probe = getattr(module, "quota_probe", None)
    if not callable(probe):
        return
    try:
        extra = probe(data)
    except Exception as e:  # noqa: BLE001 - 后置检查绝不能影响验证结果
        _log.warning("欠费探测异常 %s: %s", credential_type, type(e).__name__)
        return
    if not isinstance(extra, dict):
        return
    extra_info = extra.get("account_info")
    if isinstance(extra_info, dict) and extra_info:
        result.account_info = {**info, **extra_info}
    if extra.get("message"):
        result.message = str(extra["message"])


def validate_credential(credential_type: str, data: dict) -> list[str]:
    """调用插件校验字段"""
    module = get_module(credential_type)
    if module is None:
        return [_module_missing_error(credential_type)]
    return call_validate(module, data)


def get_display_summary(credential_type: str, data: dict) -> str:
    """调用插件获取显示摘要。

    **预留接口，当前未接线**：列表「附加信息」列渲染的是 ``status_detail.account_info``
    （见 frontend/src/views/Credentials.vue 的 ``extraInfo()``），没有任何生产路径调用本函数；
    调用者仅 tests/test_plugin_state.py（验证「插件未实现该可选方法时返回空串」）。
    保留是因为它属插件可选契约（见 backend/plugin_sdk.py 的接口表），删掉会破坏契约面。
    """
    module = get_module(credential_type)
    if module is None:
        return ""
    return call_display_summary(module, data)


def has_actions(credential_type: str) -> bool:
    """该插件是否声明了「简易操作」（可选能力）。"""
    module = get_module(credential_type)
    return bool(module is not None and hasattr(module, "actions"))


def plugin_actions(credential_type: str, data: dict) -> list:
    """该插件对给定凭据可用的操作列表。"""
    module = get_module(credential_type)
    return call_actions(module, data) if module else []


def plugin_capabilities(credential_type: str, data: dict) -> dict:
    """该凭据当前真正能用的操作（可选能力）。

    **会联网**（插件要拿凭据去打上游接口探权限），所以包在代理作用域里，与 `plugin_models` 同理。
    """
    module = get_module(credential_type)
    if module is None:
        return {"available": None, "note": "", "error": True}
    with _credential_scopes(credential_type, data, "权限探测"):
        return call_capabilities(module, data)


def run_plugin_action(credential_type: str, action_id: str, params: dict, data: dict) -> dict:
    """执行插件的某个操作。"""
    module = get_module(credential_type)
    if module is None:
        return {"ok": False, "error": _module_missing_error(credential_type)}
    with _credential_scopes(credential_type, data, "操作"):
        return call_run_action(module, action_id, params, data)


def plugin_models(credential_type: str, data: dict, force: bool = False) -> list:
    """该插件对给定凭据可用的模型列表（可选能力）。

    ``force=True`` 表示**用户手动刷新**，透传给插件的 ``list_models``（穿透 24h 模型清单缓存）。
    """
    module = get_module(credential_type)
    if module is None:
        return []
    with _credential_scopes(credential_type, data, "模型列表"):
        return call_list_models(module, data, force)


def stream_plugin_action(credential_type: str, action_id: str, params: dict, data: dict,
                         on_delta, on_progress=None, should_cancel=None) -> dict:
    """流式执行插件操作；插件未实现时回退为普通执行。

    注意：调用方（``api.stream_credential_action``）是在**新线程**里跑这个函数的，
    所以代理作用域必须在这里建立——设在调用线程里子线程读不到。

    两个可选回调：``on_progress(current, total, text)`` 进度、
    ``should_cancel()`` 取消查询（见 ``plugin_base.call_stream_action``）。
    """
    module = get_module(credential_type)
    if module is None:
        return {"ok": False, "error": _module_missing_error(credential_type)}
    with _credential_scopes(credential_type, data, "流式操作"):
        return call_stream_action(module, action_id, params, data, on_delta,
                                  on_progress, should_cancel)


def list_installed_plugins(*, sync: bool = True) -> list[dict]:
    """列出所有插件（含凭据数量）；默认先做一次目录扫描同步，使新放入的插件即时可见。

    ``sync=False`` 供已经同步过的调用方（如 CLI ``status``）避免重复扫描与重复写库。
    """
    if sync:
        sync_plugins()
    plugins = db.list_plugins()
    for p in plugins:
        creds = db.list_credentials(credential_type=p["credential_type"])
        p["credential_count"] = len(creds)
        p["category_icon"] = get_category_icon(p.get("category") or "")
        p["rel_path"] = _plugin_rel_path(p.get("plugin_path") or "")
    return plugins
