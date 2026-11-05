<script setup>
/**
 * 仪表盘（Vault Core HUD）。
 *
 * 数据来自一次 `dashboard_overview()` 聚合（后端一次遍历算出状态 / 类别 / 分组 / 风险 /
 * 最近验证 / 任务 / 系统状态），前端不再拉全量凭据二次统计。全局事件只做**去抖重取**：
 * 验证 / 插件 / 任务 / MCP / 代理变化都会让快照过期。
 *
 * 视觉：中央核心由 DashboardCore 负责（SVG 状态环，零图表依赖）；
 * 其余面板全部用 `--v-*` 主题令牌，6 套主题自动适配。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, SetUp, VideoPlay } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

import BatchVerifyDialog from '@/components/BatchVerifyDialog.vue'
import DashboardCore from '@/components/DashboardCore.vue'
import { api, EVENTS, onEvent } from '@/api'
import { useVaultStore } from '@/stores/vault'
import { formatDateTime, formatRelative, statusText } from '@/utils/format'

const router = useRouter()
const vaultStore = useVaultStore()

const overview = ref(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const clock = ref('')
const batchVisible = ref(false)
const batchCandidates = ref([])
const batchLoading = ref(false)

// ── 模块显隐（纯界面偏好，存 localStorage，与其它布局偏好一致）──
const MODULE_KEY = 'vault.dashboard.hiddenModules'
const MODULES = [
  { key: 'health', row: 'top', label: '状态矩阵', desc: '凭据总数、状态与新鲜度分布' },
  { key: 'core', row: 'top', label: '健康度核心', desc: '状态环、轨道与健康度' },
  { key: 'system', row: 'top', label: '系统状态', desc: 'MCP / 代理 / TLS / 匿名模式 / 任务' },
  { key: 'categories', row: 'mid', label: '类别分布', desc: '按插件类别的凭据数量与状态构成' },
  { key: 'risks', row: 'mid', label: '风险队列', desc: '失效、异常、从未验证与长期未验证' },
  { key: 'activity', row: 'bottom', label: '最近活动', desc: '最近验证记录与进行中的后台任务' },
  { key: 'groups', row: 'bottom', label: '分组矩阵', desc: '按分组统计凭据数量' },
]
const MODULE_KEYS = new Set(MODULES.map((item) => item.key))

function readHiddenModules() {
  try {
    const raw = JSON.parse(localStorage.getItem(MODULE_KEY) || '[]')
    if (!Array.isArray(raw)) return []
    const list = [...new Set(raw.filter((key) => MODULE_KEYS.has(key)))]
    // 手工改坏或旧版本残留导致「全部隐藏」时回到默认，避免空白页
    return list.length >= MODULES.length ? [] : list
  } catch {
    return []
  }
}

const hiddenModules = ref(readHiddenModules())
const customizeVisible = ref(false)
const draftModules = ref([])

const showModule = (key) => !hiddenModules.value.includes(key)
const rowVisible = (row) => MODULES.some((item) => item.row === row && showModule(item.key))
const anyModuleVisible = computed(() => MODULES.some((item) => showModule(item.key)))

function openCustomize() {
  draftModules.value = MODULES.filter((item) => showModule(item.key)).map((item) => item.key)
  customizeVisible.value = true
}

function resetModules() {
  draftModules.value = MODULES.map((item) => item.key)
}

function saveModules() {
  const selected = new Set(draftModules.value.filter((key) => MODULE_KEYS.has(key)))
  if (!selected.size) {
    ElMessage.warning('至少保留一个显示模块')
    return
  }
  hiddenModules.value = MODULES.map((item) => item.key).filter((key) => !selected.has(key))
  try {
    localStorage.setItem(MODULE_KEY, JSON.stringify(hiddenModules.value))
  } catch { /* 隐私模式 / 存储被禁用：本次会话内仍生效 */ }
  customizeVisible.value = false
}

let clockTimer = null
let reloadTimer = null
let unsubs = []

const STATUS_META = {
  valid: { label: '有效', color: 'var(--v-success)' },
  invalid: { label: '失效', color: 'var(--v-danger)' },
  error: { label: '异常', color: 'var(--v-warning)' },
  unknown: { label: '未知', color: 'var(--v-info)' },
}

