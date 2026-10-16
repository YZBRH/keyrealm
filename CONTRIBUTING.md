# 参与开发

面向要**改代码、构建、发布**的人。只想使用的话看 [README](README.md) 就够了。

---

## 1. 开发环境

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.11+ | 后端与打包 |
| Node.js | 18+ | 仅用于构建前端 |
| WebView2 Runtime | — | Windows 运行前提；Windows 10/11 通常已内置 |

```bash
git clone https://github.com/YZBRH/keyrealm.git
cd keyrealm

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
python main.py
```

> **必须用虚拟环境**：部分依赖（`httpx[socks]` 的 socksio、`PySocks`、`telethon` 的
> `python-socks`）在 PyInstaller 静态分析里看不到，构建脚本会**显式按环境核对**它们；
> 装进系统 Python 而用 venv 跑构建，就会出现「明明装了却报缺」。

> **Linux 的界面后端二选一**（Windows 靠系统 WebView2、macOS 靠 WebKit，都不用选）：
> 默认 **Qt** —— `pip install "pywebview[pyside6]"`，打包时 spec 会**强制要求**它、缺了直接
> 中止构建（免得打出一个起不了窗口的包）；也可以选系统 **GTK**。源码运行用
> `KEYREALM_GUI=gtk`，打包用 `--gui gtk`。
>
> **GTK 的系统包（各发行版）** —— 体积实测：系统侧约 105 MB（WebKitGTK 93.5 MB + GTK3 9 MB +
> pygobject 1 MB）、venv 侧约 0；桌面发行版通常已经装着 WebKit，最小化安装的服务器版则没有。
> **要求你的 Python 与系统 Python 是同一个版本**（`gi` 是按系统 Python 编译的）。
>
> | 发行版 | 命令 |
> |---|---|
> | Debian / Ubuntu / Kali | `sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1` |
> | Fedora | `sudo dnf install python3-gobject gtk3 webkit2gtk4.1` |
> | CentOS / RHEL 8 | `sudo dnf install python3-gobject gtk3 webkit2gtk3` |
> | Arch / Manjaro | `sudo pacman -S python-gobject gtk3 webkit2gtk-4.1` |
>
> **打包成冻结包**：可以 —— CI 的 `linux-gtk-*` 专项档正是这么打的（`pip install PyGObject`
> 现场编译 + spec 显式收 `gi.*` + `hooksconfig` 钉 typelib 版本）。**但产物只在该构建系统上稳**
> （gi/glib/WebKit 都来自构建机），所以按发行版出专项档、不能一份包通吃。详见
> [docs/LINUX_GTK_MATRIX.md](docs/LINUX_GTK_MATRIX.md)。
>
> **CentOS 8 三个坑**：① 官方 yum 源**已 EOL**，要先把源指向 `vault.centos.org` 才装得上包；
> ② 系统 Python 是 **3.6**（跑不了本项目），得先弄一个 3.11+（`uv python install 3.12` 最省事）
> ——而这时**只能用 Qt**：系统的 `gi` 是 3.6 编的、3.12 导不进来；③ 前端的 Vite 5 要 **Node 18+**，
> 而 CentOS 8 自带的 nodejs 太老——用 [NodeSource](https://github.com/nodesource/distributions)：
> `curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash - && sudo dnf install -y nodejs`。
>
> ⚠️ **Qt 还得钉版本**：PySide6 **6.10 起**的 wheel 要求 **glibc ≥ 2.34**，而 CentOS 8 是 **2.28**
> ——直接 `pip install "pywebview[pyside6]"` 会报「找不到匹配的发行版」。必须钉到 **6.9.3**
> （它的 wheel 是 `manylinux_2_28`，实测可用）：
>
> ```bash
> pip install "pywebview[pyside6]" "PySide6==6.9.3"
> ```
>
> 判据很直接：**wheel 文件名里的 `manylinux_2_XX` 就是它要求的 glibc 版本**（`2_28` ≤ CentOS 8 的 2.28 才行）。

## 2. 在浏览器里调前端（不必启动 Python）

```bash
cd frontend && npm run dev
```

没有 pywebview 时 `src/api/mock.js` 会自动装一个**内存 Mock 后端**，主密码 `demo1234`。
两条必须守住的约束：

- **方法名与签名要和主进程一致**：mock 直接冒充 pywebview 桥，所以它的键是后端 IPC 名
  （`list_proxies` / `set_tree_order` …），不是 `src/api/index.js` 里的驼峰包装名；
  且只走位置参数，签名要与 `main.py` 的 `VaultApi` 逐一对齐。
- **插件目录是生成产物**：`src/api/mockPlugins.js` 与 `pluginIcons.js` 由
  `scripts/gen_mock_assets.py` 从 `plugins/` 现扫生成。**加了/改了插件就重跑一次**，
  否则 dev 模式下会少插件、搜不到、下拉里没有。

```bash
.venv/bin/python scripts/gen_mock_assets.py
```

**装了 git 钩子就不用记这条**：

```bash
.venv/bin/python scripts/install_git_hooks.py
```

之后每次提交只要动到 `plugins/` 或该生成脚本，`pre-commit` 就会自动重跑并把产物一并暂存
（`core.hooksPath` 指向版本库内的 `scripts/git-hooks/`，**仅对本仓库生效**；
`git commit --no-verify` 可临时跳过）。钩子在缺解释器 / 缺 Pillow / 脚本报错时**只警告不拦提交**
——不让一个 dev-only 产物卡死正常提交。

## 3. 打包为可执行文件

**默认输出到仓库根（`.git` 同级）**，可用 `--output DIR` 改（`--output release` 即旧行为）。
**PyInstaller 不能跨平台编译，要得到 Windows exe 必须在 Windows 上构建。**

```bash
pip install pyinstaller
python scripts/build.py --frontend
```

| 选项 | 作用 |
|---|---|
| `--frontend` | 先跑 `npm run build` 再打包 |
| `--output` / `-o` | 产物落点目录（默认仓库根，即 `.git` 同级；传 `release` 即旧行为） |
| `--console` | 保留控制台窗口（调试用） |
| `--smoke` | 构建后跑 `--print-paths` 校验冻结路径（隐含 `--console`） |
| `--allow-missing-deps` | 依赖缺失时仍继续构建（默认中止） |

构建产出的 `release/` 结构：

```
release/
├── Keyrealm.exe      # 单文件可执行程序（后端代码 + frontend/dist 已内置）
├── keyrealm-src.zip  # 源码归档（由 scripts/pack_source.py 生成）
├── plugins/          # 插件【唯一事实来源】，放 exe 同级：放入即加载、删除即卸载
│                     #   .state.json 记录启用/禁用状态（不存在 = 全部启用）
└── data/             # 首次运行自动创建（数据库、主密码校验、日志、界面偏好）
```

打包边界：

| 内容 | 是否进 exe | 说明 |
|---|---|---|
| 后端代码、`frontend/dist` | ✅ 内置 | 只读资源，运行时从临时解压目录读取 |
| `plugins/` | ❌ 外部 | 放 exe 同级；启动时扫描自动加载，删子插件目录即卸载 |
| `data/` | ❌ 外部 | 运行时可写，保证数据持久化 |

`release/` 自包含、可便携分发，**不进 git**（`Keyrealm.spec` 与 `scripts/*.py` 保留追踪）。

> **升级时 `data/` 原样保留**：`build.py` 只替换 exe 与 `plugins/`（**不覆盖**
> `plugins/.state.json`，你的启用/禁用状态不会被重置），另清理 `data/installed_plugins/`
> （zip 安装时代的遗留缓存）。其余一律不碰——`data/webview/` 是界面偏好、`data/logs/` 是日志。

### 3.1 打包源码归档

```bash
python scripts/pack_source.py     # 生成/覆盖 release/keyrealm-src.zip
```

打包范围 = **当前工作树**（git 已追踪 + 未被忽略的新增文件），改完直接跑，无需先 commit。
自动排除 `.venv` / `node_modules` / `data/`（含真实密钥）/ `release` / `dist` / `build` /
`frontend/dist` / `__pycache__`，不含符号链接；内置安全校验：一旦发现 `real_keys` / `*.db` /
`master.key` 会被打包即中止。

## 4. 构建期检查（都是「静态分析看不见」类问题的兜底）

- **建表自检**：`build.py` 打包前跑一次 `scripts/check_schema.py`，在临时库上**真跑一遍**建表与各表
  CRUD。建表 SQL 只有真连上 SQLite 才会被解析，而单元测试通常把 `database` 整个打桩——
  于是「SQL 写错」会一路测试全绿、打包后运行才崩（例如把 SQLite 保留字 `values` 当列名 →
  建表 `syntax error`）。改了表结构后可单独跑 `python scripts/check_schema.py`。
- **依赖检查**：`build.py` 一上来按 `requirements.txt`（含 extras 展开）核对当前环境，缺包即中止。
  之所以要查 extras：httpx 是在**函数体内** `import socksio` 的，构建机没装时 PyInstaller
  既不报错也不打包，直到 exe 里用 socks5 代理才暴露。
  `Keyrealm.spec` 还会再核对一遍那几个「静态分析看不到」的依赖（socksio / PySocks /
  telethon / python-socks），**缺一个即中止**。逃生口是 `CV_ALLOW_MISSING_DEPS=1`
  （`--allow-missing-deps` 会自动设上，两边口径一致）。
- **插件用到的标准库**：插件由 `importlib` 运行时加载，`main.py` 从不 `import plugins`，
  静态分析看不到。第三方库（httpx 等）后端也在用已被收集，但**只有插件用**的标准库
  （`imaplib` / `smtplib` / `email` / `zipfile` / `xml` / `html`）不会进包——症状是 exe 里一执行
  对应操作就 `ModuleNotFoundError`，构建期无警告、开发机也复现不出。spec 会扫
  `plugins/**/*.py` 的顶层 import 补进 `hiddenimports`，新增插件自动生效。
- **`--frontend` 的前端预检**：`node_modules` **存在不等于能用**。从 Linux/macOS 拷过来的
  `node_modules` 里 `.bin/vite` 是 POSIX 脚本/符号链接，Windows 要的是 `.bin/vite.cmd`——
  症状是 `npm run build` 报 `'vite' is not recognized as an internal or external command`
  （看着像代码问题，其实是依赖问题）。`build.py` 会先查 `.bin` 里有没有可执行的 `vite`，
  命中就直接给出「删掉 `frontend/node_modules` 重新 `npm install`」的修法。

## 5. 项目结构

```
keyrealm/
├── main.py                  # 应用入口
├── Keyrealm.spec            # PyInstaller 打包规格
├── requirements.txt
├── backend/
│   ├── api.py               # pywebview JS-API 桥接层
│   ├── crypto.py            # AES-256-GCM 加密/解密
│   ├── database.py          # SQLite 数据访问层
│   ├── backup.py            # 备份 / 恢复（导出单个 JSON；导入为「填充式」并落两份日志）
│   ├── paths.py             # 冻结感知路径解析（打包/开发统一）
│   ├── icon_ink.py          # 插件 logo 深色主题可见度判定
│   ├── plugin_base.py       # 插件接口定义与模块加载
│   ├── plugin_manager.py    # 插件目录扫描与自动加载
│   ├── plugin_sdk.py        # 框架暴露给插件的唯一接口层
│   ├── redact.py            # 输出净化（回显的敏感字段值替换成 ***）
│   ├── preset_store.py      # 类别 / 插件默认设置（存 + 继承 + 注入）
│   ├── proxy*.py            # 代理工具 / 注册表 / 连通性对照诊断
│   ├── single_instance.py   # 单实例闸门
│   ├── scheduler.py         # 定时验证调度器
│   ├── tasks.py             # 后台任务注册表（状态 / 进度 / 取消，落 data/tasks.json）
│   ├── verify_options.py    # 批量验证运行参数（并发 / 超时）
│   ├── idle_lock.py         # 空闲自动锁定看门狗
│   ├── autostart.py         # 开机自启动（直接读写系统自启条目）
│   ├── vault_proxy_ctx.py   # 按调用生效的代理上下文
│   ├── logger.py            # 日志配置（轮转 + 级别可调 + 关键词搜索）
│   ├── tray.py              # 系统托盘
│   ├── updater.py           # 程序内更新（检查 / 备份 / 构建 / 逐级迁移 / 重启，唯一实现）
│   └── mcp_server.py        # MCP Server（SSE）
├── assets/                  # 应用图标资源（由 scripts/make_icon.py 生成）
├── scripts/                 # 构建与生成脚本（build / pack_source / check_schema / gen_mock_assets；
│                            #   程序内更新：build_update / db_update / restart + check_version(.py) + migrations/）
├── plugins/                 # 插件源码（打包时作为外部目录，不进 exe）
├── frontend/                # Vue 3 前端
│   ├── src/{views,components,stores,api}/
│   └── vite.config.js
└── docs/                    # 架构 / 前端 / 插件 / MCP / 数据库 / 更新 六份文档
```

## 6. 版本、数据库与发布

- **版本规则**：末位 +1 = 修 bug / 优化；中位 +1 = 新增特性；首位 +1 = 较大型框架变动。
  **版本号只在明确要求时才提升**——其余提交照常，只写 commit、不动版本号；**每次提升都必须
  同步更新 [`CHANGELOG.md`](CHANGELOG.md)**。版本号唯一事实来源是 `backend/version.py`，
  界面上以 `1.2.0 (<commit>)` 展示（括号里是**构建时**注入的短 hash）。
- **数据库结构有变动时**：必须在 `scripts/` 附一份 `migrate_*.py` 迁移脚本，并同步提升
  `SCHEMA_VERSION`。应用内**不再新增**自动迁移（`database._migrate()` 里那几段只服务 1.0 之前的
  旧库，已冻结）。迁移脚本默认**只演练**，加 `--apply` 才写库；跑之前请完全退出应用
  （脚本会先抢一次单实例锁来确认）。
- **插件 `credential_type` 改名**时要额外注意：老库里该类型的凭据会**找不到插件**（列表里显示不出来、
  也不能验证）。参考既有的一次性迁移脚本 `scripts/migrate_doubao_to_ark.py`。
- **改名 / 换标识**时不要动这些（会破坏兼容）：备份格式串、`vault_*` MCP 工具名、`vault:*`
  事件名、`VAULT_*` 环境变量、`data/`、`credentials.db`、`master.key`、插件 `credential_type`。
  详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §1.1。

## 7. 程序内更新

应用内「设置 → 更新」可检查并一键更新（**面向源码运行**的部署；打包分发的 exe 走 Releases）。
**唯一实现在 `backend/updater.py`**，被应用内（`backend/api.py` → `main.py::VaultApi`）与命令行脚本共用。

**两段式**：应用内「立即更新」= **准备（应用进程内，带进度、可取消）** + **交棒（脱离进程）**。
`prepare_update()` 备份 + 下载预编译包 / 拉源码 + 装依赖 + 构建到 `.update-staged/`；准备成功才
`begin_handoff()` 起脱离进程跑 `apply_update()`（解压替换 / 安装 + 迁移 + 重启）。
**构建 / 迁移 / 替换必须留在交棒阶段**——`build.py` 会覆盖运行中的可执行文件、`run_migrations`
要独占数据库，两者都要求应用已退出。

| 能力 | 脚本 | 说明 |
|---|---|---|
| （检查更新） | 无 shell 包装；`scripts/check_version.py` | 应用内已承担；`.py` 保留为**无头 CLI**，`--json` 便于自动化 |
| 构建更新 | `scripts/build_update.{py,sh,bat}` | **完整流程**：等应用退出 → 备份 → `git fetch` → `build.py --frontend` → 迁移 → 重启（无准备产物时的回落路径） |
| 数据库更新 | `scripts/db_update.{py,sh,bat}` | 逐级迁移（默认演练，`--apply` 才写） |
| 程序重启 | `scripts/restart.{py,sh,bat}` | 脱离终端重启应用 |

- `.sh`/`.bat` 只做「找 / 装 Python」（缺 `.venv` 就 `python -m venv` 并装 `requirements.txt`
  + `pyinstaller`），再 `exec` 调同名 `.py`；**全程无交互**。每次运行输出另存 `data/logs/<脚本名>-*.log`。
- **两档口径**：正式版按 `APP_VERSION` 比大小；测试版按「版本号 **或** commit」任一不同。
  两档都跟踪 `main`。渠道存 `settings.update_channel`。

### 7.1 改了数据库结构要做什么

**两件事都要做**：① 在 `scripts/migrations/` 加一份 `m_<old>__to_<new>.py`（接口见
`scripts/migrations/README.md`）；② 提升 `backend/version.py` 的 `SCHEMA_VERSION`。
**只提供相邻两版的脚本、绝不跨级**——运行器会逐级串起来升。

> 使用方式、命令行参数、备份/回滚、故障排查、Windows 验收清单见
> **[程序内更新](docs/UPDATE.md)**。

## 8. 插件开发

完整规范见 **[docs/PLUGIN_GUIDE.md](docs/PLUGIN_GUIDE.md)**（目录结构、`manifest.json` 字段、
`plugin.py` 接口、可选「操作」、签名器校准、新增插件的检查清单）。要点：

- 一个服务 = 一个插件目录，放 `plugins/` 即加载、删目录即卸载；没有安装器。
- 插件私有的辅助模块**必须带厂商前缀命名**（`bce_signer.py`，不要写 `signer.py`）——同类别下
  同名模块会因 `sys.modules` 互相顶掉，拿到别家的实现且一路不报错。
- docstring 末尾用 `参考文档：` 附官方文档链接（只写官方页，不写转述页）。

## 9. 发布预编译包（GitHub Actions）

打 tag（`vX.Y.Z`）会触发 **`.github/workflows/release.yml`**：在三个 runner 上并行构建，把产物挂到
该 tag 的 GitHub Release，用户下载即用。

- 矩阵：`windows-latest`（x64）、`macos-latest`（Apple Silicon）、`ubuntu-22.04`（Linux：**通用版**与
  **GTK 专项版**各一条）。暂不出 macOS Intel 版；以后要加 macOS Intel 就补一条 `macos-13` / `macos-x64`。
  **必须矩阵**——PyInstaller **不能跨平台编译**，每个平台只能在对应系统上构建。
- 每个 runner：装 Python + Node → `pip install -r requirements.txt pyinstaller` → `npm ci` →
  `python scripts/build.py --frontend --output release` → 把 `release/`（可执行文件 + `plugins/`
  + `.update-meta/`）打成 `Keyrealm-<版本>-<平台>.zip`（顶层一个干净目录，不散落）→ 挂到 Release。
  > 包里的 `.update-meta/`（`version.json` 记 `schema_version`/`commit`，`migrations/` 是迁移脚本）
  > 让程序内「正式版更新」在**没有源码**的机器上也能知道目标结构版本并逐级迁移——所以它必须随包走。
- **手动触发**（Actions → Release → Run workflow）只产出构建产物、不发 Release，可用于验证。
- 目标机前提：**Windows** 需 WebView2 Runtime（Win10/11 通常自带）；**macOS** 产物**未签名**，
  首次打开会被 Gatekeeper 拦，需右键→「打开」或 `xattr -dr com.apple.quarantine <文件>` 放行。
- **Linux 出两类包**（详见 [docs/LINUX_GTK_MATRIX.md](docs/LINUX_GTK_MATRIX.md)）：
  - **通用版** `Keyrealm-<版本>-linux-general-x86_64.zip`：`ubuntu-22.04` 上构建、**Qt 后端**（自包含），
    任意发行版可用（glibc ≥ 2.35）。
  - **GTK 专项版** `Keyrealm-<版本>-linux-gtk-<系统>-x86_64.zip`：在**目标发行版自己的环境**里构建、
    **GTK 后端**（用系统 WebKitGTK）——CI 额外 apt 装 GTK3 / WebKit2GTK 4.1 / AyatanaAppIndicator 的
    typelib 与编译依赖，再 `pip install "PyGObject<3.51"`，spec 仅在 GTK 档收 `gi.*`。**产物只在构建它的
    那个发行版上稳**，故包名带构建系统（如 `ubuntu22.04`）。

## 10. 提交约定

- **提交信息用中文**，沿用 conventional 前缀（`feat` / `fix` / `docs` / `chore` / `refactor` /
  `perf` / `style`）。
- **改代码要同步改文档**：`docs/*.md`、`README.md` 与代码 docstring 三者要一致，收尾做一次
  旧符号全仓 grep。
- 一次提交只做一件事；把「顺手改的无关内容」单独拆出去。
