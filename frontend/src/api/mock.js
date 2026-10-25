/**
 * 仅开发模式使用的内存 Mock 后端。
 *
 * 目的：在没有 pywebview 的普通浏览器里跑 `npm run dev` 时，前端仍然可以完整
 * 走通所有页面与交互，便于开发与联调。生产构建中该模块通过 `import.meta.env.DEV`
 * 守卫被完全摇树移除，不会出现在 dist 里。
 *
 * Mock 主密码：demo1234
 */

import { PLUGIN_ICON_IMAGES } from './pluginIcons'
import { MOCK_PLUGINS } from './mockPlugins'

const MOCK_PASSWORD = 'demo1234'
const LATENCY = 120

const delay = (ms = LATENCY) => new Promise((resolve) => setTimeout(resolve, ms))
const iso = (date = new Date()) => date.toISOString()
const daysAgo = (n) => iso(new Date(Date.now() - n * 86_400_000))
const minutesAgo = (n) => iso(new Date(Date.now() - n * 60_000))
const minutesDate = (n) => new Date(Date.now() - n * 60_000)

const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
const LOG_LEVEL_VALUES = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 }
const MOCK_LOG_FILE = '<开发模式>/data/logs/keyrealm.log'

/** 生成一行与后端格式一致的日志文本：YYYY-MM-DD HH:MM:SS [LEVEL] name: message */
function logLine(level, name, message, date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  const ts =
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  return `${ts} [${level}] ${name}: ${message}`
}

function emit(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }))
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

/** 与后端 crypto.mask_secret 保持一致的掩码规则 */
function maskSecret(value) {
  const text = String(value || '')
  if (!text) return ''
  if (text.length <= 10) return `${text.slice(0, 2)}…`
  return `${text.slice(0, 6)}…${text.slice(-4)}`
}

