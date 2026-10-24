# 架构设计文档

## 1. 系统总览

```
┌──────────────────────────────────────────────────────────────┐
│                         系统托盘                              │
│  [图标] 右键菜单：显示窗口 / MCP状态 / 退出                   │
│  关闭窗口 → 最小化到托盘（后台持续运行）                      │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                        桌面应用窗口                           │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               Vue 3 前端（WebView）                     │  │
│  │  凭据管理 │ 分组 │ MCP │ 设置                          │  │
│  └───────────────────────┬────────────────────────────────┘  │
│                          │ window.pywebview.api.*             │
│  ┌───────────────────────▼────────────────────────────────┐  │
│  │               Python 后端（pywebview JS-API）           │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────────────────────┐  │  │
│  │  │ Crypto  │ │ Database │ │    Plugin Manager       │  │  │
│  │  │ AES-GCM │ │  SQLite  │ │  目录扫描，放入即加载   │  │  │
│  │  └─────────┘ └──────────┘ └─────────────────────────┘  │  │
│  │  ┌─────────────────┐  ┌────────────────────────────┐   │  │
│  │  │    Scheduler    │  │       MCP Server           │   │  │
│  │  │  APScheduler    │  │  FastMCP (SSE:8765)        │   │  │
│  │  │                 │  │  + Bearer Token 鉴权       │   │  │
│  │  └─────────────────┘  └────────────────────────────┘   │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │              System Tray（pystray）               │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────────┐          ┌─────────────────┐
  │  data_dir/      │          │  外部 API 服务   │
  │  credentials.db │          │ OpenAI/Anthropic │
  │  (默认 ./data)  │          │ 方舟/智谱/文心   │
  └─────────────────┘          └─────────────────┘
```

### 1.0 运行形态与数据目录

同一份 `main.py` / `backend` / 前端产物支持三种形态，**不是三个分支**：

| 形态 | 启动方式 | 窗口 | 端口 |
|---|---|---|---|
| 桌面 | 冻结包无参数 / `--gui` | pywebview + 托盘 | 仅 MCP（若开启） |
| Web | `--web` | 浏览器 | HTTP（默认 8766） |
| CLI | `--cli <命令>` / CLI 包直接跟命令 | 无 | 仅 `mcp start` 时 |

- 桌面专用依赖（`webview` / `pystray` / `PIL`）**延迟导入**：CLI 包不含它们，Web/CLI 也能在无
  `DISPLAY` 的服务器上启动。
- 三种形态共用 `backend/bootstrap.py::init_core()`；CLI 传 `full=False`，不启动调度器、空闲锁、
  MCP 与自动更新检查。
- **单实例锁按程序入口目录**（`<入口>/data/.instance.lock`），与数据目录无关；CLI 一次性命令
  不抢锁，`mcp start` / 自更新会抢锁。

**数据目录**由 `backend/paths.py` 在**导入其它 backend 模块之前**解析（database/logger/tasks
在模块级把路径固化成常量）。优先级：

1. CLI `--data-dir PATH`（一次性；仅 CLI 版或 `--cli` 模式）
2. 入口目录 `keyrealm.conf.json` 的 `data_dir`（持久化）
3. 默认 `<入口目录>/data`

配置里的相对路径按配置文件所在目录解析；命令行的相对路径按 CWD 解析。`app_dir()` 仍是程序
入口目录（插件、配置文件），`data_dir()` 才是数据库/日志/任务/备份/下载临时目录。单实例锁
与 `plugins/` 不随数据目录移动。

### 1.1 名字与「故意不改」的标识

应用叫 **Keyrealm**（中文名「凭域」）。1.2.0 从 `Credential Vault` 改名而来，
**只有用户与系统看得见的名字变了**，凡是会牵动数据兼容的标识**一律保持原样**——改它们
换不来任何用户可见的好处，却要背上迁移风险：

| 保持原样（连同理由） | |
|---|---|
| 备份格式串 `"credential-vault-backup"` | 改了它，**用户手上所有旧备份都导不进来** |
| `vault_*` MCP 工具名、`vault:*` 事件名、`VAULT_*` 环境变量 | 是「vault（保险库）」这个通用词，不是产品名；改了要同步所有 Agent 侧配置与文档 |
| `data/`、`credentials.db`、`master.key` | 改了老用户的库会在原地「消失」 |
| `_vault_plugin_*` / `_vault_*_signer` 模块名前缀 | 纯内部实现细节 |
| 插件 `credential_type`（`llm_openai` 等） | 库里的关联键，改名要跑迁移脚本 |

**跟着改了的**：显示名与窗口/托盘标题、exe 与包名（`Keyrealm.exe` / `Keyrealm.spec`）、
单实例互斥名、MCP 客户端配置里的服务 key（`keyrealm`）、日志文件名（`data/logs/keyrealm.log`）、
备份默认文件名，以及 MCP 令牌前缀 **`cv_` → `kr_`**（老令牌**仍有效**——鉴权按 SHA-256 哈希比对，
前缀只用于展示；且全局只有一个活跃令牌，轮换一次就统一了）。

> **改名会留下系统级的孤儿，必须迁移**：开机自启动的条目是按名字写在系统里的
> （Windows 注册表 `Run` 的值名、Linux `.desktop` 文件名 + 归属标记、macOS LaunchAgent 的
> `Label`）。旧条目新名字**读不到**，症状是「开关显示已关闭，系统里却仍有旧条目 → 开机弹
> 一次『找不到文件』，用户再打开开关就变成两条」。因此启动时跑一次
> `autostart.migrate_legacy()`（幂等，见 `backend/autostart.py`）：**旧条目存在 == 用户当初
> 就是开着的**，删旧建新把「开着」的意图保留下来；只认三种「确实是我们写的」证据
> （值名 / 归属标记 / Label），对不上就一个字节都不动。

---

## 2. 核心模块

**启动与单实例**（`backend/single_instance.py`）：`main()` 的**第一件事**就是取单实例锁
——Windows 用命名互斥体 `CreateMutexW`（内核在进程退出时自动释放，**不像 pid 文件那样会在
崩溃/被强杀后留下陈旧锁**），其它平台用**程序入口目录**的 `data/.instance.lock` 上的 `fcntl.flock`
（与可配置的数据目录无关）。
取不到锁说明已有实例在跑：第二套进程**不创建界面**，而是 `FindWindowW("Keyrealm")`
+ `ShowWindow` + `SetForegroundWindow` 把已有窗口拉到前台，然后自己退出——用户感知上就是
「双击 exe 就出来了」。拿不到句柄/目录不可写等意外一律**放行**（宁可多开，也不能让应用起不来）。
互斥检查放在 `logger.setup_logging()` 之前，因为重复启动不该产生任何副作用；而
`--print-paths` 这个调试分支在 `main()` 之外，不受影响。

### 2.1 加密模块（crypto.py）

**职责**：主密码管理、AES 密钥派生、字段级加密/解密。

**密钥派生流程**：
```
用户主密码
    │
    ▼ PBKDF2-HMAC-SHA256（100,000 次迭代，随机 16 字节 salt）
    │
    ▼
32 字节 AES 密钥（仅存于内存，不落盘）
```

**加密方案**：AES-256-GCM（认证加密，防篡改）
- 每个字段独立加密，nonce 随机生成（12 字节）
- 存储格式：`base64(nonce + ciphertext + tag)`
- salt 存储在 `settings` 表，首次初始化时生成

**主密码验证**：
- 存储 `verify_token = AES-GCM(key, b"VAULT_OK")`
- 启动时用主密码派生密钥，尝试解密 verify_token
- 解密成功 → 密码正确；失败 → 密码错误

**主密码重置**：
- 用旧密码解密所有字段 → 用新密码重新加密 → 更新 verify_token 和 salt

---

### 2.2 数据库模块（database.py）

**职责**：SQLite 连接管理、CRUD 操作、数据迁移。

**数据库位置**：`<data_dir>/credentials.db`。默认 `data_dir` 是程序入口目录下的 `data/`，
可通过入口目录 `keyrealm.conf.json` 或 CLI `--data-dir` 改到别处（解析顺序见 §1.0）。

```
<程序入口>/
├── keyrealm.conf.json     # 可选：{"data_dir": "…"}
└── data/                  # 默认数据目录（已在 .gitignore）
    ├── credentials.db
    ├── credentials.db-wal
    └── credentials.db-shm
```

> 应用启动时自动创建当前数据目录。数据库路径以 `paths.data_dir()` 为准，与 CWD 无关。

**连接策略**：单连接 + 线程锁（SQLite WAL 模式），避免并发写冲突。

---

### 2.3 插件管理器（plugin_manager.py）

**职责**：扫描 `plugins/` 目录、注册插件、按需加载插件模块。**放入即加载，删除目录即卸载**。

