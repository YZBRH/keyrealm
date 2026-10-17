# 插件开发指南

Keyrealm 的插件用于**验证一类凭据的有效性**（如某家 LLM 厂商的 API Key）。
每个服务是一个独立插件，放入 `plugins/` 目录即自动加载，删除即卸载。没有安装器、也没有
「插件商店」——`plugins/` 就是插件的**唯一事实来源**；「插件管理」页只做一件事：
**禁用 / 启用**插件（状态写 `plugins/.state.json`，见 §1.1）。要彻底卸载某个插件，自己删掉
`plugins/<类别>/<服务>/` 目录即可——**凭据保留**，插件记录与状态里的残留 key 由下次
`sync_plugins()` 扫描清理。

---

## 1. 目录结构

```
plugins/
├── llm/                        # 类别目录（LLM 模型）
│   ├── category.json           # 类别元数据：{"name": "LLM 模型", "icon": "🤖"}
│   ├── llm_common.py           # 类别共享模块：仅本类别特有的辅助（额度/欠费探测）
│   ├── llm_protocols.py        # 协议级工具（探针 / 简单对话等），供子插件按需复用
│   ├── openai/                 # 一个服务 = 一个独立子插件
│   │   ├── manifest.json
│   │   └── plugin.py
│   ├── deepseek/
│   │   ├── manifest.json
│   │   └── plugin.py
│   └── …（共 18 个子插件）
├── api/                        # 类别目录（API 服务）
│   ├── category.json
│   ├── firecrawl/
│   ├── tavily/
│   ├── metaso/
│   ├── coze/
│   ├── dify/
│   ├── fofa/
│   └── google_api_credentials/
├── dns/                        # 类别目录（域名解析）
│   ├── category.json
│   ├── dnspod/
│   ├── duckdns/
│   └── godaddy/
├── dev/                        # 类别目录（开发者平台）
│   ├── category.json
│   ├── github/
│   ├── huggingface/
│   ├── npm/
│   └── wandb/
├── im/                         # 类别目录（即时通讯）
│   ├── category.json
│   ├── feishu/
│   ├── dingtalk/               # 应用 / Webhook 双形态
│   ├── wecom/                  # 企业微信，应用 / Webhook 双形态
│   ├── telegram/
│   ├── discord/
│   ├── qq_bot/
│   ├── wechat_ilink/
│   ├── telegram_apihash/
│   ├── wechat_mp/              # 微信公众号（AppID + AppSecret）
│   └── wechat_miniprogram/     # 微信小程序（AppID + AppSecret）
├── cloud/                      # 类别目录（云服务商）
│   ├── category.json
│   ├── google_sa.py            # 协议级工具：服务账号 JWT 换 token（gcp / firebase 共用）
│   ├── aliyun/
│   ├── tencentcloud/
│   ├── aws/
│   ├── gcp/
│   ├── huaweicloud/
│   ├── firebase/
│   ├── jdcloud/                # 京东云（签名器 jdcloud_signer.py）
│   ├── ucloud/                 # UCloud（签名器 ucloud_signer.py）
│   ├── baiducloud/             # 百度智能云 BCE（签名器 bce_signer.py）
│   ├── volcengine/             # 火山引擎（签名器 volc_signer.py）
│   ├── ctyun/                  # 天翼云 EOP（签名器 eop_signer.py）
│   └── cloudflare/             # Cloudflare（API Token / Email + Global API Key）
├── map/                        # 类别目录（地图）
│   ├── category.json
│   ├── amap/                   # 高德地图 Key（Web 服务 / JS API / iOS / Android / 小程序）
│   ├── baidu/                  # 百度地图服务端 AK（+ 可选私钥 SK）
│   └── tencent/                # 腾讯位置服务 WebService Key（+ 可选 SK 签名）
├── exchange/                   # 类别目录（交易所）
│   ├── category.json
│   ├── okx/
│   └── binance/
├── mail/                       # 类别目录（邮箱 / 短信）
│   ├── category.json
│   ├── mail_common.py          # 类别共享模块：仅本类别特有的辅助（SOCKS 解析、TunneledSMTP/IMAP）
│   ├── smtp/                   # 通用 SMTP 邮箱
│   └── gateway/                # 短信网关 / 接码服务商
└── my_flat_plugin/             # 也支持根级平铺插件（无类别）
    ├── manifest.json
    └── plugin.py
```

规则：

- 启动时 `sync_plugins()` 扫描 `plugins/` 两层结构：
  - 根级含 `manifest.json` 的目录 → 平铺插件（`category` 为空，前端归入"其他"）；
  - 否则视为类别目录，其下含 `manifest.json` 的子目录为子插件，
    `category.json` 提供类别显示名与图标。
- **一个服务就是一个插件**。删除某个子插件目录不影响同类别其他子插件运行；
  共享模块（`llm_common.py` 等）只是被各子插件 `import` 的普通 Python 文件。
  **通用工具已上收框架层的 `backend/plugin_sdk.py`**（结果类型、HTTP 客户端、代理、超时、
  解析/兜底辅助等），类别模块只保留**本类别特有**的内容；具体验证逻辑在各子插件内。
- **插件私有的辅助模块必须带厂商前缀命名，别用通用名**（如写 `bce_signer.py`，
  不要写 `signer.py`）。框架加载插件时会把**插件自身目录与它的类别父目录**临时插进
  `sys.path`，同类别下多个插件各带一份**同名**模块时，`sys.modules` 里先加载的那份会一直
  霸占该名字，后加载的插件会拿到**别人的实现**——算法不同也不报错，只会算出看不懂的错误码
  （如签名恒失败）。名字唯一后这类冲突从根上消失，框架也不会再打「辅助模块与已加载的
  同名模块冲突」的警告。**并且要按显式文件路径加载**（`importlib.util.spec_from_file_location`
  + 基于 `__file__` 的绝对路径）而不是裸 `import`：框架会临时注入插件目录，但直接 `exec`
  插件文件的调用方（如 `tests/`）只注入到类别目录，裸 `import` 在那里会失败。
- 名称以 `_` 或 `.` 开头的目录会被跳过（可用于临时禁用）。

### 1.1 启用 / 禁用：`plugins/.state.json`

插件**只以 `plugins/` 为准**，没有第二份来源。启用/禁用**不改动磁盘目录**，只改一个状态文件。

**位置与结构**：`plugins/.state.json`，形如

```json
{"disabled": ["llm_groq", "api_shodan"]}
```

键是插件的 `credential_type`。

**读取约定**（`sync_plugins()` 每次扫描时读）：

- **文件不存在 = 全部启用**（出厂态：发行包不带该文件，见 `scripts/build.py`）。
- **文件损坏同样退化为全部启用**——非法 JSON、根不是对象、`disabled` 不是数组，一律当作
  「一个都不禁用」，**绝不显示空列表**（宁可多显示，也不能因为一个坏文件让插件整片消失）。
- 目录被删掉后，状态文件里对应的 `credential_type` 会在下次扫描时**自动清理**。

**写入约定**：先写 `plugins/.state.json.tmp`，再**原子替换**成 `.state.json`——避免写到一半崩溃
留下半截文件（半截文件虽然也能被上面的兜底救回，但仍不该产生）。

**与数据库的关系**：`sync_plugins()` 把状态文件**镜像**进 DB 的 `plugins.enabled` 列——该列只是
**只读镜像**，**不再是功能闸门**；`get_plugin_by_type()` **不按 `enabled` 过滤**。真正的闸门是
`plugin_manager.get_module()`：插件被禁用即返回 `None`，于是它的 `has_actions` 为假、
`plugin_actions` / `plugin_models` 返回 `[]`、`plugin_capabilities` 返回
`{"available": None, "note": "", "error": True}`、`run` / `stream_plugin_action` 返回
`{"ok": False, "error": …}`、`get_display_summary` 返回 `""`、`plugin_uses_proxy` 返回 `False`、
`verify_credential` 返回 `status="error"` 且 message 为 `插件已禁用: <credential_type>`。

**手工编辑**：`.state.json` 可以手工改，**改完重启应用生效**（只有扫描时才读它）。

**界面行为**：插件被禁用**不等于未安装**——其名下凭据**照常显示、可编辑、可删除**，也能随备份
正常导入/导出（见 `backend/backup.py`）；只是该插件的**「操作」「验证」按钮置灰**（悬停提示
「插件已禁用」），且**批量验证自动不含**它的凭据。

禁用还带来三处**显示层**变化，全部由前端按插件启用状态**实时派生**，**不写进库**：

| 位置 | 表现 |
|---|---|
| 凭据列表「状态」列 | 显示为**「插件禁用」**（压过库里的 `status`），悬停说明缘由；排序时排在**最后**（它是「暂时动不了」，不是「待处理」）。前端 `Credentials.vue` 的 `effectiveStatus()` 派生 |
| 「添加凭据」 | **不允许**给已禁用插件加新凭据：点了给提示且不打开表单（按钮**不置灰**——给反馈比闷着不动强）；若目录树选中的是**类别**，默认服务会跳过类别下的已禁用服务 |
| 「编辑凭据」 | **照常可编辑**：编辑表单的字段（`fields_schema`）取自 `pluginByType`（**含已禁用插件**），已存的值照常带出。这里必须用 `pluginByType` 而非「仅已启用」列表，否则插件查不到 → 表单一个字段都不渲染，看起来就是「凭据值没带出来」 |

> **为什么派生而不入库**：启用/禁用只是 `.state.json` 里的一个开关（可随时来回切），若把
> `plugin_disabled` 写进 `credentials.status`，重新启用后这些凭据就永远停在「插件禁用」——成了
> 洗不掉的脏数据。按插件启用状态实时算，**重新启用即自动回弹**到原值。
>
> 「状态」**筛选**下拉里**没有**「插件禁用」这一项：该下拉的取值是直接递给后端
> `list_credentials(status=…)` 做 SQL 过滤的，而派生状态在库里查不到，加进去只会永远筛出 0 条。

> 后端接口是 `set_plugin_enabled(credential_type, enabled)`（`backend/api.py` + `main.py` 转发），
> 只推 `vault:plugins_changed` 事件；旧的 `delete_plugin` / `list_available_plugins` / `install_plugin`
> **已删除**。

---

## 2. manifest.json

