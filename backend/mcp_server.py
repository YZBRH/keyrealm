"""
MCP Server — SSE 传输，Bearer Token 鉴权
端口：8765（端口被占用时直接启动失败，不自动退让）
"""
from __future__ import annotations

import asyncio
import hmac
import threading
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

# MCP 这一串依赖（mcp / starlette / uvicorn，连带 pydantic…）常驻约 58MB RSS，但本模块
# **只有真正启动服务**时才需要它们，而 MCP 默认关闭、要用户手动开启（见 main.py）。
# 故全部下沉到用到的函数里按需导入，顶层只留标准库与 backend 内部模块。
if TYPE_CHECKING:  # 仅供类型标注；本文件有 `from __future__ import annotations`，运行期不求值
    import uvicorn
    from mcp.server.mcpserver import MCPServer

from . import backup
from . import database as db
from . import plugin_manager
from .crypto import decrypt_dict, hash_token

# 由 api.py 注入
_get_key_cb = None
_event_cb = None

# 本请求是否已通过 Bearer 鉴权，通过 ContextVar 在 ASGI → asyncio.to_thread 链中传播
# （全局只有一个 Token，无需携带 token 记录本身——工具权限统一由「工具权限」全局开关决定）
_authenticated_var: ContextVar[bool] = ContextVar("_mcp_authenticated", default=False)

_server_thread: Optional[threading.Thread] = None
_uvicorn_server: Optional[uvicorn.Server] = None
_mcp_app: Optional[MCPServer] = None
_running = False
_port = 8765
_host = "127.0.0.1"


def set_callbacks(get_key_cb, event_cb):
    global _get_key_cb, _event_cb
    _get_key_cb = get_key_cb
    _event_cb = event_cb


def _get_key() -> Optional[bytes]:
    return _get_key_cb() if _get_key_cb else None


def _push_event(name: str, detail: dict):
    if _event_cb:
        try:
            _event_cb(name, detail)
        except Exception:
            pass


# ── 权限检查 ──────────────────────────────────────────────

def _perm_denied(tool_name: str) -> Optional[dict]:
    """无权时返回**可读的错误载荷**；有权返回 ``None``。

    **刻意不抛异常**：MCP 客户端把工具异常呈现成一句「Error executing tool xxx」，
    **原因被吞掉**，用户只看到那句、不知道该去哪开权限。改成**返回结构化错误**，
    message 才能原样送到用户眼前。

    返回类型与各工具的**声明保持一致**（dict 工具返回 dict、list 工具返回 ``[denied]``），
    免得触发 MCP 的输出校验。
    """
    record = _authenticated_var.get()
    if not record:
        return {"error": "permission_denied", "tool": tool_name, "reason": "no_identity",
                "message": f"无法确认调用身份，未执行「{tool_name}」。"}
    if not db.is_tool_enabled(tool_name):
        return {
            "error": "permission_denied", "tool": tool_name, "reason": "global_disabled",
            "message": (f"工具「{tool_name}」未授权：它在 MCP 里默认就是**关闭**的（高危工具）。"
                        "请到 Keyrealm →「设置 → MCP 服务 → 工具权限」勾选启用后重试。"),
        }
    return None


# ── MCP 工具定义 ──────────────────────────────────────────