**插件禁用 / 启用（插件管理）**：插件只来自 `plugins/` 一处（不再有「可添加」来源）。启用/禁用**不改动磁盘目录**，其状态与影响如下：

- **状态来源**：`plugins/.state.json`（`{"disabled": ["<credential_type>", …]}`；文件**不存在或损坏都当全部启用**）。写入方式是「先写 `.tmp` 再原子替换」。
- **DB 列降级为镜像**：`sync_plugins()` 每次扫描把该状态文件**镜像**进 DB 的 `plugins.enabled` 列——该列**只是只读镜像、不再是功能闸门**，`get_plugin_by_type()` **不按 `enabled` 过滤**。
- **真正的闸门是 `get_module()`**：插件被禁用即返回 `None`，其操作 / 模型 / 能力一律为空，`run` 与 `verify_credential` 直接报「插件已禁用」；但**凭据本身仍照常显示与存取**（列表状态显示为**派生的「插件禁用」**——由插件启用状态实时算出、不写库，重新启用即回弹）。
- **编辑表单仍带出已存值**：`fields_schema` 取自 `pluginByType`（**含已禁用插件**）。
- **卸载**：界面不提供「删除插件」入口——删掉插件目录即可（**删目录 = 卸载，凭据保留**）；插件记录与状态文件里残留的 key 由下次 `sync_plugins()` 扫描清理。

**插件目录**：`<app>/plugins/` 是唯一事实来源，采用两层结构：

```
plugins/
├── llm/                      # 类别目录
│   ├── category.json         # 类别显示名 / 图标
│   ├── llm_common.py         # 类别共享模块：仅本类别特有辅助（额度/欠费探测）
│   ├── llm_protocols.py      # 协议级工具（探针 / 简单对话），供子插件按需复用
│   ├── openai/               # 服务子插件（独立，可单独删除）
│   │   ├── manifest.json
│   │   └── plugin.py
│   ├── deepseek/
│   └── …                     # 共 18 个 LLM 子插件
├── api/                      # API 服务类别：firecrawl / tavily / metaso / fofa / coze / dify / google_api_credentials / baidu_face（共 8 个）
├── dns/                      # 域名解析：dnspod / duckdns / godaddy
├── dev/                      # 开发者平台：github / huggingface / wandb / npm
├── im/                       # 即时通讯：feishu / dingtalk / wecom / telegram / telegram_apihash / discord / qq_bot / wechat_ilink / wechat_mp / wechat_miniprogram（dingtalk、wecom 为「应用 / Webhook 双形态」）
├── cloud/                    # 云服务商：aliyun / tencentcloud / aws / gcp / huaweicloud / firebase / jdcloud / ucloud / baiducloud / volcengine / ctyun / cloudflare（另含 google_sa.py 协议级共享模块）
├── map/                      # 地图：amap（高德）/ baidu（百度）/ tencent（腾讯位置服务）
├── exchange/                 # 交易所：okx / binance
├── storage/                  # 对象存储：s3（任意 S3 兼容 / MinIO）/ anon（匿名桶探测）（另含 storage_s3v4.py、storage_common.py 类别共享模块）
└── mail/                     # 邮箱 / 短信：smtp（通用 SMTP）/ gateway（短信网关 / 接码服务商）
```

> `mail/gateway` 是短信网关 / 接码服务商（一个插件内按 manifest 的 `vendor` 分派校验），它调用的就是普通
> HTTPS API，与 api/ 其余服务同形，故并入 api/ 而不单开类别——新建类别**不再**需要连带克隆一份
> `*_common.py`（通用工具已上收 `backend/plugin_sdk.py`），但单开一类的归类代价仍大于收益。

> 十个类别共 66 个子插件：llm 20 / api 9 / im 10 / cloud 11 / storage 2 / dns 4 / dev 4 / map 3 / exchange 2 / mail 1。
> 类别目录**不再强制**带共享模块——通用原语已上收 `backend/plugin_sdk.py`（框架暴露给插件的唯一接口层）；
> 只有类别确有「类别特有」工具时才建一个（现仅 `llm/llm_common.py`、`mail/mail_common.py`、
> `storage/storage_common.py + storage/storage_s3v4.py` 三例；**类别级共享模块必须带类别前缀**，
> 否则同名模块会在 `sys.modules` 里互相顶掉且不报错）。
> **协议级模块**另立：`cloud/google_sa.py`（把服务账号的邮箱 + 私钥签成 RS256 JWT 换 token，
> gcp / firebase 共用）与 `llm/llm_protocols.py`。这些都不是子插件。

每个类别目录至少含 `category.json` + 各服务子目录（`manifest.json` + `plugin.py`），共享模块可选。

- **一个服务 = 一个独立子插件**：各自持有 `manifest.json`（字段 schema、credential_type）与薄 `plugin.py`；删除任一子插件目录不影响其他子插件运行。根目录下直接含 `manifest.json` 的目录视为无类别的平铺插件。
- **类别共享模块（可选）**：只在类别**确实有类别特有工具**时才建。通用工具与 `VerifyResult` 已上收
  `backend/plugin_sdk.py`，不再逐类别克隆；现有两例均为类别特有——`llm/llm_common.py` 的额度/欠费
  探测辅助、`mail/mail_common.py` 的 SOCKS 解析与 `TunneledSMTP*` / `TunneledIMAP4*`。
  **协议级工具**（如 `plugins/llm/llm_protocols.py` 的探针与 `chat_completion`、
  `cloud/google_sa.py` 的服务账号签名）另建独立模块供子插件按需引用。加载子插件时临时将其目录
  与类别目录注入 `sys.path`（见 plugin_base.load_plugin_module）。
- **插件 logo 的深色可见度判定（`backend/icon_ink.py`）**：`list_plugins()` 下发的每条记录除
  `icon_image`（data URL）外，还有 `icon_lift`（bool）——该 logo 在深色主题下**要不要垫一层浅色底**。
  判据是「可见度」而非亮度：位图取不透明像素 `max(r,g,b)` 的 p90、**SVG 取 `fill`/`stroke` 声明的颜色**
  （属性式 `fill="#fff"` 与 CSS 式 `<style>{fill:#1c99ff}` / `style="fill:#fff"` 都解析——官方导出的 SVG
  常把颜色放在后者；一个颜色都取不到时按 SVG 默认纯黑处理）。p90 低于 `LIFT_CUTOFF`（0.30）才算会糊
  （纯黑标 `aws` / `github` / `openai` …）。深色底上「看不看得见」取决于离黑有多远，而 Rec.709 亮度
  会把饱和色误判成「糊」。判定按 `credential_type` 缓存在 `plugin_manager._icon_lift_cache`，
  与 `_icon_image_cache` 同生命周期（只在 `sync_plugins()` 清空）。前端只给 `icon_lift` 为真的
  `<img>` 加 `.icon-lift`，`base.css` 的白板规则据此限定——避免给彩色标套上白框（见 docs/FRONTEND.md）。
  **服务专属逻辑在各子插件内自带，新增插件无需改动共享模块。**
- **同步流程**：启动时 `sync_plugins()` 扫描两层结构 → upsert plugins 表（含 category / category_name / batch_verify）→ 清理已消失插件的记录与模块缓存。
- **批量验证开关**：manifest 的 `batch_verify`（默认 true）落库到 plugins.batch_verify；调度器与前端批量时跳过为 false 的类型——**有写副作用或无法自动化**的（如 duckdns / wechat_ilink），以及**判定常为「未验证」、批量会刷掉人工结论**的（telegram_apihash / mail/gateway）。前端在批量勾选弹窗中禁用其勾选，但单条手动验证仍可用。

**插件模块约定**：必须暴露 `verify(data)`；可选 `validate(data)`、`get_display_summary(data)`。
`verify` 返回值鸭子类型兼容 VerifyResult / dataclass / dict。

**「操作」框架（`actions` / `run_action` / `capabilities` / `stream_action`）**：插件可选声明一组操作，
前端凭据行的「操作」按钮据此渲染。数据流：

```
前端 openActions(row)
  ├─ list_credential_actions(id)   → plugin_manager.plugin_actions（纯本地，渲染菜单）
  └─ credential_capabilities(id)   → plugin_manager.plugin_capabilities → 插件的 capabilities()
                                      （会联网探权限，包在代理作用域里；失败一律 fail-open）
        │
        ├─ 1 个操作 → 直接进；≥2 个 → 弹选择菜单，探测结果回来再收敛
        ▼
  run_credential_action(id, action_id, params)
        → plugin_manager.run_plugin_action → 插件的 run_action(action_id, params, data)
        → 结果经 redact.redact_deep 递归净化后才回前端
```

三个设计要点：

- **`actions()` 必须便宜且无副作用**，`capabilities()` 才是联网探测的那个 —— 分开才能把
  「没有操作 / 有操作但没权限 / 探测失败」三种状态说清楚（混在一起时，探测失败会伪装成「没有操作」，用户
  就分不清该去修密钥还是修网络）。
