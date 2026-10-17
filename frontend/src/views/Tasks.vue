<script setup>
/**
 * 任务（后台任务管理 + 定时验证计划）。
 *
 * 为什么独立成页：下载/打包这类长操作跑在后台线程里，关掉发起它的弹窗也继续跑。
 * 用户需要一个**固定入口**回来看「还在跑哪些、进度多少、成没成」，并能随时取消。
 * 弹窗里那条 `vault:action_stream` 是一次性事件流，弹窗一关就没人接了，撑不起这个需求。
 *
 * 定时验证计划也放这一页：它跟后台任务同属「程序自己在后台干的事」，用户想找它们时
 * 直觉是来这一页，而不是「系统设置」。计划本身由后端调度器（APScheduler）持有，
 * 这一页只做增删改查与「立即运行」。
 *
 * 数据契约：call('list_tasks') 直接返回数组，且**已按「进行中在前、其内按创建时间倒序」
 * 排好**（排序在后端做，前端照序渲染即可，别自己再排一遍——两边规则不一致会打架）。
 * 增量更新走 `vault:tasks` 事件，detail 是**发生变化的那一条快照**（不是全量列表），
 * 因此这里就地更新/插入对应行，避免每次进度推送都拉一次全量。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight, Delete, Edit, Plus, Refresh, Timer, VideoPlay } from '@element-plus/icons-vue'

import { api, EVENTS } from '@/api'
import CredentialTreePicker from '@/components/CredentialTreePicker.vue'
import { useVaultStore } from '@/stores/vault'
import { formatDateTime, formatRelative } from '@/utils/format'

const vaultStore = useVaultStore()

const tasks = ref([])
const loading = ref(false)

// 状态 → 标签文案/颜色。后端状态是固定集合，这里兜底 unknown 免得出现空白标签。
// interrupted：上次退出时还在跑的任务。线程随进程没了，任务不可能继续，单列一档
// 与「失败」「已取消」区分——用户没做错什么，也不是他点的取消。
const STATUS_META = {
  running: { label: '进行中', type: 'primary' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  canceled: { label: '已取消', type: 'info' },
  interrupted: { label: '已中断', type: 'warning' },
}

const runningCount = computed(() => tasks.value.filter((t) => t.status === 'running').length)

// ── 分页：与「凭据管理」同一套语义（纯展示层；每页条数持久化，页号不持久化）──
const PAGE_SIZE_KEY = 'vault.tasks.pageSize'
// 预设值；下拉允许手输（allow-create），5/10/20/50 只是入口、不是白名单
const PAGE_SIZES = [5, 10, 20, 50]
const PAGE_SIZE_DEFAULT = 10

/** 把任意来源的每页条数规整成合法值（正整数，否则回落默认）。 */
function normalizePageSize(value) {
  const n = Math.floor(Number(value))
  return Number.isFinite(n) && n > 0 ? n : PAGE_SIZE_DEFAULT
}

const pageSize = ref(normalizePageSize(localStorage.getItem(PAGE_SIZE_KEY)))
// 当前页**不持久化**：页号是「我正看到哪儿」，下次打开还停在第 5 页只会让人以为列表少了内容
const currentPage = ref(1)
watch(pageSize, (value) => localStorage.setItem(PAGE_SIZE_KEY, String(value)))

/** 手输的每页条数写回前先规整；非法值要说出来，别让用户以为界面悄悄改了。 */
function onPageSizeChange(value) {
  const normalized = normalizePageSize(value)
  if (normalized !== pageSize.value) pageSize.value = normalized
  if (!(Number(value) > 0)) {
    ElMessage.warning(`每页条数需为正整数，已恢复为 ${PAGE_SIZE_DEFAULT}`)
  }
}

const totalPages = computed(() => Math.max(1, Math.ceil((tasks.value.length || 0) / pageSize.value)))

// 条数变少（清空已完成 / 调大每页条数）后停在越界页会看到一片空白，把页号夹回合法范围。
// 用 watch 而不是在 computed 里顺手改：computed 里写 ref 是反向依赖，Vue 会警告。
watch(totalPages, () => {
  if (currentPage.value > totalPages.value) currentPage.value = totalPages.value
})

