# 程序内更新

Keyrealm 可以在程序内检查并一键更新。**两种渠道的更新方式不同**：

| 渠道 | 怎么判断「有更新」 | 怎么更新 |
|---|---|---|
| **正式版**（formal） | 对比 **GitHub Release 的最新正式版标签**（`vX.Y.Z`） | **下载对应平台的预编译包替换当前程序**，再迁移数据库——**不编译** |
| **测试版**（test） | 跟 `main` 最新提交（`APP_VERSION` **或** commit 任一不同） | **从源码重新构建**（本机没有源码会先拉一份到程序目录），再迁移数据库 |

- 正式版适合日常使用：作者发了 Release（打了 `vX.Y.Z` 标签、CI 出了包）才会提示更新。
- 测试版跟开发进度：跟到 `main` 的最新提交即可用，但需要本机能构建（Python + Node）。

> **代理**：「设置 → 更新」里有独立的**「更新代理」**选择——**跟随全局默认 / 直连 / 指定「代理列表」
> 里的某个代理**（与应用内每凭据的「使用代理」同一套语义，只是全局一份）。本机直连 GitHub 不通时，
> 在这里选一个代理即可。环境变量 `KEYREALM_UPDATE_PROXY` 可临时覆盖（设 `direct` 强制直连）。
> 对 `git` 而言代理是经 **`git -c http.proxy=…`** 注入的，**会覆盖 git 全局配置里的 `http.proxy`**
> （否则用户已有的 git 代理配置会把它盖掉，环境变量对它无效）；`pip` / `npm` 则走
> `http_proxy` / `all_proxy` 环境变量。当前实际出口在卡片里显示。

> **数据目录**：本文中的 `data/...` 均指**当前数据目录**（默认 `<程序入口>/data`，可通过
> `keyrealm.conf.json` 或 CLI `--data-dir` 改到别处）。准备产物、备份、状态与日志都随之移动；
> 交棒进程会显式继承发起时的数据目录。

> **访问令牌（可选）**：本仓库**已公开**，不填令牌也能检查与更新（`git` 匿名可读、Release 资产走
> 公开下载 URL）。填一个 **GitHub PAT** 的用处是——把「检查更新 / 取更新内容」走的 GitHub API 从
> **匿名 60 次/小时**提到**认证 5000 次/小时**，避开匿名限流。仓库若哪天转回**私有**，则**必须**填
> （否则 `git` 报 `could not read Username for 'https://github.com'`、公开资产 URL 返回 **404**）。
> 权限只需对本仓库的 **Contents: read**（经典或细粒度 PAT 均可）。令牌用**主密码加密**后存本地
> settings，**明文不落库**；对 `git` 的认证是经**环境变量注入**的 `http.extraheader`，**不写进
> URL / 命令行**（否则会出现在 `ps` 与错误信息里）。
> 检查、更新，以及交棒起的更新子进程都会自动带上。**锁库即清**内存中的令牌；公开仓库留空即可。
> 命令行单独跑脚本时用环境变量 **`KEYREALM_UPDATE_TOKEN`**（或用 `KEYREALM_UPDATE_REMOTE`
> 把远端指向别处，便于离线自测）。

> **两段式**：应用内「立即更新」先跑 **准备阶段**（备份 + 下载预编译包 / 拉源码 + 装依赖 + 构建，
> 全程在**应用进程内**、带进度、可取消），准备成功后才**交棒**给一个脱离进程做
> **构建 / 解压替换 / 数据库迁移 / 重启**——这几步要覆盖运行中的程序文件、且要独占数据库，
> 必须等应用退出。因此：**准备阶段看得到实时进度，交棒阶段看 `data/logs/update-*.log`，重启后看「上次更新」的逐阶段报告**。

---

## 1. 在应用里更新

**设置 → 更新** 卡片顶部有**「自动检测更新」开关（默认开启）**：开启时每次**程序启动**
（以及首次解锁）后会在后台自动查一次有没有新版本，发现时弹一条可点击的通知（点它跳到本卡片）。
关掉后仍可随时手动「检查更新」。

