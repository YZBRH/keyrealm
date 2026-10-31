"""应用启动的公共部分（桌面 / Web / CLI 三种形态共用）。

桌面与 Web 模式需要托盘、调度器、MCP、空闲锁等常驻服务；CLI 模式只做一次性命令，
不应该顺手开端口或起后台线程——因此这里用 ``full`` 明确区分“只初始化”和“常驻服务”。
"""
from __future__ import annotations

from . import api, database as db, idle_lock, logger, plugin_manager
from . import proxy as proxy_config
from . import scheduler, tls_store, verify_options, version
from .paths import app_dir, data_dir, is_frozen


def init_core(mode: str = "desktop", *, full: bool = True, gpu_reason: str = ""):
    """初始化日志 / 数据库 / 代理 / TLS / 插件，并注入回调。

    ``full=False``（CLI）时**不**启动调度器、空闲锁、MCP、自动更新检查——这些都会
    长期驻留或开端口，与“命令行跑完就退出”的语义冲突。
    """
    # CLI 的输出要留给命令结果本身，日志只写文件；桌面 / Web 保持原行为。
    logger.setup_logging(console=False if mode == "cli" else None)
    log = logger.get_logger("main")

    db.init_db()

    persisted = db.get_setting("log_level", "").strip().upper()
    if persisted in logger.LEVELS:
        logger.set_level(persisted)

    proxy_config.apply_proxy(db.get_setting("verify_proxy", ""))
    verify_options.apply_timeout()
    tls_store.apply_global()

    log.info("应用启动（模式=%s，版本=%s，frozen=%s，日志级别=%s，程序目录=%s，数据目录=%s）",
             mode, version.version_string(), is_frozen(), logger.get_level(),
             app_dir(), data_dir())
    from . import paths
    if paths.config_error():
        log.warning("数据目录配置文件读取失败，已回退默认目录：%s", paths.config_error())
    if gpu_reason:
        log.info("图形回退：已退回软件渲染（原因：%s）", gpu_reason)

    plugin_manager.sync_plugins()
    api.init_callbacks()

    if full:
        # 「不锁定」：在常驻服务启动前恢复会话，调度器 / MCP / 自动更新才能看到已解锁状态。
        # CLI 是一次性进程，不读取持久化密钥。
        if mode != "cli":
            api.restore_persisted_session()
        scheduler.start_scheduler()
        idle_lock.start()
        api.autostart_mcp()
        api.start_auto_update_check()
    return log
