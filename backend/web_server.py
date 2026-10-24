"""Web 模式（``--web``）的服务端。

同一套后端 API 与 `frontend/dist` 通过 HTTP 提供：
- `GET  /`                 带 token 的前端入口（注入 web-bridge.js）
- `POST /api/call`         `{method, args}` → 调用 VaultApi 的同名方法
- `GET  /api/events`       SSE：把后端 `push_event` 原样转成浏览器事件
- `POST /api/export_backup` 构建备份文档并交给浏览器下载（不弹本机文件框）
- `POST /api/import_backup` 接收浏览器上传的备份文档并导入

安全：默认只监听 127.0.0.1，并要求启动时生成的随机 token（Authorization: Bearer）。
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import api, logger
from . import database as db

_log = logger.get_logger(__name__)

# 这些接口依赖本机窗口 / 原生文件对话框，Web 模式下由前端桥接层单独处理或拒绝。
_BLOCKED_METHODS = {
    "pick_save_path", "open_log_file", "open_external_url",
    "minimize_to_tray", "confirm_close_action", "set_close_action",
    "set_autostart", "set_light_mode",
}


def _safe_equal(a: str, b: str) -> bool:
    try:
        return secrets.compare_digest(str(a), str(b))
    except Exception:  # noqa: BLE001
        return False


class EventBus:
    """把任意线程里的 ``push_event`` 广播给所有 SSE 订阅者。"""

    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def publish(self, event_name: str, detail: Any) -> None:
        payload = {"event": event_name, "detail": detail}
        for loop, queue in list(self._subs):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:  # noqa: BLE001 - 订阅者已断开
                pass

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue, tuple]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sub = (loop, queue)
        self._subs.append(sub)
        return loop, queue, sub

    def unsubscribe(self, sub: tuple) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass


def create_app(vault_api, token: str, dist_dir: Path) -> Starlette:
    bus = EventBus()
    api.add_event_sink(bus.publish)
    index_file = dist_dir / "index.html"

    def authorized(request) -> bool:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return _safe_equal(auth[7:].strip(), token)
        return _safe_equal(request.query_params.get("token", ""), token)

    def json_response(payload: Any, status_code: int = 200) -> Response:
        # 与 pywebview 的 JS-API 序列化口径一致：未知类型一律 str()
        return Response(
            json.dumps(payload, ensure_ascii=False, default=str),
            status_code=status_code,
            media_type="application/json",
        )

    async def index(request):
        if not authorized(request):
            return HTMLResponse(
                "<h3>Keyrealm Web</h3><p>缺少访问令牌。请使用启动时打印的带 "
                "<code>?token=…</code> 的地址打开。</p>",
                status_code=401,
            )
        try:
            html = index_file.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("前端构建产物不存在：请先运行 npm run build", status_code=500)
        inject = (
            "<script>window.__KEYREALM_WEB__ = {token: "
            + json.dumps(token)
            + "};</script><script src=\"/web-bridge.js\"></script>\n"
        )
        html = html.replace('<script type="module"', inject + '<script type="module"', 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def events(request):
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        loop, queue, sub = bus.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                bus.unsubscribe(sub)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def call(request):
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        method = str(body.get("method") or "")
        args = body.get("args") or []
        if not method or method.startswith("_") or method in _BLOCKED_METHODS:
            return JSONResponse({"ok": False, "message": f"接口不可用：{method}"}, status_code=400)
        fn = getattr(vault_api, method, None)
        if not callable(fn):
            return JSONResponse({"ok": False, "message": f"后端未实现接口：{method}"}, status_code=404)
        if not isinstance(args, list):
            return JSONResponse({"ok": False, "message": "args 必须是数组"}, status_code=400)
        try:
            result = await run_in_threadpool(fn, *args)
        except Exception as exc:  # noqa: BLE001 - 与 pywebview 一致，把异常转成调用失败
            _log.exception("Web API 调用失败：%s", method)
            return JSONResponse({"ok": False, "message": f"{type(exc).__name__}: {exc}"},
                                status_code=500)
        return json_response(result)

    async def export_backup(request):
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        scope = str(body.get("scope") or "all")
        encrypted = bool(body.get("encrypted", True))
        credential_ids = body.get("credential_ids")
        built = await run_in_threadpool(
            api.build_backup_doc, scope, encrypted, credential_ids,
        )
        if not built.get("ok"):
            return json_response(built)
        doc = built["document"]
        count = int((doc.get("counts") or {}).get("credentials") or 0)
        filename = f"keyrealm-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
        return json_response({
            "ok": True, "document": doc, "filename": filename,
            "count": count, "encrypted": encrypted, "scope": scope,
        })

    async def import_backup(request):
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        doc = body.get("document")
        if not isinstance(doc, dict):
            return JSONResponse({"ok": False, "message": "备份文档格式不正确"}, status_code=400)
        source = str(body.get("source") or "web-upload.json")
        password = body.get("orig_password") or None
        result = await run_in_threadpool(
            api.apply_backup_doc, doc, True, source, password,
        )
        if isinstance(result, dict) and result.get("need_password"):
            result["path"] = source
        return json_response(result)

    async def log_file(request):
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        name = str(request.query_params.get("name") or "").strip()
        if not name or Path(name).name != name or name in (".", ".."):
            return Response("非法的日志文件名", status_code=400)
        target = (logger.LOG_DIR / name).resolve()
        try:
            target.relative_to(logger.LOG_DIR.resolve())
        except ValueError:
            return Response("not found", status_code=404)
        if not target.is_file():
            return Response("not found", status_code=404)
        return FileResponse(target, filename=name)

    async def download(request):
        """取回 Web 模式下插件写到服务器临时目录的文件（下载/打包类操作）。"""
        if not authorized(request):
            return JSONResponse({"ok": False, "message": "unauthorized"}, status_code=401)
        raw = str(request.query_params.get("path") or "").strip()
        if not raw:
            return Response("缺少 path", status_code=400)
        root = (db.DATA_DIR / "web_downloads").resolve()
        target = Path(raw).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return Response("not found", status_code=404)
        if not target.is_file():
            return Response("not found", status_code=404)
        # 显示名只取 basename，避免响应头里出现路径
        name = Path(str(request.query_params.get("name") or target.name)).name or target.name
        return FileResponse(target, filename=name)

    async def static_file(request):
        rel = request.path_params.get("path") or ""
        # 防止路径穿越：Resolve 后必须仍在 dist 内
        target = (dist_dir / rel).resolve()
        try:
            target.relative_to(dist_dir.resolve())
        except ValueError:
            return Response("not found", status_code=404)
        if target.is_file():
            headers = {}
            if target.suffix == ".html" or target.name == "web-bridge.js":
                # 桥接层随程序升级而变，必须每次校验，避免浏览器用旧脚本打新后端
                headers["Cache-Control"] = "no-store"
            elif "/assets/" in str(target):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return FileResponse(target, headers=headers)
        # SPA 回退：非文件路径回到 index（路由由前端 hash 处理，通常用不到）
        if not rel or "." not in Path(rel).name:
            return await index(request)
        return Response("not found", status_code=404)

    routes = [
        Route("/", index),
        Route("/index.html", index),
        Route("/api/events", events),
        Route("/api/call", call, methods=["POST"]),
        Route("/api/export_backup", export_backup, methods=["POST"]),
        Route("/api/import_backup", import_backup, methods=["POST"]),
        Route("/api/log_file", log_file),
        Route("/api/download", download),
        Route("/{path:path}", static_file),
    ]
    return Starlette(routes=routes)


def serve(vault_api, host: str, port: int, token: str, dist_dir: Path) -> None:
    """阻塞运行 uvicorn（Ctrl+C 退出）。"""
    app = create_app(vault_api, token, dist_dir)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    uvicorn.Server(config).run()