1. 选**更新渠道**（正式版 / 测试版）。
2. 选**更新代理**（跟随全局默认 / 直连 / 指定某个代理）；**访问令牌**可选（仓库已公开，不填也能检查与更新；填了走 GitHub API、不易被匿名限流）。
3. 点**检查更新**：**后台任务**（只读，比对 GitHub 上的最新版本，不改动本机；在「任务」页可见）。
   结果**持久化**——换页、跳去别的功能再回来、甚至重启，都还显示着上次结果，**直到被下一次检查替换**。
   **测试版会一并显示远端 commit 哈希**（版本号相同、仅提交不同时，标题会写成「发现新提交 `<commit>`」）。
4. 有更新 → 显示「发现新版本 X」（正式版还会显示将下载的包名）+ **本次更新的内容** + **立即更新**按钮。
   内容按渠道取，**只影响展示、不影响检查结果**（取不到就提示一句）：
   - **正式版**：目标版本在远端 `CHANGELOG.md` 里的那一段（拿不到再退回该 Release 的说明正文）。
   - **测试版**：当前 commit → 远端 commit 之间的**全部提交标题**（GitHub compare API，最多列 100 条）。
5. 点**立即更新** → 二次确认 → **准备阶段在应用内进行**（卡片显示进度条，可**取消**）：
   - 正式版：备份 + 下载预编译包（按字节显示百分比）。
   - 测试版：备份 + 拉取源码 + 装依赖 + 构建（不确定时长，进度条走不定态）。
6. 准备成功 → **弹窗询问是否立即重启**：
   - 「立即重启」：交棒给脱离进程完成**解压替换 / 安装 / 数据库迁移 / 重启**，
     这几步的输出落 **`data/logs/update-*.log`**（**两平台都不弹控制台窗口**）。
   - 「稍后」：程序保持运行，**准备产物留在本地**（`data/.update-prepared.json` + 包 / `.update-staged/`）。
     下次点「安装已准备好的更新」或「立即更新」时，会先检查本地产物：
     正式版的包仍对应当前最新 Release、或测试版的构建仍对应当前远端 commit，就**跳过下载 / 构建**直接复用。
7. 重启后，设置页「上次更新」显示**版本更迭**（`旧 (commit) → 新 (commit)`——测试版两版的版本号
   常常相同，靠 commit 哈希才看得出换了版本）与**逐阶段报告**（✓/✗ + 日志路径）。

> **准备失败或取消 → 程序不退出**：错误直接显示在卡片上，可重试。
> 交棒一步走的是**脱离进程**：先把它起起来、确认起来了，才关闭本程序；顺序不可颠倒。
> 准备产物的有效期是 **7 天**；超期、Release / 远端 commit 已前进、或文件校验失败时自动作废并重新准备。

---

## 2. 正式版：下载预编译包（不编译）

**准备阶段（应用内，程序不退出）**：
1. 查 GitHub Release 的最新正式版标签；与本机 `APP_VERSION` 比，更高才继续。
2. **备份**当前程序（可执行文件 + `plugins/` + `data/`）。
3. **下载**对应平台的包 `Keyrealm-<版本>-<平台>.zip` 到 `data/backup/_asset-<版本>.zip`
   （平台取当前系统与架构；目前 CI 发布的只有 `windows-x64` 与 `macos-arm64`），算 **SHA-256**。
4. 写**准备清单** `data/.update-prepared.json`（版本 / 包路径 / sha256 / 目标 `schema_version`）。

> 若清单里已有**同一最新版本**、包仍在且 sha256 一致的产物（上次选了「稍后」），跳过第 3 步下载，直接复用。

**交棒阶段（应用退出后）**：
5. **校验**清单里的包存在且 sha256 一致（不符则**回落到重新下载**）。
6. **解压替换**到程序目录（可执行文件 + `plugins/`；包内 `.update-meta/` 也在内，见 §4）。
   **`data/` 目录绝不覆盖**，换 exe 先改名 `Keyrealm.old` 腾位（同 §3 第 6 步）。