const statusList = computed(() => {
  const counts = (overview.value && overview.value.status) || {}
  return Object.entries(STATUS_META).map(([key, meta]) => ({
    key, ...meta, count: Number(counts[key]) || 0,
  }))
})

const statusTotal = computed(() => statusList.value.reduce((sum, item) => sum + item.count, 0))

const freshnessList = computed(() => {
  const data = (overview.value && overview.value.freshness) || {}
  return [
    { key: 'day', label: '≤ 1 天', color: 'var(--v-success)' },
    { key: 'week', label: '≤ 7 天', color: 'var(--v-primary)' },
    { key: 'month', label: '≤ 30 天', color: 'var(--v-warning)' },
    { key: 'older', label: '> 30 天', color: 'var(--v-danger)' },
    { key: 'never', label: '从未验证', color: 'var(--v-info)' },
  ].map((item) => ({ ...item, count: Number(data[item.key]) || 0 }))
})

const freshnessTotal = computed(() => freshnessList.value.reduce((sum, item) => sum + item.count, 0))

const categoryRows = computed(() => {
  const items = (overview.value && overview.value.categories) || []
  const max = Math.max(1, ...items.map((item) => item.count))
  return items.map((item) => {
    const count = Math.max(0, item.count)
    return {
      ...item,
      width: `${Math.max(8, Math.round((count / max) * 100))}%`,
      parts: [
        { key: 'valid', value: item.valid, color: 'var(--v-success)' },
        { key: 'invalid', value: item.invalid, color: 'var(--v-danger)' },
        { key: 'error', value: item.error, color: 'var(--v-warning)' },
        { key: 'unknown', value: item.unknown, color: 'var(--v-info)' },
      ].filter((part) => part.value > 0)
        .map((part) => ({ ...part, width: `${(part.value / count) * 100}%` })),
    }
  })
})

const groupRows = computed(() => {
  const items = (overview.value && overview.value.groups) || []
  const max = Math.max(1, ...items.map((item) => item.count))
  return items.map((item) => ({
    ...item,
    width: `${Math.max(8, Math.round((item.count / max) * 100))}%`,
    color: item.color || 'var(--v-primary)',
  }))
})

const riskRows = computed(() => (overview.value && overview.value.risks) || [])
const recentRows = computed(() => (overview.value && overview.value.recent) || [])
const runningTasks = computed(() => {
  const info = (overview.value && overview.value.tasks) || {}
  return Array.isArray(info.items) ? info.items : []
})

const systemRows = computed(() => {
  const system = (overview.value && overview.value.system) || {}
  const mcp = system.mcp || {}
  const proxy = system.proxy || {}
  const schedules = (overview.value && overview.value.schedules) || {}
  const taskInfo = (overview.value && overview.value.tasks) || {}
  return [
    {
      label: 'MCP 服务',
      value: mcp.running ? `${mcp.host}:${mcp.port}` : '未运行',
      hint: mcp.running ? `${mcp.active_tokens || 0} 个令牌` : '可在「MCP 设置」启动',
    },
    {
      label: '代理出口',
      value: proxy.display || '直连',
      hint: `已登记 ${proxy.count || 0} 个出口`,
    },
    {
      label: 'TLS 策略',
      value: String(system.tls_version || 'auto').toUpperCase(),
      hint: '全局默认',
    },
    {
      label: '匿名模式',
      value: system.anonymous ? '已开启' : '关闭',
      hint: system.anonymous ? '直连请求将被拒绝' : '不限制出口',
    },
    {
      label: '定时计划',
      value: `${schedules.enabled || 0} / ${schedules.total || 0} 启用`,
      hint: schedules.next_run_at
        ? `下次 ${formatRelative(schedules.next_run_at)}`
        : '暂无下次运行',
    },
    {
      label: '后台任务',
      value: `${taskInfo.running || 0} 进行中`,
      hint: `共 ${taskInfo.total || 0} 条记录`,
    },
  ]
})

function pct(value, total) {
  return total ? `${(value / total) * 100}%` : '0%'
}

function riskTone(severity) {
  return ['is-danger', 'is-warning', 'is-info', 'is-stale'][severity] || 'is-stale'
}

function taskPercent(task) {
  const total = Number(task.total) || 0
  const current = Number(task.current) || 0
  return total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0
}

