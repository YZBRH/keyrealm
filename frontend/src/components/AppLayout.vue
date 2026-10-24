<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Connection,
  Document,
  Download,
  Expand,
  Fold,
  FolderOpened,
  Grid,
  InfoFilled,
  Key,
  List,
  RefreshLeft,
  Setting,
  Lock,
  Minus,
} from '@element-plus/icons-vue'

import { api, EVENTS } from '@/api'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const vaultStore = useVaultStore()

const collapsed = ref(localStorage.getItem('vault.layout.asideCollapsed') === '1')

// Web / CLI 模式没有系统托盘，「最小化到托盘」按钮不适用
const isWebMode = computed(() => !!(window.__KEYREALM_WEB__ || {}).token)

watch(collapsed, (value) => {
  localStorage.setItem('vault.layout.asideCollapsed', value ? '1' : '0')
})

// 导航项默认顺序。用户可在侧栏直接拖拽调整，顺序存 localStorage（纯界面偏好，不进后端）。
const NAV_ITEMS = [
  { path: '/credentials', title: '凭据管理', icon: Key },
  { path: '/plugins', title: '插件管理', icon: Grid },
  { path: '/groups', title: '分组管理', icon: FolderOpened },
  { path: '/mcp', title: 'MCP 设置', icon: Connection },
  { path: '/logs', title: '日志', icon: Document },
  { path: '/tasks', title: '任务', icon: List },
  { path: '/settings', title: '系统设置', icon: Setting },
  { path: '/backup', title: '备份', icon: Download },
  { path: '/about', title: '关于', icon: InfoFilled },
]

const NAV_ORDER_KEY = 'vault.layout.navOrder'

function readSavedOrder() {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_ORDER_KEY) || 'null')
    return Array.isArray(raw) ? raw.filter((p) => typeof p === 'string') : []
  } catch {
    return []
  }
}

/** 按已保存的顺序排列；保存顺序里没有的项（如新增页面）追加到末尾，已移除的项直接忽略。 */
function applyOrder(saved) {
  if (!saved.length) return NAV_ITEMS.slice()
  const pending = new Map(NAV_ITEMS.map((item) => [item.path, item]))
  const ordered = []
  for (const path of saved) {
    const item = pending.get(path)
    if (item) {
      ordered.push(item)
      pending.delete(path)
    }
  }
  for (const item of NAV_ITEMS) {
    if (pending.has(item.path)) ordered.push(item)
  }
  return ordered
}

const navItems = ref(applyOrder(readSavedOrder()))

const isCustomOrder = computed(
  () => navItems.value.some((item, i) => item.path !== NAV_ITEMS[i].path),
)

const dragPath = ref('')
const dropPath = ref('')
const dropAfter = ref(false)

function resetDrag() {
  dragPath.value = ''
  dropPath.value = ''
  dropAfter.value = false
}

function onDragStart(item, event) {
  if (collapsed.value) return
  dragPath.value = item.path
  event.dataTransfer.effectAllowed = 'move'
  // Firefox 必须有 setData 才会真正开始拖拽
  event.dataTransfer.setData('text/plain', item.path)
}

function onDragOver(item, event) {
  if (!dragPath.value || item.path === dragPath.value) return
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
  const rect = event.currentTarget.getBoundingClientRect()
  dropPath.value = item.path
  dropAfter.value = event.clientY > rect.top + rect.height / 2
}

function onDrop(item, event) {
  event.preventDefault()
  const from = dragPath.value
  const to = item.path
  const after = dropAfter.value
  resetDrag()
  if (!from || from === to) return

  const list = navItems.value.slice()
  const fromIndex = list.findIndex((i) => i.path === from)
  if (fromIndex < 0) return
  const [moved] = list.splice(fromIndex, 1)
  let toIndex = list.findIndex((i) => i.path === to)
  if (toIndex < 0) return
  if (after) toIndex += 1
  list.splice(toIndex, 0, moved)

  navItems.value = list
  localStorage.setItem(NAV_ORDER_KEY, JSON.stringify(list.map((i) => i.path)))
}