7. 读包内 `.update-meta/version.json` 的 `schema_version`，**逐级迁移**数据库。
8. 重启新的可执行文件。

> **源码部署也可用**：选了正式版，就等于切到官方的预编译二进制（以后重启跑的是它）。
> 想留在源码部署，用**测试版**（从源码构建）或手动 `git pull`。

---

## 3. 测试版：从源码构建

**准备阶段（应用内，程序不退出）**：
1. 备份当前程序。
2. **获取源码**：程序目录是 git 仓库就对齐到 `main` 最新；**不是就就地初始化并检出**
   （`git init` + `fetch` + `checkout` —— 即把程序目录转成源码部署；可执行文件、`data/` 等未跟踪文件原样保留）。
   > **工作区有改动时直接丢弃**（`git reset --hard`）——更新要的就是「拉最新」，而部署目录里的改动多半是
   > 构建/依赖留下的痕迹（典型是 `npm install` 改写的 `package-lock.json`）。想保留就先 commit，
   > 或用 CLI 的 `--force`（先 `git stash` 再合并）。**未跟踪 / 被忽略的文件不受影响。**
   > **这一步是「提交点」**：源码一更新，工作区就前进了，而数据库还没迁移——所以此后**不再接受取消**，
   > 会直接进入构建；构建完成后询问是否立即重启。若交棒失败，用 `scripts/build_update.sh --kind test` 手动补完（回落流程会补做构建/迁移/替换）。
3. **准备构建环境**：项目下没有 `.venv` 就现建一个，再装 `requirements.txt` + `pyinstaller`（全程走「更新代理」）。
4. `python scripts/build.py --frontend --output .update-staged` —— **构建到 `.update-staged/`**，
   不触碰运行中的程序文件（默认落点会覆盖运行中的可执行文件）。
5. 写**准备清单** `data/.update-prepared.json`（commit / `staging_dir` / venv 解释器 / 目标 `schema_version`）。

> 若清单里的产物仍指向**远端同一 commit**、`.update-staged/` 里的可执行文件还在（上次选了「稍后」），
> 跳过第 2~4 步（拉源码 / 装依赖 / 构建），直接复用该构建。
> 由于准备阶段已把源码推进到目标 commit，下次「检查更新」会因 `compare` 无差异而看不见更新——
> 此时后端会依据本地产物把结果标成「本地已有准备完成但未安装的更新」，按钮变成「安装已准备好的更新」。

**交棒阶段（应用退出后）**：
6. **安装** `.update-staged/`（可执行文件 + `plugins/`）到程序目录。换 exe 时**先把旧文件改名成
   `Keyrealm.old` 腾出路径**再放新的——Windows 上**运行中的 exe 不能被覆盖（会 `WinError 32`）但可以改名**，
   而交棒进程自己就跑在这个 exe 上，所以绝不能直接覆盖。`data/` 与 `plugins/` 之外的原文件不动。
7. 读**新源码**的 `SCHEMA_VERSION` 与 `APP_VERSION`，逐级迁移数据库。
8. 重启（源码部署重启 `python main.py --gui`；二进制部署重启构建出的可执行文件）。

> **需要能构建**：本机要有 **git**、**Python 3.11+**、**Node.js 18+**。缺哪样都会明确报出来。
> 只想「下载即用」而不想装这些，用**正式版**。

---

## 4. 数据库逐级迁移

结构与接口详见 **`scripts/migrations/README.md`**。要点：

- 迁移脚本命名 `m_<from>__to_<to>.py`，**只提供「低一级 → 高一级」**（如 `m_1.0.0__to_1.1.0.py`），
  每个暴露 `FROM_VERSION`/`TO_VERSION`/`preview(conn)`/`apply(conn)`。