- **`kind: "list"`** 让插件用 `columns` + `rows` + `row_actions` 描述一个表格，行内按钮的 `params`
  支持 `$row.<列key>` 模板，目标 action 是 `form` 就预填、是 `list` 就下钻（前端用栈保存上一帧，返回零请求）。
- **净化边界在 `backend/redact.py`**：`redact_deep()` 递归遍历**整个结果**
  （`output`/`error`/`rows`/任意嵌套）把该凭据 `sensitive` 字段的明文替换掉——只扫顶层字段会让
  列表类操作把密钥从 `rows` 里漏出去。该模块被**两处**共用以闭合整类问题：`api.py` 的
  操作路径，以及 `plugin_manager.verify_credential()` 的验证路径（其结果会被 `scheduler`
  **落日志 + 落库到 `status_detail`**）。插件自己 `[:N]` 切的片段不在此列 —— 写日志用 `mask_secret`。
- **下载类操作**（`save_to`）：前端先弹「另存为」拿路径，路径经 `params` 交给插件，**插件自己下载**
  —— 不让插件返回 `{url, headers}` 由后端代下，是因为那会把鉴权头/预签名 URL 送进结果 dict 并穿到 JS 桥。
  对话框只在 `backend/api.py` 里弹：流式路径的插件跑在新建线程上，跨线程弹窗会卡界面。

---

### 2.4 调度器（scheduler.py）

**职责**：定时批量验证。

**实现**：APScheduler `BackgroundScheduler`，任务持久化到 SQLite `schedules` 表。

**调度类型**：
- `interval`：每隔 N 小时/天执行
- `cron`：cron 表达式（如每天 02:00）

**触发逻辑**：
```
调度器触发
    │
    ▼ 保险库已锁定？→ 是则【整轮跳过】：不查目标凭据、不验证，记一条 INFO（见下）
    │
    ▼ 查询目标凭据（全部 / 指定分组 / 指定类型 / 指定凭据）
    │   scope 与目标的对应：group/type 读 target_id（多态），
    │   credentials 读 credential_ids（勾选的具体凭据 id 数组）
    │   （自动剔除 batch_verify=false 与被禁用插件的类型，计为「跳过」）
    │
    ▼ 并发调用各插件的 verify()（每条凭据一个任务，并发数可配，见下）
    │
    ▼ 更新 credentials.status / last_verified_at
    │   （status == valid 时同时更新 last_valid_at）
    │
    ▼ 通过 pywebview 事件通知前端刷新
```

> `scope='credentials'` 时空勾选**必须在 `_run_schedule()` 里短路**：`batch_verify()` 对空
> `credential_ids` 视同「不带筛选」，会去验证**全库**——与「一条也不验」正好相反。短路时只记
> 一条告警日志并照常刷新 `last_run_at`（跳过也算运行过，否则界面永远停在「从未运行」）。

> **锁定即整轮跳过**：保险库锁定时（判据同 `api.is_unlocked` —— 拿不到解密密钥），
> `_run_schedule()` 在**入口**就早退，连线程池都不进。不早退的话每条凭据都会各自在
> `_verify_single_credential` 的 `key is None` 处短路，最后汇总成一行「异常 N」——日志里分不清
> 是锁着还是真出错，还白占并发。早退只记一条
> `定时计划 #N「…」跳过：数据库已锁定，本轮不执行验证` 的 **INFO** 日志，并同样照常刷新
> `last_run_at`。**判据不成立时不拦**：只有 `_get_key_callback` 已注入（`api.init_callbacks()`，
> 生产路径恒成立）且返回 None 才跳过——调度器脱离 api 单独启动时拿不准，宁可放行让逐条各自报错。

**验证代理（多代理 + 按调用生效）**：代理存在 `proxies` 表里、各自取名，**其它地方一律按 id 引用**
（`proxy_store`）。id 是 **UUID 而非自增整数**——自增只在单库唯一，备份导入（代理按原样沿用 id 重建）
后一旦对不上，凭据/预设里的引用会静默悬空 → 回落直连，界面上却看不出异常。解析时**按优先级逐层找**，
第一层给出可用的指定就停：

| 优先级 | 来源 | 怎么设 |
|---|---|---|
| 1 | **本条凭据** | 凭据编辑表单里的「使用代理」（`data._proxy_mode` / `_proxy_id`） |
| 2 | **子模块预设** | 该插件的默认设置（`preset_defaults` scope=`plugin`） |
| 3 | **上层模块预设** | 所属类别的默认设置（scope=`category`） |
| 4 | **全局** | 「系统设置 → 网络与代理」的默认代理（写进 `settings.global_proxy_id` + 环境变量 `VAULT_PROXY`） |

第 1 层里「跟随上层设置」（`_proxy_mode = global` 或缺省）**不表示「用全局」**，而是「本层不指定，
继续往上找」——所以改了插件/类别的预设，已有凭据会跟着变。**引用的代理被删了也一样往上找**
（不直接掉到全局，更不报错）。第 2、3 层的合并顺序由 `preset_store.defaults_for()` 保证（插件覆盖类别）。

难点在于**批量验证是并发的**：不同凭据可能要走不同出口，而插件只认进程级环境变量
`VAULT_PROXY`，直接改环境变量会互相干扰。所以引入了一条**按调用生效**的通道：

- `backend/vault_proxy_ctx.py`（框架持有）用 `threading.local()` 存「本次调用用哪个代理」，
  两个哨兵 `UNSET`（未指定 → 回退环境变量）与 `DIRECT`（强制直连、压过所有代理变量）
  必须分开——用同一个 `None` 表达会让「跟随全局」在外部设了 `ALL_PROXY` 时无法正确回退。
- `main.py` 启动时把它以**固定别名**注册进 `sys.modules`：`sys.modules["vault_proxy_ctx"]`。
  插件侧 `backend/plugin_sdk.py` 的 `proxy()` **懒查**这个名字——插件不直接 import backend
  内部模块，只通过 SDK 取用；脱离主程序单独跑时该模块不在，自然回退到环境变量。因此**插件代码一行都不用改**。
- 收口在 `plugin_manager` 的四个入口（`verify_credential` / `run_plugin_action` /
  `plugin_models` / `stream_plugin_action`）用 `credential_proxy_scope(data)` 包住，
  于是单条 / 批量 / MCP / 流式四条路径全自动覆盖。**必须 `begin/end(token)` 成对还原**：
  `ThreadPoolExecutor` 会复用线程，不还原就会把上一条凭据的代理带给下一条。
  `stream_credential_action` 另开线程，作用域因此只能建在它调的 plugin_manager 里，
  建在调用线程里会静默失效。

进程环境变量那条通道仍然保留（`backend/proxy.py` 的 `apply_proxy()`），给没走 ctx 的场景兜底。

**类别 / 插件默认设置（`backend/preset_store.py`）**：新建凭据时的预填值，存
`preset_defaults` 表，按「插件级 > 类别级 > manifest 的 `field.default`」继承。框架只做
**存 + 继承 + 注入**三件事，「哪些字段可以预设」由插件自己在 manifest 里声明——默认全部
**非敏感**字段都可预设，加 `"preset": false` 可排除；`api_key` 之类的敏感字段**永远不进默认值**
（`values` 是明文列，写入 / 读取 / 注入三处各剔一次）。注入点有三处且**缺一不可**：
前端新增表单（`CredentialForm.loadDefaults`）、`api.create_credential`（**在校验之前**，
默认值可能正好满足必填项）、以及 MCP 的 `vault_create_credential`——后者原本是复制出来的
第二份创建逻辑，现已改为直接调用 `api.create_credential`，避免「表单继承、MCP 不继承」这类
不一致再次出现。**更新（update）一律不套默认**，否则用户显式清空的字段会被复活。

**运行参数（`backend/verify_options.py`）**：批量验证的**并发线程数**（默认 8，1–32）与**请求超时**（默认 20 秒，5–300 秒）在「批量验证」对话框里就地调整，是这两项的**单一事实来源**——持久化到 `settings` 表的 `batch_concurrency` / `verify_timeout`，前端取值范围也由它下发（`limits()`），避免前后端各写一份常量。两条消费路径：

- **并发数**：`scheduler.batch_verify()` 内层 `ThreadPoolExecutor(max_workers=verify_options.concurrency(...))`，取 `min(并发设置, 待验证条数)`；手动批量传本次选择，定时任务不传 → 读已保存的设置。
- **total 的口径**：`plugin_manager.batchable_types()` 是「可批量验证类型」的**唯一判定处**
  （manifest `batch_verify` 为真 **且** 插件在当前 `.state.json` 下已启用）。
  `scheduler.batch_verify()` 用它挑要验的凭据并**返回实际验证条数**，
  `api.batch_verify()` 直接拿这个返回值当 `total` 回给前端——判定只有一处，
  避免「返回给前端的 total」与「实际执行数」两个口径对不上。