/** 当前页要展示的任务。总数 ≤ 每页条数时就是全部（1 页）。 */
const pagedTasks = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return tasks.value.slice(start, start + pageSize.value)
})

function statusMeta(task) {
  // 「已请求取消」是进行中的子状态：插件还没跑到检查点，结果未定，所以单独给文案与颜色，
  // 避免用户以为点了取消却没生效。
  if (task.cancel_requested && task.status === 'running') {
    return { label: '正在取消…', type: 'warning' }
  }
  return STATUS_META[task.status] || { label: task.status || '未知', type: 'info' }
}

/** total<=0 表示「总量未知」（如分块下载拿不到 Content-Length），此时进度条走不定态。 */
function progressPercent(task) {
  if (!task.total || task.total <= 0) return 0
  return Math.min(100, Math.round((task.current / task.total) * 100))
}

function formatTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}

/** 终态行只展示一行摘要：失败优先看 error，成功/取消看 output。
 *  中断的任务两者都为空，改用它被改判时写下的说明（text）。 */
function resultLine(task) {
  if (task.status === 'running') return ''
  return task.error || task.output || (task.status === 'interrupted' ? task.text : '') || ''
}

async function load() {
  loading.value = true
  try {
    const list = await api.listTasks()
    tasks.value = Array.isArray(list) ? list : []
  } catch (err) {
    ElMessage.error(err.message || '加载任务失败')
  } finally {
    loading.value = false
  }
}

/**
 * 把一条任务快照落到列表里。
 *
 * 已存在就整行替换（后端推的是最新快照，字段齐全）；新任务（本页没见过的）按后端的
 * 排序规则插入，保证「进行中在前」这个不变量不被破坏。
 */
function applySnapshot(snapshot) {
  if (!snapshot || !snapshot.id) return false
  const list = tasks.value
  const index = list.findIndex((t) => t.id === snapshot.id)
  if (index >= 0) {
    list[index] = snapshot
    return true
  }
  if (snapshot.status === 'running') {
    list.unshift(snapshot)
  } else {
    // 已结束的新记录（本页错过了它的进行中阶段）：插到「最后一条进行中任务」之后。
    const firstFinished = list.findIndex((t) => t.status !== 'running')
    if (firstFinished < 0) list.push(snapshot)
    else list.splice(firstFinished, 0, snapshot)
  }
  return true
}

function onTasksEvent(event) {
  const snapshot = event && event.detail
  // 拿不到快照（后端结构变了等）就整表兜底，宁可多拉一次也别让列表卡在旧状态。
  if (!applySnapshot(snapshot)) load()
}

async function cancelTask(task) {
  try {
    await ElMessageBox.confirm(
      `将停止「${task.title}」并清理未完成的临时文件，确定取消吗？`,
      '取消任务',
      { type: 'warning', confirmButtonText: '取消任务', cancelButtonText: '返回' },
    )
  } catch {
    return
  }
  try {
    await api.cancelTask(task.id)
    // 不主动刷新：取消是协作式的，状态要等插件跑到检查点才变，
    // 由 vault:tasks 推送回来更新，这里抢跑会显示成「取消没生效」。
    ElMessage.success('已请求取消')
  } catch (err) {
    ElMessage.error(err.message || '取消失败')
  }
}

