# CLI / Web / 数据目录

同一份源码、同一套后端与数据库，提供三种运行形态：

| 形态 | 入口 | 是否开端口 | 说明 |
|---|---|---|---|
| 桌面 | `Keyrealm`（无参数 / `--gui`） | 否（除非开 MCP） | pywebview 窗口 + 系统托盘 |
| Web | `Keyrealm --web` / `Keyrealm-CLI --web` | 是（默认 8766） | 浏览器操作，功能与桌面一致 |
| CLI | `Keyrealm --cli <命令>` / `Keyrealm-CLI <命令>` | 否 | 一次性命令，执行完退出 |

> 源码运行时桌面窗口必须显式 `--gui`；冻结的桌面包双击无参数仍直接开窗口。
> 源码无参数只打印启动方式。

> **发行包布局**：根目录的 `Keyrealm` / `Keyrealm-CLI` 是启动器，应用本体在 `runtime/`。
> 启动器把参数原样转交并透传退出码（脚本里的退出码判断不受影响）；移动或升级时请保持
> `runtime/` 与启动器相邻。

---

## 1. CLI 命令

```bash
Keyrealm-CLI status                 # 版本 / 初始化 / 插件 / 全局代理 / TLS
Keyrealm-CLI init                   # 首次设置主密码
Keyrealm-CLI list                   # 凭据列表
Keyrealm-CLI show 3 --reveal        # 敏感字段默认掩码
Keyrealm-CLI add --type api_fofa --name main -f api_key=xxx
Keyrealm-CLI update 3 -f notes=备用
Keyrealm-CLI delete 3 --yes
Keyrealm-CLI verify --all           # 任一失败退出码 1
Keyrealm-CLI groups / group add|update|rm
Keyrealm-CLI plugins / plugin enable|disable
Keyrealm-CLI proxy list|add|update|rm|use|test
Keyrealm-CLI tls show / tls set 1.2
Keyrealm-CLI export backup.json / import backup.json
Keyrealm-CLI dedupe [--keep ID --remove ID]
Keyrealm-CLI logs [--limit N] [--level L] [--search Q]
Keyrealm-CLI data-dir show|set|clear
Keyrealm-CLI mcp status|start|token|permissions|permission
Keyrealm-CLI self-update check|install|status
```

### 主密码

优先级：`--password` / 环境变量 `KEYREALM_MASTER_PASSWORD` / 交互式 `getpass`。
非交互环境（管道、CI）必须用前两者，否则明确报错。

```bash
export KEYREALM_MASTER_PASSWORD='…'
Keyrealm-CLI --json list
```

### 输出与退出码

- `--json`：机器可读输出；错误仍写 stderr。
- 退出码：`0` 成功，`1` 业务失败（密码错误、验证失败、导入失败等），`2` 参数错误。

### 单条凭据的代理 / TLS

```bash
Keyrealm-CLI update 3 --proxy 本机 --tls 1.3
Keyrealm-CLI update 3 --proxy-mode direct
Keyrealm-CLI update 3 --proxy-mode global --tls inherit
```

写入的是凭据数据的保留键 `_proxy_mode` / `_proxy_id` / `_tls_version`，与桌面 / Web 完全一致。

---

## 2. 数据目录

默认：`<程序入口目录>/data`（数据库、日志、备份、任务、下载临时文件、实例锁之外的运行文件）。

查找顺序：

1. CLI `--data-dir PATH`（一次性，仅 CLI 版或 `--cli` 模式）
2. 入口目录 `keyrealm.conf.json` 的 `data_dir`（持久化）
3. 默认 `<入口目录>/data`

```json
{
  "data_dir": "/path/to/data"
}
```

```bash
Keyrealm-CLI data-dir show
Keyrealm-CLI data-dir set /mnt/vault-data
Keyrealm-CLI data-dir clear
Keyrealm-CLI --data-dir /tmp/alt-data list
```

- 配置里的相对路径按**配置文件所在目录**解析；命令行的相对路径按当前工作目录解析。
- 桌面窗口模式不接受 `--data-dir`，只认配置文件；桌面版的 `--cli` 模式可以。
- `plugins/` 仍固定在入口目录，不随数据目录移动。
- 换目录等于换一个**新的空保险库**；沿用数据请先复制原 `data/` 内容。
- 单实例锁仍在 `<入口目录>/data/.instance.lock`，与数据目录无关。

---

## 3. Web 模式

```bash
Keyrealm-CLI --web
Keyrealm-CLI --web --host 127.0.0.1 --port 8766 --token <固定令牌>
Keyrealm-CLI --web --no-browser
```

- 默认只监听 `127.0.0.1`；启动时打印带 `?token=…` 的地址。
- Token 也可用 `--token` 固定；随机令牌只打印一次。
- 页面通过 `/web-bridge.js` 把 `window.pywebview.api` 映射到 HTTP RPC，SSE 推送同名事件。
- 备份导入走浏览器文件选择，导出走浏览器下载；下载类插件操作先写服务器临时目录，再取回浏览器。
- 系统托盘 / 开机自启 / 轻量模式在 Web 模式隐藏。
- 监听 `0.0.0.0` 时请自行做网络隔离；Token 就是唯一访问凭据。

---

## 4. CLI 启动 MCP

```bash
Keyrealm-CLI mcp start                      # 前台，Ctrl+C 停止
Keyrealm-CLI mcp start --port 8775 --new-token
Keyrealm-CLI mcp status
Keyrealm-CLI mcp token --rotate
Keyrealm-CLI mcp permissions
Keyrealm-CLI mcp permission vault_list_credentials on
```

- 启动前解锁保险库，随后占住单实例锁，避免与桌面端同时运行。
- 启动成功写入 `mcp_enabled=1`；Ctrl+C / SIGTERM 停止时写回 `0`。
- 已有 Token 只显示前缀；新 Token 仅在 `--new-token` / `token --rotate` 时显示一次。
- 普通 CLI 命令不会自动启动 MCP，也不会监听任何端口。

---

## 5. 自更新

```bash
Keyrealm-CLI self-update check [--test]
Keyrealm-CLI self-update install [--test] [--no-restart] [--no-frontend]
Keyrealm-CLI self-update status
```

- 正式版按当前构建选择 `-cli` 资产；测试版从源码构建 `--cli` 包。
- 更新期间占住单实例锁；桌面端需先退出。
- 准备产物、请求文件、日志都落在当前数据目录。
- 交棒进程会显式继承当前 `--data-dir`，一次性目录也能正确更新。
- 更新重启不会重复执行 `self-update install`。
- `--password` / `KEYREALM_MASTER_PASSWORD` 可解锁已保存的更新令牌；也可直接设置 `KEYREALM_UPDATE_TOKEN`。

---

## 6. 测试

```bash
python tests/test_paths_config.py
python tests/test_help_cli.py
python tests/test_cli_integration.py
python tests/test_mcp_cli.py
python tests/test_web_mode.py
python tests/test_mcp_permissions.py
```

这些脚本自建临时目录 / 沙盒数据库 / 随机端口，不接触真实凭据库；MCP / Web 用完会停进程并检查端口释放。