async function resetNavOrder() {
  try {
    await ElMessageBox.confirm(
      '将把侧栏菜单恢复到默认顺序，当前的自定义排列会被清除。是否继续？',
      '恢复默认顺序',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  localStorage.removeItem(NAV_ORDER_KEY)
  navItems.value = NAV_ITEMS.slice()
  resetDrag()
  ElMessage.success('已恢复默认顺序')
}

const activePath = computed(() => route.path)

/* ── 后台任务角标 ───────────────────────────────────────
 * 侧栏上要能一眼看到「还有几个任务在跑」，所以这里自己维护一个计数。
 * 关键是**别每次进度推送都拉全量**：进度事件一秒可能好几条，拉全量会白打后端。
 * 于是记一份「任务 id → 状态」，靠快照的前后对比算出增减：
 * 状态从非进行中变成进行中 → +1，反之 → -1，其余（同一状态内的进度刷新）不动。
 * 拿不准的场合（拿到还没见过的任务）只影响增量，不影响准确性——计数与 id 表始终一致。
 */
const runningCount = ref(0)
const taskStatusById = new Map()

function applyTaskCountSnapshot(snapshot) {
  if (!snapshot || !snapshot.id) return
  const wasRunning = taskStatusById.get(snapshot.id) === 'running'
  const isRunning = snapshot.status === 'running'
  taskStatusById.set(snapshot.id, snapshot.status)
  if (isRunning && !wasRunning) runningCount.value += 1
  else if (!isRunning && wasRunning) runningCount.value = Math.max(0, runningCount.value - 1)
}

/** 整表重算（首次进入、以及事件给的快照用不上时的兜底）。 */
async function refreshTaskCount() {
  try {
    const list = await api.listTasks()
    taskStatusById.clear()
    let count = 0
    for (const task of Array.isArray(list) ? list : []) {
      if (!task || !task.id) continue
      taskStatusById.set(task.id, task.status)
      if (task.status === 'running') count += 1
    }
    runningCount.value = count
  } catch {
    // 未解锁 / 后端未就绪时静默：角标只是提示，不该弹错误打扰用户。
  }
}

function onTasksChanged(event) {
  const snapshot = event && event.detail
  if (!snapshot || !snapshot.id) {
    refreshTaskCount()
    return
  }
  applyTaskCountSnapshot(snapshot)
}

// 侧栏品牌旁的版本号（取 get_version 的 version，形如 1.4.1）
const appVersion = ref('')

async function loadAppVersion() {
  try {
    const info = await api.getVersion()
    appVersion.value = (info && info.version) || ''
  } catch {
    // 拿不到就不显示：装饰性信息，不打扰用户
  }
}

onMounted(() => {
  window.addEventListener(EVENTS.tasksChanged, onTasksChanged)
  refreshTaskCount()
  loadAppVersion()
})

onBeforeUnmount(() => {
  window.removeEventListener(EVENTS.tasksChanged, onTasksChanged)
})

const pageTitle = computed(() => route.meta.title || 'Keyrealm')

/* ── 批量验证横幅 ───────────────────────────────────────
 * 刻意做成**非阻塞**的横幅而不是模态弹窗：批量验证可能跑很久，模态会把整个界面按在前台，
 * 用户既看不了别的凭据、也去不了「任务」页。横幅只提示，其余操作照常可用
 * （「验证期间禁止改动凭据」那条互斥由 Credentials.vue 自己控制）。
 */
const batchBannerVisible = computed(
  () => vaultStore.batch.running || !!vaultStore.batch.result,
)

/** 完成后横幅的配色：有异常要一眼看出来，正常则是成功色。 */
const batchBannerType = computed(() => {
  const result = vaultStore.batch.result
  if (!result || result.canceled) return 'info'
  return result.error > 0 ? 'warning' : 'success'
})

const batchSummary = computed(() => {
  const result = vaultStore.batch.result
  if (!result) return ''
  if (result.canceled) {
    return `批量验证已取消（已处理 ${result.done ?? 0}/${result.total ?? 0}）`
  }
  const skipped = result.skipped ? `（跳过 ${result.skipped}）` : ''
  return `批量验证完成：有效 ${result.valid} · 无效 ${result.invalid} · 异常 ${result.error}${skipped}`
})

/** 运行中横幅的抬头：区分「自己点的」与「背后跑起来的」，用户才知道界面为什么被锁/没被锁。 */
const batchRunningLabel = computed(() => {
  const trigger = vaultStore.batch.trigger
  if (trigger === 'schedule') return '定时批量验证进行中'
  if (trigger === 'mcp') return 'MCP 触发的批量验证进行中'
  return '正在批量验证'
})

/** 关掉结果横幅 = 认掉这次结果；下一次验证开始时横幅会自然重新出现。 */
function closeBatchResult() {
  vaultStore.batch.result = null
}

function goTasks() {
  router.push('/tasks')
}

async function handleLock() {
  try {
    await appStore.lock()
    router.push('/unlock')
  } catch (err) {
    ElMessage.error(err.message || '锁定失败')
  }
}

async function handleMinimize() {
  try {
    // 成功不弹提示：缩到托盘是常规操作，窗口随即隐藏、提示也看不到，纯粹是噪音
    await appStore.minimizeToTray()
  } catch (err) {
    ElMessage.error(err.message || '最小化失败')
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside
      :width="collapsed ? '64px' : '176px'"
      class="layout-aside"
      :class="{ 'is-collapsed': collapsed }"
    >
      <div class="brand" :class="{ 'is-collapsed': collapsed }">
        <span class="brand-mark">
          <img src="/favicon.svg" class="brand-logo" alt="Keyrealm" />
        </span>
        <span v-show="!collapsed" class="brand-text v-grad-text">Keyrealm</span>
        <span v-show="!collapsed && appVersion" class="brand-version">v{{ appVersion }}</span>
      </div>

      <!-- 预留位：后续功能按钮加在这里 -->
      <div class="aside-toolbar">
        <el-tooltip
          :content="collapsed ? '展开侧栏' : '收起侧栏'"
          :placement="collapsed ? 'right' : 'bottom'"
        >
          <el-button text @click="collapsed = !collapsed">
            <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon>
          </el-button>
        </el-tooltip>
        <el-tooltip v-if="isCustomOrder" content="恢复默认顺序" placement="bottom">
          <el-button text :icon="RefreshLeft" @click="resetNavOrder" />
        </el-tooltip>
        <span class="toolbar-spacer" />
      </div>

      <el-menu :default-active="activePath" :collapse="collapsed" :collapse-transition="false" router class="layout-menu">
        <el-menu-item
          v-for="item in navItems"
          :key="item.path"
          :index="item.path"
          :draggable="!collapsed"
          :class="{
            'is-dragging': dragPath === item.path,
            'is-drop-before': dropPath === item.path && !dropAfter,
            'is-drop-after': dropPath === item.path && dropAfter,
          }"
          @dragstart="onDragStart(item, $event)"
          @dragover="onDragOver(item, $event)"
          @drop="onDrop(item, $event)"
          @dragend="resetDrag"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
          <!-- 进行中任务数角标：为 0 时不渲染（不占位、不显示「0」） -->
          <span
            v-if="item.path === '/tasks' && runningCount > 0"
            class="nav-badge"
          >{{ runningCount }}</span>
        </el-menu-item>
      </el-menu>

      <div v-show="!collapsed" class="aside-foot">
        <span class="aside-foot-dot" />
        <span>本地保险库</span>
      </div>
    </el-aside>

    <el-container>
      <el-header class="layout-header">
        <div class="header-title">
          <span class="header-accent" />
          {{ pageTitle }}
        </div>
        <div class="header-actions">
          <el-tooltip :content="appStore.tray.mcp_running ? 'MCP 服务运行中' : 'MCP 服务已停止'" placement="bottom">
            <span
              class="mcp-indicator"
              :class="[appStore.tray.mcp_running ? 'is-on' : 'is-off']"
            >
              <i class="dot" />
              MCP {{ appStore.tray.mcp_running ? '运行中' : '已停止' }}
            </span>
          </el-tooltip>

          <template v-if="!isWebMode">
            <el-divider direction="vertical" />
            <el-button text :icon="Minus" @click="handleMinimize">最小化到托盘</el-button>
          </template>
          <el-button type="primary" plain :icon="Lock" @click="handleLock">锁定</el-button>
        </div>
      </el-header>

      <!-- 批量验证横幅：不阻塞、不遮罩，用户仍可切页 / 翻凭据 / 去「任务」页 -->
      <el-alert
        v-if="batchBannerVisible"
        class="batch-banner"
        :type="batchBannerType"
        :closable="!vaultStore.batch.running"
        show-icon
        @close="closeBatchResult"
      >
        <template #default>
          <span class="batch-banner-row">
            <span class="batch-banner-text">
              <template v-if="vaultStore.batch.running">
                {{ batchRunningLabel }}
                <strong>{{ vaultStore.batch.done }}/{{ vaultStore.batch.total || '—' }}</strong>
                · 当前：<strong>{{ vaultStore.batch.currentName || '准备中…' }}</strong>
              </template>
              <template v-else>{{ batchSummary }}</template>
            </span>
            <el-button
              v-if="vaultStore.batch.running"
              size="small"
              text
              type="primary"
              @click="goTasks"
            >查看任务</el-button>
          </span>
        </template>
      </el-alert>

      <el-main class="layout-main">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
  overflow: hidden;
  background: transparent;
}

/* ── 侧栏：一层薄玻璃，让背景极光透过来 ───────────────────────────── */

.layout-aside {
  position: relative;
  display: flex;
  flex-direction: column;
  background: var(--v-glass-bg);
  -webkit-backdrop-filter: var(--v-glass-backdrop, none);
  backdrop-filter: var(--v-glass-backdrop, none);
  border-right: 1px solid var(--v-border);
  transition: width 0.24s var(--v-ease);
  overflow: hidden;
}

/* 侧栏右缘一道竖直渐变，替代生硬的分割线 */
.layout-aside::after {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  width: 1px;
  height: 100%;
  background: linear-gradient(
    to bottom,
    transparent,
    color-mix(in srgb, var(--v-primary) 30%, transparent),
    transparent
  );
  pointer-events: none;
}

/* ── 品牌区 ─────────────────────────────────────────────────────── */

/* 宽度 176px 是按**改名前的旧品牌名**（`Credential Vault`，17 字符）定的：152px 时那个名字
   会被截成带省略号的样子。现在的名字 `Keyrealm` 只有 8 字符，176px 明显有富余——
   但侧栏宽度还被别处引用，收窄要连着看这些地方，故先留着。（见「品牌区」下方各处的 176px） */
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 62px;
  padding: 0 12px;
  white-space: nowrap;
  border-bottom: 1px solid var(--v-border);
  flex-shrink: 0;
}

/* 静态品牌标记。这里原来挂了一个 5s 无限「呼吸」的 box-shadow 动画，但侧边栏是
   常驻的，常驻的微弱闪动没有信息量，只剩持续重绘 —— 改成一次画好的静态内描边。 */
.brand-mark {
  position: relative;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: 9px;
  background: var(--v-grad-brand-soft);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--v-primary) 30%, transparent);
}