async function clearFinished() {
  try {
    await ElMessageBox.confirm(
      '将移除所有已结束（已完成 / 失败 / 已取消 / 已中断）的任务记录，进行中的任务不受影响。',
      '清空已完成',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await api.clearFinishedTasks()
    ElMessage.success('已清空已结束的任务')
    await load()
  } catch (err) {
    ElMessage.error(err.message || '清空失败')
  }
}

/* ── 定时验证计划 ───────────────────────────────────── */

const WEEKDAYS = [
  { label: '周一', value: 'mon' },
  { label: '周二', value: 'tue' },
  { label: '周三', value: 'wed' },
  { label: '周四', value: 'thu' },
  { label: '周五', value: 'fri' },
  { label: '周六', value: 'sat' },
  { label: '周日', value: 'sun' },
]

const INTERVAL_UNITS = [
  { label: '分钟', value: 'minutes' },
  { label: '小时', value: 'hours' },
  { label: '天', value: 'days' },
]

const WEEKDAY_LABEL = WEEKDAYS.reduce((acc, item) => {
  acc[item.value] = item.label
  return acc
}, {})

const schedules = ref([])
const schedulesLoading = ref(false)
const scheduleDialog = ref(false)
const scheduleSubmitting = ref(false)
const scheduleFormRef = ref(null)
const editingId = ref(null)
const togglingId = ref(null)
const runningId = ref(null)

const scheduleForm = reactive({
  name: '',
  trigger_type: 'interval',
  intervalValue: 6,
  intervalUnit: 'hours',
  cronMode: 'daily',
  cronTime: '03:00',
  cronWeekday: 'mon',
  advanced: false,
  triggerArgsText: '',
  target_scope: 'all',
  target_id: null,
  // 仅 target_scope === 'credentials' 时使用：目录树勾选的具体凭据 id
  credential_ids: [],
})

const isEdit = computed(() => editingId.value !== null)

const scheduleRules = {
  name: [{ required: true, message: '请输入计划名称', trigger: 'blur' }],
}

const groupOptions = computed(() => vaultStore.groups.map((item) => ({ label: item.name, value: item.id })))
const typeOptions = computed(() => vaultStore.typeOptions)

const triggerArgsPreview = computed(() => {
  if (scheduleForm.advanced) return scheduleForm.triggerArgsText.trim() || '{}'
  return JSON.stringify(previewTriggerArgs(), null, 2)
})

function previewTriggerArgs() {
  if (scheduleForm.trigger_type === 'interval') {
    return { [scheduleForm.intervalUnit]: Number(scheduleForm.intervalValue) || 1 }
  }
  const [hour, minute] = String(scheduleForm.cronTime || '03:00').split(':').map((part) => Number(part) || 0)
  if (scheduleForm.cronMode === 'weekly') {
    return { day_of_week: scheduleForm.cronWeekday, hour, minute }
  }
  return { hour, minute }
}

function buildTriggerArgs() {
  if (!scheduleForm.advanced) return previewTriggerArgs()
  const text = scheduleForm.triggerArgsText.trim()
  if (!text) throw new Error('高级模式下触发器参数不能为空')
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('触发器参数不是合法的 JSON，请检查格式')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('触发器参数必须是一个 JSON 对象')
  }
  return parsed
}

function describeTrigger(schedule) {
  const args = schedule.trigger_args || {}
  if (schedule.trigger_type === 'interval') {
    for (const unit of INTERVAL_UNITS) {
      if (args[unit.value] !== undefined) {
        const suffix = { minutes: '分钟', hours: '小时', days: '天' }[unit.value]
        return `每 ${args[unit.value]} ${suffix}`
      }
    }
    return '按间隔执行'
  }
  if (schedule.trigger_type === 'cron') {
    const hour = String(args.hour ?? 0).padStart(2, '0')
    const minute = String(args.minute ?? 0).padStart(2, '0')
    if (args.day_of_week !== undefined) {
      const label = WEEKDAY_LABEL[String(args.day_of_week)] || String(args.day_of_week)
      return `每${label} ${hour}:${minute}`
    }
    if (args.day !== undefined) return `每月 ${args.day} 日 ${hour}:${minute}`
    return `每天 ${hour}:${minute}`
  }
  return schedule.trigger_type
}

function describeTarget(schedule) {
  if (schedule.target_scope === 'group') {
    const group = vaultStore.groups.find((item) => item.id === schedule.target_id)
    return group ? `分组：${group.name}` : `分组 #${schedule.target_id}`
  }
  if (schedule.target_scope === 'type') {
    const found = vaultStore.plugins.find((item) => item.credential_type === schedule.target_id)
    return found ? `类型：${found.name}` : `类型：${schedule.target_id}`
  }
  if (schedule.target_scope === 'credentials') {
    return `指定凭据：${(schedule.credential_ids || []).length} 条`
  }
  return '全部凭据'
}

function resetScheduleForm() {
  scheduleForm.name = ''
  scheduleForm.trigger_type = 'interval'
  scheduleForm.intervalValue = 6
  scheduleForm.intervalUnit = 'hours'
  scheduleForm.cronMode = 'daily'
  scheduleForm.cronTime = '03:00'
  scheduleForm.cronWeekday = 'mon'
  scheduleForm.advanced = false
  scheduleForm.triggerArgsText = ''
  scheduleForm.target_scope = 'all'
  scheduleForm.target_id = null
  scheduleForm.credential_ids = []
}