def _build_mcp_app() -> MCPServer:
    from mcp.server.mcpserver import MCPServer  # 重依赖下沉，见文件头注释

    mcp = MCPServer(
        "Keyrealm",
        instructions="Keyrealm MCP Server — 管理、验证 LLM API Key 及其他凭据",
    )

    # ── vault_list_credentials ────────────────────────────
    @mcp.tool()
    def vault_list_credentials(
        group_id: Optional[int] = None,
        credential_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        """列出凭据（不含敏感字段明文）"""
        denied = _perm_denied("vault_list_credentials")
        if denied:
            return [denied]
        creds = db.list_credentials(group_id=group_id, credential_type=credential_type,
                                     status=status, search=search)
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "credential_type": c["credential_type"],
                "group_name": c.get("group_name"),
                "status": c["status"],
                "data_plain": c.get("data_plain", {}),
                "last_verified_at": c.get("last_verified_at"),
            }
            for c in creds
        ]

    # ── vault_get_credential ──────────────────────────────
    @mcp.tool()
    def vault_get_credential(credential_id: int) -> dict:
        """获取单条凭据详情（不含敏感字段明文）"""
        denied = _perm_denied("vault_get_credential")
        if denied:
            return denied
        c = db.get_credential(credential_id)
        if not c:
            return {"error": "not_found"}
        return {
            "id": c["id"],
            "name": c["name"],
            "credential_type": c["credential_type"],
            "group_id": c.get("group_id"),
            "group_name": c.get("group_name"),
            "status": c["status"],
            "status_detail": c.get("status_detail", {}),
            "data_plain": c.get("data_plain", {}),
            "last_verified_at": c.get("last_verified_at"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }

    # ── vault_create_credential ───────────────────────────
    @mcp.tool()
    def vault_create_credential(
        name: str,
        credential_type: str,
        data: dict,
        group_id: Optional[int] = None,
        auto_verify: bool = True,
    ) -> dict:
        """创建凭据（与界面共用同一份实现，因此同样会继承类别/插件默认设置）"""
        denied = _perm_denied("vault_create_credential")
        if denied:
            return denied
        # 不要在这里复制一份创建逻辑——否则会出现「表单创建继承默认、MCP 创建不继承」
        # 这类不一致（两份实现改一处漏一处）。
        from . import api as _api
        result = _api.create_credential(name, credential_type, data, group_id, auto_verify)
        if isinstance(result, dict) and result.get("error"):
            return result
        return {"ok": True, "credential": result}

    # ── vault_update_credential ───────────────────────────
    @mcp.tool()
    def vault_update_credential(
        credential_id: int,
        name: Optional[str] = None,
        group_id: Optional[int] = None,
        data: Optional[dict] = None,
    ) -> dict:
        """修改凭据"""
        denied = _perm_denied("vault_update_credential")
        if denied:
            return denied
        from .crypto import encrypt_dict

        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}

        kwargs: dict[str, Any] = {}
        if name is not None:
            kwargs["name"] = name
        if group_id is not None:
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

            # 防御：敏感字段缺失或为空时保留已存明文，避免部分更新覆盖密钥
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
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "credential": updated}

    # ── vault_delete_credential ───────────────────────────
    @mcp.tool()
    def vault_delete_credential(credential_id: int) -> dict:
        """删除凭据"""
        denied = _perm_denied("vault_delete_credential")
        if denied:
            return denied
        db.delete_credential(credential_id)
        _push_event("vault:credentials_changed", {})
        return {"ok": True}

    # ── vault_verify_credential ───────────────────────────
    @mcp.tool()
    def vault_verify_credential(credential_id: int) -> dict:
        """触发验证单条凭据"""
        denied = _perm_denied("vault_verify_credential")
        if denied:
            return denied
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        from .scheduler import verify_single_credential
        return verify_single_credential(credential_id, trigger_type="mcp")

    # ── vault_batch_verify ────────────────────────────────
    @mcp.tool()
    def vault_batch_verify(
        scope: str = "all",
        group_id: Optional[int] = None,
        credential_type: Optional[str] = None,
        credential_ids: Optional[list[int]] = None,
    ) -> dict:
        """批量验证（异步执行，进度通过事件推送）

        三种选法，**``credential_ids`` 优先**：

        - ``credential_ids``：只验证这些 id（要精确点名一批凭据时用这个）；
        - ``scope`` + ``group_id`` / ``credential_type``：按分组或按插件类型圈一批。
          ``scope`` 取值 ``all`` / ``group`` / ``type``，配 ``group_id`` 或 ``credential_type``。

        注意 ``credential_ids`` 传了**空列表**等同于没传（会退化成按 scope 圈），
        所以「一条也不验」要在调用方拦住。返回的 ``total`` 是本次实际会验证的条数。
        """
        denied = _perm_denied("vault_batch_verify")
        if denied:
            return denied
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        from .scheduler import batch_verify
        total = batch_verify(scope=scope, group_id=group_id, credential_type=credential_type,
                             credential_ids=credential_ids, trigger_type="mcp")
        return {"ok": True, "total": total, "message": f"批量验证已启动（{total} 条）"}

    # ── vault_list_groups ─────────────────────────────────
    @mcp.tool()
    def vault_list_groups() -> list[dict]:
        """列出分组"""
        denied = _perm_denied("vault_list_groups")
        if denied:
            return [denied]
        return db.list_groups()

    # ── vault_create_group ────────────────────────────────
    @mcp.tool()
    def vault_create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
        """创建分组"""
        denied = _perm_denied("vault_create_group")
        if denied:
            return denied
        group = db.create_group(name, description, color)
        _push_event("vault:credentials_changed", {})
        return {"ok": True, "group": group}

    # ── vault_delete_group ────────────────────────────────
    @mcp.tool()
    def vault_delete_group(group_id: int) -> dict:
        """删除凭据分组

        **组里的凭据不会被删掉**，只是解除归属（回到「未分组」）。
        """
        denied = _perm_denied("vault_delete_group")
        if denied:
            return denied
        db.delete_group(group_id)
        _push_event("vault:credentials_changed", {})
        return {"ok": True}

    # ── vault_list_plugins ────────────────────────────────
    @mcp.tool()
    def vault_list_plugins() -> list[dict]:
        """列出全部插件（含 enabled 状态，被禁用的也在内）"""
        denied = _perm_denied("vault_list_plugins")
        if denied:
            return [denied]
        return plugin_manager.list_installed_plugins()

    # ── vault_get_secret ──────────────────────────────────
    @mcp.tool()
    def vault_get_secret(credential_id: int, field: Optional[str] = None) -> dict:
        """获取解密后的敏感字段（高危操作）"""
        denied = _perm_denied("vault_get_secret")
        if denied:
            return denied
        key = _get_key()
        if key is None:
            return {"error": "vault_locked"}
        cred = db.get_credential(credential_id)
        if not cred:
            return {"error": "not_found"}
        try:
            data = decrypt_dict(key, cred["data_encrypted"])
        except Exception as e:
            return {"error": "decrypt_failed", "message": str(e)}
        if field:
            return {"field": field, "value": data.get(field)}
        return {"data": data}

    # ── vault_export_backup ───────────────────────────────
    @mcp.tool()
    def vault_export_backup(
        scope: str = "all",
        encrypted: bool = True,
        credential_ids: Optional[list] = None,
        path: Optional[str] = None,
    ) -> dict:
        """导出备份（触及整库数据，高危）。

        - `scope`：`all` = 凭据 + 分组 + 偏好设置；`credentials` = 仅凭据
        - `encrypted`：True 时敏感字段保持密文（只能用同一主密码导入）；
          **False 会把密钥明文写出**，仅在明确需要跨主密码迁移时使用
        - `credential_ids`：只导出这些凭据 id（省略 = 全部）
        - `path`：写入该文件；**省略则把备份文档放在返回值的 `document` 字段里**
          （便于直接交给 `vault_import_backup` 或由 Agent 自行保存）
        """
        denied = _perm_denied("vault_export_backup")
        if denied:
            return denied
        from . import api as _api

        built = _api.build_backup_doc(scope, bool(encrypted), credential_ids)
        if built.get("error") or built.get("ok") is False:
            return built
        doc = built["document"]

        result: dict = {
            "ok": True,
            "count": doc["counts"]["credentials"],
            "scope": scope,
            "encrypted": bool(encrypted),
        }
        if path:
            try:
                backup.write_backup_file(path, doc)
            except Exception as e:
                return {"ok": False, "message": f"写入失败: {e}"}
            result["path"] = path
        else:
            result["document"] = doc
        return result

    # ── vault_import_backup ───────────────────────────────
    @mcp.tool()
    def vault_import_backup(
        document: Optional[dict] = None,
        path: Optional[str] = None,
        dedupe: bool = True,
    ) -> dict:
        """导入备份（合并入库，高危：会新增凭据、可能覆盖偏好设置）。

        `document`（备份 JSON 对象）与 `path`（备份文件路径）**二选一**。
        `dedupe=True` 时「同类型 + 敏感值相同」的凭据会跳过。

        导入是**填充式**的：以当前插件的字段表为蓝本。插件未安装 / 已禁用、备份里含当前插件
        不认识的字段、必填字段在备份里没有着落（且插件无默认值）的条目，都会**整条判失败**
        且不入库 —— 返回值里的 `failed`/`failed_count` 逐条给出原因。

        每次导入都会在 `data/logs/` 生成完整导入日志（敏感值已脱敏）；有失败时另生成
        `*_failed.log`（**含明文**，供人工比对）。路径见返回值的 `log_file`/`failed_log_file`。
        加密备份需与当前主密码一致，否则报错。
        """
        denied = _perm_denied("vault_import_backup")
        if denied:
            return denied
        from . import api as _api

        if document is None and not path:
            return {"ok": False, "message": "需要提供 document 或 path"}
        doc = document
        if doc is None:
            try:
                doc = backup.read_backup_file(path)
            except Exception as e:
                return {"ok": False, "message": f"读取失败: {e}"}
        if not isinstance(doc, dict):
            return {"ok": False, "message": "备份内容必须是 JSON 对象"}

        result = _api.apply_backup_doc(doc, dedupe=bool(dedupe), source=path or "document")
        if result.get("ok"):
            result["source"] = path or "document"
        return result

    # ── 验证代理管理 ──────────────────────────────────────
    # 一律复用 backend/api.py 的同名实现（而不是直接捅 proxy_store）：
    # api 那层负责写日志、改完重设全局代理的环境变量，并**回吐最新全量列表**——
    # 调用方拿到返回值就知道改完是什么状态，不必再 list 一次。
    # 代理 id 是 **UUID 字符串**（见 database.py 的 proxies 表注释），别当自增整数用。
    @mcp.tool()
    def vault_list_proxies() -> dict:
        """列出全部验证代理 + 当前全局默认代理"""
        denied = _perm_denied("vault_list_proxies")
        if denied:
            return denied
        from . import api as _api
        return _api.list_proxies()

    @mcp.tool()
    def vault_create_proxy(name: str, url: str) -> dict:
        """新增一个验证代理（地址需带 scheme，如 socks5://127.0.0.1:1080）"""
        denied = _perm_denied("vault_create_proxy")
        if denied:
            return denied
        from . import api as _api
        return _api.create_proxy(name, url)

    @mcp.tool()
    def vault_update_proxy(proxy_id: str, name: Optional[str] = None,
                           url: Optional[str] = None) -> dict:
        """修改代理的名称/地址（只传要改的那个）"""
        denied = _perm_denied("vault_update_proxy")
        if denied:
            return denied
        from . import api as _api
        return _api.update_proxy(proxy_id, name, url)

    @mcp.tool()
    def vault_delete_proxy(proxy_id: str) -> dict:
        """删除代理；若它正被设为全局默认，会自动回落到直连"""
        denied = _perm_denied("vault_delete_proxy")
        if denied:
            return denied
        from . import api as _api
        return _api.delete_proxy(proxy_id)

    @mcp.tool()
    def vault_set_global_proxy(proxy_id: Optional[str] = None) -> dict:
        """设置全局默认代理；不传（或传空）= 直连。

        只影响「跟随全局」的凭据；单条凭据自己指定的代理不受影响。
        """
        denied = _perm_denied("vault_set_global_proxy")
        if denied:
            return denied
        from . import api as _api
        return _api.set_global_proxy(proxy_id)

    @mcp.tool()
    def vault_test_proxy(proxy_id: Optional[str] = None) -> dict:
        """实测某个代理能否连通（会真的发一次外网请求）；不传 = 测当前全局生效的那个"""
        denied = _perm_denied("vault_test_proxy")
        if denied:
            return denied
        from . import api as _api
        return _api.test_proxy_id(proxy_id)

    return mcp