- 运行器把链**一层层串起来**升（1.0.0→1.1.0→1.2.0），**绝不跨级**；缺链会点名缺哪一跳。
- 脚本可从**两处**取：源码树的 `scripts/migrations/`，以及**下载包里的 `.update-meta/migrations/`**
  ——后者让「正式版更新」（本机没源码）也能迁移。
- 目标版本：正式版取包内 `.update-meta/version.json` 的 `schema_version`；测试版取**拉取后新源码**的。
- 每跳前留 SQLite 快照；迁移失败回滚该跳。
- **改了数据库结构必须**：① 写 `m_<old>__to_<new>.py`；② 提升 `backend/version.py` 的 `SCHEMA_VERSION`。

> **凭据数据格式的变化**（如某插件字段拆并）**也算数据结构变化**：同样提升 `SCHEMA_VERSION`、同样在
> `scripts/migrations/` 留一跳脚本（哪怕表结构没动）。只是那一跳的**数据部分**做不到交棒阶段——
> 凭据敏感字段**加密**、那时没有主密码——由 `backend/data_upgrades.py` 在**解锁后**完成（判据就是
> `SCHEMA_VERSION`）；**导入备份**在落库前走同一套规则。插件只描述**当前**格式、不写双格式兼容。

---

## 5. 备份

- **内容**：可执行文件 + `plugins/` + `data/`（数据库走 SQLite 一致性快照）。
  **另外还随包带一份「全量导出」`data/keyrealm-full-backup.json`**（就是「备份」页那份整库备份：
  凭据 / 分组 / 代理 / 预设 / 定时计划 / 设置 / 保险库 KDF / MCP / 插件启停）——**原库损坏或丢失时，
  靠它也能把数据还原回来**（在「备份 → 导入」里选它，来自别的保险库时会再问当时的主密码）。
  **排除** `data/logs/`、`data/installed_plugins/` 与 **`data/webview/`**（后者是 WebView2 的
  用户数据目录，运行时被 WebView **独占**，一读就 `Permission denied`；它只是可再生的缓存）。
- **读不了的文件会被跳过而不是让备份失败**：更新前的备份失败会**直接挡住更新**，而读取失败
  多半只是文件被运行中的程序占用——跳过的条目会**如实汇总**在「已备份」的提示里（`N 个文件被占用已跳过`）。
- **位置**：`data/backup/keyrealm-backup-<渠道>-<版本>-<时间戳>.zip`，**只保留一份**（写新备份前删旧的）。
  应用内更新在**准备阶段**就备份（因此准备失败或取消时也会留下一份 zip）。
- **⚠ 含真实密钥**：`data/` 里有 `credentials.db` 与明文密钥文件；备份 zip 因此**含明文**，
  以 `0600` 落盘、目录已被 `.gitignore` 忽略，请自行妥善保管。
- **恢复**：`scripts/build_update.sh --rollback` 会把备份解压覆盖回程序目录；也可手动解压。

---

## 6. 命令行脚本

逻辑只有一份（`backend/updater.py`），`.sh` / `.bat` 只是**薄引导**（找 Python；缺 `.venv` 就
自动建并装依赖，全程不问话）。**每个脚本都会把本次运行输出另存一份到 `<data_dir>/logs/<脚本名>-<时间戳>.log`**
（跨平台：记在 Python 入口，`.bat` 无需 `tee`）。

### 6.1 CLI 自更新（`Keyrealm-CLI self-update`）

CLI 版不需要打开浏览器或窗口也能更新自己：

```bash
Keyrealm-CLI self-update check                  # 正式版
Keyrealm-CLI self-update check --test           # 测试版（main）
Keyrealm-CLI self-update status                 # 最近一次更新状态
Keyrealm-CLI self-update install                # 准备 + 交棒，自动重启
Keyrealm-CLI self-update install --no-restart   # 只替换，不自动重启
Keyrealm-CLI self-update install --test         # 从源码构建 CLI 测试版
```