- **登记成后台任务**：批量验证会建一条 `kind=batch_verify` 的 `tasks` 任务，把 done/total 喂给任务表
  （「任务」页因此能看到进度并**随时取消**）。取消是协作式的：消费循环每收一个结果前查一次
  `tasks.should_cancel()`，命中就 `pool.shutdown(wait=False, cancel_futures=True)` 收尾
  （正在跑的那几个让它自己跑完，不强杀线程），任务标成「已取消」并如实写「已处理 n/m」。
- **三个事件**（前端据此做横幅与行内 loading，不用模态弹窗把人按在前台）：
  `vault:batch_verify_started`（带本次要验证的凭据 id 列表**与触发方 `trigger`**——`manual` 才让
  前端整页互斥，`mcp` / `schedule` 是用户没看着时跑起来的，只提示不锁页）、`..._progress`、`..._done`
  （取消时额外带 `canceled` 与 `done`）。
- **超时**：同样借环境变量下发——插件不直接 `import backend`，只经 `backend/plugin_sdk.py` 取用：
  `apply_timeout()` 把值写入 `VAULT_TIMEOUT`，插件侧 SDK 的 `timeout()` 每次发请求时读取，
  改完立即生效。外部显式设置的同名变量优先。机制与 `VAULT_PROXY` 同构，见 `docs/PLUGIN_GUIDE.md` 第 2.3 节。

**单条验证的并发保护**：`verify_single_credential()` 外层套了一个**同 id in-flight 集合**
（`_inflight` + 锁），同一凭据已在验证中时直接返回「该凭据正在验证中，请稍候」而不重复发请求。
批量验证走的也是这个函数，所以「手动点验证」与「定时/批量」撞车时同样只会跑一次。
前端另有一层：验证状态记的是 **id 列表**而非单个 id，逐行独立 loading。

**欠费探测后置检查（`plugin_manager._apply_quota_probe`）**：一次凭据验证的主流程是
`api/scheduler → plugin_manager.verify_credential() → call_verify(module, data)`；返回前多一道**可选**检查——
仅当结果 `valid`、且 `account_info` 里既无 `balance` 也无 `usage_rate` 时，才调用插件的
`quota_probe(data)`（未接入该函数的插件直接跳过）。这是「Key 有效」与「账户有钱」之间的补丁：
`/models` 探针对欠费账户同样返回 200，只有真实发一次 `max_tokens=1` 的对话才能分辨。开关与模型是
**凭据自身**的字段（manifest 的 `probe_chat` / `probe_model`，随 `data` 一起进插件），逐条凭据独立、
默认关闭——**不经过全局设置，也不需要环境变量**（数据本来就在插件手里）。后置检查里的任何异常都被
吞掉并只写日志——**它绝不允许影响主验证结果**。详见 `docs/PLUGIN_GUIDE.md` 第 2.4 节。

**代理连通性诊断**（`backend/proxy_test.py`）：代理字符串本身看不出好坏，故做一次**对照实验**——同一批探测分别在「直连」与「走代理」下各跑一遍，再比对：① 国内站点（baidu.com）可达性 ② 国外站点（gstatic.com/generate_204）可达性 ③ 出口 IP 与归属地。综合给出 `ok / warn / fail` 结论（如「出口 IP 已切换」「经代理国内外站点均不可达」「出口 IP 与直连相同：代理可能未生效」）。

两个刻意的设计：`trust_env=False` 屏蔽系统 `HTTP(S)_PROXY`/`ALL_PROXY`，否则「直连」对照组会被环境代理污染；出口 IP **只认无国内 CDN 的 HTTPS 见证源**（ipify / icanhazip / ifconfig.me / ipinfo.io）——ip-api.com 在国内有 CDN，会被规则型代理客户端判为直连，令「挂代理」仍读到本机真实 IP、结论反向，故它仅用于查归属地（IP 写在路径里，与路由无关）。六项探测并发执行，单项失败不影响其它项。

**缺 SOCKS 依赖的处理**：httpx 走 socks 需要独立的 `socksio` 包，而它是在 httpx 内部**函数体内** `import` 的——打包时构建机没装，PyInstaller 既不报错也不打包，直到运行时用 socks5 代理才抛一句英文 `ImportError`。这条链路做成三层兜底：

| 层 | 位置 | 行为 |
|----|------|------|
| 构建期 | `scripts/build.py` / `Keyrealm.spec` | 构建前按 `requirements.txt`（**含 extras 展开**）核对当前环境，缺包就打印清单并**中止**（`--allow-missing-deps` 可放行），提示 `pip install -r requirements.txt`；spec 自己还会再核对一遍那四个「静态分析看不到」的依赖（socksio / PySocks / telethon / python-socks），**缺一个即中止构建**——直接用 `pyinstaller Keyrealm.spec` 时同样会停，逃生口是 `CV_ALLOW_MISSING_DEPS=1`（`--allow-missing-deps` 会自动设上） |
| 诊断 | `backend/proxy_test.py` | `proxy_config.needs_socks()` + `socks_available()` 提前判定，socks 代理 + 缺依赖时**不发请求直接短路**，给出 `fail` 结论与中文处理办法（伪造一份代理侧探测结构，界面两列卡片照常渲染） |
| 插件 | `backend/plugin_base.py` | `_friendly_error()` 把异常文本里的 socksio 报错换成中文处理办法；原始异常仍完整写入日志，不缺排查线索 |

判定与文案集中在 `backend/proxy.py`：`needs_socks()` / `socks_available()` / `SOCKS_HINT`（单一事实来源，三层共用）。

**备份 / 恢复**：`backend/backup.py` 负责构建/导入备份文档（JSON，**v2 起为「全量」**）。加密备份的敏感字段直接沿用库中密文；**整库档还会带上 `vault.salt` / `vault.verify_token`**——有了这份 KDF 材料，导出文件才**自包含**：原库丢了也能用「文档里的 salt + 原主密码」把密文解开（见下）。明文备份本就跨主密码可导（以当前主密钥重新加密写库）。文件读写通过 pywebview 的原生文件对话框（`api._file_dialog`，优先用 `webview.FileDialog` 枚举——旧的模块级 `SAVE_DIALOG`/`OPEN_DIALOG` 常量会打印弃用警告）。

**跨保险库还原**：`api._resolve_decrypt_key` 比较文档的 `vault.salt` 与当前库的 salt——**不同**就说明这份备份来自另一个保险库（密文是用「那份 salt + 当时的主密码」派生的 key 加密的）。此时界面会补问**原主密码**，用它派生源 key（并用文档里的 `verify_token` 校验），导入时**逐条解密后以当前库的 key 重新加密**写回；`_ENCRYPTED_SETTINGS`（如 `update_token`）同样重加密。**绝不覆盖当前库的 `salt`/`verify_token`**——那是当前保险库的根，改了整库都打不开。

**导入是填充式的**：以当前库（本质是各插件的 `fields_schema`）为蓝本，把备份条目里的值往这个蓝本里填，**不删也不覆盖既有数据**。一条要满足三条才导得进去，否则**整条判失败、不写库**：① 插件**存在且启用**；② 备份里的字段**都能在当前插件里找到**（多出未知字段 = 两边同构性已不成立）；③ 插件的**必填字段在备份里有着落**（备份没有、插件也没默认值 → 填进去必是一条注定验证失败的凭据）。插件新增的**选填**字段一律留空——这正是「库扩增后新列留空」的落点。去重仍按「同类型 + 敏感值相同」跳过（`dedupe=true`）。

判定失败的条目逐条记下原因，并落到 `data/logs/` 下**两份普通文本文件**（与运行日志同目录，但不用 `logging`：这是一次性、条目化的审计记录，混进滚动日志会把两者都搅浑）：完整导入日志（含成功条目，敏感值脱敏）与**导入失败日志**（仅失败条目，**保留明文**，供人工比对补录，故以 `0600` 权限落盘）。返回给界面的失败条数有上限（日志文件里是全量）。

导出有两个**互不重叠**的维度：`scope` 决定是否附带**整库档**（`all` 带、`credentials` 不带），`credential_ids` 决定**导出哪些凭据**（`None` = 全部，给定列表 = 只导这些；不影响 scope 语义）。因此「全部 + 勾选若干」与「仅凭据 + 勾选若干」的差别仍然只是整库档那部分，文档里写入 `credential_ids` 便于回溯。前端「备份」页把两者做成两组控件（范围单选 + 凭据目录树勾选，勾选器是共享组件 `components/CredentialTreePicker.vue`——「系统设置 → 定时计划 → 指定凭据」用的是同一个）。