# ── SSE 服务启动 ──────────────────────────────────────────

def _make_asgi_app(mcp: MCPServer):
    """将 MCPServer 的 SSE app 包装上 Bearer 鉴权中间件"""
    from starlette.applications import Starlette
    from starlette.datastructures import Headers
    from starlette.middleware import Middleware as MW
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    # 注意：**不能用 BaseHTTPMiddleware**。它会把响应体重新包一层异步迭代器，
    # 而 SSE 的长连接会触发 Starlette 的断言
    # `Unexpected message: http.response.start`，连接直接 500。
    # 这里用纯 ASGI 中间件：只在进入应用前鉴权，不碰 response body。
    class BearerAuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                return await self.app(scope, receive, send)
            headers = Headers(scope=scope)
            auth = headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                response = JSONResponse(
                    {"error": "missing_token", "message": "需要 Bearer Token"},
                    status_code=401,
                )
                return await response(scope, receive, send)

            token = auth[7:].strip()
            record = db.get_mcp_token_record()
            # 常量时间比较，别按字符逐位泄漏比对进度（Token 就是这里的唯一凭据）
            if not record or not hmac.compare_digest(hash_token(token), record["token_hash"]):
                response = JSONResponse(
                    {"error": "invalid_token", "message": "Token 无效"},
                    status_code=401,
                )
                return await response(scope, receive, send)

            _authenticated_var.set(True)
            return await self.app(scope, receive, send)

    sse_app = mcp.sse_app()
    # 在 sse_app 外包一层中间件
    wrapped = Starlette(
        routes=[Mount("/", app=sse_app)],
        middleware=[MW(BearerAuthMiddleware)],
    )
    return wrapped