```json
{
  "name": "DeepSeek",
  "version": "1.0.0",
  "credential_type": "llm_deepseek",
  "author": "Keyrealm",
  "description": "验证 DeepSeek API Key 有效性",
  "icon": "🐋",
  "batch_verify": true,
  "fields": [
    {
      "key": "base_url",
      "label": "Base URL",
      "type": "url",
      "required": false,
      "placeholder": "留空使用默认地址",
      "default": "https://api.deepseek.com/v1"
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "sensitive": true,
      "placeholder": "sk-..."
    },
    {
      "key": "notes",
      "label": "备注",
      "type": "textarea",
      "required": false
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `credential_type` | **插件身份，全局唯一**。它是数据库插件表的 UNIQUE 键、模块缓存键、凭据与插件的关联键。同类插件约定前缀：`llm_<厂商>`、`api_<服务>` |
| `fields[].type` | `text` / `url` / `password` / `textarea` / `select`（配 `options`）/ `number` / `checkbox` / `model`（**可下拉可自由输入**的模型名，候选来自 `list_models`，取不到也能手打） |
| `fields[].options` | 仅 `select`。两种写法都行：纯字符串数组 `["openai","anthropic"]`（值与显示都用它）；或对象数组 `[{"value":"chatgpt","label":"ChatGPT 登录态"}]`（**显示 label、存 value**）。前端统一归一（`utils/format.normalizeOptions`），需要给用户看中文时用对象写法，**不要把中文当作值存库** |
| `fields[].show_if` | 可选。**条件显隐**：`{"另一个字段key": 值 或 [值,…]}`，只有那个字段的当前值命中时才渲染本字段。用于「同厂商不同模式」——例如 `openai` 的 `auth_mode` 选 `api_key` 时只显示 API Key 那组、选 `chatgpt` 时只显示登录态那组。前端按当前表单值实时求值（`utils/format.visibleFields`），**保存时未命中字段的值不提交**（免得切模式后另一组的密钥还留在库里）。 |
| `fields[].presets` | 可选。**端点候选**：`[{"label":"阿里云百炼 · 新加坡","value":"https://dashscope-intl.aliyuncs.com/compatible-mode/v1"}]`，用于 `url`（Base URL / 服务端点）或 `text`（SMTP 服务器）字段。前端渲染为**可自由输入 + 下拉补全**的 `el-autocomplete`（`utils/format.matchPresets`），字段没有 `presets` 时仍是普通输入框。候选值必须来自厂商**官方文档**（区域 / 国内外站 / 兼容路径），不要填中转站或用户自建地址 |
| `fields[].preset` | 可选，是否允许作为「类别/插件默认设置」被预设。**默认允许**（只要非敏感）；填 `false` 可排除该字段。**敏感字段永远不可预设**（默认值表是明文列） |
| `fields[].hint` | 可选，字段下方的一行灰字说明。用于解释开关或高级项（如 LLM 的「余额未知时发一次对话探测」），前端 `CredentialForm.vue` 渲染为 `.field-hint` |
| `fields[].sensitive` | `true` 的字段加密存储（AES-256-GCM），列表页只显示掩码（`crypto.mask_secret`，形如 `sk-123…cdef`），也参与重复凭据检测 |
| `icon` | 服务显示图标。填 emoji（如 `🐋`）直接使用；也可填插件目录内的图片文件名（如 `"logo.png"`，支持 png/jpg/svg/webp/ico/gif），后端读取为 data URL 返回（`icon_image` 字段），前端优先展示图片，缺省回退 emoji `🔑`。图片形态下后端还会附一个 `icon_lift`（bool）——深色主题下该 logo 是否需要垫一层浅色底，由 `backend/icon_ink.py` 自动判定（位图取不透明像素 `max(r,g,b)` 的 p90、SVG 取 `fill`/`stroke` 声明的颜色，纯黑标为真），**作者无需关心** |
| `batch_verify` | 是否参与批量验证，默认 `true`。**有写副作用**的插件应设为 `false`（如 `duckdns`——它的校验本身就是一次写解析）：前端在选中该类型时禁用「批量验证」按钮，只能逐条手动验证；后端批量时也会跳过这些类型并在结果里统计「跳过 N」 |

> **提示文本按纯文本渲染**：`description` 与各字段的 `label` / `placeholder` / `hint` 前端**不解析 markdown**，
> 直接写中文即可。写了 `**加粗**` 会在解析 manifest 时被自动剥成纯文本（`backend/plugin_base.plain_text`），
> 别指望它变粗；glob 形式的 `**`（如 `**/*.py`）不受影响。

> **插件只描述「当前」格式，不写数据兼容**：字段改了形态（拆开 / 合并 / 改名）时，**不要在
> `verify` / `validate` 里做双格式容忍**（例如"字段里出现 `|` 就拆开"）。这属于**数据结构变化**：
> 按 `backend/version.py` 的规矩**提升 `SCHEMA_VERSION`** 并在 `scripts/migrations/` **留一跳脚本**
> （哪怕表结构没动）；老数据由 `backend/data_upgrades.py` 的规则在**导入备份**与**更新后解锁时**
> 转成当前格式，插件只认当前形态即可。

> **动了插件就要提升插件版本号**：改完 `manifest.json` / `plugin.py` 记得把 `version` 一起升
> （如 `1.0.0 → 1.1.0`）——它是用户与排障时唯一能看出「这个插件是否已更新」的依据。
> 改完重跑 `scripts/gen_mock_assets.py`（顺带当一次加载冒烟测试）。

> **插件 logo 的来源与许可**：内置插件的品牌图标是**随包分发**的第三方商标图形，取自
> [Simple Icons](https://simpleicons.org/)（CC0-1.0）、Iconify 的 `thesvg` 集合（MIT）、
> **App Store 官方图标**（中文区 `itunes.apple.com/search?country=cn`，取 `artworkUrl512` 的高分辨率版；
> 扣子 / 飞书 / 企业微信等中文服务多由此取源）、**厂商官方品牌包**（如微信的 `wechat.design` 品牌资源 zip，
> 公众号 / 小程序图标即由此取源）、以及厂商官网 / 官方仓库的原始素材
> （官网 `<link rel="icon">` 的 favicon，或官网静态资源目录里的原始 SVG/PNG——如 W&B 取自
> `site.wandb.ai` 的 favicon、腾讯 QQ 取自 `static-res.qq.com` 的 `qq-logo.png`、
> Hugging Face 取自官网仓库的彩色版 `huggingface_logo-noborder.svg`、
> 京东云 / UCloud / 百度智能云 / 火山引擎 / 天翼云取自各自官网静态资源里的原始矢量）；
> 商标归各自权利人所有，此处仅用于标识对应服务。
>
> 选图有四条硬要求（渲染位只有 **16px**（凭据树选择器）与 **22px**（插件管理）两种，见
> `CredentialTreePicker.vue` / `PluginManager.vue`）：
> 1. **方形单色标优先**——横向 wordmark（如 `Dify`、`OKX` 的官方字标）缩到 16px 会糊成一团；
> 2. **必须能独立渲染**——不要在 SVG 里用 `currentColor`：图标是以 `<img src="data:image/svg+xml;base64,…">`
>    渲染的，没有 CSS 继承，`currentColor` 只会解析成黑色。（颜色写在 `<style>` / `class` 里**本身没问题**，
>    `icon_ink` 会连 CSS 声明一起解析；`<img>` 不会继承页面样式。）
> 3. **不留烤死的白底 / 白边**——深色主题下白底会变成一块白砖（白底烤进位图即 100% 不透明，
>    如 `wecom`），白边则是低分辨率圆角砖硬缩放留下的晕（如 `feishu`，源图仅 48px）。若源图只有
>    「彩色图形 + 白底」，不能直接按亮度设 alpha（那会把图形内部也判成半透明、整块发灰），要按合成
>    模型 `observed = ink·a + 255(1−a)` **解出 a 再把墨色反解回来**，并**把墨色向透明邻域外扩一圈**
>    （alpha 不动）——否则缩到 16px 时透明区的黑 RGB 会被插值污染，在深色底上留一圈深灰边
>    （图标本身的深色可见度另由 `backend/icon_ink.py` 判定）。
> 4. **位图长边压在 128px 以内**——渲染位最大 22px，640×640 纯属浪费（当初 20 张 640² 缩到 128² 后，
>    冷启动分析总耗时从 232 ms 降至 66 ms；**当前 64 插件 / 61 张 logo 实测约 76 ms**）。
>    位图一律缩到 **128×128**（LANCZOS，保 alpha）；
>    例外是原生就小的图（如 `qq_bot` 官方 favicon 只有 62×62 且无更高清源，原样保留，不做假放大）。

### 字段输入约定（新增插件必守）

**凭据输入一律尽量单行**：一个字段一个值，不要求用户「粘贴整份配置」。

- **能拆就拆**：邮箱 / 私钥 / 项目 ID 各占一格，不要用一个大 `textarea` 收一坨。
  硬约束来自前端渲染优先级——`CredentialForm.vue` 按 `text||url` → `password||sensitive` → `textarea`
  顺序判定，**`sensitive` 压过 `textarea`**：声明成 `textarea` 的敏感字段实际渲染的是**单行密码框**，
  「多行」与「加密存储」不可兼得。所以声明要与渲染一致：

  ```json
  { "key": "session_string", "type": "text", "sensitive": true }    // 对：单行 + 加密
  { "key": "session_string", "type": "textarea", "sensitive": true } // 错：声明与渲染不一致
  ```

- **不接收整段 JSON**：服务账号、各类 SDK 的 `*.json` 一律**取关键字段扁平化**，每个值单独一栏，
  由插件拼回协议要的形状。范例：`cloud/google_sa.py` 只收 `client_email` + `private_key`，
  自己还原成 PEM 再签 JWT。整段 JSON 塞一栏的代价：常量与展示字段跟着落库、与相邻字段重复
  且可能对不上、报错时说不清是哪个子字段错了。
- **单行框吃换行**：多行内容粘进单行框会被压成一行（PEM 私钥尤其明显），插件侧要**自己还原**，
  并在 `hint` 里写明。范例 `google_sa.normalize_pem`：先抹掉全部空白再按 64 列折行，于是
  「原样多行 / 压成一行 / 带字面 `\n` / CRLF」四种粘贴形态收敛到同一个结果；同时校验
  「私钥之外不该再有内容」，避免把整段 JSON 顺手存进来。
- **例外**：`notes`（备注）这类自由文本仍用 `textarea`——它不是凭据，没有加密与换行的约束。

---

## 2.1 验证代理

「系统设置 → 网络与代理」可配置验证代理（`http` / `https` / `socks5`），持久化到 settings 表。
应用启动时把它写入环境变量 `VAULT_PROXY`，各插件在发起请求时读取该变量（见 `backend/plugin_sdk.py` 的 `make_client()`）。
解析优先级：`VAULT_PROXY` > `ALL_PROXY`/`all_proxy` > `HTTPS_PROXY`/`https_proxy`；
若 `VAULT_PROXY` 由外部环境显式设置，则它优先于界面设置。socks5 需 `httpx[socks]`（`socksio`），已在 `requirements.txt` 声明。

**多代理与「按调用生效」的覆盖值**：代理现在可以配多个（各自取名，存 `proxies` 表）。
解析按优先级逐层找：**本条凭据 > 该插件预设 > 所属类别预设 > 全局默认**；
「跟随上层设置」= 本层不指定、继续往上找（改上层预设，已有凭据会跟着变）。
批量验证是并发的，所以不能靠改环境变量——
框架用 `backend/vault_proxy_ctx.py` 提供一条**线程隔离**的覆盖通道，启动时以固定别名注册进
`sys.modules["vault_proxy_ctx"]`。`backend/plugin_sdk.py` 的 `proxy()` 会**先查它**：

```python
def proxy():
    ctx = sys.modules.get("vault_proxy_ctx")     # 脱离主程序单独跑时取不到 → 退回环境变量
    if ctx is not None:
        value = ctx.current()
        if value is not ctx.UNSET:
            return None if value is ctx.DIRECT else value   # DIRECT = 本次强制直连，压过所有环境变量
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        ...
```

**子插件什么都不用做**：`make_client()` 签名没变，它会自动带上本次调用该用的代理。
框架在 `plugin_manager` 的五个入口（验证 / 权限探测 / 简易操作 / 模型列表 / 流式操作）设好作用域，
所以单条、批量、MCP、流式四条路径都正确且互不干扰。

> **每次调用都记一行「实际生效的出口」**（INFO，日志页可见）：
>
> ```
> 验证 llm_nvidia：代理出口 = socks5://127.0.0.1:10808
> 验证 llm_moonshot：代理出口 = 直连（未配置代理）
> 验证 llm_nvidia：引用的代理 #1 **已不存在**，已回落直连（请到「系统设置 → 网络与代理」重新选择，或改预设）
> ```
>
> 为什么专门记它：凭据里写着「用代理 #1」、实际却直连，这种错配在界面上**完全看不出来**
> （下拉里照样显示着代理名），表现就是「配了代理还是不生效」且查不到线索。
> **引用的代理已不存在时升级成 WARNING**——那是静默降级成直连最常见的原因
> （删除又重建过代理时，`proxies` 表的自增 id 不会复用，而预设/凭据里存的还是旧 id）。

> **打包时注意**：httpx 是在**函数体内** `import socksio` 的，构建机没装这个包时
> PyInstaller 既不报错也不会打包它，症状是 exe 里一用 socks5 代理就抛
> `the 'socksio' package is not installed`。因此 `scripts/build.py` **构建前会按
> `requirements.txt`（含 extras 展开，所以 `httpx[socks]` → `socksio` 会被查出来）核对环境**，
> 缺包就打印清单并中止，提示 `pip install -r requirements.txt`（`--allow-missing-deps` 可强行继续，
> 该 flag 会以 `CV_ALLOW_MISSING_DEPS=1` 透传给 spec）；
> **`Keyrealm.spec` 自己还会再核对一遍** socksio / PySocks / telethon / python-socks
> 这四个「静态分析看不到」的依赖，**缺一个即中止构建**（直接 `pyinstaller Keyrealm.spec`
> 同样会停；逃生口就是那个环境变量）。

注意：SMTP / IMAP 走的是**裸 TCP**，不吃 HTTP 代理那一套，但可以走 SOCKS / HTTP CONNECT 隧道
（依赖 PySocks，见 §2.6），所以邮箱同样支持「使用代理」。

**测试代理**：同一页有「测试代理」按钮，传当前输入框的值（可「先测再存」），调用
`backend/proxy_test.py` 做一次对照实验——同一批探测分别在直连与走代理下各跑一遍，比对
国内站点（baidu.com）可达性、国外站点（gstatic.com/generate_204）可达性与出口 IP 归属地，
综合给出「代理是否真的生效」的结论并在界面展示。排查「代理配了但插件仍走本地出口」时先用它。
详见 `docs/ARCHITECTURE.md` 第 2.4 节。

---

## 2.2 附加信息（account_info）

`verify` 返回的 `account_info` 会显示在凭据列表的「附加信息」列，前端按「标签 值」渲染。约定：

| 场景 | account_info 写法 | 展示效果 |
|------|-------------------|----------|
| 余额 | `{"balance": 12.34, "currency": "CNY"}` | 余额 12.34 CNY（保留 2 位小数） |
| 使用率（plan/额度制） | `{"usage_rate": 0.52}` | 使用率 52%（已用 ÷ 总额度，按服务适配） |
| 其它可读字段 | 如 `organization` / `domain_count` / `account_id` | `组织 Xxx` / `域名数 1` / `账号 ID …` |

- **LLM 类**：附加信息只展示**余额**；无余额概念的 plan 账号展示**使用率**。余额由**各厂商自己的 `plugin.py`** 按各自的余额端点 best-effort 查询（金额合理性闸见下条）——目前 DeepSeek / Moonshot 有余额接口，OpenRouter 用 `/credits` 计算余额与使用率；OpenAI 的 ChatGPT 登录态模式经实验性端点 `GET /backend-api/wham/usage` 读 5 小时窗的 `used_percent` 作为 `usage_rate`（付费套餐才查，取不到就不显示）；Anthropic / Gemini / 智谱等多数无公开余额接口，附加信息为空。
- **金额合理性闸**：one-api / new-api 类中转站在「不限额」时会把硬限额（`hard_limit_usd`）填成哨兵值（New API 官方写死 **`100000000`（1e8）**，且只在**令牌维度**出现——`token.UnlimitedQuota` 分支），拿它去减消耗就会得出 `99999900` 这种假余额。所有要展示的金额都必须先过 `llm_common.plausible_amount()`（有限数值且绝对值 < `AMOUNT_LIMIT = 1e7`），不通过**则不展示该金额**。新增带余额的厂商/中转插件时需一并加这道闸。
- **中转站**：`openai_compatible`（通用 OpenAI 兼容）与 `newapi`（New API 中转站专用）都走 one-api 兼容的 `/dashboard/billing/subscription` + `/dashboard/billing/usage`。New API 的 `hard_limit_usd` 是**令牌总额度（剩余 + 已用）**，`total_usage` 单位是**美分**，故 `余额 = hard_limit_usd − total_usage / 100`（依据官方 `controller/billing.go` / `channel-billing.go`）；命中哨兵值则按「不限额度」处理，改为展示 `套餐 不限额度` 而非数字。**两处官方源码确认的字段差异**：(a) `total_usage` 是**终身累计**、非本月（new-api 的 `GetUsage` **忽略** `start_date`/`end_date`，那是 OpenAI 原生接口的行为）；(b) `*_USD` 里的数字**不一定是美元**——官方按站点 `QuotaDisplayType` 三分支填 `USD`/`CNY`/`TOKENS`，故 `newapi` 在「`used > total`」（两字段不同单位的信号）时**不显示余额**，宁缺毋滥。
- **多条 = 多行**：前端把 `account_info` 摊成**每个字段一行**（不再「最多 2 条、用 ` · ` 拼一行」）。
  所以**每多回一个键，列表里就多一行**——只想要一行的信息，就只回一个键。
- **值是对象/数组的键前端会跳过**（渲染不出来）。想把数组里的东西展示出来，请在插件里**先摊平成标量**
  （如 Google 的授权域只回 `domain_count`，而不是把 `authorized_domains` 塞进去）。
- **没有中文标签的键会显示原始英文键**（如 `project_id proj-1`）。新增可读字段时，
  顺便往 `frontend/src/views/Credentials.vue` 的 `ACCOUNT_INFO_LABELS` 补一条中文标签。
- 无信息量的字段（`object` / `type` / `auth_mode` / 原始时间戳等）不展示，前端统一处理。
- **两个"只留账号"的现成范例**：`cloud/aws` / `cloud/aliyun` / `cloud/tencentcloud` / `cloud/huaweicloud`
  的 `verify` 都只回 `{"account_id": …}` 一项——ARN / UserId / domain_id 这类挪进 `whoami` 操作里展示。

---

## 2.3 验证超时

单个请求的连接/读取超时可调，入口在**「凭据管理 → 批量验证」对话框**（默认 20 秒，
范围 5–300 秒），持久化到 settings 表的 `verify_timeout`。与代理同一套机制：应用启动与
保存设置时把它写入环境变量 `VAULT_TIMEOUT`，插件侧读取——**插件只允许 `import backend.plugin_sdk`，
不得 import backend 的其它模块**，环境变量是超时值唯一的参数通道。

`backend/plugin_sdk.py` 的 `timeout(fallback=TIMEOUT)` 取代裸常量 `TIMEOUT`。`fallback`
是给**类别层**用的：邮箱类插件的默认超时是 15 秒而非 20 秒，`plugins/mail/mail_common.py`
只需 `def timeout(): return _sdk_timeout(TIMEOUT)`（`_sdk_timeout` 是 SDK 的 `timeout`），
不必重写整套「读环境变量」的逻辑。

```python
def timeout(fallback: float = TIMEOUT) -> float:
    """实际使用的超时秒数：VAULT_TIMEOUT（应用设置写入）> fallback。"""
    raw = os.environ.get("VAULT_TIMEOUT")
    ...
```

用法：`make_client()` 里 `httpx.Client(timeout=timeout(), ...)`、`guarded()` 的超时提示
`f"请求超时（{int(timeout())} 秒）"`、以及非 httpx 的插件（`mail_smtp` 用标准库
`smtplib`/`imaplib`，默认 15 秒）各自在连接处传 `timeout()`。

- `timeout()` **每次发请求时读取**，所以改完立即生效、无需重启；
- `VAULT_TIMEOUT` 由外部环境显式设置时优先于设置页的值（与 `VAULT_PROXY` 同约定）；
- 模块里的 `TIMEOUT` 常量退化为**默认值**，不再是唯一来源。源码见 `backend/verify_options.py`。

**单次请求可以覆盖全局超时**（`make_client(request_timeout=…)`、
`probe_openai_chat(..., timeout=…)`）：用于「一次 `verify` 里要连试多个候选、每个只给短超时」
的厂商——例如 NVIDIA 的模型清单是过期超集，单个模型名不可靠，于是按候选逐个试
（`plugins/llm/nvidia/plugin.py` 的 `_candidates` + `PROBE_TIMEOUT`）。候选名单**运行时候取**
（`llm_protocols.probe_model_candidates`，24h 缓存），**不再内置短名单**。

> ⚠️ 自己循环试候选时，**每个候选的超时（`httpx.TimeoutException`）与网络异常必须在插件内
> `try` 掉**。让它冒泡到 `@guarded` 会直接结束整次 `verify`——第一个候选挂死就白费了后面的候选。

---

## 2.4 欠费探测（quota_probe）

**问题**：`GET /v1/models` 这类探针只证明「Key 认证通过」，证明不了「账户有钱」。厂商没有
余额接口时（OpenAI / Anthropic / Gemini / 智谱 …），或中转站令牌是「不限额度」哨兵
（见 §2.2）时，凭据明明有效、实际一发对话就报欠费——界面却显示「凭证有效」。

**办法**：给这类凭据**可选地**补发一次 `max_tokens=1` 的最小对话请求，用真实计费路径换一个
确定结论。这是**凭据层面**的配置：LLM 插件的 manifest 里声明两个字段，用户在「新增/编辑凭据」
表单里逐条勾选，默认**关闭**（会真实消耗极少量额度）。

| manifest 字段 | 类型 | 含义 |
|---------------|------|------|
| `probe_chat` | `checkbox` | 余额未知时是否发一次最小对话探测，默认 false |
| `probe_model` | `model` | 探测模型（可下拉可手输）；留空 = 自动从模型列表挑一个便宜的 |

后端在验证通过、且 `account_info` 里**没有** `balance` 也没有 `usage_rate` 时，才会把机会交给
插件的 `quota_probe`（见 `backend/plugin_manager._apply_quota_probe`）。有余额的凭据不会再花
这一发请求。

**接入方式**

*通用协议*（OpenAI / Anthropic / Gemini）——子插件一行即可（`<对话协议>` 是
`openai` / `anthropic` / `gemini`，注意不是模块里的 `PROTOCOL` 常量）：

```python
from backend.plugin_sdk import VerifyResult, guarded
from llm_common import make_quota_probe
...
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")
```

*鉴权或端点非通用格式*——插件**自己实现** `quota_probe(data)`，复用 `llm_common` 提供的
读取与结论辅助函数即可，不必重复造轮子：

```python
from llm_common import probe_requested, probe_model, quota_ok, quota_insufficient
from llm_protocols import is_insufficient_balance

def quota_probe(data: dict):
    if not probe_requested(data):           # 该凭据是否勾选了 probe_chat
        return None
    ...                                     # 自己的鉴权 + 最小对话请求
    if 请求成功:
        return quota_ok()
    if 厂商错误码/文案表示欠费:
        return quota_insufficient()
    return None                             # 其它情况一律不下结论
```

实例：`plugins/llm/wenxin/plugin.py`——文心要用 API Key + Secret Key 两个字段换 `access_token`、
聊天端点还带模型后缀，通用实现拼不出来；另有一处特殊行为：**免费档模型（ernie-speed / lite / tiny）
不计费，欠费时照样能对话**，所以自动选型只挑付费档（从端点表现推，见 `_PAID_MODELS`）。

另一个实例：`plugins/llm/nvidia/plugin.py`——NVIDIA 没有余额接口，且**模型清单是过期超集**
（列着但已下线的模型会 `410`/`404`）。它的 `quota_probe` 复用 `verify` 的同一批**运行时候选**
（`probe_model_candidates`，24h 缓存）逐个试，谁通就用谁，全试完没结论就 `None`。

`quota_probe(data)` 的返回约定（后端消费，插件不必自己判断展示）：

| 返回 | 含义 |
|------|------|
| `None` | **没开启探测**（后端什么都不改）——只有这一种情况才该返回它 |
| `quota_ok()` → `{"account_info": {"chat_probe": "可用"}}` | 实测通过 →「附加信息」补一项「实测 可用」 |
| `quota_insufficient()` → `{"message": "..."}` | 实测发现欠费 → 替换状态说明（`status` 仍是 `valid`，欠费 ≠ 凭据失效） |
| `quota_unknown()` → `{"account_info": {"chat_probe": "异常"}}` | **试过但没跑通**（模型都不可用 / 鉴权 / 限流 / 网络）→ 补一项「实测 异常」 |

> **三种结局都要说出来。** 只回「可用」的话，探测失败时「附加信息」列什么都**不显示**——
> 用户勾了开关却看不到任何反馈，也分不清「没开探测」和「探测失败」。聚合站
> （MiniMax / OpenRouter / 华为云 MaaS / 商汤 / 智谱 / 中转站）上尤其明显：它们的模型表很大，
> 靠名字猜出的前几个候选未必真能对话，试满 3 个就放弃。
>
> 用「**异常**」而不是「未通过」：凭据的**状态本来就是「有效」**，同屏再出现「未通过」
> 自相矛盾（看着像凭据有问题）。「异常」说的是**探测这一步**没跑通，不涉及凭据本身。
> 具体失败原因（哪个模型、报了什么）写日志，不塞进界面上那一行。
>
> 另外：`quota_chat_probe()`（`llm_protocols`）**自己仍以 `None` 表示「无法判定」**——
> `nvidia` 那类自己遍历候选、靠真值判断「这个模型有没有结论」的调用方依赖这个契约。
> 把 `None` 翻译成「实测 异常」是 `make_quota_probe()`（`llm_common`）的职责。

判定细节（`llm_protocols.quota_chat_probe`，通用实现）：

- **模型选择**：凭据里指定的模型 > 自动挑便宜的（优先名字含 `flash`/`lite`/`mini`/`nano`/
  `haiku`/`turbo` 等，并排除 `embed`/`rerank`/`tts`/图像类、以及 `banana`（Google 的
  nano-banana 图像生成系列，名字里带 `nano` 会被误当成便宜聊天模型）；候选最多试
  **10** 个——在 OpenRouter 这类几百个模型的聚合站上候选太少极易全灭；模型不存在就换下一个。
- **「成功」的判据是「请求到达模型并正常返回」，不是「拿到了文本」**：`max_tokens=1` 的最小探测
  打给**推理型模型**时，那 1 个 token 会被思维链吃干净，`choices[0].message.content` 是空串。
  官方文档直接佐证——商汤写的是「**Reasoning content and output share the `max_tokens` quota**」，
  百炼与智谱的思考内容也都走 `reasoning_content`、且思考 token **先于**正文产生。
  所以 `chat_completion` 在「**信封完整**（`choices[0].message` 在）但文本为空」时返回
  `reached_model: True`，探测据此判**可用**——请求已经到过计费环节并成功返回，这就是确证。
  （否则这类账号会永远显示「实测 异常」，尽管其实际可用。）
- **欠费判定**走 `is_insufficient_balance()`：**先排除限流**，再认关键词/错误码。
  > 先排除的原因是**同一个词在不同厂商含义相反**：百炼把限流码写成 `Throttling.AllocationQuota`，
  > 而码里含 `insufficient_quota`；OpenAI 的 `insufficient_quota` 却是**真欠费**。
  > 中文侧**刻意不加泛词**：智谱 `1316/1317` 原文同时含「主账号**余额不足**」和「**限额**将在…
  > 重置」，把 `限额`/`使用上限` 当限流词会**误排除**一条真·余额问题。
  > 各家已核对的欠费码：百炼 `Arrearage` / `isv.OUT_OF_SERVICE` / `AllocationQuota.FreeTierOnly` /
  > `PrepaidBillOverdue` / `PostpaidBillOverdue` / `BudgetLimitExceeded`；智谱 `1113`；
  > MiniMax `1008`；商汤**没有**专门的余额码（额度耗尽与限流共用 `429 quota_exceeded_error`，
  > 官方自己都不区分，框架只能报「异常」而不是猜「欠费」）。
- **MiniMax 的 `base_resp`**：它的 200 里带 `base_resp`（官方：成功时 `status_code == 0`），
  **业务错误可能藏在 200 里**，只看 HTTP 状态码会漏判成成功；而它的 4xx 又是**另一套**信封
  `{"type":"error","error":{…}}`、数字码塞在 message 文本里（如 `(1004)`）。两种形状都要认。
  注意：`1039 Token 限制` 的 solution 逐字写着「请调整 `max_tokens`」——任何拿 `limit`/`token`/`quota`
  做欠费关键字的写法都会把它误报成欠费。
- **开关与模型从凭据数据里读**（`data["probe_chat"]` / `data["probe_model"]`），不经环境变量——
  数据本来就在插件手里。插件侧没有 `quota_probe` 属性时后端直接跳过，故**接入是可选的**。
- **仅支持流式的模型**：百炼有一批开源思考模型用非流式调用会直接回
  `parameter.enable_thinking only support stream call`。探测遇到它会**改用流式再试一次**
  （`_STREAM_ONLY_HINT`），否则这批模型的探测会一直停在「异常」，尽管它们实际可用。
- **`token_param`**：官方已把 `max_tokens` 标 deprecated、要求改用 `max_completion_tokens` 的厂商，
  由其插件显式传入——百炼、MiniMax、OpenAI（api_key 模式）、Moonshot、`openai_compatible` 都属于
  这一类。`chat_completion` / `chat_completion_stream` / `make_quota_probe` / `quota_chat_probe` /
  `probe_openai_chat` 都带这个参数，**默认仍是 `max_tokens`**（其它厂商照旧）。
  anthropic 协议必须用 `max_tokens`、gemini 走 `maxOutputTokens`，都不受影响；
  华为明确「`max_tokens` 与 `max_completion_tokens` 不可同时设置」，故只发一个。

**「简单对话」的两个显示约定**（`chat_completion_stream`）：

- **纯思考输出显示思维链**：模型只产出了思维链、正文为空时（`max_tokens` 偏小会被思考吃光），
  把思维链当正文返回，不留空白气泡。**字段名各家不同**，`_reasoning_of()` 逐个适配：
  百炼 / 智谱 / 华为 MaaS / MiniMax 是 `delta.reasoning_content`；商汤自家 `sensenova-*`
  是 `delta.reasoning`，而它网关里**转挂**的 deepseek/glm 仍是 `reasoning_content`
  —— 同一个网关内两种写法并存，两个都要取；Anthropic 走 `thinking_delta` / `delta.thinking`。
- **`400` 不一定是密钥有问题**：智谱那批业务码（`1211 模型不存在` / `1221 API 已下线` /
  `1222 API 不存在` / `1261 Prompt 超长` / `1301 内容被安全审核拦截`）说明**密钥有效、是请求
  本身被拒**，`_interpret_chat` 命中它们时返回「**有效** + 具体原因」，而不是含糊的
  「无法确认凭证状态」——那是**假异常**。判据用**业务码**（`_REQUEST_LEVEL_CODES`），
  **不用文案**：官方错误码表把 `1113` 写成「您的账户已欠费，请充值后重试」，线上实际回的是
  「余额不足或无可用资源包,请充值。」，同一个码两套文案。

> **通用原则：同一个 HTTP 状态码可能含义完全不同，必须读响应体再下结论。**
> 写 `if status in (400, 401, 403): 判无效` 之前，先确认该厂商会不会用这些码表达**非凭据问题**。
> 两个已知反例：Gemini 的 `400` 既可能是 `API_KEY_INVALID`（真失效），也可能是
> `FAILED_PRECONDITION`「出口地区不支持」（key 没问题）；NVIDIA 的 `/models` **根本不鉴权**，
> 拿它验证会「任何 key 都有效」。判定时读 `error.status` / `details[].reason` / 错误文案，
> 拿不准就返回 `error`（不是 `invalid`）——**假红（误报失效）比假绿更危险**。

---

## 2.5 不使用代理的插件要显式声明（USES_PROXY）

「用哪个代理」是**每个凭据的通用设置**，默认所有插件都会出现该选项——判据是插件**默认走代理**
（对应 `backend/plugin_manager.plugin_uses_proxy()`，默认 `True`）。

确实**完全不走网络**的插件在模块级显式声明即可把它从界面上隐去（目前仓库里已没有这类插件）：

```python
USES_PROXY = False   # 本插件无出网请求，代理选项没有意义
```

> **`smtplib` / `imaplib` 类插件不适用这条**：它们不能走 HTTP 代理，但可以走 **SOCKS / HTTP CONNECT
> 隧道**，所以照样支持「使用代理」。做法见 §2.6。

> 判据不能是「模块里有没有 `make_client`」：那会**漏掉间接发请求的插件**——例如 NVIDIA NIM 自己
> 没有 `import make_client`，而是经 `llm_protocols` 的 `probe_openai_chat` 发请求，于是被误判为
> 「不走代理」而隐藏了选项。故取「默认 True + 显式排除」。

---

## 2.6 非 HTTP 协议怎么走代理（TCP 隧道）

`http.client` / `httpx` 那套（`make_client(proxy=…)`）只对 HTTP(S) 生效。**SMTP / IMAP 等裸 TCP 协议**
用不了它，但可以把连接**隧道**穿出去：SOCKS5（含 `socks5h` 远端解析）或 HTTP CONNECT。

框架侧的这个能力（SOCKS / HTTP CONNECT 隧道建连）在 `plugins/mail/mail_common.py` 里——
`open_socket()` 建一个「已经穿过代理」的 socket，再交给标准库——只覆写建连那一处，其余行为与标准库一致：

```python
class TunneledSMTP(smtplib.SMTP):          # 普通连接
    def _get_socket(self, host, port, timeout):
        return open_socket(host, port, timeout)

class TunneledSMTP_SSL(smtplib.SMTP_SSL):  # SMTPS
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(open_socket(host, port, timeout), server_hostname=self._host)
# IMAP 对应的钩子是 IMAP4._create_socket
```

要点：

- `open_socket()` 自己读 `proxy()`（与其它类别同一套优先级），**没配代理就退回普通 `create_connection`**，
  所以插件可以无脑用它，不必分两条路径。
- 隧道依赖 **PySocks**（`import socks`，见 `requirements.txt`）。它是运行时动态加载的插件用到的，
  PyInstaller 静态分析看不到，**spec 里显式 `collect_all("socks")`**（构建环境缺它即中止打包，
  见 §2.1）；缺失时 `open_socket()` 抛一句中文处理办法，而不是甩 `ImportError`。
- 支持的协议：`socks5` / `socks5h` / `socks4` / `socks4a` / `http`(CONNECT)。账号密码写在 URL 里
  （`socks5://user:pass@host:port`），百分号编码会还原。

---

## 3. plugin.py 接口

必须暴露 `verify(data)`；可选 `validate(data)` 与 `get_display_summary(data)`。
`data` 是用户按 `fields` 填写的字典。

**每个子插件自带完整实现**，只从 `backend/plugin_sdk.py` 与类别模块引用工具；服务之间允许重复代码
（刻意为之）——但**通用工具不再重复**，一律取自 `backend/plugin_sdk.py`。

**模块 docstring 里要带官方参考链接** —— 写清实现端点与判定阈值时依据的**官方文档页**，
一行一个：

    参考文档：https://docs.example.com/api-reference

- **可以没有，不能乱写**：确认不了官方文档地址的，这一行**就不写**，不要凭印象编 URL；
  也**只写官方**页面，不写 CSDN / 掘金 / 博客 / 第三方 SDK 仓库这类转述页。
- 官方**文档页**优先于官网首页。
- **位置不限，在模块 docstring 里即可**：惯用「紧跟首段摘要之后」或「放在 docstring 末尾」，
  两种现网都有，不必强求统一；但不要把它混进正文注释里（那样就不成其为「模块说明」了）。
- 依据了多个官方页（如「鉴权」与「错误码」各一页）就写多行 `参考文档：`，但不要为凑数堆链接。
- **写进去之前要实际打开确认过**（能访问、内容确实是该服务的文档）；被挡或 404 的一律不写。
- **只给开发者看，不上界面**：这行只留在 `plugin.py` 的 docstring（代码注释）里。
  不许写进 `manifest.json` 的 `description` 或任何字段的 label/hint/placeholder，不许出现在
  `verify()` / `get_display_summary()` 返回的 message 里，也不许写进 `actions` 文案 ——
  凡是前端会渲染出来的地方都不能有它。
  > 判定口径：**只在 docstring 出现、正文里找不到**的 URL 才算参考链接；正文里也出现的那些是
  > 端点/默认值常量（如 ``DEFAULT_HOMESERVER``），它们在 manifest 里当 `default` / `placeholder`
  > 渲染是功能性的，不算外泄。

```python
"""MyVendor 验证插件（plugins/llm/my_vendor/，自包含实现）。

参考文档：https://docs.myvendor.com/api-reference
"""
from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json

DEFAULT_BASE_URL = "https://api.myvendor.com/v1"


def validate(data):
    return [] if (data or {}).get("api_key") else ["API Key 不能为空"]


def _verify(data) -> VerifyResult:
    if not data.get("api_key"):
        return VerifyResult(status="invalid", message="API Key 为空")
    base = (data.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    with make_client() as client:
        resp = client.get(f"{base}/models", headers={"Authorization": f"Bearer {data['api_key']}"})
    if resp.status_code == 200:
        return VerifyResult(status="valid", message="凭证有效", account_info=None)
    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


def verify(data) -> VerifyResult:
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data):
    return "MyVendor"
```

要点：

- **通用工具与 `VerifyResult` 已上收 `backend/plugin_sdk.py`**：`TIMEOUT`、`log`、`proxy`、
  `make_client`、`safe_json`、`body_snippet`、`guarded` 等一律从那里取；插件需实现的接口
  （`verify` / `validate` / `get_display_summary` / `actions` / `run_action` …）见该模块的 docstring。
  **类别模块（`llm_common.py` / `mail_common.py`）只保留本类别特有的工具**。
  **协议级、跨服务完全相同的工具**（如 `llm_protocols.py` 的 `probe_*` 探针与
  `chat_completion`）另建**独立工具模块**，由需要的子插件按需引用；
  **厂商/服务专属逻辑**（端点、字段、余额解析、签名等）写在各子插件自己的 `plugin.py` 里，
  服务之间允许重复（刻意为之）。加载子插件时框架会把插件目录与类别目录注入 `sys.path`。
- **端点、字段、判定、余额、签名等具体逻辑写在各子插件自己的 `plugin.py` 里**，
  **新增服务插件无需修改上层代码、`backend/plugin_sdk.py` 或任何类别模块**——参照同级子插件即可。
- `verify` 用 `guarded(...)` 包住，保证不抛异常；异常只暴露类型名（有些服务密钥在
  URL/query 中，透出异常文本会把密钥写进凭据记录）。
- `verify(data)` 的返回值做鸭子类型处理，接受：
  - `VerifyResult` 风格的 dataclass（`status` / `message` / `account_info`）；
  - 普通 `dict`，如 `{"status": "valid", "message": "…", "account_info": {...}}`。
- `status` 取值：`valid`（有效）/ `invalid`（凭据无效）/ `error`（网络等异常）。
  **欠费（余额不足/负余额/402/限流欠费）不算失效**：仍判 `valid`，把欠费原因写进 `message`、
  负余额写进 `account_info`（前端「附加信息」会展示余额）。
- `account_info` 为可选 dict，验证通过后回传账户信息（见「附加信息」约定），前端凭据列表会展示。
- **插件只允许 `import backend.plugin_sdk`（框架暴露的唯一插件接口层），不得 import backend 的
  其它模块**——这是框架与插件之间唯一被承诺稳定的边界，backend 内部实现可自由重构而不影响插件。

### 3.1 可选「简易操作」（actions / run_action / capabilities）

插件可选实现这几个函数，为凭据提供额外操作（前端凭据行的「操作」按钮）：

| 函数 | 必需 | 作用 |
|------|------|------|
| `actions(data) -> list[dict]` | ✅ | 声明有哪些操作；返回 `[]` 表示没有可用操作（凭据行的按钮**仍然显示**，只是置灰 + 悬停提示「暂无可用功能」，见 §5）。**必须是本地、便宜、无副作用的**——它只负责渲染菜单 |
| `run_action(action_id, params, data) -> dict` | ✅ | 执行操作 |
| `capabilities(data) -> dict` | 可选 | **探测该凭据真正能用哪些操作**（可联网）；没实现 = 全部可用 |
| `list_models(data, force=False) -> list` | 可选 | 可用模型名列表（LLM「简单对话」把它变成可搜索下拉）。`force=True` 表示**用户点了「刷新模型列表」**，插件应据此穿透模型清单缓存 |
| `stream_action(action_id, params, data, on_delta)` | 可选 | 流式执行；未实现时自动回退 `run_action` |

**模型清单缓存**：实现在 `llm_protocols`——`fetch_models` 与 `probe_model_candidates`（欠费探测挑候选）
**共用一份 24h 缓存**（键 = 协议 + Base URL + 密钥指纹；文件 `plugins/.model_cache.json`，打包排除）。
自动路径（探测挑候选）读缓存，**只有用户手动刷新才重新联网**。

> **规则：不要把模型名写死在代码里。** 模型名与可用性随**账号 / 套餐 / 地域 / 客户端版本**快速变化
> （实例：codex 目录端点按 `?client_version=` 发清单，`gpt-6.1-sol` 要到 **0.159.0** 才出现——
> 旧版 codex 二进制里没有这个串）。默认模型一律**运行时取**：
> 用户指定 → 运行时清单首项 → 拿不到就**报错让用户手选**，绝不退回内置名
> （`llm_protocols.resolve_chat_model` 就是这条约定）。同理，`actions()` 里 chat 操作的
> `model` 字段**留空 `""`**（前端打开时会自动填运行时清单首项）。

#### 操作项（action）的字段

| 字段 | 适用 | 说明 |
|------|------|------|
| `id` / `name` | 全部 | 标识与显示名（`run_action` 按 id 分发） |
| `kind` | 全部 | **前端渲染方式：`chat`（对话气泡）/ `form`（动态表单）/ `list`（表格 + 行内按钮）** |
| `group` / `order` / `description` | 全部 | 可选。多个操作时弹选择菜单，按 `group` 分组、`order` 排序（小的在前） |
| `fields` | `form` / `list` | 输入项：`[{key,label,type(text/password/select/textarea/number),default,placeholder,options,required}]`。**`list` 也能带 fields**：点「拉取/刷新」时把表单值作为 `params` 传进 `run_action`。**`required: true` 会在操作表单里渲染红色星号并在提交前校验**（空着点提交会被拦下，`form` 与 `list` 两处同规则） |
| `submit` | `form` / `list` | 提交/刷新按钮文案（默认「运行」） |
| `write` | 全部 | 可选布尔，声明为**会改动远端数据**。前端在**提交前**弹二次确认（只列非敏感字段值） |
| `model` | `chat` | 模型输入框的**初始值**（用户可改）。**推荐留空 `""`**——前端打开时自动填运行时清单首项（见上「规则」），**不要在这里写死模型名** |
| `columns` | `list` | `[{key,label,width?,mono?(等宽),type?("text"/"number"/"tag")}]` |
| `row_actions` | `list` | 每行的按钮，见 §3.1.2 |
| `empty_text` | `list` | 空列表文案 |
| `save_to` | `form` / `list` | 下载类操作，见 §3.1.3 |
| `confirm` | 全部 | 可选。**覆盖写操作二次确认的默认文案**。涉及不可逆副作用时应当写清楚——例如「刷新 access_token」会轮换 `refresh_token`，就会提示用户本机 Codex CLI 可能需要重新登录 |
| `progress` | 全部 | 可选布尔。声明该操作**会上报进度**（下载 / 打包这类长操作）：前端改走流式入口并在弹窗里渲染进度条，同时它会被登记成一条**后台任务**（关掉弹窗照样跑，可在「任务」页取消）。插件侧需实现 `stream_action(..., on_progress=…, should_cancel=…)`，见 §3.1.9 |
| `updated_data`（**返回值**） | 全部 | 可选。`run_action` 的返回里带上 `{"字段key": 新值}`，框架会把这些值**写回该凭据**（只接受插件在 manifest 里声明过的字段）。用于「用 refresh_token 换一组新令牌」这类必须原子保存的操作。写回值**不会**回传给前端（那是新明文令牌），前端只收到 `data_updated: [字段名]`，据此刷新列表并复验。见 §3.1.8 |

`run_action` 的返回：

- 普通：`{"ok": True, "output": "多行文本"}` 或 `{"ok": False, "error": "人话原因"}`
  —— **失败时 `error` 会原样展示给用户**，不要写「执行失败」这种没信息量的。
- **列表**：`{"ok": True, "rows": [{"<列key>": 值, ...}], "columns"?, "row_actions"?, "empty_text"?, "total"?, "has_more"?}`
  —— `rows` 里每项的键就是 `columns` 的 `key`；后四个可以在返回里**覆盖声明**（做动态列）。

#### 3.1.1 `kind: "list"` 长什么样

```
╔══════════════════════════════════════════╗
║ 站点列表                          [刷新] ║   ← submit 文案
╠══════════════════════════════════════════╣
║ 站点       记录数  状态      操作        ║   ← columns / row_actions
║ a.com          12  正常     [解析][新增] ║
║ b.com           3  待续费   [解析][新增] ║
╚══════════════════════════════════════════╝
```
点「解析」会**下钻**到另一个 `list`（前端自动加面包屑与「返回」，返回时直接还原上一帧、不重新请求）；
点「新增」会打开一个 `form`（自动预填当前行的域名）。加载中、空、出错各有独立表现，出错**不关弹窗**并提供「重试」。

#### 3.1.2 `row_actions`：行内按钮与下钻

```python
{"id": "list_records",          # 目标 action 的 id（必须在同一个 actions() 里声明）
 "label": "解析",               # 按钮文字
 "params": {"zone": "$row.zone", "type": "A"},   # 字面量 + `$row.<列key>` 模板
 "write": True}                 # 可选：目标操作是写操作时带上
```
- `params` 里字符串形式的 `$row.<列key>` 会被替换成**该行**的值；**解析不到的行，按钮自动置灰**（绝不会把 `undefined` 传给你的插件）。
- 点下去后按**目标 action 的 `kind`** 分派：`form` → 打开表单并预填；`list` → 下钻；其它 → 直接执行。
- 所以**同一个 action 既可以被菜单直接调用、又可以被某一行调用**，靠 `params` 里有没有值来区分。例如
  `add_record` 直接调用时让用户填域名，从某一行调用时自动带上域名。
- **纯下钻操作不进顶层菜单**：若某个 action 只被别的 action 当 `row_action` 目标、且自身没有任何
  `fields`（点了也没参数可填），前端会把它从「操作」菜单里隐去——否则用户点进去只会得到一句
  「缺少 xxx」。它仍照常被行内按钮调用；要让它也能单独用，就给它加上可填的 `fields`
  （例如 `list_records` 加一个 `domain` 输入框）。

#### 3.1.3 `save_to`：下载类操作（会自动弹「另存为」）

```python
{"id": "download_repo", "name": "打包下载", "kind": "form",
 "fields": [{"key": "repo", "label": "仓库", "required": True}],
 "save_to": {"key": "path", "default_name": "$row.name.zip"}}
```
执行顺序由框架保证：**先确认（若 `write`）→ 前端弹系统「另存为」拿到路径 → 路径塞进 `params` → 调 `run_action`**。
插件侧只负责把内容写进那个路径，用 `backend/plugin_sdk.py` 的原子落盘辅助：

```python
path = (params.get("path") or "").strip()
if not path:
    return {"ok": False, "error": "未指定保存路径"}
with make_client() as client:
    with client.stream("GET", url, headers=headers) as resp:
        ok, message = download_to_file(resp, path)
        if not ok:
            return {"ok": False, "error": message}
return {"ok": True, "output": f"已保存到 {path}"}
```

- `download_to_file(response, path) -> (ok, message)`：**非 200 直接失败、不碰磁盘**；先写 `.part` 再 `os.replace` 原子替换，
  避免留下「名字正常、内容截断」的半成品。**不要在插件里自己 open/write。**
- **插件绝不要弹任何对话框**：流式路径上插件跑在新建线程里，跨线程弹窗会卡界面；而且对话框属于界面层的事。

#### 3.1.4 `capabilities`：权限探测（隐藏用不了的操作）

```python
def capabilities(data: dict) -> dict:
    """{"available": ["list_buckets", "list_instances"], "note": "具备云主机与对象存储权限"}"""
```
- 前端**只在点「操作」时调一次**（按凭据缓存，不落 localStorage），所以这里可以联网——但要**串行、每个都 try、给足超时**，不要让用户等太久。
- `available` 列出**当前凭据真正可用**的操作 id：**没权限的操作会被隐藏**；`available == []` 时前端把 `note` 展示给用户。
- **探测失败必须 fail-open**：拿不准时**抛异常**（框架会兜住并当作「未知 → 全部可用」）或返回 `{"available": None}`。
  **不要为省事返回空列表** —— 那等于一次网络抖动就把用户能用的功能全部隐藏。
- 三种状态在前端是可区分的：**没有操作** / **有操作但没权限（显示 note）** / **探测失败（全部展示）**。

#### 3.1.5 可复用的辅助（`backend/plugin_sdk.py` 提供）

| 辅助 | 用途 |
|---|---|
| `VerifyResult` / `guarded()` | 结果类型（`status`/`message`/`account_info`）；`guarded` 兜底未预期异常 → error 结果 |
| `log()` / `log_debug()` | 写应用统一日志（不要用 `print`，打包成 GUI 后 stdio 已关闭，告警会静默丢失） |
| `proxy()` / `make_client()` | 本次生效的代理；建 httpx 客户端（自动带上代理与超时） |
| `TIMEOUT` / `timeout()` | 超时的默认值与实际生效值（读 `VAULT_TIMEOUT`，见 §2.3） |
| `safe_json()` / `body_snippet()` | 响应体解析为 dict；正文压成一行并截断，用于把上游报错带进提示 |
| `first_list(payload, *keys) -> list` | 从各家五花八门的 JSON 信封里捞第一个列表（`items`/`list`/`data`/`ZoneList`…）。**拿不准就显式传 keys** |
| `download_to_file(response, path) -> (ok, message)` | 原子落盘（见 §3.1.3） |
| `mask_secret(value) -> str` | 脱敏展示敏感值（保留首 6 位与末 4 位，如 `sk-123…abcd`）。**写日志、拼用户可见提示一律用它，不要自己切字符串**，理由见下 |

> **`mask_secret` 不许自己切字符串**：手写的 `api_key[:8]` 会把密钥片段写进日志文件与凭据的
> `status_detail`，而框架在 `plugin_manager` 出口做的净化拦不住这种自己拼的片段。
> 空值返回空串；过短的值只保留首 2 位。

#### 3.1.6 最小骨架模板

```python
def actions(data: dict) -> list[dict]:
    if not (data or {}).get("api_key"):
        return []
    return [
        {"id": "list_things", "name": "资源列表", "kind": "list", "group": "查看", "order": 10,
         "submit": "刷新",
         "columns": [{"key": "name", "label": "名称"}, {"key": "id", "label": "ID", "mono": True}],
         "row_actions": [{"id": "add_thing", "label": "新增", "params": {"name": "$row.name"}}],
         "empty_text": "还没有资源"},
        {"id": "add_thing", "name": "新增资源", "kind": "form", "group": "管理", "order": 20,
         "write": True, "submit": "创建",
         "fields": [{"key": "name", "label": "名称", "required": True},
                    {"key": "type", "label": "类型", "type": "select",
                     "options": [{"value": "A", "label": "A 记录"}, {"value": "CNAME", "label": "CNAME"}]}]},
    ]


def capabilities(data: dict) -> dict:
    available, notes = [], []
    try:
        _call(data, "GET", "/ping")           # 只读探测
        available.append("list_things")
        available.append("add_thing")
    except Exception:
        notes.append("缺少读权限")
    return {"available": available, "note": "；".join(notes)}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    params = params or {}
    try:
        if action_id == "list_things":
            payload = _call(data, "GET", "/things")
            rows = [{"name": item.get("name", ""), "id": item.get("id", "")}
                    for item in first_list(payload, "items", "data")]
            return {"ok": True, "rows": rows, "total": len(rows)}
        if action_id == "add_thing":
            _call(data, "POST", "/things", json={"name": params.get("name"), "type": params.get("type")})
            return {"ok": True, "output": f"已创建 {params.get('name')}"}
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行失败：{type(exc).__name__}"}
```

#### 3.1.7 输出净化（后端统一兜底）

净化逻辑住在 `backend/redact.py`，由框架在**两处**收口，插件什么都不用做：

| 路径 | 收口点 | 被净化的字段 |
|---|---|---|
| **验证**（单条 / 批量 / MCP / 定时） | `plugin_manager._redact_result()` | `message`、`account_info` —— 这两者会被 `scheduler` **落日志 + 落库**（`status_detail`） |
| **操作** | `api.run_credential_action` / `stream_credential_action` | `output` / `error` / 流式 `delta`，**以及 `rows` 等任意嵌套结构** |

做法是把该凭据所有 `sensitive: true` 字段的明文替换成 `***`（长度 ≥ 4）。短于阈值的值
（或 manifest 里一个敏感字段都没声明）会记一条 WARNING，而不是静默不保护。

**注意它只认得完整密钥**：插件自己 `api_key[:8]` 切出来的片段，不在替换范围里 —— 写日志、
拼提示一律用 `mask_secret`（见 §3.1.5）。

#### 3.1.8 把结果写回凭据（`updated_data`）

```python
def run_action(action_id, params, data):
    ...
    return {"ok": True,
            "output": "已刷新 access_token；refresh_token 已轮换并写回本凭据",
            "updated_data": {"access_token": 新值, "refresh_token": 新值}}
```

`backend/api.py` 拿到 `updated_data` 后：按该插件 manifest 里**声明过的字段**过滤（其它键丢弃）→
合并进凭据明文 → 重加密落库 → 回给前端一个 `data_updated: [字段名]`（**新值本身不回传**，那是明文令牌）
→ 前端刷新列表并自动复验一次。

**这是「写凭据」的能力，只在必要时用**。典型场景是**刷新令牌**：用 refresh_token 换新 access_token 时
上游会**轮换** refresh_token，若不把新值原子写回，库里与本机 CLI 的登录态就永久错位了。
注意配套要求：

- 操作必须标 `write: true`（前端会二次确认），并用 `confirm` 把不可逆后果写清楚；
- 只读校验路径**绝不能**走这条（换令牌是写副作用）；
- 详见 `plugins/llm/openai/plugin.py` 的 `_do_refresh`。

#### 3.1.9 长操作报进度（`progress` + `on_progress`）

下载 / 打包这类要跑十几秒的操作，声明 `"progress": True` 后前端会改走**流式入口**并在弹窗里
渲染进度条：

```python
{"id": "download_prefix", "name": "打包下载", "kind": "form", "progress": True,
 "save_to": {...}, "fields": [...]}

def stream_action(action_id, params, data, on_delta, on_progress=None):
    """progress 类操作必须实现它，并把 on_progress 一路传到下载循环里。"""
    if action_id not in ("download_object", "download_prefix"):
        return run_action(action_id, params, data)      # 其它操作照旧
    return _run_download(..., progress=on_progress)

def on_progress(current: int, total: int, text: str = "") -> None:
    """total <= 0 表示总量未知（前端只显示已完成量）；text 是给用户看的一句话。"""
```

约定与注意事项：

- **`on_progress` 必须是关键字参数且带默认值**（`on_progress=None`）。框架用
  `inspect.signature` 判断插件认不认这个名字，**不认就不传**——所以只写
  `stream_action(a, p, d, on_delta)` 的老插件照常工作，不会被新参数打断。
- **节流**：不要在 `iter_bytes` 的每个 chunk 上回调（大文件会推出几万条事件）。
  按每 ~256KB 或每 0.2 秒报一次即可。
- 报什么：单对象下载按**字节**报（`current=已写, total=Content-Length`）；多对象打包按
  **对象个数**报（跨对象累加字节不直观）。
- 进度是**尽力而为**：后端推送出错不会让操作失败；`current/total` 会被夹到合法区间。
- 终态仍由框架的 `done` 事件给出（`output` / `error`），插件不必自己报「完成」。

**协作式取消**：同一条通道上还有第二个可选关键字 `should_cancel`——

```python
def stream_action(action_id, params, data, on_delta, on_progress=None, should_cancel=None):
    ...
    for chunk in resp.iter_bytes(65536):
        if should_cancel and should_cancel():        # True = 用户点了取消
            os.remove(tmp)                           # 半成品要删干净
            return {"ok": False, "error": "下载已取消"}
```

- 取消是**协作式**的：框架只置一个标志位（`backend/tasks.py`），真正停下靠插件在循环里查。
  做不到立刻杀线程，但会在下一个检查点（每个 chunk / 每个对象）停下。
- **插件只负责干净地停下并返回 ok=False**，不要猜「取消还是失败」——框架持有取消标志，
  会据实把任务标成「已取消」（刚好在取消前跑完的仍算成功）。
- 必须**清掉 `.part` 之类的临时文件**，不要留下半成品。

---

## 4. 编写一个新插件（以新增 OpenAI 兼容服务为例）

1. 新建目录 `plugins/llm/my_vendor/`。
2. 写 `manifest.json`（`credential_type` 取 `llm_my_vendor`，字段参照上文结构）。
3. 写 `plugin.py` —— **参照同类别下任一已有子插件**（如 `plugins/llm/openai_compatible/plugin.py`），
   端点、判定、余额、`validate`、`get_display_summary` 全部自带，只从 `backend/plugin_sdk.py`
   引用通用工具（类别特有工具则从 `llm_common` 取）：

```python
"""MyVendor 验证插件（自包含）。"""
from backend.plugin_sdk import VerifyResult, guarded, make_client

DEFAULT_BASE_URL = "https://api.myvendor.com/v1"


def validate(data):
    return []


def _verify(data) -> VerifyResult:
    ...


def verify(data) -> VerifyResult:
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data):
    return "MyVendor"
```

4. **无需修改 `backend/plugin_sdk.py`、类别模块或任何上层代码**——SDK 只提供通用工具，
   厂商专属逻辑都在你自己的 `plugin.py` 里（服务之间允许重复）。
5. 重启应用即自动加载；在"添加凭据"的服务下拉中即可搜索到。

安全约定：

- 声明 `sensitive: true` 的字段全程加密存储，日志与列表页均不出现明文。
- 重复凭据检测会比较敏感字段明文（内存中解密比对），相同 Key 重复添加时前端弹确认。

---

## 5. 插件清单

**llm/ 类别（18 个）**：openai（含 Codex/ChatGPT 登录态模式）、anthropic、deepseek、openrouter、
gemini、minimax、nvidia、moonshot、siliconflow（硅基流动）、dashscope（阿里百炼）、huawei_maas、
glm（智谱）、ark（火山方舟，托管豆包 / DeepSeek / Qwen 等多家模型）、wenxin（文心）、
openai_compatible（第三方中转）、newapi（New API 中转站）、
sensenova（商汤日日新，OpenAI 兼容）、codebuddy（腾讯 CodeBuddy 客户端登录态）。

> **同一厂商的多种接口格式不拆分插件**，而是合成一个。用不用「格式」字段取决于**key 是否按协议分权**：
>
> - **不分权就不设字段**——`glm`（智谱）即如此：同一把 key 在 OpenAI 端点与
>   Anthropic 端点都能通过（智谱把 `claude-sonnet-4` 之类模型名也做了映射），所以插件只有一条
>   路径（OpenAI 兼容端点），既不设开关、也不对旧地址做特判。普通平台用默认地址，Coding Plan
>   在 Base URL 里填 `/api/coding/paas/v4`。
> - **部署形态真的不同才设字段**——`openai_compatible`（第三方中转）保留 `api_style` 下拉：
>   中转站千差万别，有只暴露 `/v1/messages` 的 Claude 专属站，光靠 URL 猜不可靠。
>
> 格式字段一律**只认显式值**（manifest 给默认），不做 Base URL 嗅探，也不为旧凭据类型写数据迁移
> ——**1.0 之前不考虑向下兼容（含旧版本数据库）**，旧的 `llm_glm_anthropic` /
> `llm_anthropic_compatible` 类型不再有对应插件，那类凭据直接在界面上删掉、按新插件重建即可。

**api/ 类别（8 个）**：firecrawl（网页抓取）、tavily（搜索）、metaso（秘塔 AI 搜索）、fofa（FOFA 网络空间测绘）、
coze（字节扣子）、dify（Dify 应用 / 工作流）、google_api_credentials（Google API，一对 key/secret）、baidu_face（百度人脸，人脸识别）。

**dns/ 类别（3 个）**：dnspod、duckdns、godaddy。

**dev/ 类别（4 个）**：github（令牌）、huggingface、wandb（Weights & Biases）、npm（npm 访问令牌）。

**im/ 类别（10 个）**：feishu（飞书）、dingtalk（钉钉）、wecom（企业微信）、telegram（Bot）、discord（Webhook）、
qq_bot（QQ 频道机器人）、wechat_ilink（微信 iLink Bot）、telegram_apihash（Telegram API ID / Hash）、
wechat_mp（微信公众号）、wechat_miniprogram（微信小程序）。
其中 **dingtalk 与 wecom 是「应用 / Webhook 双形态」**插件：`kind` 下拉选 `app`（自建应用）或
`webhook`（群机器人），字段用 `show_if` 按 `kind` 显隐；`kind` 缺省回落 `app`，故旧的 `im_dingtalk`
凭据不受影响（原独立的通用 `im_webhook` 插件已按厂商拆入这两个插件）。
**wechat_mp / wechat_miniprogram 同为 `AppID + AppSecret` 两件套**，校验都走
`cgi-bin/token` 换 access_token（只读；顺带取关注者列表 / 小程序基础信息与接口配额），二者据
拿到的是公众号还是小程序的凭据分别独立成插件——同 AppID 在两种主体下不通用。

**cloud/ 类别（12 个）**：aliyun（阿里云 AK/SK）、tencentcloud（腾讯云 SecretId/Key）、aws（AWS Access Key）、
gcp（GCP 服务账号）、huaweicloud（华为云 AK/SK）、firebase（Firebase 管理密钥）、
jdcloud（京东云 AK/SK）、ucloud（UCloud PublicKey/PrivateKey）、baiducloud（百度智能云 BCE AK/SK）、
volcengine（火山引擎 AK/SK）、ctyun（天翼云 AK/SK）、
cloudflare（Cloudflare API Token，或 Email + Global API Key）。
国内五家（京东云 / UCloud / 百度智能云 / 火山引擎 / 天翼云）**一律只用只读接口做校验**
（IAM 列举 / GetRegion / DescribeRegions / 资源池列表），且各自提供一组只读查询操作
（身份、地域、可用区、云主机等）；`region` / `region_id` 为选填默认地域，只影响查询与部分端点选择，
**校验本身不依赖它**。
服务账号这类凭据按「一行一项」收 **邮箱 + 私钥**，另两个字段选填（密钥 ID 只当 JWT 头的 `kid`、
项目 ID 只喂额外探测），**不接收整段 JSON**——见 §2「字段输入约定」。
`google_sa.py` 是协议级共享模块：gcp 与 firebase 都要把邮箱 + 私钥签成 RS256 JWT 换 access_token，
这步两家完全一致，故单独成模块（判定「什么状态码算有效」仍在各自 plugin.py 内）。
私钥在单行框里被压掉换行后照样换得到 token；`private_key_id` 缺失不影响；只填
`client_email` + `private_key` 也能通过。
（Cloudflare 已移到 `dns/` —— 它本质是域名解析类。）

**storage/ 类别（2 个）**：s3（**任意 S3 兼容对象存储**：MinIO / Ceph RGW / SeaweedFS / 各云厂商的 S3 端点）、
anon（**匿名对象存储**：无凭据探测公开桶）。
`s3` 自带 **AWS Signature Version 4**（类别共享模块 `storage_s3v4.py`，`service=s3`，S3 必须签
`x-amz-content-sha256`）；**端点由用户填、允许 `host:port` 与 http**，寻址方式可选**路径式**（默认，MinIO / IP 端点）
或**虚拟主机式**（AWS S3 等）。操作 = `list_buckets` → 行内「预览」下钻 `list_objects`（文件夹 / 文件分行，
可继续进入前缀）· 文件行「下载」（`save_to`）· 文件夹行「打包下载」（zip，上限 1000 个 / 512 MiB）。
自签名证书的自建服务可勾「跳过 TLS 证书校验」（仅对该凭据生效）。`region` 默认 `us-east-1`；
填错时若服务端在错误体里回了 `<Region>`，提示会直接给出正确值。`validate()` 在服务端兜底必填与端点格式。
`anon` **不带任何凭据**——输入**桶根地址**（虚拟主机式 `https://<桶>.<域>` 或路径式 `https://<域>/<桶>` 都行），
用**未签名请求**判定并浏览：`verify` = 「能否匿名列举」（200 valid / 403 私有 / 404 不存在）；
操作 = `list_objects` / `download_object` / `download_prefix`（即「公开可列 / 可读」），外加
`write_probe`（`write: true`，**PUT 一个临时探针对象后立即 DELETE** 判定「公开可写」；删不掉时**不算成功**
并把残留对象的完整地址回给用户手动清理）。`validate()` 拒绝预签名 URL（带查询串）。
XML 解析与带进度 / 取消的流式下载放在类别共享模块 `storage_common.py`。

**map/ 类别（3 个）**：amap（高德地图 Key；「Web 服务」类型实调校验，其它平台按「该 Key 是否存在」判定）、
baidu（百度地图服务端 AK，选填私钥 SK —— AK 开了 **SN 校验**时补上 SK 由插件自动签名）、
tencent（腾讯位置服务 WebService Key，选填 SK 密钥 —— Key 开了 **签名校验**时补上 SK，由插件按
`md5(path + "?" + 升序参数 + SK)` 计算 `sig` 签名）。
三家都只需一个 Key（百度、腾讯多一个可选 SK），校验都用 **IP 定位接口**
（高德 `/v3/ip`、百度 `/location/ip`、腾讯 `/ws/location/v1/ip`）——
无需额外参数、也不消耗配额型服务，顺带回填所在省市。
把 Key 放进 **URL 查询串**是这三家的接口形制，故异常文本必须走 §3.1.7 的输出净化。

**exchange/ 类别（2 个）**：okx、binance。

**mail/ 类别（2 个）**：smtp（通用 SMTP，覆盖 Gmail / QQ / 163 / 126 等）、
gateway（短信网关 / 接码服务商——字段按各厂商叫法通用化，`app_key` / `app_secret` 装不下的一律塞
`extra_params`；有可用只读校验接口的厂商：Engagelab、Infobip、CM、laaffic、nxcloud、buka、Ultramsg——
Infobip 印度节点 200 有效、香港节点在该入口 403「无权限」故判 `error`；Ultramsg 对任意实例 / 口令都返回
同一段文案，只能报「未验证」；其余厂商没有只读接口，一律如实报「未验证」）。

smtp 的 TLS 版本**交给框架的「TLS 版本」策略**（`plugin_sdk.ssl_context()` 复用；凭据 >
插件预设 > 类别预设 > 全局默认，见 `backend/tls_store.py`），默认 `auto` = 最低 **TLS 1.2**、不降级。
服务器只开 **TLS 1.1**（老校内邮件常见）时会报「SSL/TLS 握手失败」（服务端回 `protocol version`
告警），把该凭据的「TLS 版本」固定成 **1.1**（或 1.0）即可——框架在固定旧版时会自动放宽 OpenSSL
安全级别，否则旧套件协商不出来。降级会让授权码走在更弱的信道上，故须确认服务器可信再改。
SMTP 与 IMAP 两条路径共用同一策略。

> 说明：标记 `batch_verify: false`（只能手动单个验证）的共**五个**：
> `dns/duckdns` 的校验本身是写操作（先写入 `1.1.1.1` 确认生效再还原原值）；
> `im/telegram_apihash` 与 `mail/gateway`（`credential_type` 仍为历史的 `sms_gateway`）则是判定常为
> 「未验证」（前者要登录态、后者多数厂商没有只读接口），批量跑会把人工判定的状态刷成「未验证」；
> `im/wechat_mp` 与 `im/wechat_miniprogram` 是因为 **access_token 是账号级的**——
> 批量并发取 token 会互相作废、还可能打断同一账号下其它正在运行的系统
> （插件本身走 `stable_token` + `force_refresh=false`，只在账号当前没有可用 token 时才新签发）。

> 本节共 **64 个**，即 `plugins/` 下的**全部**插件，**默认全部启用**。想临时停用某个用
> 「插件管理」里的**禁用**即可（状态记在 `plugins/.state.json`，见 §1.1），无需删除目录——
> 禁用后其功能选项在界面置灰、批量验证自动跳过，其凭据**状态显示为「插件禁用」（派生，重新启用
> 即回弹）、且不许再给它添加凭据**，而编辑 / 删除与已有数据照常保留。

### 内置「简易操作」一览

凭据行的「操作」按钮**常驻**（见 §3.1）。按钮可点即说明该凭据至少有一个操作；点开后一个凭据有
多个操作时先弹选择菜单，**只有一个就直接进**；带「（写）」的是 `write: true`，提交前会二次确认。
**没有声明 `actions()` 或声明了但返回 `[]` 的插件，按钮仍显示但置灰，悬停提示「暂无可用功能」**
——按钮时有时无会让同一列里每行的按钮位置都对不齐。下表只列**真有操作**的插件。

- **llm**：18 个厂商 —— 简单对话（`chat`，流式）；其中 **openai** 另有「认证方式」下拉，
  选「ChatGPT 登录态（Codex）」时除「简单对话」外还提供「登录态详情」（`token_info`，只读解析本地
  JWT，**不回显任何令牌**）；`codebuddy` 是纯登录态类凭据，同样只有 `token_info`。
  > 登录态**按字段分开填**（`access_token` 必需；`id_token` / `refresh_token` / `account_id` /
  > `last_refresh` 可选），不要求粘贴整份 `~/.codex/auth.json`——既不便于维护，也会把不必要的字段一起存下来。
  > 字段名与 auth.json 对齐：`account_id` 取 `tokens.account_id`、`last_refresh` 取顶层同名项。
  > 联网头 `chatgpt-account-id` **以 `account_id` 字段为准**，留空才回落 access_token 的 JWT claim，
  > 两处都取不到就不带这个头。**实测（2026-10，个人 Pro 账号）该头不被校验**：`/me`、`/wham/usage`、
  > `/codex/models`、`/codex/responses` 在「带对 / 带错 / 不带」三种取值下表现**完全一致**——
  > 它的实际用途是**多组织 / 工作区账号下指定用哪个工作区**。`last_refresh` 纯展示（「登录态详情」里
  > 显示这份登录态有多旧），用户手动刷新成功后由插件写成本次时间。
  > 字段按模式显隐（`show_if`）：选 API Key 时只看到 API Key 那组，选 ChatGPT 登录态时只看到登录态那组。
  > **只读校验绝不换令牌**（`GET /v1/models`、`GET /backend-api/me` 与 best-effort 的
  > `GET /backend-api/wham/usage` 都是只读；后者读付费套餐的额度窗，取不到就不显示「使用率」一行）。
  > 填了 `refresh_token` 时会多出「刷新 access_token」这个**手动**操作：它 `write: true`、
  > 带自定义确认文案，成功后新值经 `updated_data` **自动写回本凭据**——因为上游换令牌会**轮换**
  > refresh_token，不写回就会让本机 CLI 与库里的登录态永久错位。
  > codex 模式的「简单对话」走 chatgpt.com 的**非公开实验端点** `POST /backend-api/codex/responses`
  > （SSE 流式；`input` 必须是数组、`instructions` 非空、`stream` 恒 true）。该端点与 `/me` 同源，
  > 403 多为人机挑战，**不判凭据失效**。
  > 模型清单**不写死**：现取 `GET /backend-api/codex/models?client_version=<本机 codex 版本>`
  > （取 `visibility=="list"` 的条目），退到 Codex CLI 的 `~/.codex/models_cache.json`，都没有就留空让用户手输。
  > ⚠️ 该目录**按客户端版本**发：`client_version` 低于 **0.159.0** 时服务端**不返回 `gpt-6.1-sol`**
  > （0.157.1 → 9 条、0.159.0 → 10 条），且该参数**必填**（不带→400）；所以本机装旧版 codex 也会
  > 看不见新模型，插件用下界 `_CODEX_MIN_CLIENT_VERSION` 兜底。另：目录**不是准入白名单**——
  > `gpt-5.5` 标 `hide` 也能对话、`gpt-6.1-sol` 目录里没有但 `/codex/responses` 接受。
  >
  > `codebuddy`（纯登录态类）同样是分字段填写：**必需** `access_token`、`uid`（探针请求头
  > `X-User-Id`）；**可选** `refresh_token`（只存不用）、`enterprise_id`、`domain`
  > （默认 `www.codebuddy.cn`）。它**没有**刷新操作——刷新端点未经确认，
  > 不用推测的接口去轮换用户的令牌。
  >
  > `ark`（火山方舟，原 `doubao`，已泛化）—— **方舟是字节的模型托管平台，不等于豆包**：同一把 key
  > 能调豆包 / DeepSeek / 智谱 GLM / 阿里 Qwen 等多家模型。两点必知：**`/models` 不含你自己的
  > 「推理接入点」`ep-xxxxxxxx`**（接入点才是很多人实际调用的对象，**必须手输**，列表里永远没有）；
  > **`/models` 混着大量已关停的历史模型**（135 条里 83 条 `status=="Shutdown"`，且按创建时间排、
  > **首项就是关停款**）→ 插件在 `list_models` 里**剔除 `Shutdown`、并把非对话类（embedding /
  > seedance / seedream / tts…）排到最后**，免得默认取首项时选到死模型。另：**接入点被关是 HTTP 400
  > `InvalidEndpoint.ClosedEndpoint`，不代表 key 失效**（key 有效时 `/models` 仍 200，别误判失效）。
  > 老库里 `llm_doubao` 类型的凭据用 `python scripts/migrate_doubao_to_ark.py` 一次性迁移
  > （默认只演练，`--apply` 才写库）。
- **dns**：
  - dnspod / godaddy —— **域名（站点）列表 → 行内「解析」下钻到解析记录 → 行内「修改」**，
    以及行内「新增」；两者都是 `write`
  - duckdns —— 当前解析（`list`：A / TXT 两行）→ 行内「修改」下钻到「更新解析」（`write`），
    另有「清除 TXT」（`write`）。DuckDNS 没有域名列表接口（一个 token 对应自有子域名），
    查询经 Cloudflare DoH；更新/清除走其 `update` 接口
- **cloud**：aliyun / tencentcloud / aws / huaweicloud **四家同构** ——  `whoami`（身份 + **探测到的权限**）· `list_regions`（**地域列表**，含中文名；华为用**内置静态表**，
  因为它的区域列表接口要 token）→ 行内「实例」下钻 `list_instances`（云主机，`region` 可预填可手填）·
  `list_buckets`（**对象存储**桶列表）→ 行内「预览」下钻 `list_objects`（**区分文件夹前缀与文件**，
  可继续进入前缀）；文件行「下载」（`save_to` 单对象）、文件夹行「打包下载」（`download_prefix`，
  本地打成 zip 后 `save_to`，对象数/总字节有上限，超出会如实报错而非静默截断）。
  四家的「附加信息」都只回 `{"account_id": …}`（ARN / UserId / domain_id 这类挪进 `whoami`）。
  **`region` 留空 = 并发枚举全部地域**，结果行**带「区域」列**——把「这批资产分布在哪些地域」一次看全。
  枚举在**插件内部**并发完成（`list` 类操作**没有**进度条通道，见 `docs/FRONTEND.md`），前端只转圈；
  单个地域失败（限流 / 该地域无权限）**不毁整张表**，也**不写进结果**——那条地域直接跳过。
  > **失败信息一律不展示**：结果表只列**成功取到**的地域，空态文案也只说「已扫描全部 N 个地域」。
  > 理由：这类失败（子用户被限制在某几个地域、少数地域未开通）是**常态**，把
  > `cn-east-2（HTTP 403：The IAM user is forbidden in the currently selected region）` 这种东西
  > 拼成一行「备注」塞进表格，对用户就是噪音，还会让表格里混进一条不是实例的假行。
  > **注意**：`download_prefix`（打包下载）那处的「失败 N 个」是**另一回事**，要保留——
  > 那是本次下载的**结果报告**（哪些对象没下下来），不是常态噪音。
  > **每家的端点规则不同，不要互相套用**：阿里云有相当一部分地域**不认**全局端点
  > `ecs.aliyuncs.com`，必须用它自己 `DescribeRegions` 返回的 `RegionEndpoint`
  > （33 个地域里 14 个会报 `The specified endpoint cant operate this region`）；
  > AWS 是 `ec2.{region}.amazonaws.com`；华为 ECS 要先有 `project_id`（凭据里填了就用，
  > 没填则**只读**查 `GET /v3/auth/projects` 推断，无副作用）；腾讯云按区域拼即可。
  > **华为 OBS 的 `ListBuckets` 与地域无关**（任何地域的 endpoint 都返回全部区域的桶，
  > 返回字段里也**没有** `ExtranetEndpoint`），所以桶的地域直接取 `Location`、端点在插件里自拼。
  >
  > **国内五家新云（京东云 / UCloud / 百度智能云 / 火山引擎 / 天翼云）各自一组只读查询**，
  > **不套用上面四家的同构结构**（各家的 IAM / 地域 / 主机模型都不一样）：
  > - jdcloud —— 查看调用者身份 · AccessKey 列表 · 地域与可用区 · 云主机实例列表 · 云硬盘列表
  > - ucloud —— 账号信息 · 地域列表 · 项目列表 · 云主机列表 · 弹性 IP 列表 · 防火墙列表
  > - baiducloud —— IAM 子用户 / 用户组 / 角色 / 权限策略列表 · BOS 存储桶列表 → 行内「浏览」下钻 BOS 对象
  > - volcengine —— 账号信息 · 地域列表 · 可用区列表 · 云服务器列表 · 镜像列表
  > - ctyun —— 地域（资源池）列表
  >
  > 五家都用**只读**接口做校验（IAM 列举 / GetRegion / DescribeRegions / 资源池列表），
  > `region` / `region_id` 为选填默认地域、只影响查询与部分端点选择，**校验本身不依赖它**。
  > 写新云插件时**不要**凭印象重写签名——各家的签名器自成一份模块，细节见本节末尾附录。
  - cloudflare —— **域名（站点）列表 → 行内「解析」下钻到解析记录 → 行内「修改」**，以及行内「新增」
    （同一个表单，留空 `record_id` 即新增）
- **storage**：s3（**任意 S3 兼容对象存储**）—— `list_buckets`（桶列表）→ 行内「预览」下钻 `list_objects`
  （文件夹 / 文件分行，可继续进入前缀）；文件行「下载」（`save_to` 单对象）、文件夹行「打包下载」
  （zip，上限 1000 个 / 512 MiB，超出如实报错而非静默截断）。端点自填（支持 `host:port`），
  寻址可选路径式 / 虚拟主机式。
  anon（**匿名对象存储**）—— **无凭据**，`list_objects` / `download_object` / `download_prefix`
  （即「公开可列 / 可读」）+ `write_probe`（**`write`**：匿名 PUT 一个探针对象后立即 DELETE，
  判定「公开可写」；删不掉时**不算成功**并把残留对象的地址回给用户手动清理）。
- **map**：amap（高德，**仅「Web 服务」类型的 Key** 提供下列操作 —— 其它平台的 Key 调不动 REST 接口）—— 地理编码 · 逆地理编码 · 行政区划 · 实况天气 · 预报天气 · IP 定位 · 距离测量；
  baidu（百度）—— 地理编码 · 逆地理编码 · 地点检索 · IP 定位 · 实况天气 · 预报天气；
  tencent（腾讯位置服务）—— 地理编码 · 逆地址解析 · 地点搜索 · 行政区划 · 距离计算 · IP 定位 · 实况天气 · 预报天气。
  三家都是**纯只读查询**（校验走 IP 定位接口，见 §5 清单）。百度若 AK 开了 **SN 校验**、
  腾讯若 Key 开了 **签名校验**，在凭据里补上 **SK** 即可，插件自动按各自官方规则签名。
  > 坐标顺序三家不同：高德 `location` 是「**经度,纬度**」，百度与腾讯是「**纬度,经度**」。
- **dev**：github —— 账号信息（含 `X-OAuth-Scopes` 权限）· **仓库列表 → 行内「打包下载」（`save_to`，弹另存为）**
  与行内「详情」下钻；huggingface / wandb —— 查看账号信息
- **im**：feishu —— 应用信息与权限 · 机器人信息 · 用户详情（按 `open_id` 或邮箱）· 群组 · 知识库 · 云文档；
  wechat_mp（公众号）—— 获取调用凭据 · 关注者列表 · 接口调用配额；
  wechat_miniprogram（小程序）—— 获取调用凭据 · 小程序基础信息 · 接口调用配额；
  telegram（机器人信息、发送消息（写））· discord（Webhook 信息）·
  wechat_ilink —— 拉取消息（`list`，短轮询）→ 行内「下载」下钻到附件下载（图片 / 语音 / 文件 / 视频，
  CDN 密文本地 AES 解密后经 `save_to` 写盘）
- **exchange**：okx（账户余额）· binance（账户信息、交易所时间）
- **api**：coze（工作区列表）· tavily（执行搜索）· firecrawl（抓取网页）·
  metaso（搜索（`list`，行内「阅读」下钻）· 网页解析）·
  baidu_face（人脸库分组列表（`list`，只读））·
  dify（应用信息 · 应用参数 · 对话（`chat`，流式）· 运行工作流（`write`）——key 是**应用级**的，
  能看到什么由该应用的 `mode` 决定：`capabilities()` 按 `/info` 的 mode 显隐，
  workflow 应用不出现「对话」、普通对话应用不出现「运行工作流」）
- **mail**：smtp —— 收件箱（`list`，可填拉取封数/文件夹/IMAP 主机与端口）→ 行内「看正文」下钻
  > **网易 163 / 126 要求客户端在登录后、`EXAMINE`/`SELECT` 之前先按 RFC 2971 发 `ID` 声明身份**，
  > 否则 `select` 会回 `Unsafe Login. Please contact kefu@188.com`。实现见 `_send_imap_id()`：
  > 用 `conn.xatom("ID", ...)`（CPython 的 `imaplib.Commands` 里**没有** `ID`，直接
  > `_simple_command` 在 AUTH 态会抛 `command ID illegal in state AUTH`），
  > 且**尽力而为、失败放过**（服务器 CAPABILITY 里没有 `ID` 就跳过，发送异常一律吞掉——不引入新失败点）。
  > 在 163 与 126 邮箱上验证过：不发 `ID` 必现该错，发出后正常打开 INBOX。

> 以上按**声明了操作（`actions()`）**的插件列举。以下 9 个**未定义 `actions()`**、
> 目前只做凭据验证：`api/fofa`、`mail/gateway`、`api/google_api_credentials`（原 `api/app_secret`）、
> `cloud/gcp`、`dev/npm`、`im/dingtalk`、`im/qq_bot`、`im/telegram_apihash`、`im/wecom`
> （dingtalk / wecom 拆分自原 `im/webhook`）。

> **按权限显隐**：aliyun / tencentcloud / aws / huaweicloud / firebase / dify / feishu / github
> **以及新增的 jdcloud / ucloud / baiducloud / volcengine / ctyun** 实现了
> `capabilities()`，没权限的操作不会出现在菜单里；
> 一个都没有时给出说明。**探测失败一律 fail-open**（全都展示）——只有上游**明确**说「密钥无效」才判定为无可用操作。
> （示例：一把**只有 S3 权限**的 AWS key，云主机与区域列表两项被隐藏，`note` 写明
> 「已隐藏无权限的操作：区域列表（UnauthorizedOperation）、云主机实例列表（UnauthorizedOperation）」。）

#### 附：阿里云 OSS 与腾讯云 COS 的签名（自建在各自 plugin.py 内）

两者都**不复用**云 API 的签名，写新对象存储插件时不要混用：

- **OSS V1**：`StringToSign = VERB\nContent-MD5\nContent-Type\nDate\nCanonicalizedOSSHeaders + CanonicalizedResource`，
  `Signature = base64(HMAC-SHA1(AccessKeySecret, StringToSign))`（**key 不加 `&`**，这点与云 API RPC v1 不同），
  请求头 `Date`(RFC1123 GMT) + `Authorization: OSS <AK>:<Sig>`；`max-keys`/`prefix`/`marker` **不进**规范串。
- **COS**：`q-sign-algorithm=sha1`。`signKey = HMAC-SHA1(SecretKey, signTime)` —— **单次** HMAC！
  （写成 `HMAC(HMAC(SecretKey, signTime), keyTime)` 的双重形式会让所有请求 403。）
  `httpString = <method 小写>\n<path>\n<params>\n<headers>\n`（k/v 都 `quote(x, safe="-_.~")`、按字典序 `&` 连接），
  `stringToSign = "sha1\n" + signTime + "\n" + sha1(httpString) + "\n"`，`signature = HMAC-SHA1(signKey, stringToSign)`。
- 两家的响应都是 **XML**（用 `response.text` + `xml.etree.ElementTree`，用 `tag.rsplit('}', 1)[-1]` 绕开 namespace），
  **不要**用 `safe_json`。

#### 附：新增五家国内云的签名器（各自插件目录内的独立模块）

京东云 / UCloud / 百度智能云 / 火山引擎 / 天翼云都**没有**现成可用的签名库，
各自有一份**独立辅助模块**（`jdcloud_signer.py` / `ucloud_signer.py` / `bce_signer.py` /
`volc_signer.py` / `eop_signer.py`），均**经官方 SDK 源码逐字校准并留了固定向量**，
不是一个 `plugin.py` 里的内联函数。写新云插件时**不要**凭印象重写签名——这些算法与
AWS SigV4 只是「神似」，细节处处不同：

- **火山引擎 SignerV4**：`Authorization` 前缀是 **`HMAC-SHA256`**（**不是** `AWS4-HMAC-SHA256`）；
  `kDate = HMAC-SHA256(SK, date)` 用**原始 SK**、**不加** `"AWS4"` 前缀；头名精确为
  `X-Date` / `X-Content-Sha256`。注意这是 **OpenAPI 网关**的签名，与「火山方舟 Ark / 豆包」
  （`ark.cn-beijing.volces.com`，直接 `Authorization: Bearer <API Key>`）**完全不是一回事**。
- **京东云 JDCLOUD3-HMAC-SHA256**：与 SigV4 同形，两套版本（JDCLOUD2 / JDCLOUD3）**只差版本串**；
  请求**不带** `regionId` 时 scope 里填 `jdcloud-api`；**AK 不存在时返回 403**（不是 401）。
- **百度智能云 BCE V1**：`CanonicalHeaders` **含 `content-length`**，而 httpx 对无 body 的 GET
  不会自动补这个头——**空 body 的 `Content-Length: 0` 必须真的发出去**，漏了就是恒定签名失败。
- **天翼云 EOP**：没有官方 Python SDK（官方 `ctyun-it` 组织只给 Go SDK 与 Terraform Provider，
  以 Go 源码校准）；`eop-date` 用**北京时区**；成功码是 **`statusCode == 800`**（不是 HTTP 200）；
  网关域名形如 `*-global.ctapi.ctyun.cn`；鉴权错误码 **10002 / 10003**。
- **UCloud**：签名**先于** Action 名校验——拿假密钥试不出 Action 名对不对（返 `RetCode=171`）；
  值编码**不做 URL 转义**，末尾拼 `PrivateKey` 再 hex。
- **（S3 兼容）AWS SigV4 —— `storage/storage_s3v4.py`**：这是**真正的 AWS SigV4**（`AWS4-HMAC-SHA256`），
  与上面五家只是「形似」。要点：S3 的**每个**请求都要签 `x-amz-content-sha256`（GET 空体即
  `sha256("")` 的十六进制）；`host` 头**含端口**（自建常有 `:9000`）；`canonical_query` 自己按
  RFC3986 编码并按 key 升序拼（不能让 httpx 的 `params=` 插手，否则逐字节对不上）。
  **已用 AWS 官方测试向量校验**（`GET /?max-keys=2&prefix=J` → 签名 `34b48302…dc6f7`）。

> **停靠点：`401` / `403` 要分层判**。签名器**不要**把 401/403 一律叫 `bad_cred`——
> 对于「AK 根本不存在」「策略拒绝」这类，服务端也回 401/403，与本插件签名算错**不可区分**。
> 只有**明确证据**（如响应体里的具体错误码指名凭据无效）才判 `invalid`，否则判 `restricted`，
> 免得把有效但权限受限的凭据误报成失效。见 `docs/PLUGIN_GUIDE.md` 判定口径与各插件 docstring。

---
---

## 6. 调试

- 直接修改 `plugins/` 下的文件后**重启应用**即可生效（无热重载）。
- 插件可脱离主程序单独加载验证，**但 `sys.path` 必须含项目根**（插件要 `import backend.plugin_sdk`，
  只把类别目录加进去不够）。参考 `tests/test_real_vendors.py`：

```python
import importlib.util, sys
sys.path.insert(0, ".")            # 项目根：使 from backend.plugin_sdk import … 可用
sys.path.insert(0, "plugins/llm")  # 类别目录：使 from llm_common import … 可用
spec = importlib.util.spec_from_file_location("p", "plugins/llm/deepseek/plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["p"] = mod
spec.loader.exec_module(mod)
print(mod.verify({"base_url": "https://api.deepseek.com/v1", "api_key": "sk-..."}))
```

- 批量真实凭据测试：`data/real_keys.json`（含真实密钥，已 `.gitignore`）中每条可携带任意字段的
  `data` 字典；运行 `python tests/test_real_vendors.py` 会跨全部类别自动发现插件并逐一验证。
  常用开关：
  - `ONLY_NEW=1`：仅验证新增类别（dns / dev / im / cloud / exchange / mail）；
  - `VAULT_PROXY=socks5://127.0.0.1:10808`：让校验走代理（telegram / discord / huggingface 等被墙厂商必需）；
  - 条目 `expected` 取值：`valid` / `invalid` / `overdue`（按 invalid 判定）/ `ip_restricted` / `skip`。
    其中 `expected: "skip"` 的条目**不会调用 verify**（用于有写副作用或无校验接口的类型）。