**整库档（`scope=all`，v2）带的东西**：凭据 + 分组 + 代理 + 预设 + **`vault`（salt / verify_token）** + **`settings`（全量，除结构版本与归入 vault 的两项）** + **定时计划** + **MCP（工具权限 + 活跃令牌，令牌**只带哈希**）** + **插件启停**（`plugins/.state.json` 的 disabled 集）。这几样合起来才是「换台机器/原库丢了也能复原」的**全量备份**。

---

### 2.5 MCP Server（mcp_server.py）

**职责**：为 AI Agent 提供凭据管理接口。

**传输**：SSE（Server-Sent Events），默认监听 `127.0.0.1:8765`。不会随普通 CLI 命令启动；
桌面/Web 由用户手动开启或按 `mcp_enabled` 恢复，CLI 用 `mcp start` **前台常驻**启动（Ctrl+C 停止）。

**重依赖按需导入**：`mcp` / `starlette` / `uvicorn`（连带 `pydantic`）在应用自身的导入链上
多占约 **33MB** RSS，而这套东西**只有真正启动服务时才用得到**。因此本模块顶层只留标准库与
`backend` 内部模块，`mcp`/`uvicorn` 降级到 `TYPE_CHECKING` 仅供类型标注，实际导入下沉到
`_build_mcp_app()`（构造工具）、`start_server()`（起 uvicorn）里；Bearer 鉴权中间件也在
`_make_asgi_app()` 内部定义。**注意**：鉴权必须用**纯 ASGI 中间件**，不能用
`BaseHTTPMiddleware`——后者会把响应体重新包一层异步迭代器，SSE 长连接会触发 Starlette 的
`Unexpected message: http.response.start` 断言并返回 500。`main.py` / `api.py` 仍照常
`import mcp_server`，无需改动；打包侧 `Keyrealm.spec` 用 `collect_all()` 显式收了这三个包，
不受延迟导入影响，新增重依赖时照此办理。

CLI 启动流程（`backend/cli.py::cmd_mcp_start`）：占单实例锁 → 解锁保险库 → 保存 `--host/--port`
→ 创建/轮换 Token → `api.start_mcp()` → 前台等待，SIGINT/SIGTERM 设置停止事件、关闭服务并写回
`mcp_enabled=0`。安装成 PyInstaller onefile 后 Ctrl+C 可能同时落到引导进程与子进程，因此用信号
事件而不是裸 `KeyboardInterrupt`。

**监听地址可配**：`settings` 的 `mcp_host` / `mcp_port`（默认 `127.0.0.1` / `8765`）。
`start_server(host, port)` 不再把 `127.0.0.1` 写死，也**取消了「端口被占就 +1/+2」的降级**
——端口是用户显式配的，悄悄换一个会造成「配了 9000 却在 9001」的困惑；端口占用即直接报错。
保存设置时若服务在运行，会先停、等端口真正释放、再按新地址起；**若新端口起不来，会把原地址的
服务起回去**并返回错误，不把用户留在「改完啥都没了」的状态。host 只接受合法 IP 或 `localhost`
（防止误把凭据服务挂到公网）。

**Token 鉴权**（必须）：
```
Agent 发起 SSE 连接
    │
    ▼ HTTP Header: Authorization: Bearer kr_<32位hex>
    │
    ▼ [MCP Server] SHA-256(token)，与 settings.mcp_token_hash 常量时间比对
    │
    ├── 不匹配 → 401 Unauthorized，拒绝连接
    │
    └── 通过
            │
            ▼ 每次工具调用前：检查全局 mcp_permissions 表
```

**Token 格式**：`kr_` + 32 位随机十六进制（共 35 字符），只存 SHA-256 哈希，明文仅在创建 / 轮换时展示一次。
CLI 对应命令：`mcp token`（查看前缀，无 Token 时创建并显示明文）、`mcp token --rotate`
（轮换并显示新明文）；`mcp start --new-token` 在启动前轮换。已有 Token 的明文**无法找回**。

MCP 全局只保留**一个** Token（不再单独建表，就存在 `settings` 的 `mcp_token_hash` / `mcp_token_prefix`
两条键里），且**默认即存在**（无则自动创建），仅提供「轮换」（签发新 Token 覆盖旧的，明文仅展示一次）。

**权限控制**：只有**全局工具开关**一层（`mcp_permissions` 表，在 MCP 设置页统一控制）。
早先的「Token 级范围」没有配置入口、恒为空，已随之删掉。

调用未通过全局开关时，工具返回一份**结构化错误载荷**（`_perm_denied`，字段
`error="permission_denied"` + `reason` +
可照做处理的 `message`），**不抛异常**——异常正文会被客户端压成一句「Error executing
tool xxx」，把「该去哪开权限」这个关键信息吞掉。

详见 [MCP_API.md](MCP_API.md)。

---

### 2.6 pywebview API 桥接层（api.py）

**职责**：暴露给前端的 Python 方法集合，是前后端唯一通信通道。

**调用方式**（前端）：
```javascript
// 所有方法均为 async
const result = await window.pywebview.api.list_credentials({ group_id: 1 })
```

**方法命名规范**：`动词_名词`，如 `create_credential`、`verify_credential`、`check_duplicate_credential`。

**事件推送**（后端 → 前端）：
```python
# 后端发送事件
window.evaluate_js("window.dispatchEvent(new CustomEvent('vault:updated', {detail: ...}))")
```

`api.push_event()` 现在有两个出口：桌面窗口（`evaluate_js`）与注册到 `add_event_sink()` 的
Web SSE 广播器。Web 模式下 `frontend/public/web-bridge.js` 把 `window.pywebview.api` 代理成
`POST /api/call`，并把 `/api/events` 的 SSE 还原成同名 `CustomEvent`——页面代码不区分模式。

**桌面专用方法**（托盘、原生文件对话框、系统浏览器、轻量模式）由 Web 桥接层单独处理或返回
“不支持”；备份导入导出在浏览器里换成文件选择 / 下载；下载类插件操作使用服务器临时目录
（`web_pick_save_path` → `/api/download`），不把带鉴权头的 URL 交给前端。

---

### 2.7 系统托盘（tray.py）

**仅桌面模式加载**：`main.py` 在 `main()` 里才 `import tray`；CLI / Web 模式不导入 `pystray`
（无 `DISPLAY` 时导入即抛异常，CLI 包也不打包它）。`api.py` 刷新托盘改成延迟导入
`_update_tray()`，没有托盘时静默跳过。

**职责**：窗口最小化到托盘后保持后台运行，MCP Server 和调度器持续工作。

**实现**：`pystray` 库（跨平台，Windows/macOS/Linux 均支持），但**起法分两类**：

- **Windows**：后台守护线程跑 `Icon.run()`（win32 后端允许脱离主线程）。
- **macOS / Linux**：**必须**在主线程调 `run_detached()`（非阻塞），把托盘挂到 pywebview 的
  主循环上。这两个平台的后端会去跑**各自的主事件循环**，丢进线程就是把循环抢走，随后主线程的
  `webview.start()` 拿不到控制权、立刻返回，表现为**「应用启动一两秒后自己退出」**（两个平台都
  实际发生过）：
  - macOS：darwin 后端 `_run()` 里直接是 `NSApplication.sharedApplication().run()`（Cocoa 主循环）；
    因此还需在**构造 Icon 时**传入那个共享的 `NSApplication`
    （`pystray.Icon(…, darwin_nsapplication=NSApplication.sharedApplication())` —— 平台专属选项
    走**构造函数**，**不是** `run_detached()` 的参数）。
  - Linux：AppIndicator / GTK 后端要 GLib 主上下文，抢不到时报
    `g_application_run() cannot acquire the default main context because it is already acquired
    by another thread!`。

托盘若没起来（集成失败 / 后端不支持 / Wayland 下没有托盘宿主），`tray.available()` 为假，
此时**关闭窗口改为直接退出**——不能让窗口藏起来却没有托盘可以叫回。排障可用环境变量
`KEYREALM_NO_TRAY=1` 一次运行跳过托盘。

**托盘图标状态**：

| 图标颜色 | 含义 |
|----------|------|
| 🟢 绿色 | MCP Server 运行中，至少一个 Token 活跃 |
| 🔵 蓝色 | MCP Server 运行中，无活跃连接 |
| 🔴 红色 | MCP Server 已停止或出错 |
| ⚪ 灰色 | 应用未解锁（主密码未输入） |

**右键菜单**：
```
┌─────────────────────────┐
│  显示主窗口              │
│  ─────────────────────  │
│  MCP：运行中 :8765      │
│  活跃 Token：1 个       │
│  ─────────────────────  │
│  锁定（清除内存密钥）    │
│  ─────────────────────  │
│  退出                   │
└─────────────────────────┘
```