.brand-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--v-font-display);
  font-weight: 750;
  font-size: 12.5px;
  letter-spacing: -0.02em;
}

/* 品牌名右侧的版本号：小字、灰、不参与截断（窄了先挤品牌名） */
.brand-version {
  flex-shrink: 0;
  margin-left: -4px;
  font-size: 10.5px;
  font-weight: 500;
  color: var(--v-text-3);
}

.brand.is-collapsed {
  justify-content: center;
  padding: 0;
}

.brand-logo {
  width: 17px;
  height: 17px;
  border-radius: 5px;
  flex-shrink: 0;
}

/* ── 侧栏工具条 ─────────────────────────────────────────────────── */

.aside-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 7px 8px;
  border-bottom: 1px solid var(--v-border);
  flex-shrink: 0;
}

.aside-toolbar .el-button {
  padding: 6px;
  color: var(--v-text-2);
}

.toolbar-spacer {
  margin-left: auto;
}

.layout-aside.is-collapsed .aside-toolbar {
  justify-content: center;
  padding: 7px 2px;
}

.layout-aside.is-collapsed .toolbar-spacer {
  display: none;
}

/* ── 菜单 ───────────────────────────────────────────────────────── */

.layout-menu {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px 0;
  border-right: none;
  background: transparent;
}