- 正式版按当前构建选择 **`-cli` 资产**；测试版构建时自动带 `--cli`。
- `install` 在**当前进程内同步准备**（备份 + 下载/构建），成功后 `begin_handoff()` 起脱离进程；
  CLI 退出，交棒进程替换程序并按原参数重启。
- 更新期间占住单实例锁；桌面端 / 另一个 CLI 实例在跑时会拒绝。
- 一次性 `--data-dir` 也会传给更新进程（`_self_update_argv()` 显式追加），否则它会去默认目录找
  请求与准备产物。
- 可选 `--password` / `KEYREALM_MASTER_PASSWORD` 解锁已保存的更新令牌、并让备份带上全量导出；
  也可直接设置 `KEYREALM_UPDATE_TOKEN`。

| 能力 | Linux/macOS | Windows | 底层脚本 |
|---|---|---|---|
| 更新（备份→下载/构建→迁移→重启） | `scripts/build_update.sh` | `.bat` | `build_update.py` |
| 数据库更新（逐级） | `scripts/db_update.sh` | `.bat` | `db_update.py` |
| 程序重启 | `scripts/restart.sh` | `.bat` | `restart.py` |
| 版本检查（无头 CLI，便于脚本/CI 消费） | —（直接 `python scripts/check_version.py`） | — | `check_version.py` |
| CLI 自更新（检查 / 安装 / 状态） | `Keyrealm-CLI self-update check` / `install` / `status` | 同左 | `backend/cli.py` + `updater.py` |

> **「检查更新」没有 shell 包装**：它已由应用内承担（应用进程内跑）。`check_version.py` 保留为
> **纯 Python 无头 CLI**（`--json` 便于自动化），不再配 `.sh`/`.bat`。

```bash
python scripts/check_version.py --kind formal    # 或 --kind test；--json 便于脚本消费
scripts/build_update.sh --kind formal            # 正式版：自行下载预编译包（回落完整流程）
scripts/build_update.sh --kind test              # 测试版：自行拉源码 + 构建
scripts/build_update.sh --dry-run                # 只打印将执行的步骤（含「是否使用准备产物」）
scripts/build_update.sh --no-restart --no-frontend
scripts/build_update.sh --rollback [<zip>]       # 从备份恢复
scripts/db_update.sh --list                      # 版本与迁移链
scripts/restart.sh --wait
```

> **应用内 vs 命令行**：应用内「立即更新」是 **准备（应用内，带进度）+ 交棒**；`build_update.sh`
> 是一次性**完整流程**（自行下载/拉源码/构建）。两者都可用——前者体验好，后者适合无人值守。
> 离线自测：`KEYREALM_UPDATE_REMOTE` 指向本地 bare 仓库即可，不联网也能验证检查 / 更新逻辑。

---

## 7. 更新时产生的文件

| 路径 | 作用 | 是否入库 |
|---|---|---|
| `data/backup/*.zip` | 更新前备份（含密钥）；`data/` 均指**当前数据目录** | 忽略（`data/`） |
| `data/backup/_asset-*.zip` | 正式版下载的预编译包（就位后即删） | 忽略（`data/`） |
| `data/keyrealm-full-backup.json` | 更新前备份里随包的**全量导出**（原库损坏也能还原） | 忽略（`data/`） |
| `.update-staged/` | 测试版**准备阶段**的构建落点（交棒时安装进程序目录，装完即清） | 忽略（`.update-staged/`） |
| `.update-meta/` | 从预编译包解出的 `version.json` + `migrations/`（正式版更新后才有） | 忽略 |
| `data/.update-request.json` | 应用交给交棒进程的请求（含 `prepared` 指针） | 忽略（`data/`） |
| `data/.update-prepared.json` | **准备产物清单**（版本 / 包路径 / sha256 / staging / 目标 schema） | 忽略（`data/`） |
| `data/.update-status.json` | 更新进度与结果（含 `from`/`to` 版本与 `from_commit`/`to_commit`、逐阶段 `report`），重启后由新应用读取展示 | 忽略（`data/`） |
| `data/.update-check.json` | **最近一次「检查更新」的结果**（持久化：换页 / 重启后界面仍显示，直到被新结果替换） | 忽略（`data/`） |
| `data/logs/update-*.log` | 交棒进程（POSIX）无终端时的输出 | 忽略 |
| `data/logs/build-*.log` | 构建输出（排障用） | 忽略 |
| `data/logs/<脚本名>-*.log` | 命令行脚本每次运行的输出 | 忽略 |
| `Keyrealm` / `Keyrealm.exe` / `Keyrealm.old` | 构建产物与回滚副本。**换 exe 时旧文件先改名成 `<exe>.old`**（更新与构建共用这一个名字，同目录只留一份） | 忽略（`Keyrealm*`） |