**窗口关闭行为**（由设置项 `close_action` 决定）：
- `ask`（默认）→ 弹窗让用户选择"直接退出"或"后台运行"，可勾选"记住我的选择"
- `quit` → 停止所有后台服务，真正退出进程
- `background` → 隐藏窗口，最小化到托盘（不退出）
- 托盘菜单"退出" → 无论设置如何，均停止所有后台服务并退出

**托盘线程**：`pystray` 在独立守护线程中运行，通过线程安全队列与主线程通信。

---

### 2.8 空闲自动锁定（idle_lock.py）

**职责**：一段时间无用户操作后，自动清除内存中的解密密钥（等价于用户点「立即锁定」）。

**设置项**：`settings` 表的 `auto_lock_minutes`（纯 K/V，**不涉及 DDL / schema_version**）。
默认 `0` = 关闭；可选档位 `1 / 5 / 10 / 15 / 30 / 60` 分钟，非法值一律回落到 `0`。
该键随整库备份的 `settings` 段导出/导入（`backup.py` v2 起是**全量设置**，不再只挑几个偏好键）。

**为什么计时在后端而不是前端**：
1. 「后台有任务在跑就顺延」这条规则的依据 `tasks.running_count()` 在后端；
2. 应用常驻托盘运行（`close_action=background`）时窗口隐藏，WebView 会节流 JS 定时器，
   前端计时不可靠；后端守护线程不受影响。

**实现**：与调度器同款的常驻守护线程（`threading.Thread` + `threading.Event` 停止位），
每 `WATCH_INTERVAL`（10s）醒来一次，**现读现判** `db.get_setting("auto_lock_minutes")`：

- 纯函数 `should_lock(now, last_activity, minutes, running_count)` 承载判定逻辑，便于离线测试；
- 时间基准用 `time.monotonic()`（不受系统时钟调整影响）；
- 到达阈值时若 `tasks.running_count() > 0`（有 RUNNING 状态的后台任务），**本次跳过**，
  下个周期再判——避免把批量验证的剩余条目全打成「保险库已锁定」；已开始的条目不受影响；
- 命中且回调报告「已解锁」时，调用注入的 `lock_cb()`（= `api.lock_vault`）。

**回调注入**：`set_callbacks(lock_cb, is_unlocked_cb)`，与 `scheduler.set_callbacks` 同范式——
本模块只依赖 `backend.database` / `backend.tasks` / `backend.logger`，**不 import `api`**，避免循环依赖。

**前端分工**：前端只做两件事——有用户操作时**节流上报**活动（`touch_activity`，约 30s 一次），
以及收到 `vault:locked` 事件时清空明文并跳回解锁页。只有后端是计时的唯一权威。

**与其它加锁入口的关系**：`api.lock_vault()` 是唯一加锁出口，界面按钮 / 托盘菜单 / 空闲自动锁
全部汇入它，因此它统一广播 `vault:locked`，前端据此在各入口下都表现一致。

**生命周期**：`main.py` 在 `scheduler.start_scheduler()` 之后 `idle_lock.start()`；
`_shutdown_services()` 里 `idle_lock.stop()`（置停止位并 join，超时 2s）。

---

### 2.9 日志模块（logger.py）

**职责**：统一日志配置，写入 `<data_dir>/logs/keyrealm.log`（默认 `<入口目录>/data/logs/`，可配置），并可按用户选择调整级别。

**实现**：`logging` + `RotatingFileHandler`（单文件约 1 MB × 5 份备份），多行堆栈按时间戳归组。

**日志级别**：`DEBUG / INFO / WARNING / ERROR / CRITICAL`（默认 `INFO`），运行时可调整并持久化到
`settings` 表的 `log_level` 键，下次启动自动恢复。`httpx` / `httpcore` 被压制到 `WARNING`，
避免第三方库噪声。

**检索**：`read_logs(limit, level, search)` 支持最低级别过滤 + 关键词搜索。搜索按「整条记录」
匹配（含 traceback 续行），大小写不敏感，多个空白分隔的关键词需**全部命中**（AND）。

**采集范围**：插件不再用 `print(..., file=sys.stderr)` 输出——打包为 windowed 模式时
`sys.stdout`/`sys.stderr` 为 `None`，打印会静默丢失。改为 `backend/plugin_sdk.py` 暴露的
`log()` / `log_debug()`（内部是 `logging.getLogger("plugin")`），异常经 `backend/plugin_base.py` 的
`call_*` 辅助函数统一 `_log.exception` 落盘（异常被静默吞掉时，插件报错就不会进日志）。
调度器 `scheduler.verify_single_credential` 逐条记录验证结果并在批量结束时汇总一条统计。

**界面**：日志是独立页面（`frontend/src/views/Logs.vue`，侧栏「日志」），不再挂在「系统设置」下。
页面提供级别筛选、关键词搜索、显示条数、清空与刷新。

---

### 2.10 后台任务（tasks.py）

**职责**：让「点一下就开跑」的长操作（OSS 下载 / 前缀打包等）**看得见、能终止、关掉弹窗不丢结果**。

**为什么单独一个模块**：这类操作跑在后台线程里，弹窗一关就没人接收进度事件了。任务注册表把
状态从「某个弹窗的事件流」提升为「全局可查询的状态」。

- **纯状态模块**：只做任务的增删改查 + 取消标志，不认 webview、不认数据库。推送由 `api.py`
  注入（`tasks.set_notifier(push_event)`）——既避免循环依赖，也让本模块能脱离应用单独测试。
- **持久化到文件**（`<data_dir>/tasks.json`，与日志同目录，**不进数据库**）：进程重启后仍在，用户回来
  能翻到「上次那个下载到底成没成」。重启时**所有 `running` 一律改判 `interrupted`**——线程随
  进程没了，继续显示「进行中」是骗人，但也没失败（用户没做错什么），故单列一档。写盘时机：
  状态**变迁**（新建 / 结束 / 取消 / 清理）立刻落盘；高频的 `progress()` 按 `_SAVE_INTERVAL = 2s`
  节流。先写 `.tmp` 再 `replace` 原子替换，避免半个 JSON 让下次启动解析失败。已结束的只保留
  最近 `_MAX_FINISHED = 50` 条。之所以不进库：任务不参与查询与关联，进库白搭一次 schema 变动 +
  迁移脚本，且锁库时反而读不到。
- **状态**：`running` / `done` / `failed` / `canceled` / `interrupted`，外加 `cancel_requested`
  （已点取消、正在等插件停下）。

**取消是协作式的**：`cancel()` 只置一个 `threading.Event`，真正停下靠**插件**在下载循环里查
`should_cancel()`（框架把它按关键字传给 `stream_action`，与 `on_progress` 同一套「签名里有才传」的
机制）。所以做不到立刻杀线程，但会在下一个检查点（每个 chunk / 每个对象）停下并清理 `.part`。
「取消还是失败」由**框架**判断（它持有标志），不靠插件猜错误文案——刚好在取消前跑完的仍算成功。

**事件与界面**：任何变更推一次 `vault:tasks`（detail 是变化的那条快照）。前端「任务」页据此
就地更新，侧栏「任务」项带进行中数量角标。**带 `progress: true` 的操作自动成为任务**，所以
弹窗里多了一个「转到后台」——关掉弹窗任务照跑。

---

### 2.11 窗口生命周期与轻量模式（main.py）

> **仅桌面模式**有窗口。Web 模式走 `run_web_mode()`（uvicorn + 静态前端 + 浏览器桥），CLI 模式走
> `backend/cli.py`；两者都不创建窗口，也不会初始化托盘。源码启动必须 `--gui`，冻结桌面包双击
> 无参数仍直接开窗口。

`main.py` 持有唯一的窗口引用 `_window`，窗口的隐藏 / 显示都收口在这里（`tray.start_tray` 的三个
回调、`api.set_close_callbacks` 注入的 `hide_cb`、`_on_closing` 都指向它们）：

| 函数 | 触发 | 行为 |
|---|---|---|
| `_hide_to_tray()` | 托盘「最小化」/「后台运行」/ `close_action=background` | `hide()`；成功且轻量模式开启时再释放界面 |
| `_show_window()` | 托盘「显示主窗口」/ 关闭后再次打开 | `show()` → `restore()` → 恢复界面 |
| `_quit_app()` | 托盘「退出」/ 关闭框选「直接退出」 | `_shutdown_services()` → `window.destroy()` |

**菜单项与「关闭按钮」共用同一套函数**，所以「关闭窗口时后台运行」和「点托盘最小化」走的是同一条
路径——轻量模式的释放也因此对两者同时生效。

**轻量模式（`settings.light_mode`）**。开启后，隐藏窗口时额外调用 `_release_page()`：
`Window.load_html(<占位页>)` 把当前文档换成一张几十行的静态 HTML，**释放整个 SPA 的 DOM / JS 堆 /
组件实例**；再次显示时 `_restore_page()` 用 `Window.load_url(_app_url)` 重新加载前端。

