/**
 * pywebview JS-API 桥接层。
 *
 * pywebview 会把后端 Python 对象注入为 `window.pywebview.api`，
 * 所有方法均为异步（返回 Promise）。注入完成时会派发 `pywebviewready` 事件。
 *
 * 约定：
 *  - 后端返回 `{ ok: false, message }` 或 `{ error: '错误码', message?/details? }`
 *    均视为业务失败，这里统一抛出 ApiError，
 *    调用方只需 try/catch 即可（无需逐处判断 res.ok）。
 *  - 后端抛出的 Python 异常会以 rejected Promise 形式到达，同样转成 ApiError。
 *  - pywebview 桥接只支持位置参数，各方法传参顺序必须与 main.py 中
 *    VaultApi 对应方法的签名严格一致（dev mock 也保持同样签名）。
 */

const READY_EVENT = 'pywebviewready'
const READY_TIMEOUT = 20000

export class ApiError extends Error {
  constructor(message, method) {
    super(message)
    this.name = 'ApiError'
    this.method = method
  }
}

function getBridge() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null
}

let readyPromise = null

/** 等待 pywebview 注入完成。 */
export function whenReady() {
  const bridge = getBridge()
  if (bridge) return Promise.resolve(bridge)
  if (readyPromise) return readyPromise

  readyPromise = new Promise((resolve, reject) => {
    let timer = null
    const cleanup = () => {
      window.removeEventListener(READY_EVENT, onReady)
      if (timer) clearTimeout(timer)
    }
    const onReady = () => {
      cleanup()
      resolve(getBridge())
    }
    window.addEventListener(READY_EVENT, onReady)
    timer = setTimeout(() => {
      cleanup()
      readyPromise = null
      const late = getBridge()
      if (late) resolve(late)
      else reject(new ApiError('无法连接后端服务：pywebview 未就绪', 'whenReady'))
    }, READY_TIMEOUT)
  })
  return readyPromise
}

function unwrap(result, method) {
  if (result && typeof result === 'object' && !Array.isArray(result)) {
    // 后端约定一：{ ok: false, ... } 表示业务失败。
    // **插件操作返回的是 { ok: false, error: "具体原因" }**（没有 message 字段），
    // 这里必须把 error 也取上，否则所有插件错误都显示成一句通用的「操作失败」。
    if (result.ok === false) {
      const detail = Array.isArray(result.details) ? result.details.join('；') : ''
      throw new ApiError(result.message || result.error || detail || '操作失败', method)
    }
    // 后端约定二：{ error: '错误码', message?/details? } 同样表示失败
    // （如 vault_locked / validation_failed / not_found），不能当成成功返回，
    // 否则调用方会误报成功（例如凭据校验失败却提示"已创建"）。
    if (typeof result.error === 'string') {
      const detail = Array.isArray(result.details) ? result.details.join('；') : ''
      throw new ApiError(result.message || detail || `操作失败（${result.error}）`, method)
    }
  }
  return result
}

/** 调用任意后端方法。 */
export async function call(method, ...args) {
  const bridge = await whenReady()
  const fn = bridge[method]
  if (typeof fn !== 'function') {
    throw new ApiError(`后端未实现接口：${method}`, method)
  }
  let result
  try {
    result = await fn(...args)
  } catch (err) {
    const message = err && err.message ? err.message : String(err)
    throw new ApiError(message || `调用 ${method} 失败`, method)
  }
  return unwrap(result, method)
}

/** 监听后端推送事件，返回取消订阅函数。 */
export function onEvent(name, handler) {
  const listener = (event) => handler(event && event.detail)
  window.addEventListener(name, listener)
  return () => window.removeEventListener(name, listener)
}

export const EVENTS = {
  credentialUpdated: 'vault:credential_updated',
  credentialsChanged: 'vault:credentials_changed',
  pluginsChanged: 'vault:plugins_changed',
  // 代理注册表变更（本界面改动 / MCP 侧改动都会广播）。消费方按需重取列表。
  proxiesChanged: 'vault:proxies_changed',
  actionStream: 'vault:action_stream',
  // 批量验证开始：detail 带本次要验证的凭据 id 列表，前端据此把对应行的「验证」按钮置成 loading
  batchStarted: 'vault:batch_verify_started',
  batchProgress: 'vault:batch_verify_progress',
  batchDone: 'vault:batch_verify_done',
  mcpStatus: 'vault:mcp_status',
  closeConfirm: 'vault:close_confirm',
  // 保险库被锁定（界面按钮 / 托盘 / 空闲自动锁都会经 lock_vault 广播）。
  // 消费方据此清空内存明文并跳回解锁页。
  vaultLocked: 'vault:locked',
  // 后台任务变更。detail 是**发生变化的那一条任务快照**（不是全量列表），
  // 消费方就地更新/插入对应行即可；拿不到快照时才回退到 listTasks()。
  tasksChanged: 'vault:tasks',
  // 更新准备/交棒失败（准备阶段在应用内跑，失败时**不退出**）。detail={kind,message}。
  updateFailed: 'vault:update_failed',
  // 「自动检测更新」在启动/解锁后查到了新版本（detail 与 check_update 结果同形）。
  updateAvailable: 'vault:update_available',
  // （手动或自动）检查更新结束，detail 即检查结果。界面据此刷新「检查更新」卡片。
  updateChecked: 'vault:update_checked',
}