---

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| 报「无法访问远端仓库」/「Could not resolve host」 | 本机访问 GitHub 需要代理。在「设置 → 更新 → 更新代理」里**选一个代理**后重试；或用 `KEYREALM_UPDATE_PROXY` 临时覆盖。 |
| 报「无法访问远端仓库」/「could not read Username」 | 匿名被 GitHub 拒——多半是**仓库又转回了私有**，或令牌已失效。到「设置 → 更新 → 访问令牌」填一个 PAT（Contents: read）后重试；错误提示本身也会指向这里。 |
| 正式版报「远端还没有正式版标签」 | 仓库还没打过 `vX.Y.Z` 标签 / 没发过 Release。 |
| 正式版报「读取 Release 失败（HTTP 404）」 | 该 Release 需要认证才能取资产（仓库为私有），**没填访问令牌**或令牌权限不足（需 Contents: read）。公开仓库走公开下载 URL，不会因此 404。在「设置 → 更新 → 访问令牌」填/换一个 PAT 后重试。 |
| 正式版报「该 Release 里没有 …zip」 | 认证成功但该 Release 里没有本平台/架构的包（CI 只出了 Windows 与 macOS）。 |
| 测试版报「未安装 git」/「创建虚拟环境失败」 | 测试版要从源码构建：需要 git、Python 3.11+、Node.js 18+。缺哪样按提示补。 |
| 测试版更新时本地改动被丢弃 | 更新**默认直接拉最新、丢弃工作区改动**（部署目录里的改动多半是构建/依赖留下的痕迹，典型是 `npm install` 改写的 `frontend/package-lock.json`）。想保留就先 commit，或用 `scripts/build_update.sh --kind test --force`（先 `git stash` 再合并）。**未跟踪 / 被忽略的文件（`data/` 等）不受影响。** |
| 应用内「立即更新」正准备时**失败/取消** | 这是**准备阶段**（应用进程内），**程序不会退出**，错误显示在卡片上，可重试；也可改用 `scripts/build_update.sh` 一次性补完。 |
| 准备完成后**交棒失败**（更新进程没起来） | 应用不退出。正式版用 `scripts/build_update.sh --kind formal`、测试版用 `--kind test` 手动补完（回落流程会补做替换/构建/迁移）。 |
| 更新进程在跑但看不到进度 | 交棒阶段应用已退出，进度看 **`data/logs/update-*.log`**（Windows 也一样，不再弹控制台窗口）；结论在重启后的「上次更新」报告里。 |
| **更新后什么都没发生、版本还是旧的** | 逐条看：① **`data/logs/update-*.log`**——交棒进程**每个里程碑都打一行**（启动横幅 / 等待退出 / 应用已退出 / 各阶段 / 结论），失败原因也在里面；② **`data/.update-status.json`**——`phase`/`message` 说明停在哪一步（如「等待应用退出超时」＝程序没完全退出）；③ 应用内**「设置 → 更新 → 上次更新」**，失败会在下次启动时**弹通知**。全程**不会**静默：失败也保证不替换任何文件。 |
| 替换时报 **`WinError 32`（另一个程序正在使用此文件）** | 交棒进程**自己就运行在待替换的 `Keyrealm.exe` 上**，而 Windows 下**运行中的 exe 不能原地覆盖**。程序已改为**先把旧 exe 改名成 `Keyrealm.old` 腾出路径**再放新的（改名允许），正常不再报此错。若仍报，多半是**别处的残留进程**（没退干净的旧实例 / 杀毒软件锁定）占着 exe：关掉所有 Keyrealm 进程与相关杀软后重试，或用 `scripts/build_update.sh --kind test` 手动补完。 |
| 重启时报 **`Failed to execute script 'pyiboot01_bootstrap'`** / **`_MEI…\base_library.zip` 不存在** | PyInstaller onefile 的坑：父进程把临时解压目录经 **`_MEIPASS2`** 交给子进程，子进程会**复用**同一份 `_MEI`，而父进程一退出就把它删了——新 exe 再去读 `base_library.zip` 就 `FileNotFoundError`。已修：所有脱离子进程都带 **`PYINSTALLER_RESET_ENVIRONMENT=1`**（`updater._PYI_ENV`），让新 exe 解压自己的一份。**遇到此错的老版本：手动双击 `Keyrealm.exe` 即可启动**（手动启动不继承这个变量），再更新到含修复的版本即可。 |
| 迁移报「迁移链断档」 | 目标结构版本与现有脚本之间缺一跳。补齐 `m_<缺的from>__to_*.py` 或核对 `SCHEMA_VERSION`。 |
| 迁移报「检测到应用正在运行」 | 应用没完全退出。先退出应用再 `--apply`。 |
| 重启后没起来 | 看 `data/logs/relaunch-*.log`；或手动 `scripts/restart.sh`。 |