三条关键约束：

1. **只换文档，不销毁窗口**。销毁最后一个 pywebview 窗口会直接结束 GUI 主循环（winforms 后端的
   `Application.Exit()`），托盘常驻就无从谈起——所以用 `load_html`（`NavigateToString`）而非 `destroy`。
2. **`_app_url` 必须是 `file://` 形式**，绝不能把裸路径递给 `load_url`。pywebview 的
   `is_local_url()`（`webview/util.py`）把「非 http(s)/file 的字符串」一律当作需要内置 HTTP 服务
   的目标，会现起一个 `http://127.0.0.1:<随机端口>` 来托管它——origin 就从 `file://` 变成 `http://`，
   与首次加载不是同一个源，**localStorage 当场清零**（主题、列顺序、目录树展开状态全丢）。转成
   `file://` 后 `is_local_url` 返回 `False`，`load_url` 原样导航过去，origin 不变。
   转换由 `_to_url()` 负责，`main()` 里同时保留裸路径（给 `create_window`，行为不变）与
   `_app_url`（URL 形式，给恢复用）。
3. **释放期间 `push_event` 直接丢弃**（`api.mark_page_released(True)`）。占位页上没有
   `window.pywebview`，任何 `evaluate_js` 必然抛错；不拦的话每推一次事件就写一条 warning，把日志刷满。

**恢复失败不清标志**：`_restore_page()` 里 `load_url` 抛异常时保持 `_page_released = True`，占位页上
已写明「从托盘菜单选择打开窗口」，用户再点一次即重试。

**已知边角**：`single_instance.focus_existing()` 用裸 Win32 `FindWindowW` + `ShowWindow(SW_RESTORE)`
在**第二个进程**里把窗口拉起来，绕过了 `_show_window`，因此被释放的窗口可能直接显示占位页而不恢复。
pywebview 没有「再次显示」事件可挂（`Form.Shown` 只触发一次，`Activated` 不映射任何事件），所以
兜底是**占位页自己写明怎么恢复**。（理论上可另起 Win32 `IsWindowVisible` 轮询，未做。）

### 2.12 程序内更新（updater.py）

**职责**：应用内检查 GitHub 最新源码并一键更新（备份 → 从源码重编译 → 逐级迁移数据库 → 重启）。

**唯一实现放 `backend/updater.py`**：应用内（`backend/api.py` 转发 → `main.py::VaultApi`）与命令行
脚本（`scripts/{build_update,db_update,restart}.py` + 各自 `.sh`/`.bat` 薄引导；`check_version.py` 为
无头 CLI）**共用同一份**。之所以放 `backend/` 而不放 `scripts/`：冻结 exe 只打包 `main.py` 的 import 图，
`scripts/` 不进 exe —— 应用内的 `check_update()` 必须在 exe 内可调用。updater 只依赖标准库级的
内部模块（`paths`/`version`/`database`/`single_instance`），因此**任何 Python 3.11+ 都能 import
它**（含 venv 尚未创建时），脚本据此复用。

**两段式**：应用内「立即更新」拆成 **准备**（`prepare_update()`，应用进程内、带进度、可取消：
备份 + 下载预编译包 / 拉源码 + 装依赖 + 构建到 `.update-staged/`，写 `<data_dir>/.update-prepared.json`）
与 **交棒**（`begin_handoff()` 发请求 + 起脱离进程；脱离进程 `apply_update()` 用准备产物走**快路径**，
无产物则**回落完整流程**）。**准备完成后不再自动退出**：后端推 `vault:update_ready`，由前端弹窗询问
「立即重启 / 稍后」；选「立即重启」才调 `api.apply_prepared_update()` → `begin_handoff()`。选「稍后」
则保留产物，下次 `prepare_update()` 先按「正式版版本仍是最新 Release / 测试版 commit 仍是远端同一
提交」校验，命中就**跳过下载与构建**（测试版准备阶段已推进源码，`check_update` 会依据本地产物把
结果标成「本地已有准备完成但未安装的更新」）。产物有效期 7 天。

**CLI 自更新**：`Keyrealm-CLI self-update check|install|status` 直接复用 `updater.prepare_update()`
/ `begin_handoff()`。`install` 同步准备、成功后由交棒进程替换程序；`--test` 走源码构建并自动带
`--cli`，正式版按 `platform_pkg()` 选择 `-cli` 资产。交棒前会占住单实例锁、把 relaunch 参数收敛为
空（避免重启后重复执行 install），并在 `_self_update_argv()` 里**显式传出当前 `--data-dir`**——
一次性数据目录不写配置，不传下去更新进程就找不到请求/准备产物。
**构建 / 迁移 / 替换必须留在交棒阶段**——`build.py::assemble_release`
会 `copy2` 覆盖运行中的可执行文件、`run_migrations` 要独占数据库，两者都要求应用已退出。
交棒顺序不可颠倒：**先起更新进程、再关闭应用**。

**自动检测更新**（`settings.update_auto_check`，**默认开启**）：`main.py` 启动后与 `api.unlock()` 里各
调一次 `api.start_auto_update_check()`。**手动「检查更新」与自动检查共用同一台机器**
（`api._start_check`，**同一时刻只跑一个**）：手动检查登记成**后台任务**（`tasks` 里 kind=`update_check`，
在「任务」页可见），自动检查**静默**（不建任务）。结果写 `data/.update-check.json` **持久化**
（`updater.write_last_check` / `read_last_check`），界面换页 / 重启后仍能显示，**直到被新结果替换**；
手动检查**总是**落盘（含失败），自动检查**只在成功时**落盘（免得启动时未解锁的失败盖掉好结果）。
发现新版本另 `push_event("vault:update_available")` 并留一份「取出即清除」的通知
（`take_auto_update_notice()`），前端 `App.vue` 弹可点击通知跳到设置页。
**未解锁时拿不到访问令牌**（本仓库已公开，不带令牌也能查，但注入令牌后走认证请求、不易被限流）；启动那次可能没成且**不标记完成**，留待解锁后补跑一次。

**两档更新方式不同**：**正式版**对比 **GitHub Release 的最新正式版标签**，准备阶段**下载对应平台的
预编译包**（`Keyrealm-<版本>-<平台>.zip`）并算 SHA-256；交棒阶段校验后**替换当前程序**、迁移数据库、
重启——**不编译**（源码部署会因此切成二进制）。**测试版**跟 `main`（版本号 **或** commit 任一不同即
算有更新），准备阶段**从源码构建**：程序目录是仓库就快进，否则 `git init` + `fetch` + `checkout`
**就地转成源码部署**，再建/补 `.venv`、装依赖、`scripts/build.py --frontend --output .update-staged`；
交棒阶段安装该产物、迁移、重启。渠道存 `settings.update_channel`。

**更新代理**（`settings.update_proxy_mode` / `update_proxy_id`）：跟随全局默认 / 直连 / 指定某个代理，
环境变量 `KEYREALM_UPDATE_PROXY` 可覆盖。git 的代理经 `git -c http.proxy=…` 注入（**压过 git 全局
配置里的 `http.proxy`**，环境变量对它无效）；pip / npm 走 `http_proxy` / `all_proxy`。

**检查只读**：`check_update()` 用 `git clone --depth 1` 到**临时目录**读远端 `backend/version.py`
（`ast` 解析，不 `eval`）与 commit，用完删临时目录——**不碰当前工作区**。真正前进工作区的是
`fetch_latest()`（`git fetch --depth 1` + `merge --ff-only`；脏工作区默认拒绝，`--force` 才先
`stash`）。

**交棒时序（顺序不可颠倒）**：应用内 `start_update()` 完成准备并推 `vault:update_ready` → 用户确认
「立即重启」→ `apply_prepared_update()` → 写 `data/.update-request.json` →
`spawn_detached()` 把**自更新进程**起成**脱离进程**（冻结 exe 用 `Keyrealm --self-update <渠道>`、
源码运行用 `python main.py --self-update <渠道>`，两者都追加 `--data-dir <当前目录>`；
**两平台都用脱离进程 + 日志文件，不弹控制台窗口**，POSIX 走 `start_new_session`）→ **确认子进程起来之后**
才调 `_close_quit_cb()`（= `main._quit_app`）退出。
`main.py` 的 `--self-update` 分支不建窗口，直接跑 `updater.apply_update()`。脚本首步 `wait_for_app_exit()` 轮询
`single_instance.acquire()`（拿不到锁 = 应用还在跑）；它同时**占住锁**保证更新期间没人能启动
应用，**重启新应用前必须 `single_instance.release()`**（`backend/single_instance.py` 为此新增了
`release()`）。重启沿用原启动参数（`_relaunch_spec`）：CLI/Web 的 `--web`、`--port` 等不会被丢掉；
源码模式没有显式模式时自动补 `--gui`。