async function load(silent = false) {
  if (silent && document.hidden) return
  if (silent) refreshing.value = true
  else loading.value = true
  try {
    const data = await api.getDashboardOverview()
    overview.value = data || null
    error.value = ''
  } catch (err) {
    error.value = err.message || '加载仪表盘失败'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function scheduleReload() {
  clearTimeout(reloadTimer)
  reloadTimer = setTimeout(() => load(true), 700)
}

/**
 * 与凭据管理同一套批量验证：打开前取全量凭据与插件，作用范围是全部凭据（不受凭据页
 * 当前筛选影响）；对话框本身与凭据管理共用 BatchVerifyDialog。
 */
async function openBatchVerify() {
  if (vaultStore.batch.running || batchLoading.value) return
  batchLoading.value = true
  try {
    const [rows] = await Promise.all([
      api.listCredentials({}),
      vaultStore.fetchPlugins(),
    ])
    batchCandidates.value = Array.isArray(rows) ? rows : []
    batchVisible.value = true
  } catch (err) {
    ElMessage.error(err.message || '加载凭据列表失败')
  } finally {
    batchLoading.value = false
  }
}

/** 跳凭据页并按条件筛选：先改共享 filters，Credentials 挂载时会按它拉取。 */
function gotoCredentials(patch) {
  const filters = vaultStore.filters
  filters.group_id = null
  filters.credential_type = null
  filters.credential_types = null
  filters.status = null
  filters.search = ''
  Object.assign(filters, patch || {})
  router.push('/credentials')
}

function openStatus(key) {
  gotoCredentials({ status: key })
}

function openRisk(item) {
  // 搜索比状态筛选更精确：直接落到这一条（名称唯一性在仪表盘场景足够）
  gotoCredentials({ search: item.name || '' })
}

function openCategory(item) {
  // credential_types 传的是具体服务类型（category 目录名不能当筛选值）
  const types = Array.isArray(item.types) && item.types.length ? item.types : null
  gotoCredentials(types ? { credential_types: types } : {})
}

function openGroup(item) {
  if (item.id == null) gotoCredentials({})
  else gotoCredentials({ group_id: item.id })
}

function openRecent(item) {
  gotoCredentials({ search: item.name || '' })
}

function updateClock() {
  const now = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  clock.value = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
}

onMounted(() => {
  updateClock()
  clockTimer = setInterval(updateClock, 1000)
  load()
  unsubs = [
    EVENTS.credentialsChanged,
    EVENTS.credentialUpdated,
    EVENTS.pluginsChanged,
    EVENTS.tasksChanged,
    EVENTS.mcpStatus,
    EVENTS.proxiesChanged,
    EVENTS.batchDone,
  ].map((name) => onEvent(name, scheduleReload))
})

onBeforeUnmount(() => {
  clearInterval(clockTimer)
  clearTimeout(reloadTimer)
  unsubs.forEach((off) => { if (typeof off === 'function') off() })
  unsubs = []
})
</script>

<template>
  <div class="page dashboard-page">
    <div class="page-toolbar dash-toolbar">
      <h2 class="section-title">仪表盘</h2>
      <span class="dash-kicker">VAULT // OVERVIEW</span>
      <div class="spacer" />
      <span class="dash-clock">{{ clock }}</span>
      <el-button :icon="SetUp" @click="openCustomize">自定义</el-button>
      <el-button :icon="Refresh" :loading="refreshing" @click="load()">刷新</el-button>
      <el-button
        type="primary"
        :icon="VideoPlay"
        :loading="batchLoading"
        :disabled="vaultStore.batch.running"
        @click="openBatchVerify"
      >批量验证</el-button>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon class="dash-error">
      <template #title>
        {{ error }}
        <el-button link size="small" @click="load()">重试</el-button>
      </template>
    </el-alert>

    <div v-if="overview && anyModuleVisible" class="hud">
      <div v-if="rowVisible('top')" class="hud-row hud-top">
        <section v-if="showModule('health')" class="hud-card v-glass health-panel">
          <header class="hud-head">
            <span class="hud-title">STATUS MATRIX</span>
            <span class="hud-tag">{{ statusTotal }} CREDENTIALS</span>
          </header>

          <div class="stat-grid">
            <div class="stat-cell">
              <strong>{{ (overview.totals && overview.totals.credentials) || 0 }}</strong>
              <span>凭据</span>
            </div>
            <div class="stat-cell">
              <strong>{{ (overview.totals && overview.totals.groups) || 0 }}</strong>
              <span>分组</span>
            </div>
            <div class="stat-cell">
              <strong>{{ (overview.totals && overview.totals.enabled_plugins) || 0 }}</strong>
              <span>启用插件</span>
            </div>
            <div class="stat-cell">
              <strong>{{ (overview.totals && overview.totals.enabled_schedules) || 0 }}</strong>
              <span>启用计划</span>
            </div>
          </div>

          <div class="status-bar" role="img" aria-label="凭据状态分布">
            <span
              v-for="item in statusList"
              :key="item.key"
              :style="{ width: pct(item.count, statusTotal), background: item.color }"
              :title="`${item.label} ${item.count}`"
            />
          </div>

          <ul class="status-legend">
            <li
              v-for="item in statusList"
              :key="item.key"
              class="status-item"
              :class="{ 'is-dim': !item.count }"
              @click="openStatus(item.key)"
            >
              <i :style="{ background: item.color }" />
              <span>{{ item.label }}</span>
              <b>{{ item.count }}</b>
            </li>
          </ul>

          <div
            v-if="overview.status && overview.status.plugin_disabled"
            class="disabled-hint"
            @click="router.push('/plugins')"
          >
            {{ overview.status.plugin_disabled }} 条凭据所属插件已禁用
          </div>

          <div class="hud-divider" />

          <div class="fresh-head">
            <span class="hud-subtitle">FRESHNESS</span>
            <span class="hud-tag">{{ freshnessTotal }} 已验证</span>
          </div>
          <div class="fresh-bar">
            <span
              v-for="item in freshnessList"
              :key="item.key"
              :style="{ width: pct(item.count, freshnessTotal), background: item.color }"
              :title="`${item.label} ${item.count}`"
            />
          </div>
          <div class="fresh-legend">
            <span v-for="item in freshnessList" :key="item.key">
              <i :style="{ background: item.color }" />{{ item.label }}
              <b>{{ item.count }}</b>
            </span>
          </div>
        </section>

        <section v-if="showModule('core')" class="hud-card v-glass core-panel">
          <DashboardCore
            :health="overview.health"
            :status="overview.status"
            :generated-at="formatDateTime(overview.generated_at)"
          />
        </section>

        <section v-if="showModule('system')" class="hud-card v-glass system-panel">
          <header class="hud-head">
            <span class="hud-title">SYSTEM</span>
            <el-button link size="small" @click="router.push('/settings')">设置</el-button>
          </header>
          <ul class="system-list">
            <li v-for="row in systemRows" :key="row.label" class="system-row">
              <i class="system-dot" />
              <div class="system-text">
                <span>{{ row.label }}</span>
                <small>{{ row.hint }}</small>
              </div>
              <b :title="row.value">{{ row.value }}</b>
            </li>
          </ul>
        </section>
      </div>

      <div v-if="rowVisible('mid')" class="hud-row hud-mid">
        <section v-if="showModule('categories')" class="hud-card v-glass category-panel">
          <header class="hud-head">
            <span class="hud-title">CATEGORY DISTRIBUTION</span>
            <span class="hud-tag">{{ categoryRows.length }} 类</span>
          </header>
          <ul v-if="categoryRows.length" class="bar-list">
            <li v-for="item in categoryRows" :key="item.category || item.name" @click="openCategory(item)">
              <div class="bar-label">
                <span>{{ item.name }}</span>
                <b>{{ item.count }}</b>
              </div>
              <div class="bar-track">
                <div class="bar-fill" :style="{ width: item.width }">
                  <span
                    v-for="part in item.parts"
                    :key="part.key"
                    :style="{ width: part.width, background: part.color }"
                  />
                </div>
              </div>
            </li>
          </ul>
          <div v-else class="hud-empty">暂无凭据类别</div>
        </section>

        <section v-if="showModule('risks')" class="hud-card v-glass risk-panel">
          <header class="hud-head">
            <span class="hud-title">RISK QUEUE</span>
            <span class="hud-tag">{{ riskRows.length }} 项</span>
          </header>
          <ul v-if="riskRows.length" class="risk-list">
            <li v-for="item in riskRows" :key="item.id" @click="openRisk(item)">
              <i class="risk-dot" :class="riskTone(item.severity)" />
              <div class="risk-text">
                <span class="risk-name">
                  {{ item.name }}
                  <em v-if="item.plugin_enabled === false">插件已禁用</em>
                </span>
                <small>{{ item.category_name }} · {{ item.reason }}</small>
              </div>
              <span class="risk-time">{{ formatRelative(item.last_verified_at) }}</span>
            </li>
          </ul>
          <div v-else class="hud-empty">暂无风险项</div>
        </section>
      </div>

      <div v-if="rowVisible('bottom')" class="hud-row hud-bottom">
        <section v-if="showModule('activity')" class="hud-card v-glass activity-panel">
          <header class="hud-head">
            <span class="hud-title">RECENT ACTIVITY</span>
            <el-button link size="small" @click="router.push('/tasks')">任务</el-button>
          </header>
          <ul v-if="recentRows.length" class="activity-list">
            <li v-for="item in recentRows" :key="item.id" @click="openRecent(item)">
              <i :style="{ background: (STATUS_META[item.status] || STATUS_META.unknown).color }" />
              <span class="activity-name">{{ item.name }}</span>
              <span class="activity-status">{{ statusText(item.status) }}</span>
              <span class="activity-time" :title="formatDateTime(item.at)">
                {{ formatRelative(item.at) }}
              </span>
            </li>
          </ul>
          <div v-else class="hud-empty">暂无验证记录</div>

          <template v-if="runningTasks.length">
            <div class="hud-divider" />
            <div class="task-list">
              <div v-for="task in runningTasks" :key="task.id" class="task-row">
                <div class="task-label">
                  <span>{{ task.title }}</span>
                  <small>{{ task.text || '进行中' }}</small>
                </div>
                <el-progress
                  :percentage="taskPercent(task)"
                  :indeterminate="!(Number(task.total) > 0)"
                  :stroke-width="6"
                  :show-text="false"
                />
              </div>
            </div>
          </template>
        </section>

        <section v-if="showModule('groups')" class="hud-card v-glass group-panel">
          <header class="hud-head">
            <span class="hud-title">GROUP MATRIX</span>
            <el-button link size="small" @click="router.push('/groups')">管理</el-button>
          </header>
          <ul v-if="groupRows.length" class="bar-list group-list">
            <li v-for="item in groupRows" :key="item.id ?? 'none'" @click="openGroup(item)">
              <div class="bar-label">
                <span>{{ item.name }}</span>
                <b>{{ item.count }}</b>
              </div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: item.width, background: item.color }"
                />
              </div>
            </li>
          </ul>
          <div v-else class="hud-empty">暂无分组</div>
        </section>
      </div>
    </div>

    <div v-else-if="overview" class="hud-blank">
      <el-empty description="所有模块已隐藏" :image-size="70">
        <el-button type="primary" @click="openCustomize">选择显示模块</el-button>
      </el-empty>
    </div>

    <div v-else v-loading="loading" class="dash-skeleton">
      <span v-if="!loading">暂无数据</span>
    </div>

    <el-dialog v-model="customizeVisible" title="仪表盘模块" width="480px">
      <p class="custom-hint">选择要显示的模块（至少保留一个），设置仅保存在本机。</p>
      <el-checkbox-group v-model="draftModules" class="module-list">
        <el-checkbox v-for="item in MODULES" :key="item.key" :value="item.key" class="module-item">
          <span class="module-name">{{ item.label }}</span>
          <small>{{ item.desc }}</small>
        </el-checkbox>
      </el-checkbox-group>
      <template #footer>
        <el-button @click="resetModules">恢复默认</el-button>
        <el-button @click="customizeVisible = false">取消</el-button>
        <el-button type="primary" @click="saveModules">保存</el-button>
      </template>
    </el-dialog>

    <BatchVerifyDialog v-model="batchVisible" :candidates="batchCandidates" />
  </div>
</template>

<style scoped>
.dashboard-page {
  min-height: 100%;
}

.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.dash-toolbar {
  flex-wrap: wrap;
}

.dash-kicker {
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 11px;
  letter-spacing: 0.18em;
  color: var(--v-text-3);
}

.dash-clock {
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 12px;
  color: var(--v-text-2);
  letter-spacing: 0.08em;
}

.dash-error {
  margin-bottom: 4px;
}

.hud {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.hud-row {
  display: flex;
  gap: 16px;
  align-items: stretch;
}

/* 用 flex 而不是固定网格：模块可被自定义隐藏，剩余面板要能自动铺满 */
.hud-top > .health-panel { flex: 0.9 1 230px; }
.hud-top > .core-panel { flex: 1.25 1 340px; }
.hud-top > .system-panel { flex: 0.85 1 240px; }

.hud-mid > *,
.hud-bottom > * {
  flex: 1 1 0;
  min-width: 0;
}

.hud-card {
  position: relative;
  padding: 16px 18px;
  min-width: 0;
  overflow: hidden;
}

/* HUD 角的细线：用两层 linear-gradient 画在右上 / 左下，不额外占 DOM */
.hud-card::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, transparent, var(--v-primary)) top right / 46px 1px no-repeat,
    linear-gradient(180deg, transparent, var(--v-primary)) top right / 1px 46px no-repeat;
  opacity: 0.35;
}