// ── 插件目录 ────────────────────────────────────────────
// 数据不再手写：`mockPlugins.js` / `pluginIcons.js` 由 `scripts/gen_mock_assets.py`
// 从 plugins/ 目录现扫生成（与真实目录逐条一致，加插件后重跑即可）。
// 两份拆开是为了不把图标 data URL 在产物里重复存一遍。
function createState() {
  const groups = [
    { id: 1, name: '工作项目', description: '2026 年 Q4 核心项目', color: '#409EFF', sort_order: 0, created_at: daysAgo(30), credential_count: 3 },
    { id: 2, name: '个人', description: '个人测试用途', color: '#67C23A', sort_order: 1, created_at: daysAgo(20), credential_count: 2 },
    { id: 3, name: '基础设施', description: '云资源访问密钥', color: '#E6A23C', sort_order: 2, created_at: daysAgo(12), credential_count: 1 },
  ]

  // 与后端 `list_plugins()` 同形：图标 data URL 从 PLUGIN_ICON_IMAGES 现拼
  // （emoji 图标的插件在该表里没有条目 → icon_image 为 null，前端回退显示 emoji）。
  const plugins = MOCK_PLUGINS.map((p) => ({
    ...p,
    icon_image: PLUGIN_ICON_IMAGES[p.credential_type] || null,
    installed_at: daysAgo(30),   // 产物里不写时间戳，避免每次生成都产生 diff
  }))

  const credentials = [
    { id: 1, name: 'OpenAI 生产账号', credential_type: 'llm_openai', group_id: 1, data_plain: { base_url: 'https://api.openai.com/v1', notes: '生产环境主账号' }, secret: 'sk-proj-demo-aaaaaaaaaaaaaaaa', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 2.419652, currency: 'USD', usage_rate: 0.5160696 }, duration_ms: 320 }, last_verified_at: minutesAgo(2), last_valid_at: minutesAgo(2), created_at: daysAgo(30), updated_at: minutesAgo(2) },
    { id: 2, name: 'Claude 主账号', credential_type: 'llm_anthropic', group_id: 1, data_plain: { base_url: 'https://api.anthropic.com', notes: '内部工具调用' }, secret: 'sk-ant-demo-bbbbbbbbbbbb', status: 'invalid', status_detail: { message: '401 Unauthorized', duration_ms: 210 }, last_verified_at: minutesAgo(60), last_valid_at: daysAgo(4), created_at: daysAgo(25), updated_at: minutesAgo(60) },
    { id: 3, name: 'DeepSeek 测试', credential_type: 'llm_deepseek', group_id: null, data_plain: { base_url: 'https://api.deepseek.com/v1', notes: '' }, secret: 'sk-demo-cccccccccccc', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 12.34, currency: 'CNY' }, duration_ms: 180 }, last_verified_at: minutesAgo(10), last_valid_at: minutesAgo(10), created_at: daysAgo(10), updated_at: minutesAgo(10) },
    { id: 4, name: 'Gemini 个人', credential_type: 'llm_gemini', group_id: 2, data_plain: { notes: '' }, secret: 'AIza-demo-dddddddddddd', status: 'error', status_detail: { message: '连接超时', duration_ms: 20000 }, last_verified_at: minutesAgo(180), last_valid_at: null, created_at: daysAgo(3), updated_at: minutesAgo(180) },
    { id: 5, name: 'Tavily 搜索', credential_type: 'api_tavily', group_id: 3, data_plain: { notes: '搜索 API' }, secret: 'tvly-demo-eeeeeeeeeeee', status: 'unknown', status_detail: {}, last_verified_at: null, last_valid_at: null, created_at: daysAgo(7), updated_at: daysAgo(7) },
    { id: 6, name: '硅基流动', credential_type: 'llm_siliconflow', group_id: 2, data_plain: { notes: '' }, secret: 'sk-demo-ffffffffffffff', status: 'valid', status_detail: { message: '验证通过', account_info: { balance: 8.5, currency: 'CNY' }, duration_ms: 260 }, last_verified_at: minutesAgo(45), last_valid_at: minutesAgo(45), created_at: daysAgo(5), updated_at: minutesAgo(45) },
    { id: 7, name: 'SMTP - QQ 邮箱', credential_type: 'mail_smtp', group_id: null, data_plain: { host: 'smtp.qq.com', port: 465, ssl: true, username: 'demo@qq.com' }, secret: 'authcode-demo', status: 'valid', status_detail: { message: '凭证有效：SMTP 登录成功', account_info: { smtp_account: 'demo@qq.com', smtp_server: 'smtp.qq.com' }, duration_ms: 300 }, last_verified_at: minutesAgo(30), last_valid_at: minutesAgo(30), created_at: daysAgo(2), updated_at: minutesAgo(30) },
    { id: 8, name: 'LiteLLM 企业网关', credential_type: 'llm_litellm', group_id: null, data_plain: { base_url: 'https://gateway.example.com/v1', notes: '' }, secret: 'sk-demo-litellm', status: 'valid', status_detail: { message: '验证通过', account_info: { usage_label: '已消耗 $1109.51', key_name: 'default' }, duration_ms: 260 }, last_verified_at: minutesAgo(5), last_valid_at: minutesAgo(5), created_at: daysAgo(6), updated_at: minutesAgo(5) },
    { id: 9, name: 'LiteLLM 团队', credential_type: 'llm_litellm', group_id: null, data_plain: { base_url: 'https://gateway.example.com/v1', notes: '' }, secret: 'sk-demo-litellm-team', status: 'valid', status_detail: { message: '验证通过', account_info: { usage_label: '已消耗 $12.35 / $100.00', key_name: 'team', budget_duration: '30d' }, duration_ms: 240 }, last_verified_at: minutesAgo(8), last_valid_at: minutesAgo(8), created_at: daysAgo(4), updated_at: minutesAgo(8) },
  ]

  // 与后端 `_seed_mcp_permissions` 同表同序（SQL 按 tool_name 排序），共 20 个工具。
  const mcpPermissions = [
    { tool_name: 'vault_batch_verify', enabled: false, description: '批量验证（可按 credential_ids 点名）' },
    { tool_name: 'vault_create_credential', enabled: false, description: '创建凭据' },
    { tool_name: 'vault_create_group', enabled: false, description: '创建分组' },
    { tool_name: 'vault_create_proxy', enabled: false, description: '新增验证代理' },
    { tool_name: 'vault_delete_credential', enabled: false, description: '删除凭据' },
    { tool_name: 'vault_delete_group', enabled: false, description: '删除分组（组内凭据保留，仅解除归属）' },
    { tool_name: 'vault_delete_proxy', enabled: false, description: '删除验证代理' },
    { tool_name: 'vault_export_backup', enabled: false, description: '导出备份' },
    { tool_name: 'vault_get_credential', enabled: false, description: '获取凭据详情' },
    { tool_name: 'vault_get_secret', enabled: false, description: '获取解密后的敏感字段（高危）' },
    { tool_name: 'vault_import_backup', enabled: false, description: '导入备份' },
    { tool_name: 'vault_list_credentials', enabled: true, description: '列出凭据' },
    { tool_name: 'vault_list_groups', enabled: true, description: '列出分组' },
    { tool_name: 'vault_list_plugins', enabled: true, description: '列出插件' },
    { tool_name: 'vault_list_proxies', enabled: true, description: '列出验证代理与当前全局默认' },
    { tool_name: 'vault_set_global_proxy', enabled: false, description: '设置全局默认代理' },
    { tool_name: 'vault_test_proxy', enabled: false, description: '实测代理连通性' },
    { tool_name: 'vault_update_credential', enabled: false, description: '修改凭据' },
    { tool_name: 'vault_update_proxy', enabled: false, description: '修改验证代理' },
    { tool_name: 'vault_verify_credential', enabled: true, description: '触发验证' },
  ]

  return {
    initialized: true,
    unlocked: false,
    mcpRunning: false,
    mcpPort: 8765,
    groups,
    plugins,
    credentials,
    mcpPermissions,
    mcpToken: {
      id: 1,
      token_prefix: 'kr_a3f2',
      created_at: daysAgo(2),
      last_used_at: minutesAgo(2),
    },
    schedules: [
      { id: 1, name: '每日全量验证', trigger_type: 'cron', trigger_args: { hour: 2, minute: 0 }, target_scope: 'all', target_id: null, credential_ids: [], enabled: true, last_run_at: daysAgo(1), next_run_at: daysAgo(-1), created_at: daysAgo(20) },
      { id: 2, name: '工作分组巡检', trigger_type: 'interval', trigger_args: { hours: 6 }, target_scope: 'group', target_id: 1, credential_ids: [], enabled: false, last_run_at: null, next_run_at: null, created_at: daysAgo(10) },
      { id: 3, name: '重点凭据加密巡检', trigger_type: 'interval', trigger_args: { hours: 12 }, target_scope: 'credentials', target_id: null, credential_ids: [1, 3, 6], enabled: true, last_run_at: null, next_run_at: daysAgo(-0.5), created_at: daysAgo(4) },
    ],
    logLevel: 'INFO',
    closeAction: 'ask',
    autoLockMinutes: 0,
    lightMode: false,
    splashDisabled: false,
    splashAlways: false,
    splashClaimed: false,
    autostartEnabled: true,
    treeOrder: { categories: [], plugins: [] },
    // 代理注册表。id 是 **UUID 字符串**（与后端一致，其它模块一律按 id 引用），不是自增整数。
    proxies: [
      { id: 'a1eb5c2ac79c42af96011e770b7d78ca', name: '本地 v2rayN', url: 'socks5://127.0.0.1:10808', scheme: 'socks5', sort_order: 0, created_at: daysAgo(20) },
      { id: '3f7c1d90b25e4a6c8d0f2b7a91c3e5d4', name: '公司网关', url: 'http://10.0.0.8:7890', scheme: 'http', sort_order: 1, created_at: daysAgo(8) },
    ],
    globalProxyId: 'a1eb5c2ac79c42af96011e770b7d78ca',
    logs: [
      { level: 'INFO', text: logLine('INFO', 'main', '应用启动（frozen=False，日志级别=INFO，数据目录=…）', minutesDate(32)) },
      { level: 'DEBUG', text: logLine('DEBUG', 'backend.scheduler', '调度器已启动，加载 2 个计划', minutesDate(32)) },
      { level: 'INFO', text: logLine('INFO', 'backend.plugin_manager', `扫描到 ${plugins.length} 个子插件（${new Set(plugins.map((p) => p.category)).size} 个类别）`, minutesDate(30)) },
      { level: 'WARNING', text: logLine('WARNING', 'backend.api', '事件推送失败 vault:mcp_status: window not ready', minutesDate(14)) },
      { level: 'ERROR', text: logLine('ERROR', 'backend.scheduler', '加载计划失败 id=2: 触发器参数无效', minutesDate(6)) },
    ],
    sequence: 1000,
  }
}