**跨进程 / 跨重启通信**：全程写 `data/.update-status.json`（阶段、成败、from/to）；新应用启动后
由前端「设置 → 更新」卡片读取展示。

> **子进程环境必须隔离 PyInstaller 临时目录**：onefile 的父进程把解压目录经 `_MEIPASS2` 传给子进程，
> 子进程会**复用**同一份 `_MEI`；父进程一退出就删掉它，子进程（交棒进程 / 重启起来的新 exe）再去读
> `base_library.zip` 就崩。所以 `spawn_detached` / `_child_env` 一律带上
> **`PYINSTALLER_RESET_ENVIRONMENT=1`**（`updater._PYI_ENV`），让子进程解压自己的一份。

**备份**：可执行文件 + `plugins/` + 当前 `data_dir`（DB 走 `sqlite3 backup()` 一致性快照，`logs/`/`webview/` 排除）
**再外加一份全量导出 `data/keyrealm-full-backup.json`**（复用「备份」功能，见 §备份），
打成一个 zip 落 `<data_dir>/backup/`（gitignored、0600、**含真实密钥**），**只留一份**。构建失败不覆盖旧
产物（`assemble_release` 覆盖前留 `Keyrealm.old` 供回滚）；**更新流程换 exe 时沿用同一个 `Keyrealm.old`**
——Windows 上运行中的 exe 不能被覆盖（`WinError 32`），只能**先改名腾位**再放新的
（`updater._replace_exe`；交棒进程自己就跑在这个 exe 上）。
> 两份内容互补：**快照**用于「更新失败原样回退」，**全量导出**用于「原库损坏/丢失仍能还原」。

**数据库逐级迁移**：`scripts/migrations/m_<from>__to_<to>.py`（接口 `FROM_VERSION` /
`TO_VERSION` / `preview(conn)` / `apply(conn)`），运行器串链**逐级**升、**绝不跨级**，每跳留
SQLite 快照，`--apply` 前抢单实例锁。

**凭据数据格式升级**：属于「**数据结构变化**」的一部分——**同一个 `SCHEMA_VERSION` 就是同一套
数据结构**。数据结构变了（含**只是凭据格式变了**）就提升 `SCHEMA_VERSION`、并在
`scripts/migrations/` 留一跳脚本（哪怕表结构没动）。那一跳里：**表结构部分**由迁移脚本在交棒阶段
用 SQL 完成；**数据部分**落到 `backend/data_upgrades.py`——因为凭据的敏感字段**加密**，交棒阶段
（应用已退出）没有主密码，读不出也改不了。数据部分在**应用解锁后**执行一次
（`api.run_data_upgrades`，落后于 `SCHEMA_VERSION` 才跑）；**导入备份**在落库前走**同一套规则**。
**规矩**：规则只在这个模块里维护——插件只描述**当前**格式、不写双格式兼容，框架其它地方也不做
格式容忍。

**`scripts/build.py` 的输出**：默认落**仓库根**（`.git` 同级，`--output` 可改）。输出=仓库根时
`assemble_release` **跳过 `plugins/` 自我复制**（否则会把活的源目录删掉再自我复制），也不清
`data/installed_plugins`（那已是用户真实数据目录）。

> 使用方式、命令行参数、备份/回滚、故障排查与 Windows 验收清单见 **[程序内更新](UPDATE.md)**。

---

## 3. 数据流

### 3.1 添加凭据（自动验证）

```
用户填写表单
    │
    ▼ [前端] api.create_credential(data)
    │
    ▼ [后端] 校验字段（调用 plugin.validate_fields()）
    │
    ▼ [后端] crypto.encrypt(敏感字段) → 写入 SQLite
    │
    ▼ [后端] 异步调用 plugin.verify(plain_data)
    │
    ▼ [后端] 更新 status / last_verified_at（valid 时同时更新 last_valid_at）
    │
    ▼ [后端] 推送 vault:credential_updated 事件
    │
    ▼ [前端] 刷新列表，显示验证结果
```

### 3.2 MCP Agent 调用

```
AI Agent（Claude Desktop / Cursor）
    │
    ▼ SSE 连接 http://localhost:8765/sse
    │
    ▼ 调用工具 vault_list_credentials
    │
    ▼ [MCP Server] 检查 mcp_permissions 表
    │
    ▼ [MCP Server] 调用 database.list_credentials()
    │
    ▼ [MCP Server] 解密敏感字段（按权限决定是否返回明文）
    │
    ▼ 返回结果给 Agent
```

---

## 4. 线程模型

```
主线程
    │
    ├── pywebview GUI 事件循环（主线程）
    │       └── 窗口关闭事件 → 拦截，改为隐藏窗口
    │
    ├── System Tray（pystray 守护线程）
    │       └── 托盘菜单点击 → 通过线程队列通知主线程
    │
    ├── BackgroundScheduler（APScheduler 线程池）
    │       └── 定时验证任务（ThreadPoolExecutor，max_workers=10）
    │
    └── MCP Server（uvicorn 线程，端口 8765）
            ├── SSE 连接处理（asyncio 事件循环）
            └── Token 鉴权中间件（每次请求校验 Bearer Token）
```

> **注意**：
> - pywebview 的 JS-API 调用在独立线程中执行，SQLite 操作需加线程锁
> - 窗口隐藏后所有后台线程继续运行，MCP Server 不中断
> - 应用真正退出时（托盘菜单"退出"）：停止调度器 → 停止空闲锁定看门狗 → 关闭 MCP Server → 销毁托盘图标 → 退出进程

**其他形态的线程模型**：

- **Web**：uvicorn 主循环在 `run_web_mode()` 的主线程；调度器 / 空闲锁 / MCP（若开启）与桌面一致；
  每个 HTTP RPC 通过 `run_in_threadpool` 调用同步 `VaultApi`。
- **CLI 一次性命令**：进程启动 → `bootstrap.init_core(full=False)` → 执行一条命令 → 退出；
  不启动调度器、空闲锁、MCP、自动更新检查。插件验证仍可同步调用 `scheduler.verify_single_credential`。
- **CLI `mcp start`**：主线程进入信号等待循环，uvicorn 在独立线程；`SIGINT/SIGTERM` 设置停止事件后
  关闭服务，进程退出即释放内存密钥。

---

## 5. 安全设计

| 威胁 | 缓解措施 |
|------|----------|
| 数据库文件泄露 | AES-256-GCM 字段级加密，无主密码无法解密 |
| 主密码暴力破解 | PBKDF2 100,000 次迭代，增加破解成本 |
| 内存中密钥泄露 | 密钥仅存于进程内存，不写文件；托盘"锁定"（桌面）/ `lock_vault` RPC 可主动清除 |
| 无人值守时密钥常驻 | 「空闲自动锁定」按 `auto_lock_minutes` 在无操作一段时间后自动清除内存密钥（[§2.8](ARCHITECTURE.md)）；CLI 一次性进程退出即释放 |
| MCP 未授权访问 | Bearer Token 鉴权（纯 ASGI 中间件），Token 只存 SHA-256，支持轮换 |
| MCP Token 泄露 | 轮换后旧的立即失效（只存 SHA-256，不存明文） |
| MCP 工具越权 | 全局工具开关（`mcp_permissions`，界面 / `mcp permission` 唯一入口）；工具默认关闭，需显式开启 |
| Web 未授权访问 | 默认 `127.0.0.1` + 启动时随机 Bearer Token；`--host 0.0.0.0` 属用户显式暴露 |
| Web 文件接口越权 | `/api/log_file` 只接受单个日志文件名；`/api/download` 限制在 `<data_dir>/web_downloads` |
| CLI 密码泄露 | 推荐 `KEYREALM_MASTER_PASSWORD`；`--password` 会出现在进程列表，文档明确提示 |
| 插件恶意代码 | 插件在独立命名空间加载，文档中明确信任边界 |
| 验证请求泄露 Key | 验证逻辑由插件实现，Key 仅发送给对应厂商 API |
| 后台运行时窗口未锁 | 托盘菜单提供"锁定"操作，清除内存密钥，需重新输入主密码 |

---

## 6. 扩展性设计

- **新凭据类型**：在 `plugins/` 下新增子插件目录（manifest.json + plugin.py），重启即自动加载，无需修改主程序
- **新 MCP 工具**：在 `mcp_server.py` 中注册，自动出现在权限管理页
- **新验证厂商**：新建 `plugins/llm/<厂商>/`（`manifest.json` + `plugin.py`），参照同级子插件、自带端点与判定，
  **无需修改 `backend/plugin_sdk.py` 或类别模块**；重启即加载
- **数据库迁移**：`database.py` 中维护 `SCHEMA_VERSION`，启动时自动执行迁移脚本