/* 展开态：左对齐，图标 + 文字 */
.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item) {
  position: relative;
  justify-content: flex-start;
  padding-left: 13px !important;
  padding-right: 10px !important;
  font-size: 13.5px;
}

.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item .el-icon) {
  margin-right: 9px;
  font-size: 17px;
}

/* 激活项的左侧指示条改用「胶囊」贴边样式（基座里那条是给通用场景的） */
.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item.is-active)::before {
  left: -8px;
  height: 20px;
}

/* 收起态：居中，且去掉图标右间距以免偏心 */
.layout-aside.is-collapsed .layout-menu :deep(.el-menu-item) {
  position: relative;
  justify-content: center;
  padding: 0 !important;
}

.layout-aside.is-collapsed .layout-menu :deep(.el-menu-item .el-icon) {
  margin-right: 0;
}

/* 拖拽排序：展开态显示抓手、可拖、悬停高亮插入位 */
.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item) {
  cursor: grab;
}

.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item)::after {
  content: '';
  position: absolute;
  right: 8px;
  top: 50%;
  width: 10px;
  height: 12px;
  margin-top: -6px;
  background-image: radial-gradient(circle, currentColor 1px, transparent 1.2px);
  background-size: 5px 5px;
  background-position: 2px 1px;
  opacity: 0;
  transition: opacity 0.15s;
  pointer-events: none;
}

.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item:hover)::after {
  opacity: 0.35;
}

.layout-menu :deep(.el-menu-item.is-dragging) {
  opacity: 0.4;
}