export function createMockApi() {
  const state = createState()

  const nextId = () => ++state.sequence

  // ── 代理辅助 ────────────────────────────────────────────
  // 与 backend/proxy_store.py 同规则：必须带 scheme，且只认这五种；
  // id 用 32 位十六进制串（对齐后端的 `uuid4().hex`，不是自增整数）。
  const PROXY_SCHEMES = ['http', 'https', 'socks4', 'socks5', 'socks5h']
  const proxyScheme = (url) => {
    const m = /^([a-z0-9]+):\/\//i.exec(String(url || '').trim())
    return m ? m[1].toLowerCase() : ''
  }
  const proxyUrlError = (url) => {
    const raw = String(url || '').trim()
    if (!raw) return '代理地址不能为空'
    return PROXY_SCHEMES.includes(proxyScheme(raw)) ? '' : `代理地址不合法：${raw}`
  }
  const randomProxyId = () => {
    let out = ''
    while (out.length < 32) out += Math.floor(Math.random() * 0x10000).toString(16).padStart(4, '0')
    return out.slice(0, 32)
  }
  /** 等价于后端 `api.list_proxies()` 的返回：全量列表 + 当前全局默认。 */
  const proxySnapshot = () => {
    const globalId = state.globalProxyId
    const global = state.proxies.find((p) => p.id === globalId)
    return {
      ok: true,
      items: state.proxies.map((p) => ({ ...p })),
      global_id: globalId ?? null,
      global_url: global ? global.url : '',
    }
  }

  const groupName = (groupId) => {
    const group = state.groups.find((item) => item.id === groupId)
    return group ? group.name : null
  }

  const pluginOf = (credentialType) =>
    state.plugins.find((p) => p.credential_type === credentialType)

  /** 根据插件 fields_schema 计算敏感字段掩码（模拟后端 data_masked） */
  const maskedOf = (credential) => {
    const plugin = pluginOf(credential.credential_type)
    if (!plugin) return {}
    const masked = {}
    plugin.fields_schema
      .filter((f) => f.sensitive)
      .forEach((f) => {
        if (credential.secret) masked[f.key] = maskSecret(credential.secret)
      })
    return masked
  }

  const decorate = (credential) => ({
    group_name: groupName(credential.group_id),
    data_masked: maskedOf(credential),
    ...credential,
  })

  const recomputeCounts = () => {
    state.groups.forEach((group) => {
      group.credential_count = state.credentials.filter((item) => item.group_id === group.id).length
    })
    state.plugins.forEach((plugin) => {
      plugin.credential_count = state.credentials.filter((item) => item.credential_type === plugin.credential_type).length
    })
  }

  const requireUnlocked = () => {
    if (!state.unlocked) {
      const error = new Error('保险库已锁定')
      throw error
    }
  }

  const mockLog = (name, level, message) => {
    state.logs.push({ level, text: logLine(level, name, message) })
  }

  return {
    // ── 认证 / 锁 ──────────────────────────────────────────
    async is_initialized() {
      await delay(30)
      return { initialized: state.initialized }
    },

    async is_unlocked() {
      await delay(30)
      return { unlocked: state.unlocked }
    },

    async setup_master_password(password) {
      await delay()
      if (!password || String(password).length < 8) {
        return { ok: false, message: '主密码至少需要 8 个字符' }
      }
      state.initialized = true
      state.unlocked = true
      return { ok: true, message: '保险库创建成功' }
    },

    async unlock(password) {
      await delay()
      if (String(password) !== MOCK_PASSWORD) {
        return { ok: false, message: '主密码错误，请重试' }
      }
      state.unlocked = true
      return { ok: true, message: '解锁成功' }
    },

    async lock_vault() {
      await delay(40)
      state.unlocked = false
      return { ok: true }
    },

    async change_master_password(oldPassword, newPassword) {
      await delay(200)
      if (String(oldPassword) !== MOCK_PASSWORD) {
        return { ok: false, message: '原密码错误' }
      }
      if (!newPassword || String(newPassword).length < 8) {
        return { ok: false, message: '新密码至少需要 8 个字符' }
      }
      return { ok: true, message: '主密码已更新，所有凭据已重新加密' }
    },

    // ── 凭据 ──────────────────────────────────────────────
    async list_credentials(groupId = null, credentialType = null, status = null, search = null, credentialTypes = null) {
      await delay()
      requireUnlocked()
      const keyword = search ? String(search).toLowerCase() : null
      const typeSet = Array.isArray(credentialTypes) && credentialTypes.length ? new Set(credentialTypes) : null
      const items = state.credentials
        .filter((item) => (groupId == null ? true : item.group_id === groupId))
        .filter((item) => (credentialType == null ? true : item.credential_type === credentialType))
        .filter((item) => (typeSet == null ? true : typeSet.has(item.credential_type)))
        .filter((item) => (status == null ? true : item.status === status))
        .filter((item) => {
          if (!keyword) return true
          const plugin = pluginOf(item.credential_type)
          const haystack = [item.name, item.credential_type, plugin ? plugin.name : '', groupName(item.group_id) || '']
            .join(' ')
            .toLowerCase()
          return haystack.includes(keyword)
        })
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      return clone(items.map(decorate))
    },

    async get_credential(id) {
      await delay(40)
      requireUnlocked()
      const found = state.credentials.find((item) => item.id === Number(id))
      if (!found) throw new Error(`凭据 #${id} 不存在`)
      // 编辑表单需要明文预填：mock 直接把 secret 放回 api_key
      const result = decorate(found)
      result.data = { ...(clone(found.data_plain) || {}), api_key: found.secret }
      return clone(result)
    },

    // 与真实后端签名保持一致：(name, credential_type, data, group_id, auto_verify)
    async create_credential(name, credentialType, data = {}, groupId = null, autoVerify = true) {
      await delay(180)
      requireUnlocked()
      const now = iso()
      const created = {
        id: nextId(),
        name,
        credential_type: credentialType,
        group_id: groupId == null ? null : Number(groupId),
        data_plain: clone(data) || {},
        secret: data.api_key || '',
        status: autoVerify ? 'valid' : 'unknown',
        status_detail: autoVerify ? { message: '验证通过', duration_ms: 200 } : {},
        last_verified_at: autoVerify ? now : null,
        last_valid_at: autoVerify ? now : null,
        created_at: now,
        updated_at: now,
      }
      delete created.data_plain.api_key
      state.credentials.unshift(created)
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return clone(decorate(created))
    },

    async update_credential(id, name = null, groupId = null, data = null) {
      await delay(160)
      requireUnlocked()
      const target = state.credentials.find((item) => item.id === Number(id))
      if (!target) throw new Error(`凭据 #${id} 不存在`)
      if (name != null) target.name = name
      if (groupId != null) target.group_id = Number(groupId)
      if (data != null) {
        if (data.api_key) target.secret = data.api_key
        const plain = clone(data)
        delete plain.api_key
        target.data_plain = plain
      }
      target.updated_at = iso()
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return clone(decorate(target))
    },

    async delete_credential(id) {
      await delay(140)
      requireUnlocked()
      const index = state.credentials.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`凭据 #${id} 不存在`)
      state.credentials.splice(index, 1)
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true }
    },

    // 与真实后端签名保持一致：delete_credentials(ids) → { ok, deleted }
    async delete_credentials(ids) {
      await delay(140)
      requireUnlocked()
      const wanted = new Set((Array.isArray(ids) ? ids : []).map(Number))
      if (!wanted.size) return { ok: true, deleted: 0 }
      const before = state.credentials.length
      state.credentials = state.credentials.filter((item) => !wanted.has(item.id))
      const deleted = before - state.credentials.length
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true, deleted }
    },

    async verify_credential(id) {
      await delay(500)
      requireUnlocked()
      const target = state.credentials.find((item) => item.id === Number(id))
      if (!target) throw new Error(`凭据 #${id} 不存在`)
      const roll = Math.random()
      const status = roll > 0.7 ? 'invalid' : roll > 0.55 ? 'error' : 'valid'
      const message = status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时'
      const now = iso()
      target.status = status
      target.status_detail = { message, duration_ms: 200 + Math.round(Math.random() * 300) }
      target.last_verified_at = now
      if (status === 'valid') target.last_valid_at = now
      target.updated_at = now
      emit('vault:credential_updated', clone(decorate(target)))
      return { status, message, account_info: status === 'valid' ? { organization: 'Mock Org' } : null }
    },

    async batch_verify(scope = 'all', groupId = null, credentialType = null, credentialIds = null) {
      requireUnlocked()
      const idSet = Array.isArray(credentialIds) && credentialIds.length
        ? new Set(credentialIds.map(Number))
        : null
      let picked = idSet
        ? state.credentials.filter((item) => idSet.has(item.id))
        : state.credentials
            .filter((item) => (scope === 'group' ? item.group_id === Number(groupId) : true))
            .filter((item) => (scope === 'type' ? item.credential_type === credentialType : true))
      // 与后端一致：跳过禁用批量验证的类型，以及插件本身被禁用的类型
      const noBatch = new Set(state.plugins
        .filter((p) => p.batch_verify === false || p.enabled === false)
        .map((p) => p.credential_type))
      const skipped = picked.filter((item) => noBatch.has(item.credential_type)).length
      const targets = picked.filter((item) => !noBatch.has(item.credential_type))
      let valid = 0
      let invalid = 0
      let error = 0
      for (let index = 0; index < targets.length; index += 1) {
        const target = targets[index]
        await delay(220)
        const roll = Math.random()
        const status = roll > 0.75 ? 'invalid' : roll > 0.6 ? 'error' : 'valid'
        const now = iso()
        target.status = status
        target.status_detail = { message: status === 'valid' ? '验证通过' : status === 'invalid' ? '401 Unauthorized' : '连接超时', duration_ms: 220 }
        target.last_verified_at = now
        if (status === 'valid') target.last_valid_at = now
        if (status === 'valid') valid += 1
        else if (status === 'invalid') invalid += 1
        else error += 1
        emit('vault:batch_verify_progress', {
          current: index + 1,
          total: targets.length,
          credential_name: target.name,
          status,
        })
      }
      emit('vault:batch_verify_done', { total: targets.length, valid, invalid, error, skipped })
      emit('vault:credentials_changed', {})
      return { ok: true, total: targets.length }
    },

    async check_duplicate_credential(credentialType, data = {}) {
      await delay(120)
      requireUnlocked()
      const plugin = pluginOf(credentialType)
      const newValues = new Set(
        (plugin ? plugin.fields_schema : [])
          .filter((f) => f.sensitive)
          .map((f) => String(data[f.key] || '').trim())
          .filter(Boolean),
      )
      if (!newValues.size) return []
      return state.credentials
        .filter((item) => item.secret && newValues.has(item.secret))
        .map((item) => ({ id: item.id, name: item.name, credential_type: item.credential_type }))
    },

    async find_duplicate_credentials(credentialIds = null, groupId = null,
      credentialType = null, credentialTypes = null, search = null) {
      await delay(200)
      requireUnlocked()
      const wanted = Array.isArray(credentialIds) && credentialIds.length
        ? new Set(credentialIds.map(Number)) : null
      const typeSet = Array.isArray(credentialTypes) && credentialTypes.length
        ? new Set(credentialTypes) : null
      const rows = state.credentials.filter((c) => {
        if (wanted && !wanted.has(Number(c.id))) return false
        if (groupId != null && c.group_id !== Number(groupId)) return false
        if (credentialType && c.credential_type !== credentialType) return false
        if (typeSet && !typeSet.has(c.credential_type)) return false
        if (search) {
          const q = String(search).toLowerCase()
          if (!`${c.name} ${c.credential_type}`.toLowerCase().includes(q)) return false
        }
        return Boolean(c.secret)
      })
      const buckets = new Map()
      for (const c of rows) {
        const k = `${c.credential_type}\u0000${c.secret}`
        if (!buckets.has(k)) buckets.set(k, [])
        buckets.get(k).push(c)
      }
      const groups = []
      for (const items of buckets.values()) {
        if (items.length < 2) continue
        const ctype = items[0].credential_type
        const plugin = pluginOf(ctype)
        groups.push({
          credential_type: ctype,
          plugin_name: plugin ? plugin.name : ctype,
          match: [{ key: 'api_key', label: 'API Key', masked: maskSecret(items[0].secret) }],
          items: items.map((c) => ({
            id: c.id, name: c.name, credential_type: c.credential_type,
            group_id: c.group_id, group_name: groupName(c.group_id),
            notes: (c.data_plain && c.data_plain.notes) || '',
            status: c.status, created_at: c.created_at, updated_at: c.updated_at,
            last_verified_at: c.last_verified_at,
          })),
        })
      }
      groups.sort((a, b) => b.items.length - a.items.length)
      return {
        ok: true, scanned: rows.length, groups,
        group_count: groups.length,
        duplicate_count: groups.reduce((n, g) => n + g.items.length, 0),
      }
    },

    async resolve_duplicate_group(keepId, removeIds = [], mergeNotes = false) {
      await delay(150)
      requireUnlocked()
      const keep = state.credentials.find((c) => c.id === Number(keepId))
      if (!keep) throw new Error('保留项不存在')
      const ids = (removeIds || []).map(Number).filter((id) => id !== Number(keepId))
      if (mergeNotes) {
        const notes = [keep]
          .concat(state.credentials.filter((c) => ids.includes(c.id)))
          .map((c) => ((c.data_plain && c.data_plain.notes) || '').trim())
          .filter(Boolean)
        keep.data_plain = { ...(keep.data_plain || {}), notes: [...new Set(notes)].join('\n') }
      }
      state.credentials = state.credentials.filter((c) => !ids.includes(c.id))
      emit('vault:credentials_changed', {})
      return { ok: true, kept: Number(keepId), deleted: ids.length, merged_notes: Boolean(mergeNotes) }
    },

    // ── 分组 ──────────────────────────────────────────────
    async list_groups() {
      await delay()
      recomputeCounts()
      return clone([...state.groups].sort((a, b) => a.sort_order - b.sort_order))
    },

    async create_group(name, description = '', color = '#409EFF') {
      await delay(140)
      const created = { id: nextId(), name, description, color, sort_order: state.groups.length, created_at: iso(), credential_count: 0 }
      state.groups.push(created)
      return clone(created)
    },

    async update_group(id, name = null, description = null, color = null) {
      await delay(140)
      const target = state.groups.find((item) => item.id === Number(id))
      if (!target) throw new Error(`分组 #${id} 不存在`)
      if (name != null) target.name = name
      if (description != null) target.description = description
      if (color != null) target.color = color
      recomputeCounts()
      return clone(target)
    },

    async delete_group(id) {
      await delay(140)
      const index = state.groups.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`分组 #${id} 不存在`)
      state.groups.splice(index, 1)
      state.credentials.forEach((item) => {
        if (item.group_id === Number(id)) item.group_id = null
      })
      recomputeCounts()
      emit('vault:credentials_changed', {})
      return { ok: true }
    },

    // ── 插件（放入 plugins/ 目录即自动加载）────────────────
    async list_plugins() {
      await delay()
      recomputeCounts()
      return clone(state.plugins)
    },

    // 启用/禁用插件：只切换状态位，凭据一律保留（禁用后依赖插件的能力不可用）
    async set_plugin_enabled(credentialType, enabled) {
      await delay(120)
      requireUnlocked()
      const plugin = state.plugins.find((p) => p.credential_type === credentialType)
      if (!plugin) return { ok: false, message: '插件不存在' }
      plugin.enabled = !!enabled
      emit('vault:plugins_changed', {})
      return { ok: true, credential_type: credentialType, enabled: !!enabled }
    },

    // ── MCP ───────────────────────────────────────────────
    async get_version() {
      return { version: '1.0.0', build: 'mock-dev', schema_version: '1.0.0',
        display: '1.0.0 (mock-dev)' }
    },

    async get_mcp_status() {
      await delay(60)
      return {
        running: state.mcpRunning,
        port: state.mcpPort,
        active_tokens: state.mcpToken ? 1 : 0,
        url: `http://localhost:${state.mcpPort}/sse`,
      }
    },

    async start_mcp() {
      await delay(500)
      state.mcpRunning = true
      emit('vault:mcp_status', { running: true, port: state.mcpPort, active_tokens: 0 })
      return { ok: true, port: state.mcpPort, url: `http://localhost:${state.mcpPort}/sse` }
    },

    async stop_mcp() {
      await delay(300)
      state.mcpRunning = false
      emit('vault:mcp_status', { running: false, port: state.mcpPort, active_tokens: 0 })
      return { ok: true }
    },

    async list_mcp_permissions() {
      await delay()
      return clone(state.mcpPermissions)
    },

    async set_mcp_permission(toolName, enabled) {
      await delay(100)
      const target = state.mcpPermissions.find((item) => item.tool_name === toolName)
      if (!target) return { ok: false, message: '工具不存在' }
      target.enabled = !!enabled
      return { ok: true }
    },

    async get_mcp_token() {
      await delay()
      if (!state.mcpToken) {
        state.mcpToken = {
          id: nextId(),
          token_prefix: `kr_${Math.random().toString(36).slice(2, 6)}`,
          created_at: iso(),
          last_used_at: null,
        }
      }
      return { exists: true, ...clone(state.mcpToken) }
    },

    async rotate_mcp_token() {
      await delay(300)
      const secret = `kr_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`
      state.mcpToken = {
        id: nextId(),
        token_prefix: secret.slice(0, 7),
        created_at: iso(),
        last_used_at: null,
      }
      return {
        id: state.mcpToken.id,
        token: secret,                   // 明文仅此一次
        token_prefix: state.mcpToken.token_prefix,
      }
    },

    // ── 定时计划 ──────────────────────────────────────────
    async list_schedules() {
      await delay()
      return clone(state.schedules)
    },

    async create_schedule(name, triggerType, triggerArgs = {}, targetScope = 'all', targetId = null,
      credentialIds = []) {
      await delay(160)
      const created = {
        id: nextId(),
        name,
        trigger_type: triggerType,
        trigger_args: clone(triggerArgs) || {},
        target_scope: targetScope,
        target_id: targetId == null ? null : Number(targetId),
        // 仅 target_scope === 'credentials' 时有值（目录树勾选的具体凭据 id）
        credential_ids: (credentialIds || []).map(Number),
        enabled: true,
        last_run_at: null,
        next_run_at: null,
        created_at: iso(),
      }
      state.schedules.push(created)
      return clone(created)
    },

    async update_schedule(id, payload = {}) {
      await delay(160)
      const target = state.schedules.find((item) => item.id === Number(id))
      if (!target) throw new Error(`计划 #${id} 不存在`)
      Object.assign(target, clone(payload) || {})
      return clone(target)
    },

    async delete_schedule(id) {
      await delay(120)
      const index = state.schedules.findIndex((item) => item.id === Number(id))
      if (index === -1) throw new Error(`计划 #${id} 不存在`)
      state.schedules.splice(index, 1)
      return { ok: true }
    },

    // ── 托盘 ──────────────────────────────────────────────
    async get_tray_status() {
      await delay(40)
      return {
        mcp_running: state.mcpRunning,
        mcp_host: '127.0.0.1',
        mcp_port: state.mcpPort,
        active_tokens: state.mcpToken ? 1 : 0,
        is_locked: !state.unlocked,
      }
    },

    async minimize_to_tray() {
      await delay(40)
      return { ok: true }
    },

    async log_frontend_error(message) {
      // 开发模式下只打浏览器控制台，够用了。
      console.error('[Keyrealm][mock] 前端报错：', message)
      return { ok: true }
    },

    // ── 关闭窗口行为 ──────────────────────────────────────
    async get_close_action() {
      await delay(40)
      return {
        action: state.closeAction,
        options: [
          { value: 'ask', label: '每次询问我' },
          { value: 'quit', label: '直接退出程序' },
          { value: 'background', label: '后台运行（最小化到托盘）' },
        ],
      }
    },

    async set_close_action(action) {
      await delay(40)
      if (!['ask', 'quit', 'background'].includes(action)) {
        return { ok: false, message: `无效的关闭行为：${action || '(空)'}` }
      }
      state.closeAction = action
      return { ok: true, action }
    },

    async confirm_close_action(action, remember = false) {
      await delay(40)
      if (remember && ['quit', 'background'].includes(action)) {
        state.closeAction = action
      }
      return { ok: true, action }
    },

    // ── 空闲自动锁定 ──────────────────────────────────────
    async get_auto_lock() {
      await delay(40)
      return {
        minutes: state.autoLockMinutes,
        options: [
          { value: 0, label: '关闭' },
          { value: 1, label: '1 分钟' },
          { value: 5, label: '5 分钟' },
          { value: 10, label: '10 分钟' },
          { value: 15, label: '15 分钟' },
          { value: 30, label: '30 分钟' },
          { value: 60, label: '60 分钟' },
        ],
      }
    },

    async set_auto_lock(minutes) {
      await delay(40)
      if (![0, 1, 5, 10, 15, 30, 60].includes(Number(minutes))) {
        return { ok: false, message: `不支持的档位：${minutes} 分钟` }
      }
      state.autoLockMinutes = Number(minutes)
      return { ok: true, minutes: state.autoLockMinutes }
    },

    async touch_activity() {
      return { ok: true }
    },

    // ── 轻量模式 ──────────────────────────────────────────
    async get_light_mode() {
      await delay(40)
      return { enabled: state.lightMode }
    },

    async set_light_mode(enabled) {
      await delay(40)
      state.lightMode = Boolean(enabled)
      return { ok: true, enabled: state.lightMode }
    },

    // ── 开屏动画 ──────────────────────────────────────────
    async get_splash_disabled() {
      await delay(40)
      return { disabled: state.splashDisabled, always: state.splashAlways }
    },

    async set_splash_disabled(disabled) {
      await delay(40)
      state.splashDisabled = Boolean(disabled)
      return { ok: true, disabled: state.splashDisabled }
    },

    async set_splash_always(always) {
      await delay(40)
      state.splashAlways = Boolean(always)
      return { ok: true, always: state.splashAlways }
    },

    async claim_splash() {
      await delay(40)
      const already = state.splashClaimed
      if (!already) state.splashClaimed = true
      if (state.splashDisabled) return { show: false, reason: 'disabled', always: false }
      if (state.splashAlways) return { show: true, reason: 'always', always: true }
      if (already) return { show: false, reason: 'already', always: false }
      return { show: true, reason: 'startup', always: false }
    },

    // ── 开机自启动 ────────────────────────────────────────
    async get_autostart() {
      await delay(40)
      return {
        supported: true,
        enabled: state.autostartEnabled,
        platform: 'mock',
        target: 'mock://autostart/keyrealm',
      }
    },

    async set_autostart(enabled) {
      await delay(40)
      state.autostartEnabled = Boolean(enabled)
      return { ok: true, enabled: state.autostartEnabled }
    },

    // ── 凭据目录树顺序 ────────────────────────────────────
    async get_tree_order() {
      await delay(40)
      return { ok: true, ...state.treeOrder }
    },

    async set_tree_order(categories = [], plugins = []) {
      await delay(40)
      state.treeOrder = {
        categories: Array.isArray(categories) ? categories.map(String) : [],
        plugins: Array.isArray(plugins) ? plugins.map(String) : [],
      }
      return { ok: true, ...state.treeOrder }
    },

    // ── 验证代理 ──────────────────────────────────────────
    // 与后端 proxy_store 同契约：id 是 UUID 字符串；`_proxySnapshot()` 就是
    // `api.list_proxies()` 的返回形状（改完直接回吐最新全量列表，调用方不必再 list 一次）。
    async list_proxies() {
      await delay(40)
      return proxySnapshot()
    },

    async create_proxy(name, url) {
      await delay(60)
      const bad = proxyUrlError(url)
      if (bad) return { ok: false, message: bad }
      state.proxies.push({
        id: randomProxyId(),
        name: String(name || '').trim() || '未命名代理',
        url: String(url).trim(),
        scheme: proxyScheme(url),
        sort_order: state.proxies.length,
        created_at: iso(),
      })
      emit('vault:proxies_changed')
      return proxySnapshot()
    },

    async update_proxy(proxy_id, name = null, url = null) {
      await delay(60)
      const item = state.proxies.find((p) => p.id === proxy_id)
      if (!item) return { ok: false, message: '代理不存在' }
      if (url !== null) {
        const bad = proxyUrlError(url)
        if (bad) return { ok: false, message: bad }
        item.url = String(url).trim()
        item.scheme = proxyScheme(url)
      }
      if (name !== null) item.name = String(name).trim() || item.name
      emit('vault:proxies_changed')
      return proxySnapshot()
    },

    async delete_proxy(proxy_id) {
      await delay(60)
      const idx = state.proxies.findIndex((p) => p.id === proxy_id)
      if (idx < 0) return { ok: false, message: '代理不存在' }
      state.proxies.splice(idx, 1)
      // 删掉的正是全局默认那一条 → 自动回落到直连，不留悬空引用
      if (state.globalProxyId === proxy_id) state.globalProxyId = null
      emit('vault:proxies_changed')
      return proxySnapshot()
    },

    async set_global_proxy(proxy_id = null) {
      await delay(60)
      // null / 0 / 空串 = 直连（与后端同一套「空值即直连」的判定）
      const direct = proxy_id === null || proxy_id === undefined || proxy_id === '' || proxy_id === 0 || proxy_id === '0'
      if (!direct && !state.proxies.some((p) => p.id === proxy_id)) {
        return { ok: false, message: '代理不存在' }
      }
      state.globalProxyId = direct ? null : proxy_id
      emit('vault:proxies_changed')
      return proxySnapshot()
    },

    async test_proxy_id(proxy_id = null) {
      await delay(900)   // 真实实现要在直连 / 代理两种模式下各跑一轮探测，慢一点更贴合
      const target = proxy_id
        ? state.proxies.find((p) => p.id === proxy_id)
        : state.proxies.find((p) => p.id === state.globalProxyId)
      if (proxy_id && !target) return { ok: false, message: '代理不存在' }

      const direct = {
        ip: { ok: true, ip: '198.51.100.7', country: '中国', region: '北京', city: '北京', isp: '联通' },
        domestic: { ok: true, ms: 21 },
        foreign: { ok: false },
      }
      const directLine = '直连出口 IP：198.51.100.7（中国 北京 北京 · 联通），国内站点可达，国外站点不可达'

      // 全局被设为「直连」时（target 为空）与后端一致：只测直连，proxied 为 null
      if (!target) {
        return {
          ok: true,
          proxy: '',
          proxy_source: 'global',
          direct,
          proxied: null,
          verdict: {
            level: 'warn',
            headline: '未配置代理，仅测试了直连',
            details: [directLine, '未配置代理，本次仅测试直连。填入代理地址（如 socks5://127.0.0.1:10808）后可测试代理效果。'],
          },
        }
      }

      const socks = target.scheme === 'socks5'
      return {
        ok: true,
        proxy: target.url,
        proxy_source: proxy_id ? `proxy:${target.id}` : 'global',
        direct,
        proxied: {
          ip: { ok: true, ip: '203.0.113.2', country: '美国', region: 'California', city: 'Los Angeles', isp: 'Zenlayer' },
          domestic: { ok: true, ms: 364 },
          foreign: { ok: true, ms: 364 },
        },
        verdict: socks
          ? { level: 'ok', headline: '代理工作正常：出口已切换，国外站点由不可达变为可达',
              details: [directLine, '代理出口 IP：203.0.113.2（美国 California Los Angeles · Zenlayer），国内站点可达，国外站点可达'] }
          : { level: 'warn', headline: '代理可用，但国内站点延迟明显升高',
              details: [directLine, '代理出口 IP：203.0.113.2（美国 California Los Angeles · Zenlayer），国内站点可达，国外站点可达', '若不需访问国外站点，建议对该凭据改用直连'] },
      }
    },

    // ── 简易操作（插件可选能力）────────────────────────────
    async list_credential_actions(credentialId) {
      await delay(60)
      const cred = state.credentials.find((c) => c.id === Number(credentialId))
      if (!cred) return []
      if (cred.credential_type === 'mail_smtp') {
        return [{
          id: 'inbox', name: '查看收件箱', kind: 'form', submit: '拉取',
          fields: [{ key: 'count', label: '条数', type: 'text', default: '10' }],
        }]
      }
      const plugin = state.plugins.find((p) => p.credential_type === cred.credential_type)
      return plugin && plugin.has_actions
        ? [{ id: 'chat', name: '简单对话', kind: 'chat', model: 'mock-model' }]
        : []
    },

    async run_credential_action(credentialId, actionId, params) {
      await delay(400)
      requireUnlocked()
      if (actionId === 'inbox') {
        return {
          ok: true,
          output: '共 3 封，显示最近 3 封（INBOX）：\n\n1. [2026-10-05 09:12] alert@x.com\n   主题：登录提醒',
        }
      }
      const messages = (params && params.messages) || []
      const last = messages.length ? messages[messages.length - 1].content : ''
      return { ok: true, output: `（Mock 回复）你说的是：「${last}」` }
    },

    async list_credential_models(credentialId, force = false) {
      await delay(200)
      const cred = state.credentials.find((c) => c.id === Number(credentialId))
      if (!cred) return []
      return ['mock-model', 'mock-model-mini', 'mock-model-pro']
    },

    async stream_credential_action(credentialId, actionId, params) {
      requireUnlocked()
      const streamId = `mock-${Date.now()}`
      const messages = (params && params.messages) || []
      const last = messages.length ? messages[messages.length - 1].content : ''
      const reply = `（Mock 流式回复）你说的是：「${last}」`
      const chunks = reply.match(/.{1,4}/g) || []
      let index = 0
      const timer = setInterval(() => {
        if (index >= chunks.length) {
          clearInterval(timer)
          emit('vault:action_stream', { stream_id: streamId, done: true, ok: true, output: reply, error: '' })
          return
        }
        emit('vault:action_stream', { stream_id: streamId, delta: chunks[index] })
        index += 1
      }, 70)
      return { ok: true, stream_id: streamId }
    },

    // ── 备份 / 恢复 ────────────────────────────────────────
    async export_backup(scope = 'all', encrypted = true) {
      await delay(140)
      requireUnlocked()
      const count = scope === 'all' ? state.credentials.length : state.credentials.length
      return { ok: true, path: `/mock/keyrealm-backup-${Date.now()}.json`, count, encrypted: !!encrypted }
    },

    async import_backup() {
      await delay(220)
      requireUnlocked()
      return {
        ok: true, encrypted: false, imported: 0,
        skipped_plugin: 0, skipped_duplicate: 0, failed_count: 0, failed: [],
        created_groups: 0,
        log_file: 'import-20260101-000000.log', failed_log_file: '',
        path: '/mock/keyrealm-backup.json',
      }
    },

    // ── 日志 ──────────────────────────────────────────────
    async get_log_config() {
      await delay(40)
      return { level: state.logLevel, levels: clone(LOG_LEVELS), log_file: MOCK_LOG_FILE }
    },

    // mock 里没有真的「用系统程序打开文件」这回事，回个成功保持按钮可用即可
    async open_log_file(name) {
      await delay(60)
      return { ok: true, path: `/mock/logs/${name}` }
    },

    async open_external_url(url) {
      await delay(60)
      // 开发模式没有 pywebview，退回浏览器新标签页；白名单与后端保持一致
      if (url === 'https://github.com/YZBRH/keyrealm') {
        window.open(url, '_blank', 'noopener')
        return { ok: true }
      }
      return { ok: false, message: '不允许打开该链接' }
    },

    async set_log_level(level) {
      await delay(80)
      const next = String(level || '').trim().toUpperCase()
      if (!LOG_LEVELS.includes(next)) {
        return { ok: false, message: `无效日志级别：${level || '(空)'}` }
      }
      state.logLevel = next
      mockLog('backend.api', 'INFO', `日志级别已设置为 ${next}`)
      return { ok: true, level: next }
    },

    async get_logs(limit = 200, level = '') {
      await delay(80)
      const max = Math.max(1, Math.min(Number(limit) || 200, 2000))
      const threshold = String(level || '').trim().toUpperCase()
      const filtered =
        threshold && LOG_LEVELS.includes(threshold)
          ? state.logs.filter((entry) => LOG_LEVEL_VALUES[entry.level] >= LOG_LEVEL_VALUES[threshold])
          : state.logs
      const entries = filtered.slice(-max).map((entry) => entry.text)
      return { ok: true, entries, count: entries.length, log_file: MOCK_LOG_FILE }
    },

    async clear_logs() {
      await delay(60)
      state.logs.length = 0
      mockLog('backend.api', 'INFO', '日志已清空')
      return { ok: true, message: '日志已清空' }
    },

    // ── 程序内更新（dev 桩：不跑 git / 网络，返回假数据）────
    async get_update_channel() {
      await delay(40)
      return { channel: state.updateChannel || 'formal' }
    },
    async set_update_channel(channel) {
      await delay(40)
      state.updateChannel = channel === 'test' ? 'test' : 'formal'
      return { ok: true, channel: state.updateChannel }
    },
    async get_update_proxy() {
      await delay(40)
      return { ok: true, mode: 'global', proxy_id: '', resolved: '' }
    },
    async set_update_proxy(mode, proxyId = '') {
      await delay(40)
      return { ok: true, mode, proxy_id: proxyId, resolved: '' }
    },
    async start_update_check() {
      await delay(200)
      return { ok: true, task_id: 'mock-check' }
    },
    async get_update_check_result() {
      await delay(40)
      return {
        ok: true,
        kind: 'formal',
        has_update: false,
        reason: 'dev mock：未联网，视为与远端一致',
        current: { version: '1.3.0', commit: 'mock000', schema_version: '1.0.0', frozen: false, source_ok: true },
        latest: { version: '1.3.0', commit: 'mock000', schema_version: '1.0.0' },
        db_change: false,
        db_chain: [],
        source_ok: true,
        checked_at: new Date().toISOString(),
      }
    },
    async get_update_auto_check() {
      await delay(20)
      return { enabled: true }
    },
    async set_update_auto_check(enabled) {
      await delay(40)
      return { ok: true, enabled: Boolean(enabled) }
    },
    async take_update_notice() {
      await delay(20)
      return {}
    },
    async update_check(kind = 'formal') {
      await delay(400)
      return {
        ok: true,
        kind,
        has_update: false,
        reason: 'dev mock：未联网，视为与远端一致',
        current: { version: '1.3.0', commit: 'mock000', schema_version: '1.0.0', frozen: false, source_ok: true },
        latest: { version: '1.3.0', commit: 'mock000', schema_version: '1.0.0' },
        db_change: false,
        db_chain: [],
        source_ok: true,
        prepared: null,
      }
    },
    async update_status() {
      await delay(40)
      return {}
    },
    async db_migration_status() {
      await delay(40)
      return { ok: true, disk: '1.0.0', app: '1.0.0', needs_migration: false, chain: [] }
    },
    async start_update() {
      await delay(120)
      return { ok: true, message: 'dev mock：已模拟发起更新（不会真的关闭程序）' }
    },
    async apply_prepared_update() {
      await delay(120)
      return { ok: true, message: 'dev mock：已模拟交棒安装（不会真的关闭程序）' }
    },
  }
}

/** 在普通浏览器中安装 Mock 桥接（仅开发模式调用）。 */
export async function installDevBridge(gracePeriod = 800) {
  if (window.pywebview && window.pywebview.api) return false
  await delay(gracePeriod)
  if (window.pywebview && window.pywebview.api) return false

  window.pywebview = { api: createMockApi() }
  window.__VAULT_MOCK__ = { password: MOCK_PASSWORD }
  window.dispatchEvent(new CustomEvent('pywebviewready'))
  console.info(
    `[Keyrealm] 开发模式：未检测到 pywebview，已启用内存 Mock 后端。主密码：${MOCK_PASSWORD}`,
  )
  return true
}
