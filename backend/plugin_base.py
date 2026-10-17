"""
插件基类与接口定义
所有插件必须实现 verify(data: dict) -> VerifyResult
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .plugin_sdk import VerifyResult
from .proxy import SOCKS_HINT

_log = logging.getLogger("plugin")


def _friendly_error(e: Exception) -> str:
    """插件异常 → 展示给用户的文本。

    只做一处替换：httpx 在缺 socksio 时抛英文 ImportError（"…'socksio' package is not
    installed…"），这句话对用户没有可操作性，换成中文处理办法。原始异常仍由调用方
    完整写入日志（见 call_verify 的 _log.exception），不会丢排查线索。
    """
    if "socksio" in str(e):
        return SOCKS_HINT
    return str(e)


def _plugin_name(module: Any) -> str:
    """取插件的可读标识（用于日志），去掉内部模块名前缀。"""
    return getattr(module, "__name__", "?").replace("_vault_plugin_", "")


# 前端**不渲染 markdown**（提示文本一律按纯文本显示），而插件作者常按 README 习惯写
# `**加粗**`，界面上就会露出字面星号。这里在解析 manifest 时统一剥成纯文本（只留内容）。
# 正则加 `(?!/)` 负向断言：`**/` 是 glob（如 `**/*.py`）而不是加粗标记，放行——
# 即便同一段里同时出现 glob 与加粗，glob 也不会被误剥。
_MD_BOLD = re.compile(r"\*\*(?!/)(.+?)\*\*", re.S)


def plain_text(text: Any) -> Any:
    """把 `**加粗**` 剥成纯文本；字符串以外的容器递归处理，其它类型原样返回。"""
    if isinstance(text, str):
        return _MD_BOLD.sub(r"\1", text) if "**" in text else text
    if isinstance(text, list):
        return [plain_text(x) for x in text]
    if isinstance(text, dict):
        return {k: plain_text(v) for k, v in text.items()}
    return text


# VerifyResult 是框架与插件**共用**的权威定义，住在 backend/plugin_sdk.py（插件唯一
# 被允许 import 的 backend 模块）。这里只把它引进来供类型标注与鸭子类型适配使用；
# 插件返回自己的同名 dataclass 也能被 call_verify 归一化（见下）。


@dataclass
class FieldSchema:
    """插件字段定义"""
    key: str
    label: str
    type: str = "text"         # text / password / textarea / select / number / url / checkbox
    required: bool = False
    sensitive: bool = False
    default: Any = None
    placeholder: str = ""
    options: list = field(default_factory=list)   # 字符串数组，或 {value,label} 对象数组
    # 端点候选：`[{label, value}]`。供 Base URL / 服务端点这类字段做「可输入 + 下拉选择」，
    # 候选值取自厂商官方文档（区域/国内外站/兼容路径）。留空则该字段保持普通输入框。
    presets: list = field(default_factory=list)
    hint: str = ""             # 字段下方的补充说明（可选，用于解释开关/高级项）
    preset: Optional[bool] = None   # 是否允许作为「类别/插件默认设置」；None=允许（非敏感即可），False=排除
    # 条件显隐：{"另一个字段key": 值 或 [值, ...]}，仅当那个字段当前值命中时才渲染本字段。
    # 用于「同厂商不同模式」——例如 openai 的 auth_mode 选 api_key 时只显示 API Key 相关字段、
    # 选 chatgpt 时只显示登录态那组。前端负责隐藏；保存时**被隐藏字段的值不再提交**
    # （免得切模式后另一组的密钥还留在库里）。
    show_if: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "sensitive": self.sensitive,
        }
        if self.default is not None:
            d["default"] = self.default
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.options:
            d["options"] = self.options
        if self.presets:
            d["presets"] = self.presets
        if self.hint:
            d["hint"] = self.hint
        if self.preset is not None:
            d["preset"] = self.preset
        if self.show_if:
            d["show_if"] = self.show_if
        return d


@dataclass
class PluginManifest:
    """插件 manifest.json 解析结果"""
    name: str
    version: str
    credential_type: str
    author: str = ""
    description: str = ""
    icon: str = "🔑"
    fields: list[FieldSchema] = field(default_factory=list)
    # 是否允许参与批量验证。无法自动化验证或自动化验证有风险（如会产生写副作用）的
    # 插件应设为 False：前端将其排除在批量范围外，仅允许手动单个验证。
    batch_verify: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        fields = []
        for f in data.get("fields", []):
            fields.append(FieldSchema(
                key=f["key"],
                label=plain_text(f["label"]),
                type=f.get("type", "text"),
                required=f.get("required", False),
                sensitive=f.get("sensitive", False),
                default=f.get("default"),
                placeholder=plain_text(f.get("placeholder", "")),
                options=plain_text(f.get("options", [])),
                presets=plain_text(f.get("presets", [])),
                hint=plain_text(f.get("hint", "")),
                preset=f.get("preset"),
                show_if=f.get("show_if"),
            ))
        return cls(
            name=data["name"],
            version=data["version"],
            credential_type=data["credential_type"],
            author=data.get("author", ""),
            description=plain_text(data.get("description", "")),
            icon=data.get("icon", "🔑"),
            fields=fields,
            batch_verify=bool(data.get("batch_verify", True)),
        )

    def fields_schema_json(self) -> list[dict]:
        return [f.to_dict() for f in self.fields]


def load_manifest(manifest_path: Path) -> PluginManifest:
    """读取并校验 manifest.json"""
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    required_keys = {"name", "version", "credential_type", "fields"}
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"manifest.json 缺少必要字段: {missing}")
    return PluginManifest.from_dict(data)


def _helper_from_other_plugin(origin: str, own_dir: Path, category_dir: Path) -> bool:
    """``origin`` 这个文件是不是来自**另一个插件目录**（同名辅助模块串味的判据）。

    直接躺在类别目录下的共享模块（``plugins/llm/llm_common.py``、``plugins/cloud/google_sa.py``）
    是各插件有意复用的，不算串味；只有落进**别的插件目录**（该目录下有 ``plugin.py``）才算。
    越界判断沿祖先链往上走，走到自己目录或类别目录就停 —— 所以插件目录比类别目录深几层
    都不影响（``plugins/llm/shared/util.py`` 这类嵌套共享目录也不会被误判）。
    """
    try:
        parent = Path(origin).resolve().parent
    except Exception:  # noqa: BLE001 - 解析失败按「不是」处理，宁可不动全局状态
        return False
    for ancestor in [parent, *parent.parents]:
        if ancestor == own_dir or ancestor == category_dir:
            return False
        if (ancestor / "plugin.py").is_file():
            return True
    return False


def load_plugin_module(plugin_path: Path, credential_type: str) -> Any:
    """
    动态加载插件模块
    plugin_path: plugin.py 所在目录
    credential_type: 用作模块名避免冲突
    """
    py_file = plugin_path / "plugin.py"
    if not py_file.exists():
        raise FileNotFoundError(f"插件文件不存在: {py_file}")

    module_name = f"_vault_plugin_{credential_type}"
    spec = importlib.util.spec_from_file_location(module_name, str(py_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件: {py_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # 允许插件导入自身目录与类别父目录下的共享模块
    # （如 plugins/llm/deepseek/plugin.py 导入 plugins/llm/llm_common.py）。
    # 仅在执行期间临时注入 sys.path，避免污染全局模块搜索路径。
    extra_paths = [str(plugin_path), str(plugin_path.parent)]
    added = [p for p in extra_paths if p not in sys.path]
    sys.path[:0] = added

    # 插件目录下的私有模块是以**裸名**导入的（`import signer`），走的是全局 sys.modules。
    # 两个插件各带一份同名模块时（cloud/*/signer.py 就是这种），先加载的那份会一直霸占这个名字，
    # 后加载的插件 `from signer import X` 拿到的是**别人的实现**——算法不同也不报错，
    # 只会算出看不懂的错误码。故执行本插件前，把「来自别的插件目录」的同名模块从 sys.modules
    # 摘掉，让它 import 自己目录里那份；执行完若本插件没真用上，再把摘掉的那份原样还回去。
    own_dir = plugin_path.resolve()
    category_dir = plugin_path.parent.resolve()
    shadowed: dict[str, Any] = {}
    for py in sorted(plugin_path.glob("*.py")):
        name = py.stem
        if name == "plugin" or name.startswith("_"):
            continue
        prev = sys.modules.get(name)
        if prev is None:
            continue
        origin = getattr(prev, "__file__", None)
        if not origin or not _helper_from_other_plugin(origin, own_dir, category_dir):
            continue
        shadowed[name] = prev
        del sys.modules[name]
        _log.warning(
            "插件 %s 的辅助模块 %s 与已加载的同名模块冲突（后者来自 %s），本次已摘掉它；"
            "请把该辅助模块改成不会被其他插件占用的名字",
            credential_type, name, origin,
        )

    try:
        spec.loader.exec_module(module)
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass
        # 本插件没有真正引入这个名字 → 把原来那份还回去，别殃及已加载的插件
        for name, prev in shadowed.items():
            if name not in sys.modules:
                sys.modules[name] = prev

    if not hasattr(module, "verify"):
        raise AttributeError(f"插件 {credential_type} 缺少必要函数 verify(data)")

    return module


def call_verify(module: Any, data: dict) -> VerifyResult:
    """调用插件的 verify 函数，捕获异常"""
    name = _plugin_name(module)
    try:
        result = module.verify(data)
        if isinstance(result, VerifyResult):
            return result
        # 兼容插件自定义的 VerifyResult dataclass（鸭子类型）
        if hasattr(result, "status"):
            return VerifyResult(
                status=getattr(result, "status", "error"),
                message=getattr(result, "message", ""),
                account_info=getattr(result, "account_info", None),
            )
        # 兼容直接返回 dict 的插件
        if isinstance(result, dict):
            return VerifyResult(**result)
        _log.error("插件 %s 的 verify 返回了非预期类型: %s", name, type(result))
        return VerifyResult(status="error", message=f"插件返回了非预期类型: {type(result)}")
    except Exception as e:
        # 插件内部异常必须留痕：否则只会以 status=error 的形式回到界面，
        # 日志里查不到任何线索（排查线上证书/网络问题时的常见盲区）。
        _log.exception("插件 %s 的 verify 执行异常: %s", name, e)
        return VerifyResult(status="error", message=f"插件执行异常: {_friendly_error(e)}")


def call_validate(module: Any, data: dict) -> list[str]:
    """调用插件的可选 validate 函数，返回错误列表（空=通过）"""
    if not hasattr(module, "validate"):
        return []
    try:
        errors = module.validate(data)
        return errors if isinstance(errors, list) else []
    except Exception as e:
        _log.exception("插件 %s 的 validate 执行异常: %s", _plugin_name(module), e)
        return [f"validate 异常: {e}"]


def call_display_summary(module: Any, data: dict) -> str:
    """调用插件的可选 get_display_summary 函数。

    **预留接口，当前未接线**：生产路径无人调用（列表「附加信息」列走 ``status_detail.account_info``），
    仅作为插件可选契约保留，详见 ``plugin_manager.get_display_summary``。
    """
    if not hasattr(module, "get_display_summary"):
        return ""
    try:
        return str(module.get_display_summary(data))
    except Exception as e:
        # 仅影响列表「附加信息」列的展示，降为 warning 不打断主流程
        _log.warning("插件 %s 的 get_display_summary 执行异常: %s",
                     _plugin_name(module), e)
        return ""


# ── 可选「简易操作」接口 ──────────────────────────────────
# 插件可选实现：
#   actions(data) -> list[dict]      声明可用操作，每项 {"id","name",...}
#   run_action(action_id, params, data) -> dict   执行操作，返回 {"ok", "output", "error"}

def call_actions(module: Any, data: dict) -> list:
    """调用插件的可选 actions(data)；未实现或异常时返回空列表。"""
    if not hasattr(module, "actions"):
        return []
    try:
        result = module.actions(data or {})
        return result if isinstance(result, list) else []
    except Exception as e:
        _log.warning("插件 %s 的 actions 执行异常: %s", _plugin_name(module), e)
        return []


def call_capabilities(module: Any, data: dict) -> dict:
    """调用插件的可选 ``capabilities(data)``，归一为 ``{"available", "note", "error"}``。

    约定：

    - 插件的 ``capabilities`` 返回 ``{"available": [action_id, ...], "note": "缺少 XX 权限"}``；
      ``available`` 列出**当前凭据真正能用的操作 id**，``note`` 是给用户看的一句人话。
    - **未实现 / 抛异常 / 返回非 dict → ``{"available": None, "error": True}``**，
      而 ``available is None`` 表示「未知 → 全部可用」——**fail-open**：
      探测抖一下就把能用的操作全隐藏掉，比不探测还糟。
    - ``available == []`` 才是「确实一个都没有」这个状态，前端要显示 note。
    """
    if not hasattr(module, "capabilities"):
        return {"available": None, "note": "", "error": False}
    try:
        result = module.capabilities(data or {})
    except Exception as e:
        _log.warning("插件 %s 的 capabilities 执行异常: %s", _plugin_name(module), e)
        return {"available": None, "note": "", "error": True}
    if not isinstance(result, dict):
        return {"available": None, "note": "", "error": True}
    raw = result.get("available")
    available = None
    if isinstance(raw, (list, tuple, set)):
        available = [str(item) for item in raw]
    return {
        "available": available,
        "note": str(result.get("note") or ""),
        "error": False,
    }


def call_run_action(module: Any, action_id: str, params: dict, data: dict) -> dict:
    """调用插件的可选 run_action(...)；保证返回 dict 且不抛异常。

    结果里可出现一个特殊键 ``updated_data``（``{字段key: 新值}``）：框架会把这些值**写回
    该凭据**（只接受插件在自己 manifest 里声明过的字段），用于「用 refresh_token 换一组新令牌」
    这类必须原子保存的操作——换令牌会轮换 refresh_token，不写回就等于把用户的登录态搞坏。
    写回值**不会**回传给前端（那是新明文令牌），前端只会收到 ``data_updated: [字段名]``。
    """
    if not hasattr(module, "run_action"):
        return {"ok": False, "error": "该插件不支持操作"}
    try:
        result = module.run_action(action_id, params or {}, data or {})
        return result if isinstance(result, dict) else {"ok": False, "error": "插件返回了非预期结果"}
    except Exception as e:
        _log.exception("插件 %s 的操作 %s 执行异常: %s", _plugin_name(module), action_id, e)
        return {"ok": False, "error": f"操作失败: {_friendly_error(e)}"}


def call_list_models(module: Any, data: dict, force: bool = False) -> list:
    """调用插件的可选 list_models(data)：返回可用模型名列表。

    未实现、取列表失败或异常时返回空列表（前端回退为手动输入）。

    ``force`` 表示**用户手动刷新**：插件接受的话透传下去（插件据此穿透模型清单缓存），
    没写这个参数的老插件就不传——用 `_accepts_keyword` 判，免得一个 TypeError 把好插件
    打成「执行失败」。
    """
    if not hasattr(module, "list_models"):
        return []
    try:
        if force and _accepts_keyword(module.list_models, "force"):
            result = module.list_models(data or {}, force=True)
        else:
            result = module.list_models(data or {})
        return [str(x) for x in result] if isinstance(result, list) else []
    except Exception as e:
        # 取模型列表失败会让前端对话框回退为手动输入，用户看不到原因，必须留痕
        _log.warning("插件 %s 的 list_models 执行异常: %s", _plugin_name(module), e)
        return []


def _accepts_keyword(fn: Any, name: str) -> bool:
    """函数是否接受名为 ``name`` 的关键字参数（显式写了，或带 ``**kwargs``）。

    用它来**向后兼容**地扩展插件契约：老插件只写了 ``stream_action(a, p, d, on_delta)``，
    新参数就不传给它，免得一个 TypeError 把好好的插件打成「执行失败」。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def call_stream_action(module: Any, action_id: str, params: dict, data: dict, on_delta,
                       on_progress: Any = None, should_cancel: Any = None) -> dict:
    """流式执行插件的操作；插件未实现 `stream_action` 时回退到 `run_action`（无分段）。

    两个**可选**回调按关键字传给插件，且只有它把名字写进自己 `stream_action` 的签名里才传：
      - ``on_progress(current, total, text)``：进度上报（``total <= 0`` 表示总量未知）；
      - ``should_cancel() -> bool``：取消查询，返回 True 表示该停了（协作式取消，见 backend/tasks.py）。
    """
    if not hasattr(module, "stream_action"):
        return call_run_action(module, action_id, params, data)
    try:
        optional = {"on_progress": on_progress, "should_cancel": should_cancel}
        kwargs = {name: fn for name, fn in optional.items()
                  if fn is not None and _accepts_keyword(module.stream_action, name)}
        result = module.stream_action(action_id, params or {}, data or {}, on_delta, **kwargs)
        return result if isinstance(result, dict) else {"ok": False, "error": "插件返回了非预期结果"}
    except Exception as e:
        _log.exception("插件 %s 的流式操作 %s 执行异常: %s",
                       _plugin_name(module), action_id, e)
        return {"ok": False, "error": f"操作失败: {_friendly_error(e)}"}