export const api = {
  // ── 认证 / 锁 ────────────────────────────────────────────
  setupMasterPassword: (password) => call('setup_master_password', password),
  unlock: (password) => call('unlock', password),
  lockVault: () => call('lock_vault'),
  isInitialized: () => call('is_initialized'),
  isUnlocked: () => call('is_unlocked'),
  changeMasterPassword: (oldPassword, newPassword) =>
    call('change_master_password', oldPassword, newPassword),

  // ── 凭据 ────────────────────────────────────────────────
  listCredentials: (filters = {}) =>
    call(
      'list_credentials',
      filters.group_id ?? null,
      filters.credential_type ?? null,
      filters.status ?? null,
      filters.search ? filters.search.trim() : null,
      filters.credential_types ?? null,
    ),
  getCredential: (id) => call('get_credential', id),
  // 注意：pywebview 桥接只支持位置参数，顺序必须与后端
  // VaultApi.create_credential(name, credential_type, data, group_id, auto_verify) 严格一致
  createCredential: (payload) =>
    call(
      'create_credential',
      payload.name,
      payload.credential_type,
      payload.data ?? {},
      payload.group_id ?? null,
      payload.auto_verify !== false,
    ),
  updateCredential: (id, payload) =>
    call(
      'update_credential',
      id,
      payload.name ?? null,
      payload.group_id ?? null,
      payload.data ?? null,
    ),
  deleteCredential: (id) => call('delete_credential', id),
  // 批量删除：一次 IPC 删一批（后端单事务）。pywebview 位置参数，ids 传数组。
  deleteCredentials: (ids) => call('delete_credentials', ids),
  verifyCredential: (id) => call('verify_credential', id),
  listCredentialActions: (id) => call('list_credential_actions', id),
  // 该凭据「真正能用」的操作（插件可选能力）；{available, note, error}
  credentialCapabilities: (id) => call('credential_capabilities', id),
  // 弹系统「另存为」，拿用户选的路径（下载类操作先用它换路径，再塞进 params 交给插件）
  pickSavePath: (defaultName = '', fileTypes = null) => call('pick_save_path', defaultName, fileTypes),
  runCredentialAction: (id, actionId, params) => call('run_credential_action', id, actionId, params),
  listCredentialModels: (id, force = false) => call('list_credential_models', id, force),
  // 版本信息：{version, build, schema_version, display}；display 形如 "1.0.0 (fe15e3b)"
  getVersion: () => call('get_version'),
  listDraftModels: (credentialType, data = null) => call('list_draft_models', credentialType, data),
  streamCredentialAction: (id, actionId, params, title) =>
    call('stream_credential_action', id, actionId, params, title),
  batchVerify: (scope = 'all', groupId = null, credentialType = null, credentialIds = null,
    concurrency = null, timeout = null) =>
    call('batch_verify', scope, groupId, credentialType, credentialIds, concurrency, timeout),
  getBatchOptions: () => call('get_batch_options'),
  checkDuplicateCredential: (credentialType, data) =>
    call('check_duplicate_credential', credentialType, data),

  // ── 分组 ────────────────────────────────────────────────
  listGroups: () => call('list_groups'),
  createGroup: (name, description = '', color = '#409EFF') =>
    call('create_group', name, description, color),
  updateGroup: (id, payload = {}) =>
    call('update_group', id, payload.name ?? null, payload.description ?? null, payload.color ?? null),
  deleteGroup: (id) => call('delete_group', id),

  // ── 插件（放入 plugins/ 目录即自动加载，无需手动管理）────
  listPlugins: () => call('list_plugins'),
  // 启用/禁用插件（credential_type, enabled）
  setPluginEnabled: (credentialType, enabled) => call('set_plugin_enabled', credentialType, enabled),

  // ── MCP ─────────────────────────────────────────────────
  getMcpStatus: () => call('get_mcp_status'),
  startMcp: () => call('start_mcp'),
  stopMcp: () => call('stop_mcp'),
  getMcpOptions: () => call('get_mcp_options'),
  setMcpOptions: (host, port) => call('set_mcp_options', host, port),
  listMcpPermissions: () => call('list_mcp_permissions'),
  setMcpPermission: (toolName, enabled) => call('set_mcp_permission', toolName, enabled),
  getMcpToken: () => call('get_mcp_token'),
  rotateMcpToken: () => call('rotate_mcp_token'),

  // ── 定时计划 ────────────────────────────────────────────
  listSchedules: () => call('list_schedules'),
  createSchedule: (payload) =>
    call(
      'create_schedule',
      payload.name,
      payload.trigger_type,
      payload.trigger_args ?? {},
      payload.target_scope ?? 'all',
      payload.target_id ?? null,
      payload.credential_ids ?? [],
    ),
  updateSchedule: (id, payload) => call('update_schedule', id, payload),
  deleteSchedule: (id) => call('delete_schedule', id),

  // ── 托盘 ────────────────────────────────────────────────
  getTrayStatus: () => call('get_tray_status'),
  minimizeToTray: () => call('minimize_to_tray'),

  // 把**用户看不见的**失败回传到后端日志（WebView 里的 console.error 用户看不到）。
  logFrontendError: (message) => call('log_frontend_error', message),

  // ── 关闭窗口行为 ────────────────────────────────────────
  getCloseAction: () => call('get_close_action'),
  setCloseAction: (action) => call('set_close_action', action),
  confirmCloseAction: (action, remember = false) =>
    call('confirm_close_action', action, remember),

  // ── 空闲自动锁定 ────────────────────────────────────────
  getAutoLock: () => call('get_auto_lock'),
  setAutoLock: (minutes) => call('set_auto_lock', minutes),
  touchActivity: () => call('touch_activity'),

  // ── 轻量模式 ────────────────────────────────────────────
  // 开启后最小化到托盘会释放（销毁）界面文档以省内存；再次打开窗口时后端重新加载前端。
  getLightMode: () => call('get_light_mode'),
  setLightMode: (enabled) => call('set_light_mode', enabled),

  // ── 开机自启动 ──────────────────────────────────────────
  // 读写的是操作系统自启条目（Windows 注册表 Run / macOS LaunchAgent / Linux XDG
  // autostart），系统侧即唯一事实来源，后端不落 settings。
  // getAutostart 返回 { supported, enabled, platform, target }。
  getAutostart: () => call('get_autostart'),
  setAutostart: (enabled) => call('set_autostart', enabled),

  // ── 凭据目录树顺序 ──────────────────────────────────────
  // 用户在「凭据目录」里拖拽调整「类别 / 插件」两层顺序后回存。存的是顺序清单而非全量快照：
  // 未列到的条目（如新装插件）由前端按自然顺序追加到末尾，故新增插件不会让顺序失效。
  // 传空数组表示该层恢复默认顺序。返回 { ok, categories, plugins }。
  getTreeOrder: () => call('get_tree_order'),
  setTreeOrder: (categories = [], plugins = []) =>
    call('set_tree_order', categories, plugins),

  // ── 验证代理（注册表，其它模块一律按 id 引用） ──────────
  listProxies: () => call('list_proxies'),
  createProxy: (name, url) => call('create_proxy', name, url),
  updateProxy: (proxyId, name = null, url = null) => call('update_proxy', proxyId, name, url),
  deleteProxy: (proxyId) => call('delete_proxy', proxyId),
  // proxyId 传 null/0 表示「直连」
  setGlobalProxy: (proxyId = null) => call('set_global_proxy', proxyId),
  // 连通性自检：proxyId 传 null 表示测当前全局生效的那个。
  // 返回 { ok, proxy, proxy_source, direct, proxied, verdict }。
  testProxyId: (proxyId = null) => call('test_proxy_id', proxyId),

  // ── TLS 版本策略（框架级） ──────────────────────────────
  // 返回 { ok, tls_version, tls_versions }；值 ∈ auto / 1.0 / 1.1 / 1.2 / 1.3
  getTlsOptions: () => call('get_tls_options'),
  setTlsVersion: (value) => call('set_tls_version', value),

  // ── 类别 / 插件默认设置 ─────────────────────────────────
  getCredentialDefaults: (credentialType) => call('get_credential_defaults', credentialType),
  getScopeOptions: (scope, scopeKey, credentialType = '') =>
    call('get_scope_options', scope, scopeKey, credentialType),
  setScopeOptions: (scope, scopeKey, values, credentialType = '') =>
    call('set_scope_options', scope, scopeKey, values, credentialType),
  clearScopeOptions: (scope, scopeKey) => call('clear_scope_options', scope, scopeKey),

  // ── 备份 / 恢复 ─────────────────────────────────────────
  exportBackup: (scope = 'all', encrypted = true, credentialIds = null) =>
    call('export_backup', scope, encrypted, credentialIds),
  // 导入备份。来自**别的保险库**（v2 备份自带 salt）时：先不带参调用 → 返回 need_password
  // + path，再带「原主密码 + 同一文件」重试（顺序同 VaultApi.import_backup(orig_password, path)）。
  importBackup: (origPassword = null, path = null) => call('import_backup', origPassword, path),

  // ── 日志 ────────────────────────────────────────────────
  getLogConfig: () => call('get_log_config'),
  setLogLevel: (level) => call('set_log_level', level),
  // 参数顺序必须与 main.py 的 VaultApi.get_logs(limit, level, search) 严格一致。
  // search 为空串时后端按"不过滤"处理，与传 null 等价。
  getLogs: (limit = 200, level = '', search = '') => call('get_logs', limit, level, search),
  clearLogs: () => call('clear_logs'),
  // 用系统默认程序打开 data/logs/ 下的日志文件（导入失败后的一键打开）。
  // 后端只认单个文件名，路径拼接由它自己完成。
  openLogFile: (name) => call('open_log_file', name),

  // ── 后台任务 ────────────────────────────────────────────
  // 长操作（下载/打包等）跑在后台线程，关掉弹窗也在继续。listTasks 返回的数组
  // 已按「进行中在前、其内按创建时间倒序」排好，前端照序渲染即可。
  listTasks: () => call('list_tasks'),
  // 协作式取消：只置标志位，插件在下一个检查点停下（不是立刻杀线程）。
  cancelTask: (taskId) => call('cancel_task', taskId),
  // 清掉已结束（已完成/失败/已取消）的记录，进行中的不动。
  clearFinishedTasks: () => call('clear_finished_tasks'),

  // ── 程序内更新（从源码编译）──────────────────────────────
  // 渠道：formal=正式版（看版本号）/ test=测试版（看版本号 + commit）。存后端 settings。
  getUpdateChannel: () => call('get_update_channel'),
  setUpdateChannel: (channel) => call('set_update_channel', channel),
  // 更新走哪个代理：{mode: global|direct|proxy, proxy_id, resolved}。顺序同 VaultApi.set_update_proxy(mode, proxy_id)
  getUpdateProxy: () => call('get_update_proxy'),
  setUpdateProxy: (mode, proxyId = '') => call('set_update_proxy', mode, proxyId),
  // 更新访问令牌（私有仓库）：{ok, configured, mask}。明文只在后端内存/env，前端只拿掩码。空串 = 清除。
  getUpdateToken: () => call('get_update_token'),
  setUpdateToken: (token) => call('set_update_token', token),
  // 自动检测更新开关：{enabled}。**默认开启**——启动与解锁后各自动查一次。
  getUpdateAutoCheck: () => call('get_update_auto_check'),
  setUpdateAutoCheck: (enabled) => call('set_update_auto_check', enabled),
  // 取出「自动检测发现的更新」通知（取出即清除，避免重复弹）；无则返回 {}。
  takeUpdateNotice: () => call('take_update_notice'),
  // 「检查更新」= **后台任务**：立即返回 {ok, task_id}，结果经 vault:update_checked 推送，
  // 并持久化（getUpdateCheckResult 可随时取回，换页 / 重启后仍在，直到被新结果替换）。
  startUpdateCheck: () => call('start_update_check'),
  getUpdateCheckResult: () => call('get_update_check_result'),
  // 检查更新：{ok, kind, has_update, reason, current, latest, db_change, db_chain, source_ok}
  checkUpdate: (kind = 'formal') => call('update_check', kind),
  // 上一次更新的结果（读 data/.update-status.json；无则 {}）
  updateStatus: () => call('update_status'),
  // 磁盘数据库结构版本与源码版本的差异：{ok, disk, app, needs_migration, chain}
  dbMigrationStatus: () => call('db_migration_status'),
  // 立即更新：**应用内先准备**（备份+下载/拉源码/装依赖/构建，带进度、可取消），准备成功后交棒并自动关闭程序。
  // 返回 {ok, task_id, preparing}；准备进度经 vault:tasks（kind==='update'）推送。
  startUpdate: (kind = 'formal', restart = true, terminal = 'auto') =>
    call('start_update', kind, restart, terminal),
}

export default api