.hud-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}

.hud-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 11px;
  letter-spacing: 0.18em;
  color: var(--v-text-2);
}

.hud-title::before {
  content: '';
  width: 6px;
  height: 6px;
  background: var(--v-primary);
  box-shadow: 0 0 8px var(--v-primary);
}

.hud-subtitle {
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  letter-spacing: 0.16em;
  color: var(--v-text-3);
}

.hud-tag {
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--v-text-3);
  white-space: nowrap;
}

.hud-empty {
  padding: 18px 4px;
  text-align: center;
  font-size: 12px;
  color: var(--v-text-3);
}

.hud-divider {
  height: 1px;
  margin: 14px 0;
  background: linear-gradient(90deg, var(--v-border), transparent);
}

/* ── 状态矩阵 ─────────────────────────────────────────── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 12px;
  margin-bottom: 14px;
}

.stat-cell {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--v-border);
  border-radius: var(--v-radius-md);
  background: color-mix(in srgb, var(--v-bg-sunken) 72%, transparent);
}

.stat-cell strong {
  font-family: var(--v-font-display);
  font-size: 24px;
  line-height: 1;
  color: var(--v-text);
}

.stat-cell span {
  font-size: 11px;
  color: var(--v-text-3);
}

.status-bar,
.fresh-bar {
  display: flex;
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--v-border);
}

.status-bar span,
.fresh-bar span {
  display: block;
  height: 100%;
  transition: width var(--v-base) var(--v-ease-out);
}

.status-legend {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 10px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.status-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  border-radius: var(--v-radius-sm);
  font-size: 12px;
  color: var(--v-text-2);
  cursor: pointer;
  transition: background var(--v-fast) var(--v-ease);
}

.status-item:hover {
  background: var(--v-bg-hover);
}

.status-item.is-dim {
  opacity: 0.55;
}

.status-item i,
.fresh-legend i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.status-item b {
  margin-left: auto;
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  color: var(--v-text);
}

.disabled-hint {
  margin-top: 8px;
  padding: 5px 8px;
  border: 1px dashed color-mix(in srgb, var(--v-warning) 45%, transparent);
  border-radius: var(--v-radius-sm);
  font-size: 11px;
  color: var(--v-warning);
  cursor: pointer;
}

.disabled-hint:hover {
  background: color-mix(in srgb, var(--v-warning) 10%, transparent);
}

.fresh-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.fresh-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-top: 9px;
  font-size: 11px;
  color: var(--v-text-3);
}

.fresh-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.fresh-legend b {
  color: var(--v-text-2);
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
}

/* ── 中央核心 ─────────────────────────────────────────── */
.core-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 360px;
  padding: 6px 10px;
}

