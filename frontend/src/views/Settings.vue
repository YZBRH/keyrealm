<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Delete, Edit, Key, Lock, Plus, Refresh } from '@element-plus/icons-vue'

import { api, EVENTS, onEvent } from '@/api'
import ThemePicker from '@/components/ThemePicker.vue'
import { useAppStore } from '@/stores/app'
import { useProxyStore } from '@/stores/proxy'

const appStore = useAppStore()

// Web / CLI 模式没有系统托盘与桌面窗口：相关设置项不展示（后端也明确返回不支持）
const isWebMode = computed(() => !!(window.__KEYREALM_WEB__ || {}).token)

/* ── 主密码 ─────────────────────────────────────────── */

const passwordDialog = ref(false)
const passwordSubmitting = ref(false)
const passwordFormRef = ref(null)
const passwordForm = reactive({ old_password: '', new_password: '', confirm_password: '' })

const passwordStrength = computed(() => {
  const value = passwordForm.new_password
  if (!value) return { level: 0, label: '' }
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1
  if (/\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  const level = Math.min(score, 4)
  const labels = ['很弱', '较弱', '一般', '较强', '很强']
  return { level, label: labels[level] }
})

const passwordRules = {
  old_password: [{ required: true, message: '请输入当前主密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新主密码', trigger: 'blur' },
    { min: 6, message: '新密码至少 6 位', trigger: 'blur' },
  ],
  confirm_password: [
    { required: true, message: '请再次输入新主密码', trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        if (value !== passwordForm.new_password) callback(new Error('两次输入的新密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

function openPasswordDialog() {
  passwordForm.old_password = ''
  passwordForm.new_password = ''
  passwordForm.confirm_password = ''
  passwordDialog.value = true
}

async function submitPassword() {
  if (!passwordFormRef.value) return
  try {
    await passwordFormRef.value.validate()
  } catch {
    return
  }

  passwordSubmitting.value = true
  try {
    await api.changeMasterPassword(passwordForm.old_password, passwordForm.new_password)
    ElMessage.success('主密码已更新，所有凭据已使用新密钥重新加密')
    passwordDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '修改主密码失败')
  } finally {
    passwordSubmitting.value = false
  }
}

async function lockNow() {
  await appStore.lock()
  ElMessage.success('保险库已锁定')
}

/* ── 验证代理（多代理注册表 + 全局默认） ────────────────
   代理一律按 id 引用；测试的进行态与结果放在 store 里，切页不会丢。 */

const proxyStore = useProxyStore()

const proxyDialog = ref(false)
const proxySaving = ref(false)
const proxyForm = reactive({ id: null, name: '', url: '' })

const globalProxyValue = computed(() => proxyStore.globalId ?? '')

function openCreateProxy() {
  proxyForm.id = null
  proxyForm.name = ''
  proxyForm.url = ''
  proxyDialog.value = true
}

function openEditProxy(item) {
  proxyForm.id = item.id
  proxyForm.name = item.name
  proxyForm.url = item.url
  proxyDialog.value = true
}

async function saveProxy() {
  const name = proxyForm.name.trim()
  const url = proxyForm.url.trim()
  if (!name) {
    ElMessage.warning('请填写代理名称')
    return
  }
  if (!url) {
    ElMessage.warning('请填写代理地址')
    return
  }
  proxySaving.value = true
  try {
    if (proxyForm.id) {
      await proxyStore.update(proxyForm.id, name, url)
      ElMessage.success('代理已更新')
    } else {
      await proxyStore.create(name, url)
      ElMessage.success('代理已新增')
    }
    // 地址变了旧结论就失效，避免用户对着过期结果下判断
    proxyStore.clearResult()
    proxyDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    proxySaving.value = false
  }
}

async function removeProxy(item) {
  try {
    await ElMessageBox.confirm(
      `确定删除代理「${item.name}」吗？引用它的凭据会自动回退到全局代理。`,
      '删除代理',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await proxyStore.remove(item.id)
    ElMessage.success('代理已删除')
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function changeGlobalProxy(value) {
  try {
    await proxyStore.setGlobal(value === '' ? null : value)
    ElMessage.success(value === '' ? '全局已切换为直连' : '已设为全局默认代理')
  } catch (err) {
    ElMessage.error(err.message || '设置失败')
    await proxyStore.fetch().catch(() => {})
  }
}

/* 代理连通性测试：后端同一批探测分别走直连与代理，前端只负责展示对照结果 */
async function runProxyTest(id = null) {
  try {
    const result = await proxyStore.runTest(id)
    if (result && result.ok === false) {
      ElMessage.error(result.message || '代理测试失败')
    }
  } catch (err) {
    ElMessage.error(err.message || '代理测试失败')
  }
}

function verdictType(level) {
  return level === 'ok' ? 'success' : level === 'fail' ? 'error' : 'warning'
}

function probeClass(section) {
  if (!section) return ''
  return section.ok ? 'probe-ok' : 'probe-bad'
}

function probeSiteText(section) {
  if (!section) return '—'
  return section.ok ? `可达（${section.ms}ms）` : '不可达'
}

function probeIpText(info) {
  if (!info) return '—'
  if (!info.ok) return `查询失败：${info.detail || '未知原因'}`
  const geo = [info.country, info.region, info.city].filter(Boolean).join(' ')
  const isp = info.isp ? ` · ${info.isp}` : ''
  return geo ? `${info.ip}（${geo}${isp}）` : info.ip
}

/* ── 关闭窗口行为 ───────────────────────────────────── */
const closeAction = ref('ask')
const closeActionOptions = ref([
  { value: 'ask', label: '每次询问我' },
  { value: 'quit', label: '直接退出程序' },
  { value: 'background', label: '后台运行（最小化到托盘）' },
])
const closeSaving = ref(false)

async function loadCloseAction() {
  try {
    const config = await api.getCloseAction()
    if (config.action) closeAction.value = config.action
    if (Array.isArray(config.options) && config.options.length) {
      closeActionOptions.value = config.options
    }
  } catch (err) {
    ElMessage.error(err.message || '加载关闭设置失败')
  }
}

async function applyCloseAction(value) {
  closeSaving.value = true
  try {
    await api.setCloseAction(value)
    ElMessage.success('关闭行为已更新')
  } catch (err) {
    ElMessage.error(err.message || '设置关闭行为失败')
    await loadCloseAction()
  } finally {
    closeSaving.value = false
  }
}

/* ── 空闲自动锁定 ───────────────────────────────────── */
const autoLockMinutes = ref(0)
const autoLockOptions = ref([
  { value: 0, label: '关闭' },
  { value: 1, label: '1 分钟' },
  { value: 5, label: '5 分钟' },
  { value: 10, label: '10 分钟' },
  { value: 15, label: '15 分钟' },
  { value: 30, label: '30 分钟' },
  { value: 60, label: '60 分钟' },
])
const autoLockSaving = ref(false)

async function loadAutoLock() {
  try {
    const config = await api.getAutoLock()
    if (typeof config.minutes === 'number') autoLockMinutes.value = config.minutes
    if (Array.isArray(config.options) && config.options.length) {
      autoLockOptions.value = config.options
    }
  } catch (err) {
    ElMessage.error(err.message || '加载空闲锁定设置失败')
  }
}

async function applyAutoLock(value) {
  autoLockSaving.value = true
  try {
    await api.setAutoLock(value)
    ElMessage.success(value ? `已开启：空闲 ${value} 分钟后自动锁定` : '已关闭空闲自动锁定')
  } catch (err) {
    ElMessage.error(err.message || '设置空闲自动锁定失败')
    await loadAutoLock()
  } finally {
    autoLockSaving.value = false
  }
}

/* ── 不锁定 ─────────────────────────────────────────── */
const neverLock = ref(false)
const neverLockInfo = ref({ persisted: false, protection_label: '' })
const neverLockSaving = ref(false)

async function loadNeverLock() {
  try {
    const config = await api.getNeverLock()
    neverLock.value = Boolean(config.enabled)
    neverLockInfo.value = {
      persisted: Boolean(config.persisted),
      protection_label: config.protection_label || '',
    }
  } catch (err) {
    ElMessage.error(err.message || '加载「不锁定」设置失败')
  }
}

async function applyNeverLock(value) {
  neverLockSaving.value = true
  try {
    const res = await api.setNeverLock(value)
    neverLockInfo.value = {
      persisted: Boolean(res && res.persisted),
      protection_label: (res && res.protection_label) || '',
    }
    if (res && res.message) ElMessage.warning(res.message)
    else ElMessage.success(value ? '已开启「不锁定」' : '已关闭「不锁定」')
    if (!value) await loadNeverLock()
  } catch (err) {
    ElMessage.error(err.message || '设置「不锁定」失败')
    await loadNeverLock()
  } finally {
    neverLockSaving.value = false
  }
}

/* ── 轻量模式 ───────────────────────────────────────── */
// 开关只影响后端「隐藏到托盘」时的行为，前端不需要跟着变什么。
const lightMode = ref(false)
const lightModeSaving = ref(false)

async function loadLightMode() {
  try {
    const config = await api.getLightMode()
    lightMode.value = Boolean(config.enabled)
  } catch (err) {
    ElMessage.error(err.message || '加载轻量模式设置失败')
  }
}

async function applyLightMode(value) {
  lightModeSaving.value = true
  try {
    await api.setLightMode(value)
    ElMessage.success(
      value ? '已开启轻量模式：最小化到托盘时释放界面' : '已关闭轻量模式',
    )
  } catch (err) {
    ElMessage.error(err.message || '设置轻量模式失败')
    await loadLightMode()
  } finally {
    lightModeSaving.value = false
  }
}

/* ── 开屏动画 ─────────────────────────────────────── */
const splashDisabled = ref(false)
const splashAlways = ref(false)
const splashSaving = ref(false)
const splashAlwaysSaving = ref(false)

async function loadSplashDisabled() {
  try {
    const config = await api.getSplashDisabled()
    splashDisabled.value = Boolean(config.disabled)
    splashAlways.value = Boolean(config.always)
  } catch (err) {
    ElMessage.error(err.message || '加载开屏动画设置失败')
  }
}

async function applySplashDisabled(value) {
  splashSaving.value = true
  try {
    await api.setSplashDisabled(value)
    ElMessage.success(value ? '已禁用开屏动画（下次启动生效）' : '已启用开屏动画（下次启动生效）')
  } catch (err) {
    ElMessage.error(err.message || '设置开屏动画失败')
    await loadSplashDisabled()
  } finally {
    splashSaving.value = false
  }
}

async function applySplashAlways(value) {
  splashAlwaysSaving.value = true
  try {
    await api.setSplashAlways(value)
    ElMessage.success(value ? '已开启：每次打开页面都会播放开屏动画' : '已关闭：仅在启动时播放开屏动画')
  } catch (err) {
    ElMessage.error(err.message || '设置开屏动画失败')
    await loadSplashDisabled()
  } finally {
    splashAlwaysSaving.value = false
  }
}

/* ── 开机自启动 ────────────────────────────────────── */
// 状态以**操作系统**的自启条目为准（后端每次现读），前端不做本地记忆，
// 免得用户手动删掉系统条目后界面还显示"已开启"。
const autostart = ref({ supported: true, enabled: false, platform: '', target: '' })
const autostartSaving = ref(false)

async function loadAutostart() {
  try {
    const config = await api.getAutostart()
    autostart.value = {
      supported: Boolean(config.supported),
      enabled: Boolean(config.enabled),
      platform: config.platform || '',
      target: config.target || '',
    }
  } catch (err) {
    ElMessage.error(err.message || '加载开机自启动设置失败')
  }
}

async function applyAutostart(value) {
  autostartSaving.value = true
  try {
    const res = await api.setAutostart(value)
    // 回读系统侧的真实结果（写入可能被系统策略改写）
    if (res && typeof res.enabled === 'boolean') autostart.value.enabled = res.enabled
    ElMessage.success(autostart.value.enabled ? '已开启开机自启动' : '已关闭开机自启动')
  } catch (err) {
    ElMessage.error(err.message || '设置开机自启动失败')
    await loadAutostart()
  } finally {
    autostartSaving.value = false
  }
}

/* ── 程序内更新 ────────────────────────────────────────────
   检查只读：后端用 git 浅克隆读远端版本，不改本机工作区。
   「立即更新」= 应用内先「准备」（备份 + 下载/拉源码/装依赖/构建，带进度、可取消），
   准备完成后由全局弹窗询问是否立即重启；确认后才交棒给脱离进程（替换/迁移/重启）。 */
const updateChannel = ref('formal')
const updateChecking = ref(false)
const updateResult = ref(null)     // check_update 的结果
const updateLastStatus = ref(null) // 上次更新的结果（data/.update-status.json）
// 「上次更新」框可被用户关掉：记住被关掉那次的 finished_at，关掉后跨重启也不再自动冒出来，
// 直到出现**新的**更新结果（finished_at 变了）才重新显示。
const UPDATE_LAST_SEEN_KEY = 'vault.update.lastSeen'
const dismissedUpdateAt = ref(localStorage.getItem(UPDATE_LAST_SEEN_KEY) || '')
const dbMigration = ref(null)      // { disk, app, needs_migration, chain }
const updating = ref(false)            // 正在准备 / 等待交棒
const updatePrepareTask = ref(null)    // 准备阶段的后台任务快照（vault:tasks，kind==='update'）
const updateFailed = ref('')           // 准备/交棒失败原因（此时**不退出**应用）

// 更新阶段 → 界面文案（与后端 updater._STAGE_LABELS 对齐）
const STAGE_LABELS = {
  backup: '备份', download: '下载预编译包', source: '获取源码', deps: '准备构建环境',
  build: '构建', extract: '解压替换', install: '安装新版本', migrate: '迁移数据库', restart: '重启',
}
function stageLabel(name) {
  return STAGE_LABELS[name] || name
}

// 「上次更新」的版本更迭文案：`（1.3.0 (0c1c3ff) → 1.3.0 (68f89e1)）`。
// 测试版两版的版本号常常相同，只有 commit 哈希才看得出确实换了版本。
function updateTransition(status) {
  if (!status || !status.from) return ''
  const one = (v, c) => `${v || '?'}${c ? ` (${c})` : ''}`
  return `（${one(status.from, status.from_commit)} → ${one(status.to, status.to_commit)}）`
}

// 「上次更新」是否该显示：有结果，且不是用户已关掉的那一次。
const lastUpdateVisible = computed(() => {
  const st = updateLastStatus.value
  return !!(st && st.finished_at && st.finished_at !== dismissedUpdateAt.value)
})

/** 关掉「上次更新」框：按 finished_at 记住，新的更新结果（不同 finished_at）会重新显示。 */
function dismissUpdateStatus() {
  const at = updateLastStatus.value && updateLastStatus.value.finished_at
  if (!at) return
  dismissedUpdateAt.value = at
  try { localStorage.setItem(UPDATE_LAST_SEEN_KEY, at) } catch { /* 存不了也无妨，仅本次会话生效 */ }
}

// 准备进度：total<=0（总量未知，如构建）走不定态
const preparePercent = computed(() => {
  const t = updatePrepareTask.value
  if (!t || !t.total || t.total <= 0) return 0
  return Math.min(100, Math.round((t.current / t.total) * 100))
})
const prepareIndeterminate = computed(() => {
  const t = updatePrepareTask.value
  return !t || !t.total || t.total <= 0
})

// 发现更新的标题：测试版突出 commit 哈希
const updateFoundTitle = computed(() => {
  const r = updateResult.value
  if (!r || !r.ok || !r.has_update) return ''
  const v = (r.latest && r.latest.version) || ''
  const c = (r.latest && r.latest.commit) || ''
  if (r.kind === 'test' && c) {
    if (v && r.current && v === r.current.version) return `发现新提交 ${c}`
    return `发现新版本 ${v}（${c}）`
  }
  return `发现新版本 ${v}`
})

// 自动检测更新（**默认开启**）：启动/解锁后后端自动查一次，发现新版本会弹通知。
const updateAutoCheck = ref(true)
const updateAutoCheckSaving = ref(false)

async function loadUpdateAutoCheck() {
  try {
    const res = await api.getUpdateAutoCheck()
    if (res && res.enabled !== undefined) updateAutoCheck.value = Boolean(res.enabled)
  } catch { /* 容错：不阻塞其它卡片 */ }
}

async function applyUpdateAutoCheck(value) {
  updateAutoCheckSaving.value = true
  try {
    await api.setUpdateAutoCheck(value)
    ElMessage.success(value ? '已开启自动检测更新' : '已关闭自动检测更新')
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
    await loadUpdateAutoCheck()
  } finally {
    updateAutoCheckSaving.value = false
  }
}

// 更新代理：与应用内每凭据的「使用代理」同一套语义，但这里是**全局**一份。
// 选择值 = 'global' | 'direct' | <某代理 id>。
const updateProxySpec = ref('global')
const updateProxyResolved = ref('')

async function loadUpdateProxy() {
  try {
    const res = await api.getUpdateProxy()
    if (res && res.ok !== false) {
      updateProxySpec.value = res.mode === 'proxy' ? (res.proxy_id || 'global') : (res.mode || 'global')
      updateProxyResolved.value = res.resolved || ''
    }
  } catch { /* 容错：不阻塞其它卡片 */ }
}

async function applyUpdateProxy(value) {
  try {
    const res = value === 'global' || value === 'direct'
      ? await api.setUpdateProxy(value, '')
      : await api.setUpdateProxy('proxy', value)
    if (res && res.resolved !== undefined) updateProxyResolved.value = res.resolved || ''
    updateResult.value = null   // 代理变了，上一次的检查结果就作废了
    ElMessage.success('更新代理已更新')
  } catch (err) {
    ElMessage.error(err.message || '保存更新代理失败')
    await loadUpdateProxy()
  }
}

// 访问令牌（可选：认证 GitHub API 提速率上限；仓库转私有时必需）：后端加密保存，前端只拿掩码；不回显明文。
const updateTokenConfigured = ref(false)
const updateTokenMask = ref('')
const updateTokenInput = ref('')
const updateTokenSaving = ref(false)

async function loadUpdateToken() {
  try {
    const res = await api.getUpdateToken()
    if (res && res.ok !== false) {
      updateTokenConfigured.value = !!res.configured
      updateTokenMask.value = res.mask || ''
    }
  } catch { /* 容错：不阻塞其它卡片 */ }
}

async function saveUpdateToken() {
  const token = updateTokenInput.value.trim()
  if (!token) {
    ElMessage.warning('请粘贴访问令牌')
    return
  }
  updateTokenSaving.value = true
  try {
    const res = await api.setUpdateToken(token)
    if (res && res.ok === false) {
      ElMessage.error(res.message || '保存访问令牌失败')
      return
    }
    updateTokenInput.value = ''
    updateTokenConfigured.value = !!res.configured
    updateTokenMask.value = res.mask || ''
    updateResult.value = null
    ElMessage.success('访问令牌已保存')
  } catch (err) {
    ElMessage.error(err.message || '保存访问令牌失败')
  } finally {
    updateTokenSaving.value = false
  }
}

async function clearUpdateToken() {
  try {
    await ElMessageBox.confirm(
      '清除后，检查与更新将恢复为匿名访问——仓库公开时照常可用，只是会走 GitHub API 的匿名限流。是否继续？',
      '清除访问令牌',
      { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.setUpdateToken('')
    updateTokenConfigured.value = false
    updateTokenMask.value = ''
    updateResult.value = null
    ElMessage.success('访问令牌已清除')
  } catch (err) {
    ElMessage.error(err.message || '清除访问令牌失败')
  }
}

async function loadUpdateChannel() {
  try {
    const res = await api.getUpdateChannel()
    if (res && res.channel) updateChannel.value = res.channel
  } catch { /* 容错：不阻塞其它卡片 */ }
}

async function applyUpdateChannel(value) {
  updateResult.value = null
  try {
    await api.setUpdateChannel(value)
  } catch (err) {
    ElMessage.error(err.message || '保存更新渠道失败')
    await loadUpdateChannel()
  }
}

async function loadUpdateStatus() {
  try {
    updateLastStatus.value = await api.updateStatus()
  } catch {
    updateLastStatus.value = null
  }
}

async function loadDbMigrationStatus() {
  try {
    const res = await api.dbMigrationStatus()
    dbMigration.value = res && res.ok !== false ? res : null
  } catch {
    dbMigration.value = null
  }
}

// 「检查更新」现在也是**后台任务**：点了就返回，结果经 vault:update_checked 回来；
// 结果由后端**持久化**（getUpdateCheckResult），换页 / 重启后仍在，直到被新结果替换。
async function checkUpdate() {
  if (updateChecking.value) return
  updateChecking.value = true
  try {
    const res = await api.startUpdateCheck()
    if (res && res.ok === false) {
      updateChecking.value = false
      ElMessage.error(res.message || '无法开始检查更新')
    }
    // 成功：等 vault:update_checked / 任务状态回来
  } catch (err) {
    updateChecking.value = false
    ElMessage.error(err.message || '检查更新失败')
  }
}

async function loadUpdateCheckResult() {
  try {
    const res = await api.getUpdateCheckResult()
    if (res && Object.keys(res).length) updateResult.value = res
  } catch { /* 容错：不阻塞其它卡片 */ }
}

async function doUpdate() {
  const isFormal = updateChannel.value === 'formal'
  const what = isFormal ? '下载预编译包' : '拉取源码并重新构建'
  const reuse = !!(updateResult.value && updateResult.value.prepared)
  const intro = reuse
    ? '本地已有准备好的更新，将直接复用（跳过下载/构建）；准备完成后会询问是否立即重启。是否继续？'
    : `将先在程序内${what}（这一步有进度、可取消）；准备完成后会询问是否立即重启。是否继续？`
  try {
    await ElMessageBox.confirm(intro, '立即更新', {
      type: 'warning', confirmButtonText: '立即更新', cancelButtonText: '取消',
    })
  } catch {
    return
  }
  updateFailed.value = ''
  updatePrepareTask.value = null
  updating.value = true
  try {
    const res = await api.startUpdate(updateChannel.value)
    if (res && res.ok === false) {
      updating.value = false
      ElMessage.error(res.message || '无法开始更新')
    }
    // ok：后端在后台准备，进度经 vault:tasks 推送（见 onEvent 订阅）
  } catch (err) {
    updating.value = false
    ElMessage.error(err.message || '无法开始更新')
  }
}

async function cancelUpdate() {
  const t = updatePrepareTask.value
  if (!t || !t.id) return
  try {
    await api.cancelTask(t.id)
  } catch { /* 忽略：协作式取消，后端会在检查点停下 */ }
}

// 订阅后台任务：只认「更新」这一条，用它驱动准备进度与状态
let _offTasks = null
let _offUpdateFailed = null
let _offUpdateReady = null
let _offChecked = null
onMounted(() => {
  _offTasks = onEvent(EVENTS.tasksChanged, (snapshot) => {
    if (!snapshot) return
    if (snapshot.kind === 'update_check') {
      // 「检查更新」的后台任务：running 时按钮转圈；结束由 updateChecked 事件收口
      updateChecking.value = snapshot.status === 'running'
      return
    }
    if (snapshot.kind !== 'update') return
    updatePrepareTask.value = snapshot
    if (snapshot.status === 'running') {
      updating.value = true
      updateFailed.value = ''
    } else if (snapshot.status === 'done') {
      // 完成 = 已准备就绪；是否交棒由根组件的全局弹窗询问（vault:update_ready）
      updating.value = false
      if (snapshot.output) ElMessage.success(snapshot.output)
    } else if (snapshot.status === 'canceled') {
      updating.value = false
      updateFailed.value = ''
      ElMessage.info('已取消更新准备')
    } else {
      updating.value = false
      updateFailed.value = snapshot.error || '准备更新失败'
    }
  })
  _offUpdateFailed = onEvent(EVENTS.updateFailed, (d) => {
    updating.value = false
    updateFailed.value = (d && d.message) || '更新失败'
  })
  // 更新准备完成（全局弹窗负责询问是否重启）→ 卡片同步成「本地已有准备产物」态，
  // 用户选「稍后」后按钮变成「安装已准备好的更新」，不必重新检查一次才看得到。
  _offUpdateReady = onEvent(EVENTS.updateReady, (d) => {
    if (!d) return
    updateResult.value = {
      ...(updateResult.value || {}),
      ok: true,
      has_update: true,
      prepared: { kind: d.kind, version: d.version || '' },
    }
  })
  // 检查结束（手动或自动）→ 收口按钮状态并刷新卡片上的结果
  _offChecked = onEvent(EVENTS.updateChecked, (d) => {
    updateChecking.value = false
    if (d && Object.keys(d).length) updateResult.value = d
  })
})
onUnmounted(() => {
  if (_offTasks) _offTasks()
  if (_offUpdateFailed) _offUpdateFailed()
  if (_offUpdateReady) _offUpdateReady()
  if (_offChecked) _offChecked()
})

const refreshing = ref(false)

/**
 * 重新拉取本页各项设置：首屏加载与顶部「刷新」共用同一份清单，
 * 免得以后新增设置项时只改了其中一处。
 * @param {boolean} showSpinner 首屏不必转菊花 —— 页面本来就是空的
 */
/* ── 全局 TLS 版本 ──────────────────────────────────── */
const tlsVersion = ref('auto')
const tlsVersions = ref(['auto', '1.0', '1.1', '1.2', '1.3'])

const tlsLabel = (v) => (v === 'auto' ? 'auto（先 TLS 1.3，握手失败回退 1.2）' : `固定 TLS ${v}`)

async function loadTls() {
  const res = await api.getTlsOptions().catch(() => null)
  if (!res || res.ok === false) return
  tlsVersion.value = res.tls_version || 'auto'
  if (Array.isArray(res.tls_versions) && res.tls_versions.length) tlsVersions.value = res.tls_versions
}

async function changeTlsVersion(value) {
  const res = await api.setTlsVersion(value).catch(() => null)
  if (!res || res.ok === false) {
    ElMessage.error((res && res.message) || '设置失败')
    return
  }
  ElMessage.success('全局 TLS 版本已更新')
}

async function refreshAll(showSpinner = true) {
  if (showSpinner) refreshing.value = true
  try {
    await Promise.all([
      loadCloseAction(),
      loadAutoLock(),
      loadNeverLock(),
      loadLightMode(),
      loadSplashDisabled(),
      loadAutostart(),
      loadTls(),
      loadUpdateChannel(),
      loadUpdateProxy(),
      loadUpdateToken(),
      loadUpdateAutoCheck(),
      loadUpdateStatus(),
      loadUpdateCheckResult(),
      loadDbMigrationStatus(),
      proxyStore.fetch().catch(() => {}),
      appStore.refreshTray().catch(() => {}),
    ])
  } finally {
    if (showSpinner) refreshing.value = false
  }
}

onMounted(() => refreshAll(false))
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">系统设置</h2>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header><span>外观</span></template>

      <div class="setting-info">
        <div class="setting-label">界面主题</div>
      </div>

      <ThemePicker />

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">禁用开屏动画</div>
          <div class="setting-desc">
            启动时不再播放开屏动画；修改后下次启动生效。窗口分辨率过小时也会自动跳过。
          </div>
        </div>
        <el-switch
          v-model="splashDisabled"
          :loading="splashSaving"
          @change="applySplashDisabled"
        />
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">总是展示开屏动画</div>
          <div class="setting-desc">
            每次打开页面（桌面端从托盘恢复、Web 端刷新页面）都重新播放开屏动画。
            「禁用开屏动画」开启时该选项不可用。
          </div>
        </div>
        <el-switch
          v-model="splashAlways"
          :loading="splashAlwaysSaving"
          :disabled="splashDisabled"
          @change="applySplashAlways"
        />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>安全</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">主密码</div>
          <div class="setting-desc">
            主密码用于派生加密密钥。修改后将使用新密钥重新加密库中全部凭据，旧密码立即失效。
          </div>
        </div>
        <el-button :icon="Key" @click="openPasswordDialog">修改主密码</el-button>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">锁定保险库</div>
          <div class="setting-desc">立即清除内存中的解密密钥与明文数据，需重新输入主密码才能访问。</div>
        </div>
        <el-button :icon="Lock" @click="lockNow">立即锁定</el-button>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">不锁定</div>
          <div class="setting-desc">
            开启后不会因空闲或程序重启而锁定，只有手动点击「立即锁定」才会清除密钥。
            <template v-if="neverLock">
              为支持重启后自动恢复，解锁密钥交由系统凭据保护
              <template v-if="neverLockInfo.protection_label">（{{ neverLockInfo.protection_label }}）</template>；
              任何能登录本机同一账户的人都可以直接使用保险库。
            </template>
          </div>
        </div>
        <el-switch
          v-model="neverLock"
          :loading="neverLockSaving"
          @change="applyNeverLock"
        />
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">空闲自动锁定</div>
          <div class="setting-desc">
            <template v-if="neverLock">「不锁定」开启时此项不生效。</template>
            一段时间无操作后自动清除内存中的密钥。若当时后台仍有验证任务在跑，则顺延到任务结束后再锁。
          </div>
        </div>
        <el-select
          v-model="autoLockMinutes"
          class="close-select"
          :loading="autoLockSaving"
          :disabled="neverLock"
          @change="applyAutoLock"
        >
          <el-option
            v-for="opt in autoLockOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
      </div>
    </el-card>

    <el-card v-if="!isWebMode" shadow="never">
      <template #header><span>窗口与关闭</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">关闭主窗口时</div>
          <div class="setting-desc">
            点击窗口关闭按钮时的默认行为。选择"后台运行"时程序会最小化到系统托盘，MCP 服务与定时任务继续执行。
          </div>
        </div>
        <el-select
          v-model="closeAction"
          class="close-select"
          :loading="closeSaving"
          @change="applyCloseAction"
        >
          <el-option
            v-for="opt in closeActionOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">轻量模式</div>
          <div class="setting-desc">
            最小化到托盘时销毁界面以省内存，重新打开需重建（未保存的编辑会丢失）；后台服务不受影响。
          </div>
        </div>
        <el-switch v-model="lightMode" :loading="lightModeSaving" @change="applyLightMode" />
      </div>
    </el-card>

    <el-card v-if="!isWebMode" shadow="never">
      <template #header><span>启动</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">开机自启动</div>
          <div class="setting-desc">
            {{
              autostart.supported
                ? '开机后自动启动本程序（直接读写系统自启条目）。'
                : '当前平台不支持设置开机自启动。'
            }}
          </div>
          <div v-if="autostart.supported && autostart.target" class="setting-path">
            系统落点：{{ autostart.target }}
          </div>
        </div>
        <el-switch
          v-model="autostart.enabled"
          :loading="autostartSaving"
          :disabled="!autostart.supported"
          @change="applyAutostart"
        />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>网络与代理</span></template>

      <!-- 全局默认代理：凭据没单独指定时用它 -->
      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">默认代理</div>
          <div class="setting-desc">
            凭据未单独指定时用它。支持 http / https / socks5；环境变量 VAULT_PROXY 优先。
          </div>
        </div>
        <div class="inline-row">
          <el-select v-model="globalProxyValue" class="proxy-input" @change="changeGlobalProxy">
            <el-option value="" label="直连（不使用代理）" />
            <el-option
              v-for="item in proxyStore.items"
              :key="item.id"
              :value="item.id"
              :label="`${item.name}（${item.url}）`"
            />
          </el-select>
          <el-button
            :icon="Connection"
            :loading="proxyStore.testing && proxyStore.testingId === null"
            @click="runProxyTest(null)"
          >测试当前</el-button>
        </div>
      </div>

      <el-divider />

      <!-- 全局默认 TLS 版本：凭据与各层预设都没指定时用它 -->
      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">TLS 版本</div>
          <div class="setting-desc">
            凭据与各层预设都未指定时用它。auto 先按标准协商，握手失败自动回退 TLS 1.2 重试。
          </div>
        </div>
        <el-select v-model="tlsVersion" class="proxy-input" @change="changeTlsVersion">
          <el-option v-for="v in tlsVersions" :key="v" :value="v" :label="tlsLabel(v)" />
        </el-select>
      </div>

      <el-divider />

      <div class="proxy-list-head">
        <span class="setting-label">代理列表</span>
        <el-button size="small" :icon="Plus" @click="openCreateProxy">新增代理</el-button>
      </div>

      <el-empty
        v-if="!proxyStore.items.length"
        description="还没有代理，点「新增代理」添加"
        :image-size="48"
      />
      <div v-else class="proxy-list">
        <div v-for="item in proxyStore.items" :key="item.id" class="proxy-item">
          <el-tag v-if="proxyStore.globalId === item.id" size="small" type="success" effect="plain">默认</el-tag>
          <span class="proxy-name">{{ item.name }}</span>
          <code class="mono proxy-url">{{ item.url }}</code>
          <el-button
            size="small"
            :loading="proxyStore.testing && proxyStore.testingId === item.id"
            @click="runProxyTest(item.id)"
          >测试</el-button>
          <el-button size="small" :icon="Edit" @click="openEditProxy(item)" />
          <el-button size="small" type="danger" plain :icon="Delete" @click="removeProxy(item)" />
        </div>
      </div>

      <!-- 测试结果：直连 / 代理两组对照，外加综合结论（状态在 store 里，切页不丢） -->
      <template v-if="proxyStore.result || proxyStore.testing">
        <el-divider />

        <div v-if="proxyStore.testing && !proxyStore.result" class="probe-loading">
          正在探测直连与代理出口，最多约 10 秒…
        </div>

        <el-alert
          v-if="proxyStore.result && proxyStore.result.verdict"
          :type="verdictType(proxyStore.result.verdict.level)"
          :title="proxyStore.result.verdict.headline"
          :closable="false"
          show-icon
        >
          <ul class="verdict-list">
            <li v-for="(line, index) in proxyStore.result.verdict.details" :key="index">{{ line }}</li>
          </ul>
        </el-alert>

        <div v-if="proxyStore.result" class="probe-grid">
          <div class="probe-card">
            <div class="probe-title">不挂代理（直连）</div>
            <div class="probe-row">
              <span class="probe-k">出口 IP</span>
              <span class="probe-v">{{ probeIpText(proxyStore.result.direct && proxyStore.result.direct.ip) }}</span>
            </div>
            <div class="probe-row">
              <span class="probe-k">国内站点</span>
              <span class="probe-v" :class="probeClass(proxyStore.result.direct && proxyStore.result.direct.domestic)">
                {{ probeSiteText(proxyStore.result.direct && proxyStore.result.direct.domestic) }}
              </span>
            </div>
            <div class="probe-row">
              <span class="probe-k">国外站点</span>
              <span class="probe-v" :class="probeClass(proxyStore.result.direct && proxyStore.result.direct.foreign)">
                {{ probeSiteText(proxyStore.result.direct && proxyStore.result.direct.foreign) }}
              </span>
            </div>
          </div>

          <div class="probe-card">
            <div class="probe-title">
              挂代理
              <span v-if="proxyStore.result.proxy" class="text-muted">
                （{{ proxyStore.result.proxy }}）
              </span>
            </div>
            <template v-if="proxyStore.result.proxied">
              <div class="probe-row">
                <span class="probe-k">出口 IP</span>
                <span class="probe-v">{{ probeIpText(proxyStore.result.proxied.ip) }}</span>
              </div>
              <div class="probe-row">
                <span class="probe-k">国内站点</span>
                <span class="probe-v" :class="probeClass(proxyStore.result.proxied.domestic)">
                  {{ probeSiteText(proxyStore.result.proxied.domestic) }}
                </span>
              </div>
              <div class="probe-row">
                <span class="probe-k">国外站点</span>
                <span class="probe-v" :class="probeClass(proxyStore.result.proxied.foreign)">
                  {{ probeSiteText(proxyStore.result.proxied.foreign) }}
                </span>
              </div>
            </template>
            <el-empty v-else description="未配置代理" :image-size="48" />
          </div>
        </div>
      </template>
    </el-card>

    <el-card shadow="never">
      <template #header><span>更新</span></template>

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">自动检测更新</div>
          <div class="setting-desc">
            启动后后台查一次新版本，有则弹通知；关掉后仍可手动「检查更新」。
          </div>
        </div>
        <el-switch
          v-model="updateAutoCheck"
          :loading="updateAutoCheckSaving"
          @change="applyUpdateAutoCheck"
        />
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">更新渠道</div>
          <div class="setting-desc">
            正式版：下载预编译包替换当前程序；测试版：从源码重新构建。
          </div>
        </div>
        <el-radio-group v-model="updateChannel" @change="applyUpdateChannel">
          <el-radio-button value="formal">正式版</el-radio-button>
          <el-radio-button value="test">测试版</el-radio-button>
        </el-radio-group>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">更新代理</div>
          <div class="setting-desc">
            更新与构建走哪个出口；直连 GitHub 不通时在这里选一个代理。
          </div>
          <div v-if="updateProxySpec !== 'direct'" class="setting-path">
            当前出网：{{ updateProxyResolved || '直连（未配置代理）' }}
          </div>
        </div>
        <el-select v-model="updateProxySpec" class="proxy-input" @change="applyUpdateProxy">
          <el-option value="global" label="跟随全局默认" />
          <el-option value="direct" label="直连（不使用代理）" />
          <el-option
            v-for="item in proxyStore.items"
            :key="item.id"
            :value="item.id"
            :label="`${item.name}（${item.url}）`"
          />
        </el-select>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">访问令牌</div>
          <div class="setting-desc">
            可选。仓库已公开，不填也能检查与更新；填了走 GitHub API，认证后速率上限更高、不易被匿名限流；仓库若转私有则必须填。令牌加密保存，明文不落库。
          </div>
          <div v-if="updateTokenConfigured" class="setting-path">
            已配置：{{ updateTokenMask }}
          </div>
        </div>
        <div class="token-input">
          <el-input
            v-model="updateTokenInput"
            type="password"
            show-password
            clearable
            placeholder="github_pat_… 或 ghp_…"
            @keyup.enter="saveUpdateToken"
          />
          <el-button type="primary" :loading="updateTokenSaving" @click="saveUpdateToken">保存</el-button>
          <el-button v-if="updateTokenConfigured" @click="clearUpdateToken">清除</el-button>
        </div>
      </div>

      <el-divider />

      <div class="setting-row">
        <div class="setting-info">
          <div class="setting-label">检查更新</div>
          <div class="setting-desc">
            <template v-if="updateResult && updateResult.ok">
              当前版本 {{ updateResult.current && updateResult.current.version }}
              （{{ updateResult.current && updateResult.current.commit }}）。
            </template>
            <template v-else>
              检查 GitHub 上是否有新版本（只读，不改动本机）。
            </template>
          </div>
          <div v-if="updateResult && updateResult.ok" class="setting-path">
            出网：{{ updateResult.proxy ? updateResult.proxy : '直连（未配置代理）' }}
          </div>
        </div>
        <el-button :icon="Refresh" :loading="updateChecking" @click="checkUpdate">检查更新</el-button>
      </div>

      <el-alert
        v-if="updateResult"
        class="update-alert"
        :type="updateResult.ok === false ? 'warning'
          : (updateResult.has_update ? 'info' : 'success')"
        :title="updateResult.ok === false
          ? (updateResult.message || '检查更新失败')
          : (updateResult.has_update ? updateFoundTitle : '已是最新版本')"
        :closable="false"
        show-icon
      >
        <template v-if="updateResult.ok">
          <div>{{ updateResult.reason }}</div>
          <div v-if="updateResult.prepared">
            本地已准备好{{ updateResult.prepared.version ? ` v${updateResult.prepared.version}` : '更新产物' }}，安装时无需重新下载/构建
          </div>
          <div v-if="updateResult.kind === 'test' && updateResult.latest && updateResult.latest.commit">
            远端提交：{{ updateResult.latest.commit }}
            <span v-if="updateResult.current && updateResult.current.commit">
              （本地 {{ updateResult.current.commit }}）
            </span>
          </div>
          <div v-if="updateResult.db_change">
            数据库结构将逐级迁移：{{ (updateResult.db_chain || []).join(' → ') }}
          </div>
          <div v-if="updateResult.kind === 'formal' && updateResult.asset">
            将下载：{{ updateResult.asset }}
          </div>

          <!-- 本次更新带来什么：正式版 = 目标版本的 CHANGELOG 段落；测试版 = 当前→远端之间的提交 -->
          <div v-if="updateResult.has_update && updateResult.notes" class="update-notes">
            <template v-if="updateResult.notes.ok">
              <div class="notes-head">
                {{
                  updateResult.notes.kind === 'release'
                    ? `更新内容（v${updateResult.notes.version}）`
                    : `本次更新带来 ${updateResult.notes.total || (updateResult.notes.commits || []).length} 个提交`
                }}
              </div>
              <div class="notes-body">
                <template v-if="updateResult.notes.kind === 'release'">
                  <div
                    v-for="(sec, si) in updateResult.notes.sections || []"
                    :key="si"
                    class="notes-section"
                  >
                    <div class="notes-section-title">{{ sec.title }}</div>
                    <ul class="notes-list">
                      <li v-for="(item, ii) in sec.items || []" :key="ii">{{ item }}</li>
                    </ul>
                  </div>
                </template>
                <template v-else>
                  <ul class="notes-list notes-commits">
                    <li v-for="(c, ci) in updateResult.notes.commits || []" :key="ci">
                      <code>{{ c.sha }}</code>
                      <span>{{ c.subject }}</span>
                    </li>
                  </ul>
                </template>
              </div>
            </template>
            <div v-else class="notes-error">
              未能获取更新内容：{{ updateResult.notes.message }}
            </div>
          </div>
          <div v-if="updateResult.has_update" class="update-action">
            <el-button type="primary" :loading="updating" @click="doUpdate">
              {{ updateResult.prepared ? '安装已准备好的更新' : '立即更新' }}
            </el-button>
          </div>
        </template>
      </el-alert>

      <el-alert
        v-if="updating"
        class="update-alert"
        type="warning"
        :title="!updatePrepareTask || updatePrepareTask.status === 'running'
          ? '正在准备更新（准备好后会询问是否立即重启）'
          : '更新准备完成'"
        :closable="false"
        show-icon
      >
        <div v-if="updatePrepareTask" class="prepare-line">
          <el-progress
            class="prepare-bar"
            :percentage="preparePercent"
            :indeterminate="prepareIndeterminate"
            :show-text="!prepareIndeterminate"
          />
          <span class="prepare-text">{{ updatePrepareTask.text || updatePrepareTask.title }}</span>
        </div>
        <div class="update-action">
          <el-button
            v-if="updatePrepareTask && updatePrepareTask.status === 'running'"
            size="small"
            @click="cancelUpdate"
          >取消</el-button>
        </div>
      </el-alert>

      <el-alert
        v-if="updateFailed"
        class="update-alert"
        type="error"
        :title="`更新失败：${updateFailed}`"
        show-icon
        @close="updateFailed = ''"
      />

      <el-alert
        v-if="dbMigration && dbMigration.needs_migration"
        class="update-alert"
        type="info"
        :title="`数据库结构需要更新：${dbMigration.disk} → ${dbMigration.app}`"
        :closable="false"
        show-icon
      >
        <div>
          解锁时会自动逐级迁移（{{ (dbMigration.chain || []).join(' → ') }}）。
          若这条一直显示，说明自动迁移没成功，请看 <code>data/logs/</code> 下的日志。
        </div>
      </el-alert>

      <el-alert
        v-if="lastUpdateVisible"
        class="update-alert"
        :type="updateLastStatus.ok ? 'success' : 'warning'"
        :title="`上次更新：${updateLastStatus.message || ''}${updateTransition(updateLastStatus)}`"
        show-icon
        @close="dismissUpdateStatus"
      >
        <div v-if="(updateLastStatus.report || []).length" class="report-list">
          <div v-for="(s, i) in updateLastStatus.report" :key="i" class="report-item">
            <span :class="s.ok ? 'report-ok' : 'report-bad'">{{ s.ok ? '✓' : '✗' }}</span>
            <span class="report-stage">{{ stageLabel(s.stage) }}</span>
            <span class="report-msg">{{ s.message }}</span>
          </div>
        </div>
        <div v-if="updateLastStatus.log" class="setting-path">日志：{{ updateLastStatus.log }}</div>
      </el-alert>
    </el-card>

    <!-- 新增 / 编辑代理 -->
    <el-dialog
      v-model="proxyDialog"
      :title="proxyForm.id ? '编辑代理' : '新增代理'"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="proxyForm.name" placeholder="例如：示例代理 / 本机 Clash" clearable />
        </el-form-item>
        <el-form-item label="代理地址">
          <el-input v-model="proxyForm.url" placeholder="socks5://127.0.0.1:10808" clearable />
          <div class="field-hint">需带协议前缀，支持 http / https / socks4 / socks5 / socks5h</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="proxyDialog = false">取消</el-button>
        <el-button type="primary" :loading="proxySaving" @click="saveProxy">保存</el-button>
      </template>
    </el-dialog>

    <!-- 修改主密码 -->
    <el-dialog v-model="passwordDialog" title="修改主密码" width="480px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon class="mb-12">
        修改主密码需要重新加密全部凭据，请勿在批量验证进行中执行。
      </el-alert>

      <el-form ref="passwordFormRef" :model="passwordForm" :rules="passwordRules" label-position="top">
        <el-form-item label="当前主密码" prop="old_password">
          <el-input v-model="passwordForm.old_password" type="password" show-password placeholder="请输入当前主密码" />
        </el-form-item>

        <el-form-item label="新主密码" prop="new_password">
          <el-input v-model="passwordForm.new_password" type="password" show-password placeholder="至少 6 位" />
          <div v-if="passwordForm.new_password" class="strength">
            <div class="strength-bars">
              <span
                v-for="index in 4"
                :key="index"
                class="strength-bar"
                :class="index <= passwordStrength.level ? `level-${passwordStrength.level}` : ''"
              />
            </div>
            <span class="strength-label">{{ passwordStrength.label }}</span>
          </div>
        </el-form-item>

        <el-form-item label="确认新主密码" prop="confirm_password">
          <el-input
            v-model="passwordForm.confirm_password"
            type="password"
            show-password
            placeholder="请再次输入新主密码"
            @keyup.enter="submitPassword"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="passwordSubmitting" @click="submitPassword">确认修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.setting-info {
  min-width: 0;
}

.setting-label {
  font-weight: 600;
  margin-bottom: 4px;
}

.setting-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  max-width: 640px;
}

.setting-path {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  margin-top: 4px;
  word-break: break-all;
}

.inline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.mb-12 {
  margin-bottom: 12px;
}

/* 更新卡片里的提示条：它上面那一行的末尾是「出网：…」，没有上边距会贴在一起。
   （行与行之间本来由 el-divider 隔开 ≈24px；这里给 20px 与之呼应。
   相邻提示条的外边距会**折叠**，所以叠放时仍是 20px，不会双倍。） */
.update-alert {
  margin-top: 20px;
  margin-bottom: 12px;
}

.update-action {
  margin-top: 8px;
}

/* 更新内容：正式版 = CHANGELOG 段落；测试版 = 提交列表 */
.update-notes {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #ebeef5;
}

.notes-head {
  font-weight: 600;
  margin-bottom: 4px;
}

.notes-body {
  max-height: 260px;
  overflow-y: auto;
}

.notes-section {
  margin-bottom: 6px;
}

.notes-section-title {
  font-weight: 600;
  color: #606266;
}

.notes-list {
  margin: 2px 0 6px;
  padding-left: 18px;
  line-height: 1.6;
}

.notes-commits li {
  display: flex;
  gap: 8px;
}

.notes-commits code {
  flex: none;
  color: #909399;
}

.notes-error {
  color: #909399;
}

.strength {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  width: 100%;
}

.strength-bars {
  display: flex;
  gap: 4px;
  flex: 1;
}

.strength-bar {
  height: 4px;
  flex: 1;
  border-radius: 2px;
  background: var(--el-fill-color-dark);
  transition: background 0.2s;
}

.strength-bar.level-1 {
  background: var(--el-color-danger);
}

.strength-bar.level-2 {
  background: var(--el-color-warning);
}

.strength-bar.level-3 {
  background: var(--el-color-primary);
}

.strength-bar.level-4 {
  background: var(--el-color-success);
}

.strength-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  width: 32px;
  text-align: right;
}

.proxy-input {
  width: 280px;
}

/* 访问令牌：输入框 + 保存/清除按钮，右对齐成一行 */
.token-input {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 420px;
  max-width: 100%;
}

/* ── 更新：准备进度 + 上次更新的逐阶段报告 ─────────────── */
.prepare-line {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
}

.prepare-bar {
  flex: 1;
  min-width: 120px;
}

.prepare-text {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  white-space: nowrap;
}

.report-list {
  margin-top: 8px;
}

.report-item {
  display: flex;
  gap: 8px;
  font-size: 13px;
  line-height: 1.9;
}

.report-ok {
  color: var(--el-color-success);
  font-weight: 700;
}

.report-bad {
  color: var(--el-color-danger);
  font-weight: 700;
}

.report-stage {
  min-width: 96px;
}

.report-msg {
  color: var(--el-text-color-secondary);
}

/* ── 代理列表 ───────────────────────────────────────── */
.proxy-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.proxy-list {
  display: flex;
  flex-direction: column;
}

.proxy-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.proxy-item:last-child {
  border-bottom: none;
}

.proxy-name {
  min-width: 110px;
  font-size: 13px;
}

.proxy-url {
  flex: 1;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.close-select {
  width: 220px;
}

/* ── 代理测试结果 ───────────────────────────────────── */
.verdict-list {
  margin: 6px 0 0;
  padding-left: 18px;
  line-height: 1.7;
}

.probe-grid {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.probe-loading {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.probe-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
}

.probe-title {
  font-weight: 600;
  margin-bottom: 8px;
  font-size: 13px;
}

.probe-row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.8;
}

.probe-k {
  flex: 0 0 62px;
  color: var(--el-text-color-secondary);
}

.probe-v {
  flex: 1;
  word-break: break-all;
}

.probe-v.probe-ok {
  color: var(--el-color-success);
}

.probe-v.probe-bad {
  color: var(--el-color-danger);
}
</style>