---

## 9. 边界与已知限制

- **正式版**要求 GitHub 上已发布**当前平台**的预编译包；目前 CI 只出 **Windows（x64）与 macOS
  （Apple Silicon / arm64）**；**macOS Intel（x64）与 Linux 暂不出**（Linux 的 pywebview 依赖
  GTK/WebKitGTK，打包成本高）。
- **测试版**要求本机能构建（git + Python + Node）；从 Releases 装、机器上又什么都没有时，只能走正式版。
- **macOS 预编译包未签名**：首次打开会被 Gatekeeper 拦（右键→「打开」，或 `xattr -dr com.apple.quarantine <文件>`）。
- **Windows** 需 WebView2 Runtime（Win10/11 通常自带）。
- **备份含明文密钥**（见 [§5](#5-备份)）。
- Windows 的 `.bat` 与真机端到端未在开发机（Linux/WSL）实测；首次用请按 [§10](#10-windows-手工验收清单首次使用) 验收。

---

## 10. Windows 手工验收清单（首次使用）

1. `py -3 scripts\_bootstrap.bat` → 建出 `.venv\Scripts\python.exe` 并装好依赖。
2. `scripts\build_update.bat --dry-run` → 打印将执行的步骤，中文不乱码。
3. 启动应用 → 设置 → 更新 → **检查更新** → 有更新时点**立即更新**。
4. 确认**准备阶段在应用内进行**（卡片显示进度条、可取消）；准备完成后**弹窗询问是否立即重启**，
   选「立即重启」后程序关闭，**不再弹控制台窗口**，脱离进程完成替换/迁移/重启（输出落 `data\logs\update-*.log`）。
   选「稍后」时程序保持运行；再次点更新应复用本地包/构建产物（不重复下载/构建）。
5. 构建/替换产物 `Keyrealm.exe` 落在**仓库根**；`backups\` 里有且仅有一份 zip（正式版就位前还会短暂出现 `_asset-*.zip`）；测试版的构建落点在 `.update-staged\`。
6. 更新完成后程序**自动重启**；设置页「上次更新」显示**逐阶段报告**（✓/✗ + 日志路径）。
7. 应用运行期间再跑一次 `scripts\db_update.bat --apply` → 应被单实例闸门拒绝。