/* ── 系统状态 ─────────────────────────────────────────── */
.system-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.system-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 2px;
  border-bottom: 1px dashed color-mix(in srgb, var(--v-border) 70%, transparent);
}

.system-row:last-child {
  border-bottom: 0;
}

.system-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
  background: var(--v-success);
}

.system-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 1px;
}

.system-text span {
  font-size: 12px;
  color: var(--v-text-2);
}

.system-text small {
  font-size: 10px;
  color: var(--v-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.system-row b {
  margin-left: auto;
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 11px;
  font-weight: 500;
  color: var(--v-text);
}

/* ── 横向条形列表（类别 / 分组）────────────────────────── */
.bar-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.bar-list li {
  cursor: pointer;
}

.bar-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 5px;
  font-size: 12px;
  color: var(--v-text-2);
}

.bar-label span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-label b {
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-weight: 500;
  color: var(--v-text);
}

.bar-track {
  height: 6px;
  border-radius: 999px;
  background: var(--v-border);
  overflow: hidden;
}

.bar-fill {
  display: flex;
  height: 100%;
  border-radius: inherit;
  background: var(--v-primary);
  transition: width var(--v-base) var(--v-ease-out);
  overflow: hidden;
}

.bar-fill span {
  display: block;
  height: 100%;
}

.bar-list li:hover .bar-label span {
  color: var(--v-primary);
}

.group-list {
  gap: 12px;
}

/* ── 风险队列 ─────────────────────────────────────────── */
.risk-list,
.activity-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.risk-list li,
.activity-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 6px;
  border-radius: var(--v-radius-sm);
  cursor: pointer;
  transition: background var(--v-fast) var(--v-ease);
}