.layout-menu :deep(.el-menu-item.is-drop-before)::before,
.layout-menu :deep(.el-menu-item.is-drop-after)::before {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  border-radius: 1px;
  background: var(--v-primary);
  /* 覆盖基座里那条竖指示条的动画 */
  transform: none;
  animation: none;
}

.layout-menu :deep(.el-menu-item.is-drop-before)::before {
  top: -1px;
}

.layout-menu :deep(.el-menu-item.is-drop-after)::before {
  bottom: -1px;
}

/* 进行中任务角标。菜单项本身已是 position: relative，这里直接绝对定位；
   收起态没有标题文字，就把角标挪到图标右上角，免得无处可依。 */
.nav-badge {
  position: absolute;
  right: 10px;
  top: 50%;
  transform: translateY(-50%);
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  box-sizing: border-box;
  border-radius: 999px;
  background: var(--v-grad-brand);
  color: var(--v-primary-ink);
  font-size: 11px;
  font-weight: 600;
  line-height: 17px;
  text-align: center;
  pointer-events: none;
  box-shadow: 0 0 0 2px var(--v-glass-bg);
}

.layout-aside.is-collapsed .nav-badge {
  right: 5px;
  top: 5px;
  transform: none;
}

/* 角标与右侧的拖拽抓手 ::after 位置重合（都是贴右）。带角标的那一项隐掉抓手，
   免得 hover 时两者叠在一起发花。:has() 不被支持时这条规则被忽略，只是回到原来的轻微重叠。 */
.layout-aside:not(.is-collapsed) .layout-menu :deep(.el-menu-item:has(.nav-badge))::after {
  display: none;
}

/* ── 侧栏底部的状态标签 ─────────────────────────────────────────── */

.aside-foot {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 10px 14px;
  border-top: 1px solid var(--v-border);
  font-size: 11.5px;
  color: var(--v-text-3);
  flex-shrink: 0;
  white-space: nowrap;
}

.aside-foot-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--v-success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--v-success) 20%, transparent);
  flex-shrink: 0;
}

/* ── 顶栏 ───────────────────────────────────────────────────────── */

.layout-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: 62px;
  border-bottom: 1px solid var(--v-border);
  background: var(--v-glass-bg);
  -webkit-backdrop-filter: var(--v-glass-backdrop, none);
  backdrop-filter: var(--v-glass-backdrop, none);
  position: relative;
  z-index: 5;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--v-font-display);
  font-size: 17px;
  font-weight: 650;
  letter-spacing: var(--v-title-tracking);
  color: var(--v-text);
}

/* 标题左侧一小段渐变竖条 */
.header-accent {
  width: 3px;
  height: 17px;
  border-radius: 2px;
  background: var(--v-grad-brand);
  flex-shrink: 0;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* ── MCP 指示灯 ─────────────────────────────────────────────────── */

.mcp-indicator {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid var(--v-border);
  background: color-mix(in srgb, var(--v-bg-sunken) 60%, transparent);
  font-size: 12.5px;
  color: var(--v-text-2);
  transition: color var(--v-base) var(--v-ease), border-color var(--v-base) var(--v-ease);
}

.mcp-indicator .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--v-info);
  flex-shrink: 0;
}

.mcp-indicator.is-on {
  color: var(--v-success);
  border-color: color-mix(in srgb, var(--v-success) 32%, transparent);
  background: color-mix(in srgb, var(--v-success) 9%, transparent);
}

.mcp-indicator.is-on .dot {
  background: var(--v-success);
}

.mcp-indicator.is-off .dot {
  background: var(--v-danger);
}

.mcp-indicator.is-off {
  color: var(--v-text-2);
}

/* ── 内容区：透明，好让背景极光透上来 ───────────────────────────── */

.layout-main {
  padding: 20px;
  overflow-y: auto;
  background: transparent;
}

/* 批量验证横幅：贴在内容区顶部，宽满、不抢占滚动的 el-main */
.batch-banner {
  flex-shrink: 0;
  border-radius: 0;
  border-left: none;
  border-right: none;
  margin: 0;
  animation: v-fade-in var(--v-base) var(--v-ease);
}

.batch-banner :deep(.el-alert__content) {
  width: 100%;
  padding-right: 4px;
}

.batch-banner-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  line-height: 20px;
}

.batch-banner-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 基座里的 reduced-motion 规则管不到组件内的关键帧（作用域不同），这里自己补一条 */
@media (prefers-reduced-motion: reduce) {
  .brand-mark {
    animation: none;
  }
}
</style>