def _port_in_use(host: str, port: int) -> bool:
    """目标端口是否已有服务在监听（能连上就算被占）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _url() -> str:
    return f"http://{_host}:{_port}/sse"


def start_server(port: int = 8765, host: str = "127.0.0.1") -> tuple[bool, int, str]:
    """在 ``host:port`` 启动 MCP SSE 服务（后台线程）。

    **端口被占用时直接失败**，不再自动退到 +1/+2 —— 端口是用户显式配的，
    悄悄换一个会让人对着一个「配了 9000 却在 9001」的界面发懵。
    """
    global _server_thread, _uvicorn_server, _mcp_app, _port, _host

    import uvicorn  # 重依赖下沉，见文件头注释

    if _running:
        return True, _port, _url()

    if _port_in_use(host, port):
        return False, port, ""

    _mcp_app = _build_mcp_app()
    app = _make_asgi_app(_mcp_app)
    _uvicorn_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    _port = port
    _host = host

    def _run():
        global _running
        _running = True
        asyncio.run(_uvicorn_server.serve())
        _running = False

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()

    # 等待启动
    for _ in range(20):
        time.sleep(0.1)
        if _running:
            break

    return _running, _port, _url()


def stop_server():
    global _running
    if _uvicorn_server:
        _uvicorn_server.should_exit = True
    _running = False


def is_running() -> bool:
    return _running


def get_port() -> int:
    return _port


def get_host() -> str:
    return _host


def get_url() -> str:
    """MCP 服务地址（与界面、客户端配置里展示的保持一致）。"""
    return _url()