.risk-list li:hover,
.activity-list li:hover {
  background: var(--v-bg-hover);
}

.risk-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.risk-dot.is-danger { background: var(--v-danger); }
.risk-dot.is-warning { background: var(--v-warning); }
.risk-dot.is-info { background: var(--v-info); }
.risk-dot.is-stale { background: var(--v-text-3); }

.risk-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 2px;
}

.risk-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--v-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.risk-name em {
  padding: 0 5px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--v-warning) 14%, transparent);
  color: var(--v-warning);
  font-size: 10px;
  font-style: normal;
  flex: none;
}

.risk-text small {
  font-size: 10px;
  color: var(--v-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.risk-time {
  margin-left: auto;
  flex: none;
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  color: var(--v-text-3);
}

/* ── 最近活动 / 任务 ─────────────────────────────────── */
.activity-list li > i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}

.activity-name {
  font-size: 12px;
  color: var(--v-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-status {
  font-size: 10px;
  color: var(--v-text-3);
  flex: none;
}

.activity-time {
  margin-left: auto;
  flex: none;
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  color: var(--v-text-3);
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.task-label {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 5px;
}

.task-label span {
  font-size: 12px;
  color: var(--v-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-label small {
  font-size: 10px;
  color: var(--v-text-3);
  flex: none;
}

.task-row :deep(.el-progress-bar__outer) {
  background: var(--v-border);
}

.task-row :deep(.el-progress-bar__inner) {
  background: var(--v-grad-brand);
}

/* ── 载入骨架 ─────────────────────────────────────────── */
.dash-skeleton {
  min-height: 60vh;
  border: 1px dashed var(--v-border);
  border-radius: var(--v-radius-lg);
}

.hud-blank {
  min-height: 52vh;
  display: grid;
  place-items: center;
  border: 1px dashed var(--v-border);
  border-radius: var(--v-radius-lg);
}

/* ── 模块自定义对话框 ─────────────────────────────────── */
.custom-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--v-text-3);
}

.module-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.module-item {
  height: auto;
  margin-right: 0;
  align-items: flex-start;
}

.module-item :deep(.el-checkbox__label) {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.45;
}

.module-name {
  font-size: 13px;
  color: var(--v-text);
}

.module-item small {
  font-size: 11px;
  color: var(--v-text-3);
}

@media (max-width: 1280px) {
  .hud-top {
    flex-wrap: wrap;
  }
  .core-panel {
    order: -1;
    flex: 1 1 100%;
    min-height: 380px;
  }
  .hud-top > .health-panel,
  .hud-top > .system-panel {
    flex: 1 1 280px;
  }
  .hud-mid,
  .hud-bottom {
    flex-direction: column;
  }
  /* 竖排后主轴向变成高度：flex-basis: 0 会把 overflow:hidden 的卡片压成 0 高，
     只剩卡片外壳/标题、条目看不见。恢复按内容高度排布。 */
  .hud-mid > *,
  .hud-bottom > * {
    flex: 0 0 auto;
  }
}

@media (max-width: 860px) {
  .hud-top {
    flex-direction: column;
  }
  .hud-top > * {
    flex-basis: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .status-bar span,
  .fresh-bar span,
  .bar-fill {
    transition: none;
  }
}
</style>