function openCreate() {
  editingId.value = null
  resetScheduleForm()
  scheduleDialog.value = true
}

function openEdit(row) {
  editingId.value = row.id
  resetScheduleForm()
  scheduleForm.name = row.name
  scheduleForm.trigger_type = row.trigger_type
  scheduleForm.target_scope = row.target_scope || 'all'
  scheduleForm.target_id = row.target_id ?? null
  // 「指定凭据」的勾选结果（凭据 id 数组）；其它 scope 下后端存的是空数组
  scheduleForm.credential_ids = Array.isArray(row.credential_ids) ? [...row.credential_ids] : []

  const args = row.trigger_args || {}
  if (row.trigger_type === 'interval') {
    const unit = INTERVAL_UNITS.find((item) => args[item.value] !== undefined)
    if (unit) {
      scheduleForm.intervalUnit = unit.value
      scheduleForm.intervalValue = args[unit.value]
    }
    scheduleForm.advanced = !unit
  } else if (row.trigger_type === 'cron') {
    const hour = String(args.hour ?? 0).padStart(2, '0')
    const minute = String(args.minute ?? 0).padStart(2, '0')
    scheduleForm.cronTime = `${hour}:${minute}`
    if (args.day_of_week !== undefined) {
      scheduleForm.cronMode = 'weekly'
      scheduleForm.cronWeekday = String(args.day_of_week)
    } else {
      scheduleForm.cronMode = 'daily'
    }
    scheduleForm.advanced = args.day !== undefined
  } else {
    scheduleForm.advanced = true
  }

  scheduleForm.triggerArgsText = JSON.stringify(args, null, 2)
  scheduleDialog.value = true
}

async function loadSchedules() {
  schedulesLoading.value = true
  try {
    const result = await api.listSchedules()
    schedules.value = Array.isArray(result) ? result : []
  } catch (err) {
    ElMessage.error(err.message || '加载定时计划失败')
  } finally {
    schedulesLoading.value = false
  }
}

async function submitSchedule() {
  if (!scheduleFormRef.value) return
  try {
    await scheduleFormRef.value.validate()
  } catch {
    return
  }

  let triggerArgs
  try {
    triggerArgs = buildTriggerArgs()
  } catch (err) {
    ElMessage.error(err.message)
    return
  }

  // target_id 是多态字段，只对 group/type 有意义；「指定凭据」把勾选结果放 credential_ids。
  // 两个字段都随 payload 全量送出（另一个置空/空数组），否则编辑时切走 scope 会留下上一次的残值。
  const isCredentials = scheduleForm.target_scope === 'credentials'
  const payload = {
    name: scheduleForm.name.trim(),
    trigger_type: scheduleForm.trigger_type,
    trigger_args: triggerArgs,
    target_scope: scheduleForm.target_scope,
    target_id: ['group', 'type'].includes(scheduleForm.target_scope) ? scheduleForm.target_id : null,
    credential_ids: isCredentials ? [...scheduleForm.credential_ids] : [],
  }

  if (payload.target_scope === 'credentials' && payload.credential_ids.length === 0) {
    ElMessage.warning('请至少选择一条凭据')
    return
  }
  if (['group', 'type'].includes(payload.target_scope) && payload.target_id === null) {
    ElMessage.warning('请选择验证范围对应的目标')
    return
  }

  scheduleSubmitting.value = true
  try {
    if (isEdit.value) {
      await api.updateSchedule(editingId.value, payload)
      ElMessage.success('计划已更新')
    } else {
      await api.createSchedule(payload)
      ElMessage.success('计划已创建')
    }
    scheduleDialog.value = false
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '保存计划失败')
  } finally {
    scheduleSubmitting.value = false
  }
}

async function toggleSchedule(row, enabled) {
  togglingId.value = row.id
  try {
    await api.updateSchedule(row.id, { enabled })
    row.enabled = enabled
    ElMessage.success(`计划「${row.name}」已${enabled ? '启用' : '停用'}`)
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '操作失败，请确认后端 update_schedule 支持字典参数')
  } finally {
    togglingId.value = null
  }
}

async function runScheduleNow(row) {
  const scope = row.target_scope || 'all'
  const ids = Array.isArray(row.credential_ids) ? row.credential_ids : []
  // 「指定凭据」却没勾任何一条：batch_verify 把空 credential_ids 视同「不带筛选」→ 会去验全库，
  // 与该计划「一条也不验」的意图正好相反。这里必须拦住（后端 _run_schedule 同样拦）。
  if (scope === 'credentials' && ids.length === 0) {
    ElMessage.warning('该计划未选中任何凭据，无法运行')
    return
  }
  runningId.value = row.id
  try {
    await vaultStore.batchVerify(
      scope,
      scope === 'group' ? row.target_id : null,
      scope === 'type' ? row.target_id : null,
      scope === 'credentials' ? ids : null,
    )
    ElMessage.success(`已按计划「${row.name}」的范围启动验证`)
  } catch (err) {
    ElMessage.error(err.message || '启动验证失败')
  } finally {
    runningId.value = null
  }
}

async function removeSchedule(row) {
  try {
    await ElMessageBox.confirm(`确定删除定时计划「${row.name}」吗？`, '删除计划', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }

  try {
    await api.deleteSchedule(row.id)
    ElMessage.success('计划已删除')
    await loadSchedules()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

/** 顶部「刷新」：后台任务与定时计划一起刷（两者同页，只刷一个会让人以为另一个没更新）。 */
async function refreshAll() {
  await Promise.all([load(), loadSchedules()])
}

onMounted(async () => {
  window.addEventListener(EVENTS.tasksChanged, onTasksEvent)
  await Promise.all([
    load(),
    loadSchedules(),
    // 计划编辑里要用分组/类型做「验证范围」下拉，取不到也不影响本页主体，失败静默。
    vaultStore.fetchGroups().catch(() => {}),
    vaultStore.fetchPlugins().catch(() => {}),
  ])
})

onBeforeUnmount(() => {
  // 组件销毁必须摘掉监听，否则每次进出页面都会叠一层 handler（推送一来就重复更新）。
  window.removeEventListener(EVENTS.tasksChanged, onTasksEvent)
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">任务</h2>
      <span v-if="runningCount" class="text-muted">{{ runningCount }} 个进行中</span>
      <div class="spacer" />
      <el-button :icon="Refresh" :loading="loading || schedulesLoading" @click="refreshAll">刷新</el-button>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>定时验证计划</span>
          <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新建计划</el-button>
        </div>
      </template>

      <!-- 锁定期提示：写到计划表格上方，用户建/看计划时先看到这条前提 -->
      <el-alert
        class="lock-hint"
        type="warning"
        :closable="false"
        show-icon
        title="数据库锁定期间，定时任务将不会实际执行验证"
        description="保险库锁定时，到点的计划整轮跳过——不逐条验证、不发起任何网络请求，凭据状态也不会变化。"
      />

      <el-table v-loading="schedulesLoading" :data="schedules" empty-text="暂无定时计划" style="width: 100%">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <span class="plan-name">{{ row.name }}</span>
          </template>
        </el-table-column>

        <el-table-column label="触发规则" width="180">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :icon="Timer">{{ describeTrigger(row) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="验证范围" min-width="150">
          <template #default="{ row }">
            <span class="text-muted">{{ describeTarget(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              size="small"
              :loading="togglingId === row.id"
              @change="(value) => toggleSchedule(row, value)"
            />
          </template>
        </el-table-column>

        <el-table-column label="上次运行" width="130">
          <template #default="{ row }">
            <span class="text-muted">{{ formatRelative(row.last_run_at, '从未运行') }}</span>
          </template>
        </el-table-column>

        <el-table-column label="下次运行" width="170">
          <template #default="{ row }">
            <span v-if="row.next_run_at" class="text-muted">{{ formatDateTime(row.next_run_at) }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              :icon="VideoPlay"
              :loading="runningId === row.id"
              @click="runScheduleNow(row)"
            >
              立即运行
            </el-button>
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="removeSchedule(row)" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>后台任务</span>
          <span class="text-muted header-hint">记录会保留到下次启动</span>
          <div class="spacer" />
          <!-- 分页：与「凭据管理」同一套语义（每页条数可预设可手输、页号不持久化） -->
          <div v-if="tasks.length" class="pager">
            <span class="pager-unit">每页</span>
            <el-select
              v-model="pageSize"
              class="pager-size"
              title="可选择预设值，也可直接输入自定义条数"
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              @change="onPageSizeChange"
            >
              <el-option v-for="size in PAGE_SIZES" :key="size" :label="String(size)" :value="size" />
            </el-select>
            <span class="pager-unit">条</span>
            <el-button :icon="ArrowLeft" :disabled="currentPage <= 1" title="上一页" @click="currentPage--" />
            <span class="pager-page">{{ currentPage }} / {{ totalPages }}</span>
            <el-button :icon="ArrowRight" :disabled="currentPage >= totalPages" title="下一页" @click="currentPage++" />
          </div>
          <el-button type="danger" plain :icon="Delete" @click="clearFinished">清空已完成</el-button>
        </div>
      </template>

      <div v-loading="loading">
        <div v-if="tasks.length" class="task-list">
          <div v-for="task in pagedTasks" :key="task.id" class="task-row">
            <div class="task-head">
              <span class="task-title" :title="task.title">{{ task.title }}</span>
              <el-tag :type="statusMeta(task).type" size="small" effect="light">
                {{ statusMeta(task).label }}
              </el-tag>
              <div class="spacer" />
              <span class="task-time">{{ formatTime(task.created_at) }}</span>
            </div>

            <!-- 进度条只在进行中显示：终态留在那里只会让人以为还在跑 -->
            <div v-if="task.status === 'running'" class="task-progress">
              <el-progress
                class="progress-bar"
                :percentage="progressPercent(task)"
                :indeterminate="!task.total || task.total <= 0"
                :show-text="Boolean(task.total && task.total > 0)"
                :stroke-width="10"
              />
              <span v-if="task.text" class="progress-text">{{ task.text }}</span>
            </div>

            <!-- 终态摘要：一行灰字，装不下省略（hover 看全文），别把行撑高 -->
            <div v-if="resultLine(task)" class="task-result">
              <el-tooltip :content="resultLine(task)" placement="top" :show-after="400">
                <span class="result-text" :class="{ 'is-error': task.status === 'failed' }">
                  {{ resultLine(task) }}
                </span>
              </el-tooltip>
            </div>

            <div v-if="task.status === 'running'" class="task-actions">
              <el-button
                size="small"
                :disabled="task.cancel_requested"
                @click="cancelTask(task)"
              >
                {{ task.cancel_requested ? '正在取消…' : '取消' }}
              </el-button>
            </div>
          </div>
        </div>

        <el-empty v-else description="暂无任务" :image-size="60" />
      </div>
    </el-card>

    <!-- 新建 / 编辑计划 -->
    <el-dialog
      v-model="scheduleDialog"
      :title="isEdit ? '编辑定时计划' : '新建定时计划'"
      width="580px"
      :close-on-click-modal="false"
    >
      <el-form ref="scheduleFormRef" :model="scheduleForm" :rules="scheduleRules" label-position="top">
        <el-form-item label="计划名称" prop="name">
          <el-input v-model="scheduleForm.name" placeholder="例如：每晚检查所有 API Key" clearable maxlength="40" />
        </el-form-item>

        <el-form-item label="触发方式">
          <el-radio-group v-model="scheduleForm.trigger_type" :disabled="scheduleForm.advanced">
            <el-radio-button value="interval">固定间隔</el-radio-button>
            <el-radio-button value="cron">日历时间</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <template v-if="!scheduleForm.advanced">
          <el-form-item v-if="scheduleForm.trigger_type === 'interval'" label="间隔">
            <div class="inline-row">
              <span class="inline-text">每</span>
              <el-input-number v-model="scheduleForm.intervalValue" :min="1" :max="1440" controls-position="right" />
              <el-select v-model="scheduleForm.intervalUnit" class="unit-select">
                <el-option v-for="unit in INTERVAL_UNITS" :key="unit.value" :label="unit.label" :value="unit.value" />
              </el-select>
            </div>
          </el-form-item>

          <template v-else>
            <el-form-item label="周期">
              <el-radio-group v-model="scheduleForm.cronMode">
                <el-radio value="daily">每天</el-radio>
                <el-radio value="weekly">每周</el-radio>
              </el-radio-group>
            </el-form-item>

            <el-form-item :label="scheduleForm.cronMode === 'weekly' ? '星期与时间' : '执行时间'">
              <div class="inline-row">
                <el-select v-if="scheduleForm.cronMode === 'weekly'" v-model="scheduleForm.cronWeekday" class="unit-select">
                  <el-option v-for="day in WEEKDAYS" :key="day.value" :label="day.label" :value="day.value" />
                </el-select>
                <el-time-picker
                  v-model="scheduleForm.cronTime"
                  value-format="HH:mm"
                  format="HH:mm"
                  placeholder="选择时间"
                  class="time-picker"
                />
              </div>
            </el-form-item>
          </template>
        </template>

        <el-form-item label="验证范围">
          <el-select v-model="scheduleForm.target_scope" class="full-width">
            <el-option label="全部凭据" value="all" />
            <el-option label="指定分组" value="group" :disabled="!groupOptions.length" />
            <el-option label="指定类型" value="type" :disabled="!typeOptions.length" />
            <el-option label="指定凭据" value="credentials" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="scheduleForm.target_scope === 'group'" label="选择分组">
          <el-select v-model="scheduleForm.target_id" placeholder="请选择分组" class="full-width">
            <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>

        <el-form-item v-else-if="scheduleForm.target_scope === 'type'" label="选择类型">
          <el-select v-model="scheduleForm.target_id" placeholder="请选择凭据类型" class="full-width">
            <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>

        <el-form-item v-else-if="scheduleForm.target_scope === 'credentials'" label="选择凭据">
          <!-- 与「备份 → 仅选中的凭据」同一个组件（勾选语义完全相同），对话框里放矮一点 -->
          <CredentialTreePicker v-model="scheduleForm.credential_ids" :max-height="200" />
        </el-form-item>

        <el-form-item>
          <el-checkbox v-model="scheduleForm.advanced">高级：直接编辑触发器参数（APScheduler JSON）</el-checkbox>
        </el-form-item>

        <el-form-item v-if="scheduleForm.advanced" label="触发器参数">
          <el-input v-model="scheduleForm.triggerArgsText" type="textarea" :rows="4" spellcheck="false" />
        </el-form-item>

        <el-form-item label="参数预览">
          <pre class="preview">{{ triggerArgsPreview }}</pre>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="scheduleDialog = false">取消</el-button>
        <el-button type="primary" :loading="scheduleSubmitting" @click="submitSchedule">保存</el-button>
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

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: space-between;
}

/* 标题后的补充说明：比正文小一号，别抢标题的视觉重心 */
.header-hint {
  font-size: 12px;
}

.task-list {
  display: flex;
  flex-direction: column;
  /* 任务多、输出又长时整列会很长，把下面的「定时验证计划」顶到屏幕外。
     给个上限 + 内部滚动（拖动滚动条即可翻看本页其余条目）。 */
  max-height: 55vh;
  overflow-y: auto;
}

/* 分页控件：与「凭据管理」的 .pager 同一套（每页条数可预设可手输、页码 x / y） */
.pager {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

/* 等宽数字 + 固定最小宽度：「9 / 9」→「10 / 10」时页面不左右跳 */
.pager-page {
  min-width: 56px;
  text-align: center;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.pager-size {
  width: 78px;   /* 选项 label 只是数字（单位在框外的 .pager-unit），无需更宽 */
}

.pager-unit {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.task-row {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.task-row:last-child {
  border-bottom: none;
}

.task-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.task-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.task-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
}

.progress-bar {
  flex: 1;
  min-width: 0;
}

.progress-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.task-result {
  margin-top: 8px;
  min-width: 0;
}

.result-text {
  display: block;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: default;
}

.result-text.is-error {
  color: var(--el-color-danger);
}

.task-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}

.lock-hint {
  margin-bottom: 14px;
}

.plan-name {
  font-weight: 600;
}

.inline-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.inline-text {
  color: var(--el-text-color-regular);
}

.unit-select {
  width: 110px;
}

.time-picker {
  width: 160px;
}

.full-width {
  width: 100%;
}

.preview {
  margin: 0;
  padding: 10px 12px;
  width: 100%;
  background: var(--el-fill-color-dark);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
