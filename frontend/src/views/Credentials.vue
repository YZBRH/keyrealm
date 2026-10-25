<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight, Back, Check, CircleClose, CopyDocument, Delete, Edit, Expand, Fold, Operation, Plus, Refresh, Search, SetUp, Sort } from '@element-plus/icons-vue'

import CredentialForm from '@/components/CredentialForm.vue'
import ScopeDefaultsDialog from '@/components/ScopeDefaultsDialog.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useVaultStore } from '@/stores/vault'
import { STATUS_OPTIONS, formatDateTimeFull, normalizeOptions, visibleFields } from '@/utils/format'
import api, { EVENTS } from '@/api'
import { inlineIcon } from '@/utils/pluginIcon'

const route = useRoute()
const router = useRouter()
const vaultStore = useVaultStore()

const formVisible = ref(false)
const editing = ref(null)
// 正在验证中的凭据 id 列表：逐条独立，互不影响——只记单个 id 的话，点了 A 再点 B
// 会让 A 的按钮重新可点，重复触发同一凭据的验证。
const verifyingIds = ref([])
const batchStarting = ref(false)

const filters = vaultStore.filters

const statusOptions = STATUS_OPTIONS

const groupOptions = computed(() => vaultStore.groupOptions)
const hasPlugins = computed(() => vaultStore.plugins.length > 0)

// 添加凭据时的默认服务：跟随目录树当前选中项
// （选中具体服务 → 该服务；选中类别 → 类别下第一个**已启用**的服务）
const createDefaultType = computed(() => {
  if (filters.credential_type) return filters.credential_type
  if (filters.credential_types && filters.credential_types.length) {
    // 类别里可能混着「已禁用、但名下还有凭据」的服务（禁用且无凭据的不会进树），
    // 默认值要跳过它们：否则表单会默认落在一个类型下拉里根本选不到的值上。
    // 全被禁用时返回空串，交给表单自己取第一个可用类型。
    return filters.credential_types.find((type) => isPluginEnabled(type)) || ''
  }
  return ''
})

// ── 批量验证：手动勾选（含一键全选）后并发验证 ──
const batchPickVisible = ref(false)
const batchPickIds = ref([])

// 运行参数：打开对话框时从后端回填，点「验证」时随本次验证一起保存。
// 边界（min/max）由后端下发，避免前后端各写一份常量。
const batchConcurrency = ref(8)
const batchTimeout = ref(20)
const batchLimits = ref({ concurrency_range: [1, 32], timeout_range: [5, 300] })

// 批量验证的运行状态。两个来源要分开看：
//   · 手动批量（用户在「批量验证」对话框里点的）→ **整页只读**：新增、编辑、删除、行内动作
//     一律锁上，免得验证还没结束就把条目改掉/删掉，让结果与界面对不上。
//     **刻意不禁导航**——用户就是要能在后台跑的同时去「任务」页看进度。
//   · 定时任务 / MCP 触发 → 不锁页。那是用户没看着的时候自己跑起来的，把界面冻住只会
//     让人莫名点不动；只横幅提示 + 禁用本次涉及条目的「验证」按钮。
const batchRunning = computed(() => vaultStore.batch.running)
const batchLocked = computed(() => batchRunning.value && vaultStore.batch.trigger === 'manual')

// 本次批量验证覆盖的凭据 id（后端 batch_verify_started 事件下发）。
// 用字符串归一后比较：id 走一趟 JSON 之后到底是数字还是字符串不值得赌。
const batchVerifyIds = computed(
  () => new Set((vaultStore.batch.ids || []).map((id) => String(id))),
)

function isBatchVerifying(id) {
  return batchVerifyIds.value.has(String(id))
}

/** 该类型是否允许批量验证（manifest batch_verify=false 的类型只能手动单条验证；插件被禁用的也排除） */
function canBatchVerify(type) {
  const plugin = pluginFor(type)
  return isPluginEnabled(type) && (!plugin || plugin.batch_verify !== false)
}

const pickableCredentials = computed(() =>
  vaultStore.credentials.filter((c) => canBatchVerify(c.credential_type)),
)
const pickAllChecked = computed(
  () =>
    pickableCredentials.value.length > 0 &&
    batchPickIds.value.length === pickableCredentials.value.length,
)
const pickIndeterminate = computed(
  () =>
    batchPickIds.value.length > 0 &&
    batchPickIds.value.length < pickableCredentials.value.length,
)

function openBatchPick() {
  // 批量验证进行中不允许再开一轮（按钮也已置灰，这里防的是快捷键/程序化入口）
  if (batchRunning.value) return
  batchPickIds.value = pickableCredentials.value.map((c) => c.id)
  batchPickVisible.value = true
  // 回填上次保存的并发数 / 超时与取值范围（失败不阻塞对话框，沿用本地默认值）
  vaultStore
    .fetchBatchOptions()
    .then((opt) => {
      if (!opt || opt.ok === false) return
      batchConcurrency.value = Number(opt.concurrency) || batchConcurrency.value
      batchTimeout.value = Number(opt.timeout) || batchTimeout.value
      if (opt.concurrency_range) batchLimits.value.concurrency_range = opt.concurrency_range
      if (opt.timeout_range) batchLimits.value.timeout_range = opt.timeout_range
    })
    .catch(() => {})
}

function onPickAll(checked) {
  batchPickIds.value = checked ? pickableCredentials.value.map((c) => c.id) : []
}

async function startPickedBatch() {
  const ids = [...batchPickIds.value]
  if (!ids.length) return
  batchPickVisible.value = false
  batchStarting.value = true
  try {
    await vaultStore.batchVerify('all', null, null, ids,
      batchConcurrency.value, batchTimeout.value)
  } catch (err) {
    ElMessage.error(err.message || '批量验证启动失败')
  } finally {
    batchStarting.value = false
  }
}

// ── 批量删除：与「批量验证」同一套勾选对话框，但默认**全不选**。
// 破坏性操作不该替用户预勾选——省下的一次点击，代价可能是误删一整批。
const deletePickVisible = ref(false)
const deletePickIds = ref([])
const deleteStarting = ref(false)

// 勾选范围是 vaultStore.credentials（当前树 / 筛选过滤后的整批，与批量验证同源，天然跨分页）
const deleteAllChecked = computed(
  () =>
    vaultStore.credentials.length > 0 &&
    deletePickIds.value.length === vaultStore.credentials.length,
)
const deleteIndeterminate = computed(
  () =>
    deletePickIds.value.length > 0 &&
    deletePickIds.value.length < vaultStore.credentials.length,
)

function openBatchDelete() {
  // 手动批量验证进行中整页互斥（按钮也已置灰，这里防的是快捷键 / 程序化入口）
  if (batchRunning.value) return
  deletePickIds.value = []
  deletePickVisible.value = true
}

function onDeletePickAll(checked) {
  deletePickIds.value = checked ? vaultStore.credentials.map((c) => c.id) : []
}

async function startBatchDelete() {
  const ids = [...deletePickIds.value]
  if (!ids.length) return
  // 二次确认：对话框里已有一条 warning，这里再问一次（与单条删除同一句式的确认）。
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${ids.length} 条凭据吗？该操作不可撤销。`,
      '批量删除凭据',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  deleteStarting.value = true
  try {
    const res = await vaultStore.deleteCredentials(ids)
    // 后端返回的是**实际**删除条数（期间有条目被别处删掉时会小于所选数），以它为准不谎报
    const deleted = res && res.deleted != null ? res.deleted : ids.length
    ElMessage.success(`已删除 ${deleted} 条凭据`)
    deletePickVisible.value = false
    deletePickIds.value = []
  } catch (err) {
    ElMessage.error(err.message || '批量删除失败')
  } finally {
    deleteStarting.value = false
  }
}

// ── 去重：先选扫描范围（默认全选），检测后逐组选择保留项 ──
const dedupeVisible = ref(false)
const dedupeScopeIds = ref([])
const dedupeScanning = ref(false)
const dedupeScanned = ref(false)
const dedupeGroups = ref([])
const dedupeResolving = ref(false)
const dedupeKeep = ref({})          // 组下标 → 保留的凭据 id
const dedupeMergeNotes = ref({})    // 组下标 → 是否合并描述/备注

const dedupeAllChecked = computed(
  () =>
    vaultStore.credentials.length > 0 &&
    dedupeScopeIds.value.length === vaultStore.credentials.length,
)
const dedupeIndeterminate = computed(
  () =>
    dedupeScopeIds.value.length > 0 &&
    dedupeScopeIds.value.length < vaultStore.credentials.length,
)
const dedupeDuplicateCount = computed(
  () => dedupeGroups.value.reduce((n, g) => n + g.items.length, 0),
)

function openDedupe() {
  if (batchRunning.value) return
  // 默认全选当前列表（受目录树 / 筛选影响），与批量验证、批量删除同源
  dedupeScopeIds.value = vaultStore.credentials.map((c) => c.id)
  dedupeGroups.value = []
  dedupeScanned.value = false
  dedupeKeep.value = {}
  dedupeMergeNotes.value = {}
  dedupeVisible.value = true
}

function onDedupePickAll(checked) {
  dedupeScopeIds.value = checked ? vaultStore.credentials.map((c) => c.id) : []
}

async function scanDuplicates() {
  if (!dedupeScopeIds.value.length) return
  dedupeScanning.value = true
  try {
    const res = await api.findDuplicateCredentials([...dedupeScopeIds.value])
    if (res && res.ok === false) {
      ElMessage.error(res.message || '去重扫描失败')
      return
    }
    dedupeGroups.value = (res && res.groups) || []
    dedupeScanned.value = true
    const keep = {}
    const merge = {}
    dedupeGroups.value.forEach((group, index) => {
      keep[index] = group.items[0] ? group.items[0].id : null
      merge[index] = false
    })
    dedupeKeep.value = keep
    dedupeMergeNotes.value = merge
    if (!dedupeGroups.value.length) {
      ElMessage.success('未发现重复凭据')
    } else {
      ElMessage.warning(
        `发现 ${dedupeGroups.value.length} 组重复，共 ${dedupeDuplicateCount.value} 条凭据`,
      )
    }
  } catch (err) {
    ElMessage.error(err.message || '去重扫描失败')
  } finally {
    dedupeScanning.value = false
  }
}

async function applyDedupe() {
  if (!dedupeGroups.value.length) return
  dedupeResolving.value = true
  let kept = 0
  let deleted = 0
  try {
    for (let index = 0; index < dedupeGroups.value.length; index += 1) {
      const group = dedupeGroups.value[index]
      const keepId = dedupeKeep.value[index]
      if (!keepId) continue
      const removeIds = group.items.map((item) => item.id).filter((id) => id !== keepId)
      if (!removeIds.length) continue
      const res = await api.resolveDuplicateGroup(
        keepId, removeIds, Boolean(dedupeMergeNotes.value[index]),
      )
      if (res && res.ok === false) {
        throw new Error(res.message || '去重失败')
      }
      kept += 1
      deleted += (res && res.deleted != null) ? res.deleted : removeIds.length
    }
    await vaultStore.fetchCredentials()
    ElMessage.success(`去重完成：保留 ${kept} 组，删除 ${deleted} 条重复凭据`)
    dedupeVisible.value = false
  } catch (err) {
    ElMessage.error(err.message || '去重失败')
  } finally {
    dedupeResolving.value = false
  }
}

// ── 简易操作（插件可选能力：简单对话 / 表单 / 列表）──
/**
 * 对话框的初始状态只在这里定义一份。
 *
 * 模板会直接读 models / listRows 等字段，字段缺失会在渲染时抛 TypeError 让整个弹窗内容
 * 渲染失败、看起来像卡死，所以「初始化」和「打开对话框」必须共用同一份字段定义。
 */
function blankActionDialog() {
  return {
    visible: false,
    credentialId: null,
    credentialName: '', // 行内下钻/表单不再回头查凭据列表，标题里的凭据名随对话框一路带着
    actionId: '',
    kind: '',
    title: '',
    actions: [],       // 该凭据声明的全部操作（行内动作要按 id 找到目标操作）
    description: '',   // 操作的说明（菜单里与对话框顶部都会展示）
    row: null,         // 由行内动作打开时，记住那一行（save_to 的文件名模板要用）
    // ── chat ──
    model: '',
    // 该操作是否声明了 model 字段 → 决定「模型」那一行要不要**常驻**
    modelDeclared: false,
    models: [],
    modelsLoading: false,
    streamId: null,
    messages: [],
    input: '',
    modelError: '',
    // ── form ──
    fields: [],
    values: {},
    submitLabel: '运行',
    write: false,
    // 该操作声明了 progress: true → 走流式入口、渲染进度条（下载/打包这类长操作）
    progressEnabled: false,
    progress: null,
    saveTo: null,      // 声明了 save_to 的操作：执行前先弹「另存为」
    output: '',
    formRows: [],      // form 操作也可以返回 rows（例如「查用户详情」），有就渲染成表格
    // ── list ──
    listTitle: '',
    listCrumb: [],     // 下钻路径（面包屑），点「返回」逐级还原
    listColumns: [],
    listRows: [],
    listRowActions: [],
    listEmptyText: '',
    listLoading: false,
    listError: '',
    listTotal: null,
    listHasMore: false,
    listParams: {},    // 本次列表请求用的参数（「刷新」重跑要用）
    listStack: [],     // 下钻栈：返回时直接还原上一帧，不必重新请求
    // ── 公共 ──
    busy: false,
    error: '',
    // 写操作的自定义确认文案（插件用 action.confirm 声明）；空则用默认那句
    confirmText: '',
  }
}

const actionDialog = ref(blankActionDialog())

// 操作表单（form 的表单与 list 的参数行共用一个 ref：两者不同时挂载）
const actionFormRef = ref(null)

// 操作弹窗表单字段：按 show_if 过滤（与凭据表单同一套规则）
const actionFormFields = computed(
  () => visibleFields(actionDialog.value.fields, actionDialog.value.values),
)

/** 操作表单的必填校验：由插件声明的 fields[].required 生成，与凭据表单同一套规则。
 *  加 prop + 这份 rules 后，el-form-item 会自动渲染红色星号并拦截空提交。 */
const actionRules = computed(() => {
  const rules = {}
  for (const field of actionFormFields.value) {
    if (!field.required) continue
    const trigger = field.type === 'select' || field.type === 'checkbox' ? 'change' : 'blur'
    rules[field.key] = [{ required: true, message: `请填写${field.label}`, trigger }]
  }
  return rules
})

/** 进度百分比；总量未知（total<=0）时返回 0，由 el-progress 的 indeterminate 走动画 */
const progressPercent = computed(() => {
  const p = actionDialog.value.progress
  if (!p || !p.total) return 0
  return Math.min(100, Math.floor((p.current / p.total) * 100))
})

/** 模型列表拉取失败的提示：不阻塞对话，只提示用户回退为手动输入模型名 */
const MODELS_ERROR = '获取模型列表失败，请手动输入模型名'

// 「模型列表常驻」有两层意思：按钮一直在（随时可刷新），以及重开对话框不必再等网络。
const modelsCache = new Map()

/** 拉取该凭据可用模型列表；`force=true` 是用户点「刷新」——穿透服务端 24h 缓存 */
async function loadModels(force = false) {
  const dialog = actionDialog.value
  if (!dialog.credentialId || dialog.modelsLoading) return
  const cached = modelsCache.get(dialog.credentialId)
  if (!force && cached && !dialog.models.length) dialog.models = [...cached]
  dialog.modelsLoading = true
  dialog.modelError = ''
  try {
    const list = await api.listCredentialModels(dialog.credentialId, force)
    const models = Array.isArray(list) ? list : []
    if (models.length) {
      dialog.models = models
      modelsCache.set(dialog.credentialId, [...models])
      // 只在用户还没选过时自动取第一个；已经有选择就**绝不覆写**
      // （否则刷新一次就把人挑好的模型换掉了）
      if (!dialog.model) dialog.model = models[0]
    } else {
      dialog.modelError = MODELS_ERROR
    }
  } catch {
    // 失败时保留已有的列表与选择，只提示
    dialog.modelError = MODELS_ERROR
  } finally {
    dialog.modelsLoading = false
  }
}

// ── 操作权限探测（插件可选 capabilities）──
// 懒加载 + 会话内缓存；**不落 localStorage**（服务端权限会变，缓存旧结果会错误地藏掉能用的操作）。
const capsCache = new Map()

async function loadCapabilities(credentialId, { force = false } = {}) {
  const fallback = { available: null, note: '', error: true }
  if (!credentialId) return fallback
  if (!force && capsCache.has(credentialId)) return capsCache.get(credentialId)
  try {
    const caps = await api.credentialCapabilities(credentialId)
    const normalized = {
      // available === null 表示「未知 → 全部可用」（fail-open：探测抖一下不能把能用的操作藏掉）
      available: caps && Array.isArray(caps.available) ? caps.available.map(String) : null,
      note: (caps && caps.note) || '',
      error: !!(caps && caps.error),
    }
    capsCache.set(credentialId, normalized)
    return normalized
  } catch {
    capsCache.set(credentialId, fallback)
    return fallback
  }
}

/** 按 capabilities 过滤：声明了但不可用 → 隐藏；available 里没对应声明 → 静默丢弃 */
function filterActions(actions, caps) {
  if (!caps || caps.available === null) return actions
  const allowed = new Set(caps.available)
  return actions.filter((a) => allowed.has(String(a.id)))
}

/**
 * 找出「只该被行内下钻调用」的操作 id：
 * 既被别的操作在 `row_actions` 里引用，自身又没有任何可填 `fields`。
 * 这类操作在顶层菜单里点了没参数可填，只会回一句「缺少 xxx」，所以不进菜单；
 * 但它们仍要留在完整 actions 列表里，供行内按钮 `$row.*` 解析目标用。
 */
function rowOnlyActionIds(actions) {
  const targets = new Set()
  actions.forEach((a) => (a.row_actions || []).forEach((ra) => targets.add(String(ra.id))))
  const ids = new Set()
  actions.forEach((a) => {
    const hasFields = Array.isArray(a.fields) && a.fields.length > 0
    if (targets.has(String(a.id)) && !hasFields) ids.add(String(a.id))
  })
  return ids
}

const actionMenu = ref({ visible: false, x: 0, y: 0, row: null, items: [], all: [], note: '', pending: false })

function closeActionMenu() {
  actionMenu.value.visible = false
}

const actionMenuRef = ref(null)
const ACTION_MENU_MARGIN = 8

/**
 * 摆好操作菜单，别被视口裁掉：默认贴锚点左下方，右侧放不下就左移，下方放不下就翻到锚点上方。
 * 菜单高度要等渲染后才能量到，所以放在 nextTick 之后。
 */
async function placeActionMenu(anchorRect) {
  await nextTick()
  const el = actionMenuRef.value
  if (!el || !actionMenu.value.visible) return
  const m = ACTION_MENU_MARGIN
  const w = el.offsetWidth
  const h = el.offsetHeight
  const vw = window.innerWidth
  const vh = window.innerHeight
  let x = anchorRect ? anchorRect.left : actionMenu.value.x
  let y = anchorRect ? anchorRect.bottom + 4 : actionMenu.value.y
  if (x + w + m > vw) x = Math.max(m, vw - w - m)
  if (y + h + m > vh) {
    const above = anchorRect ? anchorRect.top - h - 4 : y - h - 4
    y = above >= m ? above : Math.max(m, vh - h - m)
  }
  actionMenu.value.x = x
  actionMenu.value.y = y
}

/** 点「操作」：0 个 → 提示；1 个 → 直接进；≥2 个 → 弹选择菜单 */
async function openActions(row, event) {
  closeActionMenu()
  // 锚点矩形必须在**任何 await 之前**同步取走：事件派发一结束，event.currentTarget 就被置为 null，
  // 放到下面的 await api.listCredentialActions(...) 之后再读只能拿到 null，
  // 菜单会退化成用 event.clientX/clientY 定位（贴光标而非贴按钮左下角）。
  const anchorRect = event && event.currentTarget ? event.currentTarget.getBoundingClientRect() : null
  let actions = []
  try {
    actions = await api.listCredentialActions(row.id)
  } catch (err) {
    ElMessage.error(err.message || '获取操作失败')
    return
  }
  actions = Array.isArray(actions) ? actions : []
  // 只被行内下钻用、自身又无可填字段的操作（github「仓库详情」、dnspod「解析记录」等）
  // 不进顶层菜单；完整的 actions 仍经 all 传给对话框，供行内按钮解析下钻目标。
  const rowOnly = rowOnlyActionIds(actions)
  const topLevel = actions.filter((a) => !rowOnly.has(String(a.id)))
  if (!topLevel.length) {
    ElMessage.info('该凭据暂无可用操作')
    return
  }

  // 单个操作直接进，保住原有的一键体验；只有实现 capabilities 的插件才会真的联网探测
  if (topLevel.length === 1) {
    const caps = await loadCapabilities(row.id)
    if (caps.available !== null && caps.available.length === 0) {
      ElMessageBox.alert(caps.note || '当前凭据没有任何可用操作权限', '无可用操作', {
        type: 'warning',
        confirmButtonText: '知道了',
      }).catch(() => {})
      return
    }
    openAction(row, topLevel[0], {}, { actions })
    return
  }

  const sorted = [...topLevel].sort((a, b) => (a.order ?? 999) - (b.order ?? 999))
  actionMenu.value = {
    visible: true,
    x: anchorRect ? anchorRect.left : (event?.clientX ?? 0),
    y: anchorRect ? anchorRect.bottom + 4 : (event?.clientY ?? 0),
    row,
    items: sorted,
    all: actions,
    note: '',
    pending: true,
  }
  placeActionMenu(anchorRect)
  // 菜单先按声明全列出，权限探测回来再收敛（避免「先显示后消失」的抖动）
  loadCapabilities(row.id).then((caps) => {
    if (!actionMenu.value.visible) return
    const available = filterActions(sorted, caps)
    actionMenu.value.items = available
    actionMenu.value.note = caps.note
    actionMenu.value.pending = false
    // 条目变了、高度也跟着变，重新贴一次边（否则收敛后被裁）
    placeActionMenu(anchorRect)
    if (!available.length) {
      closeActionMenu()
      ElMessageBox.alert(caps.note || '当前凭据没有任何可用操作权限', '无可用操作', {
        type: 'warning',
        confirmButtonText: '知道了',
      }).catch(() => {})
    }
  })
}

function pickFromMenu(action) {
  const row = actionMenu.value.row
  const all = actionMenu.value.all
  closeActionMenu()
  if (row) openAction(row, action, {}, { actions: all })
}

/** 统一入口：按 kind 分派到 chat / form / list */
function openAction(cred, action, seed = {}, extra = {}) {
  if ((action.kind || '') === 'list') {
    openList(cred, action, seed, extra.actions || [])
    return
  }
  const fields = action.fields || []
  const values = {}
  fields.forEach((f) => {
    values[f.key] = f.default !== undefined && f.default !== null ? f.default : ''
  })
  Object.assign(values, seed || {})
  // 基于初始状态展开，保证 fields 之外的字段（models / listRows / …）一个都不少
  actionDialog.value = {
    ...blankActionDialog(),
    visible: true,
    credentialId: cred.id,
    credentialName: cred.name || '',
    actionId: action.id,
    kind: action.kind || '',
    title: `${action.name || '操作'} · ${cred.name}`,
    description: action.description || '',
    actions: extra.actions || [action],
    row: extra.row || null,
    model: action.model || '',
    modelDeclared: 'model' in action,
    fields,
    values,
    submitLabel: action.submit || '运行',
    write: !!action.write,
    progressEnabled: !!action.progress,
    confirmText: action.confirm || '',
    saveTo: action.save_to || null,
  }
  // 模型列表要联网拉取，这里刻意不 await：先让弹窗渲染出来，加载过程由 modelsLoading 表达，
  // 否则网络慢时用户看到的是一个迟迟不出现的弹窗（表现为「卡住」）。
  if (actionDialog.value.kind === 'chat') loadModels()
}

// ── 列表类操作（kind: "list"）──

function listFrame(dialog) {
  return {
    actionId: dialog.actionId,
    title: dialog.listTitle,
    columns: dialog.listColumns,
    rows: dialog.listRows,
    rowActions: dialog.listRowActions,
    emptyText: dialog.listEmptyText,
    total: dialog.listTotal,
    hasMore: dialog.listHasMore,
    params: dialog.listParams,
  }
}

function applyFrame(dialog, frame) {
  dialog.actionId = frame.actionId
  dialog.listTitle = frame.title
  dialog.listColumns = frame.columns
  dialog.listRows = frame.rows
  dialog.listRowActions = frame.rowActions
  dialog.listEmptyText = frame.emptyText
  dialog.listTotal = frame.total
  dialog.listHasMore = frame.hasMore
  dialog.listParams = frame.params
  dialog.listError = ''
}

function openList(cred, action, params = {}, actionsOverride = []) {
  const dialog = actionDialog.value
  const sameCredential = dialog.credentialId === cred.id && dialog.visible
  const crumb = sameCredential && dialog.listTitle
    ? [...dialog.listCrumb, dialog.listTitle]
    : []
  // 行内动作要按 id 找到「目标操作」，所以必须一直带着完整操作表
  const known = (actionsOverride && actionsOverride.length)
    ? actionsOverride
    : (dialog.actions.length ? dialog.actions : [action])
  actionDialog.value = {
    ...(sameCredential ? dialog : blankActionDialog()),
    visible: true,
    credentialId: cred.id,
    credentialName: cred.name || '',
    actionId: action.id,
    kind: 'list',
    title: `${action.name || '列表'} · ${cred.name}`,
    description: action.description || '',
    actions: known,
    listTitle: action.name || '列表',
    listCrumb: crumb,
    listColumns: action.columns || [],
    listRows: [],
    listRowActions: action.row_actions || [],
    listEmptyText: action.empty_text || '暂无数据',
    listParams: params || {},
    listError: '',
    listLoading: true,
    error: '',
    busy: false,
    // 列表操作也能带 fields（例如「收件箱」要选拉取封数/文件夹）：渲染成表头上方一行表单，
    // 点「拉取/刷新」时把表单值并进 params。从某一行下钻时，行参数先填进去当默认值。
    fields: action.fields || [],
    values: buildInitialValues(action, params),
    submitLabel: action.submit || '刷新',
    confirmText: action.confirm || '',
  }
  // 注意：上面 actionDialog.value 已被换成**新对象**，局部变量 dialog 还指向旧对象，
  // 用 dialog.listParams 会取到上一次的参数（首次为空）→ 下钻/预览第一次必然缺参数、要手动刷新。
  // 必须用本次传入的 params。
  fetchList(action.id, params || {})
}

/** 表单初始值：field.default 打底，行参数（下钻/行内动作带来的）覆盖 */
function buildInitialValues(action, seed) {
  const values = {}
  ;(action.fields || []).forEach((field) => {
    values[field.key] = field.default !== undefined && field.default !== null ? field.default : ''
  })
  Object.assign(values, seed || {})
  return values
}

/** 结果没给 columns 时，按第一行的键兜底造列（插件偷懒也不至于空表） */
function deriveColumns(rows) {
  if (!Array.isArray(rows) || !rows.length || typeof rows[0] !== 'object') return []
  return Object.keys(rows[0]).map((key) => ({ key, label: key }))
}

async function fetchList(actionId, params) {
  const dialog = actionDialog.value
  dialog.listLoading = true
  dialog.listError = ''
  try {
    const res = await api.runCredentialAction(dialog.credentialId, actionId, params || {})
    dialog.listRows = Array.isArray(res.rows) ? res.rows : []
    dialog.listColumns = (Array.isArray(res.columns) && res.columns.length)
      ? res.columns
      : (dialog.listColumns.length ? dialog.listColumns : deriveColumns(dialog.listRows))
    if (res.row_actions) dialog.listRowActions = res.row_actions
    if (res.empty_text) dialog.listEmptyText = res.empty_text
    dialog.listTotal = res.total ?? null
    dialog.listHasMore = !!res.has_more
  } catch (err) {
    dialog.listError = err.message || '加载失败'
  } finally {
    dialog.listLoading = false
  }
}

/** 「拉取/刷新」：基础参数（下钻带进来的）+ 表单里当前的值 */
function listRequestParams() {
  const dialog = actionDialog.value
  return { ...dialog.listParams, ...dialog.values }
}

async function refreshList() {
  const dialog = actionDialog.value
  if (dialog.kind !== 'list' || dialog.listLoading) return
  // 列表参数里若有必填项（例如收件箱的文件夹），空着就拦下——与「拉取」按钮同一条校验
  if (actionFormRef.value) {
    try {
      await actionFormRef.value.validate()
    } catch {
      return
    }
  }
  fetchList(dialog.actionId, listRequestParams())
}

/** 「返回」：还原上一帧（不重新请求） */
function popList() {
  const dialog = actionDialog.value
  const stack = [...dialog.listStack]
  const frame = stack.pop()
  if (!frame) return
  dialog.listStack = stack
  dialog.listCrumb = dialog.listCrumb.slice(0, -1)
  applyFrame(dialog, frame)
}

// 行内动作的参数模板：只认 `$row.<列key>`，解析不到就禁用该按钮（绝不把 undefined 发出去）
const ROW_REF = /^\$row\.(.+)$/

function resolveRowParams(template, row) {
  const out = {}
  for (const [key, value] of Object.entries(template || {})) {
    if (typeof value === 'string') {
      const matched = value.match(ROW_REF)
      if (matched) {
        const actual = (row || {})[matched[1]]
        if (actual === undefined || actual === null || actual === '') return null
        out[key] = actual
        continue
      }
    }
    out[key] = value
  }
  return out
}

function rowActionDisabled(rowAction, row) {
  return resolveRowParams(rowAction.params, row) === null
}

function findAction(actionId) {
  return actionDialog.value.actions.find((a) => a.id === actionId) || null
}

function runRowAction(rowAction, row) {
  const params = resolveRowParams(rowAction.params, row)
  if (params === null) return
  const target = findAction(rowAction.id)
  if (!target) {
    ElMessage.warning(`未找到操作「${rowAction.id}」`)
    return
  }
  const kind = target.kind || ''
  if (kind === 'list') {
    // 下钻：把当前帧压栈，返回时直接还原
    actionDialog.value.listStack = [...actionDialog.value.listStack, listFrame(actionDialog.value)]
    openList({ id: actionDialog.value.credentialId, name: actionDialog.value.credentialName }, target, params)
    return
  }
  if (kind === 'form') {
    // 打开表单并用行参数预填；确认留到提交时（预填后就弹确认等于白问）
    openAction({ id: actionDialog.value.credentialId, name: actionDialog.value.credentialName }, target, params, {
      actions: actionDialog.value.actions,
      row,
    })
    return
  }
  // 其它（含 chat）：与普通操作一致
  openAction({ id: actionDialog.value.credentialId, name: actionDialog.value.credentialName }, target, params, {
    actions: actionDialog.value.actions,
    row,
  })
}

/** 写操作确认文案：只列**非敏感**字段的取值（密码、令牌一律不进确认框） */
function actionSummary() {
  const dialog = actionDialog.value
  const parts = dialog.fields
    .filter((field) => field.type !== 'password' && !field.sensitive)
    .map((field) => {
      const value = dialog.values[field.key]
      return value === undefined || value === null || value === '' ? '' : `${field.label}=${value}`
    })
    .filter(Boolean)
  return parts.join('，')
}

/** 写操作的二次确认；返回 true 表示用户确认。 */
async function confirmWrite() {
  const dialog = actionDialog.value
  const summary = actionSummary()
  const actionName = String(dialog.title || '').split(' · ')[0]
  try {
    await ElMessageBox.confirm(
      // 插件可用 action.confirm 覆盖默认文案（涉及轮换令牌这类不可逆副作用时更需要说清楚）
      dialog.confirmText
        || `此操作会改动远端数据${summary ? `（${summary}）` : ''}。确认执行「${actionName}」？`,
      '确认写操作',
      {
        type: 'warning',
        confirmButtonText: '确认执行',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
    return true
  } catch {
    return false
  }
}

/** save_to 的文件名模板：支持 `$row.<列key>`（与 params 同一套语法） */
function resolveSaveName() {
  const dialog = actionDialog.value
  const raw = String((dialog.saveTo && dialog.saveTo.default_name) || '')
  if (!raw) return ''
  // 模板里有变量却拿不到那一行（比如用户从菜单直接点「打包下载」而不是从某一行点的）
  // → 宁可留空让用户自己起名，也不要甩一个字面的 "$row.name.zip" 到保存框里
  if (/\$row\./.test(raw) && !dialog.row) return ''
  const filled = raw.replace(/\$row\.([A-Za-z0-9_]+)/g, (whole, key) => {
    const value = dialog.row ? dialog.row[key] : undefined
    return value === undefined || value === null || value === '' ? whole : String(value)
  })
  return /\$row\./.test(filled) ? '' : filled
}

/** 弹「另存为」并返回路径；空串 = 用户取消 */
async function pickSavePath() {
  try {
    const res = await api.pickSavePath(resolveSaveName())
    return (res && res.path) || ''
  } catch (err) {
    ElMessage.error(err.message || '选择保存路径失败')
    return ''
  }
}

async function runActionForm() {
  const dialog = actionDialog.value
  if (dialog.busy) return
  // 必填未过就停在原地：红星与错误提示是给用户的反馈，不该悄悄跑掉
  if (actionFormRef.value) {
    try {
      await actionFormRef.value.validate()
    } catch {
      return
    }
  }
  // 顺序：先确认（写操作）→ 再选保存路径 → 最后执行。
  // 先让人起好文件名再问「确定吗」是反的，而且取消确认不该弹出系统对话框。
  if (dialog.write && !(await confirmWrite())) return

  const params = { ...dialog.values }
  if (dialog.saveTo) {
    const path = await pickSavePath()
    if (!path) return
    params[dialog.saveTo.key || 'path'] = path
  }

  dialog.busy = true
  dialog.error = ''
  dialog.output = ''
  dialog.formRows = []
  dialog.progress = null

  // 声明了 progress 的操作走**流式入口**：后端在下载/打包过程中推 vault:action_stream 进度事件，
  // 由 handleActionStream 渲染进度条，并把终态写回 output / error（并负责结束 busy）。
  if (dialog.progressEnabled) {
    try {
      // title 给「任务」页用（操作名 · 凭据名），后端据此登记一条后台任务
      const res = await api.streamCredentialAction(
        dialog.credentialId, dialog.actionId, params, dialog.title,
      )
      if (res && res.ok === false) {
        dialog.busy = false
        dialog.error = res.error || '执行失败'
        return
      }
      dialog.streamId = res.stream_id
    } catch (err) {
      dialog.busy = false
      dialog.error = err.message || '执行失败'
    }
    return
  }

  try {
    const res = await api.runCredentialAction(dialog.credentialId, dialog.actionId, params)
    if (res && res.ok) {
      dialog.output = res.output || (Array.isArray(res.rows) && res.rows.length ? '' : '（无输出）')
      // 表单操作也可能返回 rows（契约里允许）——有就渲染成表格，而不是只留一行字
      dialog.formRows = Array.isArray(res.rows) ? res.rows : []
      if (Array.isArray(res.columns) && res.columns.length) {
        dialog.listColumns = res.columns
      } else if (dialog.formRows.length && !dialog.listColumns.length) {
        dialog.listColumns = deriveColumns(dialog.formRows)
      }
    } else {
      dialog.error = (res && res.error) || '执行失败'
    }
    // 操作把新值写回了凭据（如用 refresh_token 换了一组新令牌）：刷新列表并立刻复验一次，
    // 让状态与附加信息跟着更新。写回值本身不会回传（那是新明文令牌），这里只有字段名。
    if (Array.isArray(res?.data_updated) && res.data_updated.length) {
      const id = dialog.credentialId
      ElMessage.success(`已更新凭据字段：${res.data_updated.join('、')}`)
      await vaultStore.fetchCredentials()
      const row = (vaultStore.credentials || []).find((c) => c.id === id)
      if (row) verifyRow(row)
    }
  } catch (err) {
    dialog.error = err.message || '执行失败'
  } finally {
    dialog.busy = false
  }
}

// ── 流式对话：后端逐段推送 vault:action_stream，前端按 stream_id 追加 ──

/** 把本轮那条（通常还是空的）助手占位气泡改成红色报错——报错留在对话流里，
 *  与普通对话同形，而不是在聊天框下方另起一条提示。 */
function failAssistant(dialog, message) {
  const last = dialog.messages[dialog.messages.length - 1]
  if (last && last.role === 'assistant') {
    // 已经流出了部分内容时保留下来，报错另起一行，避免把已生成的内容一并吞掉
    last.content = last.content ? `${last.content}\n\n${message}` : message
    last.error = true
  } else {
    dialog.messages.push({ role: 'assistant', content: message, error: true })
  }
  dialog.busy = false
}

function handleActionStream(detail) {
  const dialog = actionDialog.value
  if (!detail || !dialog.streamId || detail.stream_id !== dialog.streamId) return

  // 进度事件（下载/打包这类长操作）：只更新进度条，不改终态
  if (detail.progress) {
    dialog.progress = {
      current: Number(detail.progress.current) || 0,
      total: Number(detail.progress.total) || 0,
      text: detail.progress.text || '',
    }
    return
  }

  if (detail.done) {
    dialog.busy = false
    dialog.streamId = null
    dialog.progress = null
    // 表单/列表类走流式入口的（如带进度的下载）：终态写进 output / error 即可
    if (dialog.kind !== 'chat') {
      if (detail.error) dialog.error = detail.error
      else dialog.output = detail.output || '（无输出）'
      return
    }
    if (detail.error) {
      failAssistant(dialog, detail.error)
      return
    }
    const last = dialog.messages[dialog.messages.length - 1]
    // 终态用后端净化后的完整 output 覆盖已显示的流式内容：
    // 逐块 delta 净化无法处理被切断的密钥，覆盖可兜底。
    if (last && last.role === 'assistant' && detail.output) last.content = detail.output
    return
  }
  if (detail.delta) {
    const last = dialog.messages[dialog.messages.length - 1]
    if (last && last.role === 'assistant') last.content += detail.delta
  }
}

function onActionStream(event) {
  handleActionStream(event && event.detail)
}

async function sendChat() {
  const dialog = actionDialog.value
  const text = (dialog.input || '').trim()
  if (!text || dialog.busy) return
  dialog.messages.push({ role: 'user', content: text })
  // 报错气泡只是给用户看的，别当成历史回复喂给模型（空内容同理）
  const payloadMessages = dialog.messages
    .filter((m) => !m.error && m.content)
    .map((m) => ({ role: m.role, content: m.content }))
  dialog.messages.push({ role: 'assistant', content: '' })
  dialog.input = ''
  dialog.busy = true
  try {
    const res = await api.streamCredentialAction(dialog.credentialId, dialog.actionId, {
      model: dialog.model,
      messages: payloadMessages,
    })
    if (res && res.ok === false) {
      failAssistant(dialog, res.error || '执行失败')
      return
    }
    dialog.streamId = res.stream_id
  } catch (err) {
    failAssistant(dialog, err.message || '执行失败')
  }
}

function pluginFor(type) {
  return vaultStore.pluginByType[type] || null
}

/** 该凭据类型所属插件是否启用；插件记录缺失（孤儿凭据）时视为启用，不置灰 */
function isPluginEnabled(type) {
  const plugin = vaultStore.pluginByType[type]
  return plugin ? !!plugin.enabled : true
}

/** 「操作」按钮不可用的原因；空串 = 可用（tooltip 随之关闭）。 */
function actionHint(row) {
  if (!isPluginEnabled(row.credential_type)) return '插件已禁用'
  if (!pluginFor(row.credential_type)?.has_actions) return '暂无可用功能'
  return ''
}

/** 「操作」按钮是否可点。批量进行时整页互斥（batchLocked），归入同一个闸门。 */
function canOpenActions(row) {
  return !batchLocked.value && !actionHint(row)
}

// ── 凭据的**显示**状态 ────────────────────────────────────
// 插件被禁用时，这条凭据的显示状态改成「插件禁用」，盖过它库里的 status。
//
// **派生、绝不入库**：插件启用/禁用只是 plugins/.state.json 里的一个开关（可随时来回切），
// 若把 plugin_disabled 写进 credentials.status，重新启用插件后这些凭据就永远停在「插件禁用」
// ——成了洗不掉的脏数据。所以这里按插件启用状态实时算：重新启用，状态自动回到原值。
function effectiveStatus(row) {
  return isPluginEnabled(row.credential_type) ? row.status : 'plugin_disabled'
}

/** 显示状态对应的 tooltip：插件被禁用时不能回显库里那条**过期**的验证结论，改说明缘由 */
function effectiveStatusDetail(row) {
  if (isPluginEnabled(row.credential_type)) return row.status_detail
  return {
    message: `插件「${typeLabel(row.credential_type)}」已被禁用，该凭据暂时无法验证；可在「插件管理」中重新启用`,
  }
}

function typeLabel(type) {
  const plugin = pluginFor(type)
  return plugin ? plugin.name : type
}

function typeIcon(type) {
  const plugin = pluginFor(type)
  return inlineIcon(plugin && plugin.icon)
}

/** 插件自定义图标（manifest icon 指向图片文件时后端返回 data URL） */
function iconImage(type) {
  const plugin = pluginFor(type)
  return plugin && plugin.icon_image ? plugin.icon_image : ''
}

/** 深色主题下这个 logo 要不要垫浅色底，由后端逐个判定（backend/icon_ink.py） */
function iconLift(type) {
  const plugin = pluginFor(type)
  return !!(plugin && plugin.icon_lift)
}

/** 凭据单元格副标题：掩码后的敏感值（如 sk-123…cdef） */
function maskedSubtitle(row) {
  const masked = row.data_masked
  if (!masked || typeof masked !== 'object') return ''
  const values = Object.values(masked).filter(Boolean)
  return values[0] || ''
}

// account_info 常见键的中文标签（插件返回的原始英文键 → 可读标签）
const ACCOUNT_INFO_LABELS = {
  domain_count: '域名数',
  model_count: '模型数',
  workspace_count: '工作区数',
  account_id: '账号 ID',
  user_id: '用户 ID',
  accountId: '账号 ID',
  userId: '用户 ID',
  arn: 'ARN',
  email: '邮箱',
  login: '账号',
  username: '账号',
  name: '名称',
  first_name: '名称',
  scopes: '权限',
  plan: '套餐',
  chat_probe: '实测',
  accountType: '账户类型',
  account_type: '账户类型',
  spend: '已用',
  budget: '预算',
  key_name: 'Key 名称',
  budget_duration: '预算周期',
  budget_reset_at: '预算重置',
  canTrade: '可交易',
  canWithdraw: '可提现',
  canDeposit: '可充值',
  totalEq: '总权益',
  isoEq: '持仓权益',
  uTime: '更新时间',
  server: '服务器',
  port: '端口',
  smtp_account: '邮箱账号',
  smtp_server: 'SMTP 服务器',
  channel_id: '频道 ID',
  guild_id: '服务器 ID',
  id: 'ID',
  label: '标签',
  usage: '用量',
  limit: '额度',
  limit_remaining: '剩余额度',
  org_id: '组织 ID',
  ram_user: 'RAM 用户',
  token_id: 'Token ID',
  token_status: 'Token 状态',
  app_id: 'App ID',
  app_key: 'App Key',
  tenant: '租户',
  account: '账号',
  mode: '应用类型',
  // 服务账号类（firebase / gcp）：这几个键插件会原样回传，没标签就会显示成原始英文键
  client_email: '服务账号',
  project_id: '项目 ID',
  private_key_id: '密钥 ID',
  firebase_state: '项目状态',
  hostingSite: 'Hosting 站点',
  token_expires_in: '令牌有效期（秒）',
  project_number: '项目号',
}

// 除余额/使用率/组织外，其余字段的展示顺序（只定顺序，不再限制条数）
const ACCOUNT_INFO_PRIORITY = [
  'domain_count', 'workspace_count', 'model_count',
  'login', 'username', 'name', 'email',
  'account_id', 'user_id', 'totalEq', 'channel_id', 'server',
]

/** 把 account_info 的单个值格式化为可读文本（毫秒时间戳转日期） */
function formatAccountValue(key, value) {
  if ((key === 'uTime' || key === 'updated_at') && /^\d{12,}$/.test(String(value))) {
    return formatDateTimeFull(new Date(Number(value)).toISOString())
  }
  return String(value)
}

/** 附加信息列：把 account_info 摊成「每行一个字段」的文本数组。
 *  余额/使用率/组织有专门格式化且固定在前；其余键按优先级排序后**每个各占一行**
 *  （优先级只决定顺序，不再截断条数），null / 空串 / 对象或数组值仍然跳过。 */
function extraLines(row) {
  const info = row.status_detail && row.status_detail.account_info
  if (!info || typeof info !== 'object') return []
  const lines = []
  // 插件预格式化的一行（如 LiteLLM 的「已消耗 x / ∞」），优先展示且不再重复拆字段
  if (info.usage_label) lines.push(String(info.usage_label))
  if (info.balance !== undefined && info.balance !== null && info.balance !== '') {
    const num = Number(info.balance)
    const shown = Number.isFinite(num) ? num.toFixed(2) : String(info.balance)
    lines.push(`余额 ${shown}${info.currency ? ` ${info.currency}` : ''}`)
  }
  if (info.usage_rate !== undefined && info.usage_rate !== null && info.usage_rate !== '') {
    const pct = Math.round(Number(info.usage_rate) * 100)
    if (!Number.isNaN(pct)) lines.push(`使用率 ${pct}%`)
  }
  if (info.organization) lines.push(`组织 ${info.organization}`)
  const skip = new Set(['balance', 'currency', 'organization', 'usage_rate', 'usage_label'])
  const candidates = [...ACCOUNT_INFO_PRIORITY, ...Object.keys(info)]
  const used = new Set()
  for (const key of candidates) {
    if (skip.has(key) || used.has(key)) continue
    used.add(key)
    const value = info[key]
    if (value === null || value === undefined || value === '' || typeof value === 'object') continue
    lines.push(`${ACCOUNT_INFO_LABELS[key] || key} ${formatAccountValue(key, value)}`)
  }
  return lines
}

function notesOf(row) {
  return (row.data_plain && row.data_plain.notes) || ''
}

// ── 左侧插件目录树 ────────────────────────────────────────
// 与 plugins/ 文件系统层次一致：类别目录 → 服务子插件。
// 根目录平铺插件（无类别）直接挂在「全部凭据」下。

const treeRef = ref(null)
const selectedNode = ref('all')

// 树的展开/收起状态由本组件自行维护（持久化 localStorage），
// 避免凭据保存后 treeData 重建导致已收起的节点被重新展开。
const TREE_EXPANDED_KEY = 'vault.credentials.treeExpanded'
const TREE_COLLAPSED_KEY = 'vault.credentials.treeCollapsed'

const expandedKeys = ref(JSON.parse(localStorage.getItem(TREE_EXPANDED_KEY) || '[]'))
const treeCollapsed = ref(localStorage.getItem(TREE_COLLAPSED_KEY) === '1')

function persistExpanded() {
  localStorage.setItem(TREE_EXPANDED_KEY, JSON.stringify(expandedKeys.value))
}

function collectExpandable(nodes, acc = []) {
  nodes.forEach((node) => {
    if (node.children && node.children.length) {
      acc.push(node.id)
      collectExpandable(node.children, acc)
    }
  })
  return acc
}

function handleNodeExpand(node) {
  if (!expandedKeys.value.includes(node.id)) {
    // 必须**赋新数组**、不能原地 push：el-tree 判定 `default-expanded-keys` 变化用的是数组
    // **身份**（`hasChanged`），原地 push 不触发 watcher，`store.defaultExpandedKeys` 就会停在
    // 旧值——而下一次 treeData 重建时，节点是否自动展开正是查这份旧值，于是刚展开的节点会被
    // 自动收起（表现为「展开后随便动一下就自己收回去」）。赋新数组让它同步。
    expandedKeys.value = [...expandedKeys.value, node.id]
    persistExpanded()
  }
}

function handleNodeCollapse(node) {
  expandedKeys.value = expandedKeys.value.filter((key) => key !== node.id)
  persistExpanded()
}

function toggleTreeCollapsed() {
  treeCollapsed.value = !treeCollapsed.value
  localStorage.setItem(TREE_COLLAPSED_KEY, treeCollapsed.value ? '1' : '0')
}

// ── 目录树排序：拖拽调整「类别 / 插件」两层顺序 ────────────
// 顺序存后端 settings（键 tree_order），见 stores/vault.js 的 pluginTree / saveTreeOrder。
// 这里只负责把「拖到谁的前面 / 后面」翻译成一份新的顺序清单。
//
// 两条边界：
// - **只做同级排序**，不支持把插件拖进别的类别：那是改磁盘上的插件目录，不在本功能范围。
// - 类别与插件不能混排：未归类的插件（无类别目录）在树里与类别节点同处一层。

const isCustomTreeOrder = computed(
  () => vaultStore.treeOrder.categories.length > 0 || vaultStore.treeOrder.plugins.length > 0,
)

const treeDragHint = '按住拖拽可调整顺序'

/** 当前渲染顺序下的全部类别键（`category` 为空串表示「未归类」桶） */
function categoryKeys() {
  return vaultStore.pluginTree.map((cat) => cat.category)
}

/** 当前渲染顺序下的全部插件键，按类别顺序拍平。
 *  新顺序由「把某一项移到另一项前后」推导，故必须先有一份现成的基准顺序。 */
function flatPluginKeys() {
  const keys = []
  vaultStore.pluginTree.forEach((cat) => {
    cat.children.forEach((child) => keys.push(child.credential_type))
  })
  return keys
}

/** 落点校验（el-tree 的 allow-drop）。type 为 prev / inner / next。 */
function allowTreeDrop(draggingNode, dropNode, type) {
  if (type === 'inner') return false
  const drag = draggingNode?.data
  const drop = dropNode?.data
  if (!drag || !drop || drag.id === 'all' || drop.id === 'all') return false
  const dragIsCategory = !drag.credential_type
  if (dragIsCategory !== !drop.credential_type) return false
  if (dragIsCategory) return true
  // 插件只在同一父节点（同一类别，或同一个「未归类」桶）内排序
  return draggingNode.parent?.data?.id === dropNode.parent?.data?.id
}

/** 把 dragKey 移到 dropKey 的前（after=false）或后（after=true）。
 *  dragKey 与 dropKey 相同、或任一方不在表里时返回 null（表示无需改动）。 */
function moveKey(list, dragKey, dropKey, after) {
  const from = list.indexOf(dragKey)
  if (from < 0) return null
  const next = list.slice()
  next.splice(from, 1)
  const to = next.indexOf(dropKey)
  if (to < 0) return null
  next.splice(after ? to + 1 : to, 0, dragKey)
  return next
}

async function onTreeDrop(draggingNode, dropNode, dropType) {
  const drag = draggingNode?.data
  const drop = dropNode?.data
  if (!drag || !drop) return
  const after = dropType === 'next'
  const isCategory = !drag.credential_type
  const next = isCategory
    ? moveKey(categoryKeys(), drag.category, drop.category, after)
    : moveKey(flatPluginKeys(), drag.credential_type, drop.credential_type, after)
  if (!next) return
  try {
    if (isCategory) await vaultStore.saveTreeOrder(next, vaultStore.treeOrder.plugins)
    else await vaultStore.saveTreeOrder(vaultStore.treeOrder.categories, next)
  } catch (err) {
    ElMessage.error(err?.message || '保存目录顺序失败，已恢复原顺序')
  }
}

async function resetTreeOrder() {
  try {
    await ElMessageBox.confirm(
      '将把「凭据目录」恢复为默认顺序，当前的拖拽排列会被清除。是否继续？',
      '恢复默认顺序',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await vaultStore.resetTreeOrder()
    ElMessage.success('已恢复默认顺序')
  } catch (err) {
    ElMessage.error(err?.message || '恢复默认顺序失败')
  }
}

// ── 目录树右键菜单：禁用/启用插件（凭据保留，只是依赖插件的能力不可用）──
const ctxMenu = ref({
  visible: false, x: 0, y: 0, name: '',
  scope: 'plugin', scopeKey: '', credentialType: '', canToggle: false, pluginEnabled: false,
})
const ctxMenuRef = ref(null)

// 右键的**原始**落点。菜单尺寸要等它渲染出来才知道，所以先记点、下一帧再夹进视口。
let ctxPoint = { x: 0, y: 0 }

/** 把右键菜单夹进视口：右键落在窗口右/下边缘（最常见的是列表最后一项）时，菜单会有一半
 *  跑到窗口外、点不到也看不见。这里在菜单渲染后按自身尺寸回拉，**不做翻转**——本菜单只有
 *  一两项、不会挡住关键内容，回拉比「贴底就往上弹」更好预测，也不会在边缘反复跳。 */
function clampCtxMenu() {
  const el = ctxMenuRef.value
  if (!el) return
  const margin = 6
  const maxX = window.innerWidth - el.offsetWidth - margin
  const maxY = window.innerHeight - el.offsetHeight - margin
  ctxMenu.value = {
    ...ctxMenu.value,
    x: Math.max(margin, Math.min(ctxPoint.x, maxX)),
    y: Math.max(margin, Math.min(ctxPoint.y, maxY)),
  }
}

// 类别节点与服务节点都可右键：类别给「类别默认设置」，服务节点另有「禁用/启用插件」
function onTreeContextMenu(event, data) {
  if (!data) return
  const isCategory = !!data.category && !data.credential_type
  const isService = !!data.credential_type
  if (!isCategory && !isService) return
  event.preventDefault()
  ctxPoint = { x: event.clientX, y: event.clientY }
  ctxMenu.value = {
    visible: true,
    x: ctxPoint.x,
    y: ctxPoint.y,
    name: data.label,
    scope: isService ? 'plugin' : 'category',
    scopeKey: isService ? data.credential_type : (data.category || data.rel_path || ''),
    credentialType: isService ? data.credential_type : '',
    canToggle: isService,
    // 给菜单项切换「禁用插件 / 启用插件」文案用；类别节点不涉及插件，给 false 即可
    pluginEnabled: isService ? isPluginEnabled(data.credential_type) : false,
  }
  // 渲染后再量尺寸回拉（见 clampCtxMenu）
  nextTick(clampCtxMenu)
}

function closeCtxMenu() {
  ctxMenu.value.visible = false
  closeActionMenu()
}

// ── 类别 / 插件默认设置 ─────────────────────────────────
const scopeDialog = ref({ visible: false, scope: 'plugin', scopeKey: '', credentialType: '', title: '' })

function openScopeDefaults() {
  const { scope, scopeKey, credentialType, name } = ctxMenu.value
  closeCtxMenu()
  if (!scopeKey) return
  scopeDialog.value = {
    visible: true,
    scope,
    scopeKey,
    credentialType,
    title: `${scope === 'category' ? '类别' : '插件'}默认设置：${name}`,
  }
}

async function togglePluginFromTree() {
  // 注意：ctxMenu 里存的是 credentialType（不是 type），旧实现解构 type 拿到的是 undefined
  const { credentialType, name, pluginEnabled } = ctxMenu.value
  closeCtxMenu()
  const next = !pluginEnabled
  try {
    await vaultStore.setPluginEnabled(credentialType, next)
    ElMessage.success(`已${next ? '启用' : '禁用'}插件「${name}」`)
    await load()
  } catch (err) {
    ElMessage.error(err.message || (next ? '启用插件失败' : '禁用插件失败'))
  }
}

// ── 树宽度：拖拽右侧边框调整并持久化 ──
const TREE_WIDTH_KEY = 'vault.credentials.treeWidth'
const TREE_WIDTH_MIN = 160
const TREE_WIDTH_MAX = 560

function clampTreeWidth(value) {
  return Math.min(TREE_WIDTH_MAX, Math.max(TREE_WIDTH_MIN, Math.round(value)))
}

const treeWidth = ref(clampTreeWidth(Number(localStorage.getItem(TREE_WIDTH_KEY)) || 230))
const treeResizing = ref(false)

function startTreeResize(event) {
  event.preventDefault()
  treeResizing.value = true
  const startX = event.clientX
  const startWidth = treeWidth.value
  const onMove = (e) => {
    treeWidth.value = clampTreeWidth(startWidth + (e.clientX - startX))
  }
  const onUp = () => {
    treeResizing.value = false
    localStorage.setItem(TREE_WIDTH_KEY, String(treeWidth.value))
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
  }
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ── 树搜索：按名称或相对路径过滤，并展开命中分支 ──
const treeSearch = ref('')

function treeNodeMatches(query, data) {
  const q = query.toLowerCase()
  return (data.label || '').toLowerCase().includes(q) || (data.rel_path || '').toLowerCase().includes(q)
}

function filterTreeNode(value, data) {
  const q = (value || '').trim().toLowerCase()
  if (!q) return true
  if (treeNodeMatches(q, data)) return true
  return (data.children || []).some((child) => filterTreeNode(value, child))
}

function applyTreeSearch() {
  const tree = treeRef.value
  if (!tree) return
  tree.filter(treeSearch.value)
  const q = treeSearch.value.trim().toLowerCase()
  if (!q) return
  const expandMatches = (nodes) => {
    nodes.forEach((node) => {
      const matched =
        treeNodeMatches(q, node) || (node.children || []).some((child) => treeNodeMatches(q, child))
      if (matched) tree.getNode(node.id)?.expand()
      if (node.children) expandMatches(node.children)
    })
  }
  expandMatches(treeData.value)
}

watch(treeSearch, () => {
  nextTick(applyTreeSearch)
})

const treeData = computed(() => {
  const children = []
  vaultStore.pluginTree.forEach((cat) => {
    if (cat.category === '') children.push(...cat.children)
    else children.push(cat)
  })
  return [{ id: 'all', label: '全部凭据', icon: '🗂️', children }]
})

const treeProps = { label: 'label', children: 'children' }

/** 选中节点的相对路径（如 llm/deepseek），用于在目录顶部展示提示 */
const selectedRelPath = computed(() => {
  const map = {}
  const walk = (nodes) =>
    nodes.forEach((node) => {
      map[node.id] = node.rel_path || ''
      if (node.children) walk(node.children)
    })
  walk(treeData.value)
  return map[selectedNode.value] || ''
})

function handleNodeClick(node) {
  selectedNode.value = node.id
  if (node.id === 'all') {
    filters.credential_type = null
    filters.credential_types = null
  } else if (node.types) {
    // 类别节点：展示该层级及其子层级的全部条目
    filters.credential_type = null
    filters.credential_types = node.types
  } else {
    filters.credential_type = node.credential_type
    filters.credential_types = null
  }
  applyFilters()
}

function resetTree() {
  selectedNode.value = 'all'
  filters.credential_type = null
  filters.credential_types = null
  if (treeRef.value) treeRef.value.setCurrentKey('all')
}

// ── 列配置：顺序（拖拽）与可见性（列设置），均持久化 localStorage ──

const COLUMN_DEFS = [
  { key: 'credential', label: '凭据', minWidth: 230, locked: true },
  { key: 'type', label: '类型', minWidth: 140 },
  { key: 'notes', label: '备注', minWidth: 130 },
  { key: 'extra', label: '附加信息', minWidth: 170 },
  { key: 'group', label: '分组', width: 110 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'created_at', label: '添加时间', width: 170 },
  { key: 'last_verified', label: '最后验证', width: 170 },
  { key: 'last_valid', label: '最后有效', width: 170 },
  { key: 'actions', label: '操作', width: 260, locked: true },
]

const COLUMN_ORDER_KEY = 'vault.credentials.columnOrder'
const COLUMN_VISIBLE_KEY = 'vault.credentials.visibleColumns'
const ALL_COLUMN_KEYS = COLUMN_DEFS.map((def) => def.key)

function loadColumnOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_ORDER_KEY) || '[]')
    if (Array.isArray(saved)) {
      const known = saved.filter((key) => ALL_COLUMN_KEYS.includes(key))
      const missing = ALL_COLUMN_KEYS.filter((key) => !known.includes(key))
      if (known.length) return [...known, ...missing]
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return [...ALL_COLUMN_KEYS]
}

// 首次使用时默认隐藏的列（用户可在「列设置」里打开）
const DEFAULT_HIDDEN_COLUMNS = ['group', 'created_at', 'last_verified']

function loadVisibleColumns() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_VISIBLE_KEY) || 'null')
    if (Array.isArray(saved)) {
      const known = saved.filter((key) => ALL_COLUMN_KEYS.includes(key))
      if (known.length) return known
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return ALL_COLUMN_KEYS.filter((key) => !DEFAULT_HIDDEN_COLUMNS.includes(key))
}

const columnOrder = ref(loadColumnOrder())
const visibleKeys = ref(loadVisibleColumns())

// 锁定列（凭据/操作）始终可见
const effectiveVisible = computed(() => {
  const locked = COLUMN_DEFS.filter((def) => def.locked).map((def) => def.key)
  return [...new Set([...visibleKeys.value, ...locked])]
})

const orderedColumns = computed(() =>
  columnOrder.value
    .filter((key) => effectiveVisible.value.includes(key))
    .map((key) => COLUMN_DEFS.find((def) => def.key === key))
    .filter(Boolean),
)

function handleVisibleChange(values) {
  visibleKeys.value = values
  localStorage.setItem(COLUMN_VISIBLE_KEY, JSON.stringify(values))
  nextTick(setupHeaderDrag)
}

// ── 表头拖拽排序：以列 key 标识（label-class-name），避免索引错位 ──

const tableRef = ref(null)
let dragCleanup = []

function columnKeyOf(th) {
  const cls = Array.from(th.classList).find((name) => name.startsWith('vault-th-'))
  return cls ? cls.slice('vault-th-'.length) : null
}

function setupHeaderDrag() {
  dragCleanup.forEach((fn) => fn())
  dragCleanup = []
  const tableEl = tableRef.value && tableRef.value.$el
  if (!tableEl) return
  const ths = Array.from(
    tableEl.querySelectorAll('.el-table__header-wrapper thead th'),
  ).filter((th) => columnKeyOf(th))
  ths.forEach((th) => {
    const targetKey = columnKeyOf(th)
    th.draggable = true
    th.classList.add('is-draggable')
    const onDragStart = (event) => {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', targetKey)
      th.classList.add('is-dragging')
    }
    const onDragEnd = () => th.classList.remove('is-dragging')
    const onDragOver = (event) => {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      th.classList.add('is-drag-over')
    }
    const onDragLeave = () => th.classList.remove('is-drag-over')
    const onDrop = (event) => {
      event.preventDefault()
      th.classList.remove('is-drag-over')
      const fromKey = event.dataTransfer.getData('text/plain')
      if (!fromKey || fromKey === targetKey) return
      const next = [...columnOrder.value]
      const fromIndex = next.indexOf(fromKey)
      const toIndex = next.indexOf(targetKey)
      if (fromIndex === -1 || toIndex === -1) return
      next.splice(fromIndex, 1)
      next.splice(next.indexOf(targetKey), 0, fromKey)
      columnOrder.value = next
      localStorage.setItem(COLUMN_ORDER_KEY, JSON.stringify(next))
      nextTick(setupHeaderDrag)
    }
    th.addEventListener('dragstart', onDragStart)
    th.addEventListener('dragend', onDragEnd)
    th.addEventListener('dragover', onDragOver)
    th.addEventListener('dragleave', onDragLeave)
    th.addEventListener('drop', onDrop)
    dragCleanup.push(() => {
      th.removeEventListener('dragstart', onDragStart)
      th.removeEventListener('dragend', onDragEnd)
      th.removeEventListener('dragover', onDragOver)
      th.removeEventListener('dragleave', onDragLeave)
      th.removeEventListener('drop', onDrop)
    })
  })
}

onBeforeUnmount(() => {
  window.removeEventListener(EVENTS.actionStream, onActionStream)
  dragCleanup.forEach((fn) => fn())
  dragCleanup = []
})

// ── 排序：只影响展示顺序，持久化 localStorage ──────────────

const SORT_FIELD_KEY = 'vault.credentials.sortField'
const SORT_DIR_KEY = 'vault.credentials.sortDir'

const SORT_FIELDS = [
  { value: 'created_at', label: '添加时间' },
  { value: 'name', label: '名称' },
  { value: 'status', label: '状态' },
  { value: 'last_verified', label: '最后验证' },
  { value: 'last_valid', label: '最后有效' },
  { value: 'group', label: '分组' },
]
const SORT_FIELD_VALUES = SORT_FIELDS.map((item) => item.value)
const SORT_DIRS = [
  { value: 'asc', label: '升序' },
  { value: 'desc', label: '降序' },
]

const sortField = ref(
  SORT_FIELD_VALUES.includes(localStorage.getItem(SORT_FIELD_KEY))
    ? localStorage.getItem(SORT_FIELD_KEY)
    : 'created_at',
)
const sortDir = ref(localStorage.getItem(SORT_DIR_KEY) === 'asc' ? 'asc' : 'desc')
watch(sortField, (value) => localStorage.setItem(SORT_FIELD_KEY, value))
watch(sortDir, (value) => localStorage.setItem(SORT_DIR_KEY, value))

// 状态排序权重：需要处理的排前面（配合「升序」= 异常优先）
// plugin_disabled 排最后：它的凭据是「暂时动不了」，不是「有问题等你处理」，
// 混进异常堆里只会挡住真正要看的行。
const STATUS_ORDER = { invalid: 0, error: 1, unknown: 2, valid: 3, plugin_disabled: 4 }

/** 时间字符串 → 可比较的毫秒数（空值/非法值一律当 0，即最早） */
function timeValue(value) {
  const parsed = Date.parse(value || '')
  return Number.isNaN(parsed) ? 0 : parsed
}

const nameCollator = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' })

/** 各排序依据的比较函数：一律写成「升序」方向，方向由外层统一翻转 */
const SORT_COMPARATORS = {
  created_at: (a, b) => timeValue(a.created_at) - timeValue(b.created_at),
  name: (a, b) => nameCollator.compare(a.name || '', b.name || ''),
  status: (a, b) =>
    (STATUS_ORDER[effectiveStatus(a)] ?? 9) - (STATUS_ORDER[effectiveStatus(b)] ?? 9) ||
    nameCollator.compare(a.name || '', b.name || ''),
  last_verified: (a, b) => timeValue(a.last_verified_at) - timeValue(b.last_verified_at),
  last_valid: (a, b) => timeValue(a.last_valid_at) - timeValue(b.last_valid_at),
  group: (a, b) =>
    nameCollator.compare(a.group_name || '', b.group_name || '') ||
    nameCollator.compare(a.name || '', b.name || ''),
}

const sortedCredentials = computed(() => {
  const rows = [...(vaultStore.credentials || [])]
  const compare = SORT_COMPARATORS[sortField.value] || SORT_COMPARATORS.created_at
  const sign = sortDir.value === 'asc' ? 1 : -1
  return rows.sort((a, b) => sign * compare(a, b))
})

// ── 分页：纯展示层（筛选走后端、排序在前端 computed），每页条数持久化 ──

const PAGE_SIZE_KEY = 'vault.credentials.pageSize'
// 预设值；但下拉允许手输（allow-create），所以 5/10/20/50 只是入口，不是白名单
const PAGE_SIZES = [5, 10, 20, 50]
const PAGE_SIZE_DEFAULT = 10

/** 把任意来源的每页条数规整成合法值（正整数，否则回落默认）。
 *  三个来源都要过这里：预设下拉、用户手输（allow-create 回传给 v-model 的是**字符串**）、
 *  以及 localStorage 里的旧值（可能来自旧版本的预设，早已不是合法预设之一）。 */
function normalizePageSize(value) {
  const n = Math.floor(Number(value))
  return Number.isFinite(n) && n > 0 ? n : PAGE_SIZE_DEFAULT
}

const pageSize = ref(normalizePageSize(localStorage.getItem(PAGE_SIZE_KEY)))
// 当前页**不持久化**：页号是「我正看到哪儿」，下次启动还停在第 5 页只会让人以为
// 列表少了内容；重新打开从第 1 页开始才符合直觉（与列宽/排序这类界面偏好不同）。
const currentPage = ref(1)
watch(pageSize, (value) => localStorage.setItem(PAGE_SIZE_KEY, String(value)))

// ── 行高三档：紧凑 / 标准 / 宽松 = 单元格最多显示 1 / 2 / 3 行 ──
// 纯展示偏好，和列宽/排序一样只存浏览器；档位通过 CSS 变量作用在凭据表上（见模板 class="cred-table"）。
const ROW_LINES_KEY = 'vault.credentials.rowLines'
const ROW_LINES_OPTIONS = [
  { value: 1, label: '紧凑' },
  { value: 2, label: '标准' },
  { value: 3, label: '宽松' },
]
const ROW_LINES_DEFAULT = 2

/** 规整档位值：只接受 1/2/3，其余（含 localStorage 里的旧值/脏值）一律落回默认「标准」 */
function normalizeRowLines(value) {
  const n = Math.floor(Number(value))
  return ROW_LINES_OPTIONS.some((opt) => opt.value === n) ? n : ROW_LINES_DEFAULT
}

const rowLines = ref(normalizeRowLines(localStorage.getItem(ROW_LINES_KEY)))
watch(rowLines, (value) => localStorage.setItem(ROW_LINES_KEY, String(value)))

/** 手输的每页条数写回前先规整：`0` / 负数 / 字母 / 空都会落回默认值。
 *  这种情况必须**说出来**——否则用户会以为自己填的 0 被界面悄悄改了。 */
function onPageSizeChange(value) {
  const normalized = normalizePageSize(value)
  if (normalized !== pageSize.value) pageSize.value = normalized
  if (!(Number(value) > 0)) {
    ElMessage.warning(`每页条数需为正整数，已恢复为 ${PAGE_SIZE_DEFAULT}`)
  }
}

const totalPages = computed(() =>
  Math.max(1, Math.ceil((vaultStore.credentials?.length || 0) / pageSize.value)),
)

// 条数变少（删除 / 筛选命中更少 / 调大每页条数）后停在越界页会看到一张空表，
// 这里把当前页夹回合法范围。用 watch 而不是在 computed 里顺手改：computed 里写 ref
// 是反向依赖，会触发 Vue 的递归更新警告。
watch(totalPages, () => {
  if (currentPage.value > totalPages.value) currentPage.value = totalPages.value
})

/** 页码输入框提交（回车 / 失焦）：空值或非法值夹回 1，超界夹回总页数。 */
function onPageJump(value) {
  const total = totalPages.value || 1
  const n = Number(value)
  currentPage.value = Number.isFinite(n)
    ? Math.min(total, Math.max(1, Math.trunc(n)))
    : 1
}

/** 当前页要展示的行。总条数 ≤ 每页条数时就是全部行（1 页）。 */
const pagedCredentials = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return sortedCredentials.value.slice(start, start + pageSize.value)
})

// ── 列宽自适应：按**当前页实际内容**测量，夹在 [MIN_COL_WIDTH, 原定义宽度] ──
// Element Plus 没有「内容自适应 + 上限」的列宽能力，这里量一遍 DOM 再把宽度写死。
// 关键：测量后给**所有**列都传数字 width，EP 才会把表宽收缩到列宽之和（否则会按容器拉伸）。
// 另一头还要满足「下限」：总宽不足容器时按比例摊回余量，保证铺满、操作列右侧不留大空白。
const MIN_COL_WIDTH = 84
const measuredWidths = ref({})

/** 读一个 .cell 的自然内容宽度（含 box-sizing 下的左右内边距），读完立即还原样式。 */
function naturalCellWidth(cell) {
  const prevWhiteSpace = cell.style.whiteSpace
  const prevWidth = cell.style.width
  cell.style.whiteSpace = 'nowrap'
  cell.style.width = 'max-content'
  const width = cell.getBoundingClientRect().width
  cell.style.whiteSpace = prevWhiteSpace
  cell.style.width = prevWidth
  return width
}

async function measureColumnWidths() {
  await nextTick()
  const root = tableRef.value && tableRef.value.$el
  if (!root) return
  const cols = orderedColumns.value
  if (!cols.length) {
    measuredWidths.value = {}
    return
  }
  const headerCells = root.querySelectorAll('.el-table__header-wrapper thead th .cell')
  const rows = root.querySelectorAll('.el-table__body-wrapper tbody tr')
  const widths = {}
  cols.forEach((col, index) => {
    const cap = Number(col.width || col.minWidth || 200)
    let max = 0
    const headerCell = headerCells[index]
    if (headerCell) max = Math.max(max, naturalCellWidth(headerCell))
    rows.forEach((tr) => {
      const cell = tr.children[index] && tr.children[index].querySelector('.cell')
      if (cell) max = Math.max(max, naturalCellWidth(cell))
    })
    widths[col.key] = Math.min(cap, Math.max(MIN_COL_WIDTH, Math.ceil(max)))
  })

  // 下限：总宽不足容器时按现有宽度比例摊回，刚好铺满一屏（内容偏长则不改动，交给横向滚动）
  const bodyWrapper = root.querySelector('.el-table__body-wrapper')
  const container = (bodyWrapper && bodyWrapper.clientWidth) || root.clientWidth || 0
  const sum = cols.reduce((acc, col) => acc + widths[col.key], 0)
  if (container > sum) {
    const extra = container - sum
    let allocated = 0
    cols.forEach((col, index) => {
      if (index === cols.length - 1) {
        widths[col.key] += extra - allocated
      } else {
        const add = Math.floor((extra * widths[col.key]) / (sum || 1))
        widths[col.key] += add
        allocated += add
      }
    })
  }
  measuredWidths.value = widths
}

// 合并同一帧内的测量请求：ResizeObserver 可能因行高变化连续触发
let measureScheduled = false
function scheduleMeasure() {
  if (measureScheduled) return
  measureScheduled = true
  requestAnimationFrame(() => {
    measureScheduled = false
    measureColumnWidths()
  })
}

// 数据 / 分页 / 列显隐与顺序变化后重测（内容变了，宽度要跟着变）
watch([orderedColumns, pagedCredentials], scheduleMeasure)

// 容器宽度变化（窗口缩放、侧栏收起/展开）时同样重测，保证始终铺满
let tableResizeObserver = null
onMounted(() => {
  measureColumnWidths()
  const root = tableRef.value && tableRef.value.$el
  if (root && typeof ResizeObserver !== 'undefined') {
    tableResizeObserver = new ResizeObserver(scheduleMeasure)
    tableResizeObserver.observe(root)
  }
})
onBeforeUnmount(() => {
  if (tableResizeObserver) tableResizeObserver.disconnect()
  tableResizeObserver = null
})

// ── 数据加载与操作 ────────────────────────────────────────

async function load() {
  try {
    await Promise.all([vaultStore.fetchCredentials(), vaultStore.fetchGroups(), vaultStore.fetchPlugins()])
  } catch (err) {
    ElMessage.error(err.message || '加载凭据失败')
  }
}

let searchTimer = null
watch(
  () => filters.search,
  () => {
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      currentPage.value = 1
      vaultStore.fetchCredentials().catch((err) => ElMessage.error(err.message || '搜索失败'))
    }, 350)
  },
)

function applyFilters() {
  // 换了筛选条件就是另一批数据，停在第 5 页没有意义 —— 一律回第 1 页。
  // （排序不在此列：顺序变了但总条数不变，留在原页更符合预期。）
  currentPage.value = 1
  vaultStore.fetchCredentials().catch((err) => ElMessage.error(err.message || '筛选失败'))
}

function resetFilters() {
  filters.group_id = null
  filters.status = null
  filters.search = ''
  resetTree()
  applyFilters()
}

function openCreate() {
  if (!hasPlugins.value) {
    ElMessage.warning('未检测到可用插件，请将插件目录放入 plugins/ 文件夹后重启应用')
    return
  }
  // 目录树当前选中的服务若已被禁用，就不许再往它名下加凭据：禁用插件连验证都做不了，
  // 先加进去只会攒下一批「加完就验不了」的条目。**按钮本身不置灰**，点了给明确提示即可
  // （与「插件管理」里那个灰着的「启用」按钮同理：给反馈比闷着不动强）。
  const type = createDefaultType.value
  if (type && !isPluginEnabled(type)) {
    ElMessage.warning(
      `插件「${typeLabel(type)}」已禁用，无法添加该类型的凭据；可在「插件管理」中重新启用`,
    )
    return
  }
  editing.value = null
  formVisible.value = true
}

function openEdit(row) {
  editing.value = row
  formVisible.value = true
}

async function verifyRow(row) {
  if (verifyingIds.value.includes(row.id)) return // 已在验证中：忽略重复点击
  verifyingIds.value = [...verifyingIds.value, row.id]
  try {
    const result = await vaultStore.verifyCredential(row.id)
    if (result.status === 'valid') ElMessage.success(`${row.name}：验证通过`)
    else ElMessage.warning(`${row.name}：${result.message || '验证未通过'}`)
  } catch (err) {
    ElMessage.error(err.message || '验证失败')
  } finally {
    verifyingIds.value = verifyingIds.value.filter((id) => id !== row.id)
  }
}

async function removeRow(row) {
  try {
    await ElMessageBox.confirm(
      `确定要删除凭据「${row.name}」吗？该操作不可撤销。`,
      '删除凭据',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await vaultStore.deleteCredential(row.id)
    ElMessage.success('凭据已删除')
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

watch(
  () => route.query.new,
  (value) => {
    if (value === '1') {
      openCreate()
      router.replace({ path: '/credentials' })
    }
  },
)

onMounted(async () => {
  window.addEventListener(EVENTS.actionStream, onActionStream)
  await load()
  // 首次使用（无本地记录）时默认展开全部节点
  if (!expandedKeys.value.length) {
    expandedKeys.value = collectExpandable(treeData.value)
    persistExpanded()
  }
  nextTick(() => {
    if (treeRef.value) treeRef.value.setCurrentKey('all')
    setupHeaderDrag()
  })
  if (route.query.new === '1') {
    openCreate()
    router.replace({ path: '/credentials' })
  }
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">凭据管理</h2>
      <span class="text-muted">共 {{ vaultStore.credentials.length }} 条</span>
      <div class="spacer" />
      <el-select v-model="sortField" class="sort-field" :prefix-icon="Sort">
        <el-option v-for="opt in SORT_FIELDS" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
      <el-select v-model="sortDir" class="sort-dir">
        <el-option v-for="opt in SORT_DIRS" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
      <el-select v-model="rowLines" class="row-lines" title="行高">
        <el-option v-for="opt in ROW_LINES_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
      <el-popover placement="bottom-end" :width="200" trigger="click">
        <template #reference>
          <el-button :icon="SetUp">列设置</el-button>
        </template>
        <div class="column-settings">
          <div class="column-settings-title">显示列</div>
          <el-checkbox-group
            :model-value="visibleKeys"
            @update:model-value="handleVisibleChange"
          >
            <el-checkbox
              v-for="def in COLUMN_DEFS"
              :key="def.key"
              :value="def.key"
              :disabled="!!def.locked"
              class="column-settings-item"
            >
              {{ def.label }}
            </el-checkbox>
          </el-checkbox-group>
          <div class="column-settings-hint">表头可拖拽调整顺序</div>
        </div>
      </el-popover>
      <el-button
        :icon="Refresh"
        :loading="vaultStore.loadingCredentials"
        @click="load"
      >刷新</el-button>
      <el-button :icon="Refresh" :loading="batchStarting" :disabled="batchRunning" @click="openBatchPick">批量验证</el-button>
      <el-button :icon="CopyDocument" :disabled="batchLocked" @click="openDedupe">去重</el-button>
      <el-button type="danger" plain :icon="Delete" :disabled="batchLocked" @click="openBatchDelete">批量删除</el-button>
      <el-button type="primary" :icon="Plus" :disabled="batchLocked" @click="openCreate">添加凭据</el-button>
    </div>

    <el-card shadow="never">
      <div class="filter-bar">
        <el-input
          v-model="filters.search"
          :prefix-icon="Search"
          placeholder="搜索名称、类型或分组"
          clearable
          class="filter-search"
        />
        <el-select v-model="filters.group_id" placeholder="全部分组" clearable class="filter-item" @change="applyFilters">
          <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable class="filter-item" @change="applyFilters">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-button text @click="resetFilters">重置</el-button>
        <!-- 分页整体靠右（.pager 的 margin-left:auto 把它推到边缘），顺序与用户要求一致：
             最右是翻页箭头（左右箭头 = 上一页 / 下一页），其左是「每页 xx 条」。
             条数下拉除预设值外允许手输（filterable + allow-create）。
             空列表时不渲染（「1 / 1」和每页条数下拉在空表上没有意义）。 -->
        <div v-if="vaultStore.credentials.length" class="pager">
          <!-- 「每页」「条」写在选择框外面：allow-create 造出来的选项 label 就是用户输入的原文，
               若把单位塞进预设项的 label（「每页 20 条」），自定义值就会显示成光秃秃的「37」，
               两种形态混在一处很难看。单位放外面，两种来源的显示就一致了。 -->
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
          <!-- 页码可直接输入：回车或失焦后跳转，越界（空 / 负数 / 超过总页数）自动收敛 -->
          <el-input-number
            v-model="currentPage"
            class="pager-page-input"
            :min="1"
            :max="totalPages"
            :controls="false"
            :step="1"
            step-strictly
            size="small"
            title="输入页码后回车或失焦即可跳转"
            @change="onPageJump"
          />
          <span class="pager-page">/ {{ totalPages }}</span>
          <el-button :icon="ArrowRight" :disabled="currentPage >= totalPages" title="下一页" @click="currentPage++" />
        </div>
      </div>
    </el-card>

    <div class="content-row">
      <el-card
        shadow="never"
        class="tree-card"
        :class="{ collapsed: treeCollapsed, resizing: treeResizing }"
        :style="treeCollapsed ? null : { width: treeWidth + 'px' }"
      >
        <div class="tree-header">
          <span v-if="!treeCollapsed" class="tree-title">凭据目录</span>
          <el-input
            v-if="!treeCollapsed"
            v-model="treeSearch"
            class="tree-search"
            size="small"
            placeholder="搜索服务"
            clearable
            :prefix-icon="Search"
          />
          <el-button
            v-if="!treeCollapsed && isCustomTreeOrder"
            text
            size="small"
            :icon="Refresh"
            :title="`恢复默认顺序（${treeDragHint}）`"
            @click="resetTreeOrder"
          />
          <el-button
            text
            size="small"
            :icon="treeCollapsed ? Expand : Fold"
            :title="treeCollapsed ? '展开目录' : '收起目录'"
            @click="toggleTreeCollapsed"
          />
        </div>
        <div
          v-if="!treeCollapsed && selectedRelPath"
          class="tree-path-hint"
          :title="`plugins/${selectedRelPath}`"
        >
          {{ selectedRelPath }}
        </div>
        <!-- ⚠️ `:auto-expand-parent` 必须显式关掉（el-tree 默认为 true）。
             根因：`:default-expanded-keys` 一变，el-tree 就对数组里**每个**节点调
             `node.expand(null, autoExpandParent)`，而 `expandParent` 会把该节点的**所有祖先
             一起撑开**。类别节点的祖先正是根「全部凭据」，所以只要 expandedKeys 里还剩任何
             一个类别，用户收起根之后立刻会被下一个 expand 调用顶回来 —— 表现为「全部凭据」
             怎么都收不起来（而类别本身不会，因为它的子节点是叶子、从不进 expandedKeys）。
             关掉后只影响这一处：搜索展开走的是 `node.expand()`（不传该参数，本就是假）。 -->
        <el-tree
          v-show="!treeCollapsed"
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="id"
          highlight-current
          draggable
          :allow-drop="allowTreeDrop"
          :default-expanded-keys="expandedKeys"
          :auto-expand-parent="false"
          :expand-on-click-node="true"
          :filter-node-method="filterTreeNode"
          @node-click="handleNodeClick"
          @node-expand="handleNodeExpand"
          @node-collapse="handleNodeCollapse"
          @node-drop="onTreeDrop"
          @node-contextmenu="onTreeContextMenu"
        >
          <template #default="{ data }">
            <el-tooltip
              :content="`plugins/${data.rel_path}`"
              placement="right"
              :disabled="!data.rel_path"
              :show-after="300"
            >
              <span class="tree-node">
                <img
                  v-if="data.icon_image"
                  class="tree-icon-img"
                  :class="{ 'icon-lift': data.icon_lift }"
                  :src="data.icon_image"
                  alt=""
                />
                <span v-else class="tree-icon">{{ data.icon }}</span>
                <span class="tree-label">{{ data.label }}</span>
                <span v-if="data.count !== undefined" class="tree-count">{{ data.count }}</span>
              </span>
            </el-tooltip>
          </template>
        </el-tree>
        <div v-if="!treeCollapsed" class="tree-resizer" @mousedown="startTreeResize" />
      </el-card>

      <el-card shadow="never" class="table-card">
        <el-table
          ref="tableRef"
          v-loading="vaultStore.loadingCredentials"
          class="cred-table"
          :style="{ '--cred-row-lines': rowLines + 1 }"
          :data="pagedCredentials"
          empty-text="暂无凭据，点击右上角「添加凭据」开始"
          style="width: 100%"
        >
          <el-table-column
            v-for="col in orderedColumns"
            :key="col.key"
            :label="col.label"
            :width="measuredWidths[col.key] || col.width"
            :min-width="measuredWidths[col.key] ? undefined : col.minWidth"
            :label-class-name="`vault-th-${col.key}`"
          >
            <template v-if="col.key === 'credential'" #default="{ row }">
              <div class="credential-cell">
                <img
                  v-if="iconImage(row.credential_type)"
                  class="credential-icon-img"
                  :class="{ 'icon-lift': iconLift(row.credential_type) }"
                  :src="iconImage(row.credential_type)"
                  alt=""
                />
                <span v-else class="credential-icon">{{ typeIcon(row.credential_type) }}</span>
                <div class="credential-text">
                  <div class="credential-name">{{ row.name }}</div>
                  <div class="credential-sub mono">{{ maskedSubtitle(row) || row.credential_type }}</div>
                </div>
              </div>
            </template>

            <template v-else-if="col.key === 'type'" #default="{ row }">
              <span class="text-muted">{{ typeLabel(row.credential_type) }}</span>
            </template>

            <template v-else-if="col.key === 'notes'" #default="{ row }">
              <span class="text-muted cell-ellipsis" :title="notesOf(row) || ''">{{ notesOf(row) || '—' }}</span>
            </template>

            <template v-else-if="col.key === 'extra'" #default="{ row }">
              <span class="text-muted cell-ellipsis" :title="extraLines(row).join('\n')">
                <span v-for="(line, index) in extraLines(row)" :key="index" class="extra-line">{{ line }}</span>
                <template v-if="!extraLines(row).length">—</template>
              </span>
            </template>

            <template v-else-if="col.key === 'group'" #default="{ row }">
              <el-tag
                v-if="row.group_name"
                size="small"
                effect="plain"
                class="cell-tag"
                :title="row.group_name"
              >{{ row.group_name }}</el-tag>
              <span v-else class="text-muted">未分组</span>
            </template>

            <template v-else-if="col.key === 'status'" #default="{ row }">
              <!-- 状态不是直接读 row.status，而是过一层 effectiveStatus()：插件被禁用时显示
                   「插件禁用」并压过库里的值（派生、不入库，理由见 effectiveStatus 的注释） -->
              <StatusBadge :status="effectiveStatus(row)" :detail="effectiveStatusDetail(row)" />
            </template>

            <template v-else-if="col.key === 'created_at'" #default="{ row }">
              <span class="text-muted mono">{{ formatDateTimeFull(row.created_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_verified'" #default="{ row }">
              <span class="text-muted mono">{{ formatDateTimeFull(row.last_verified_at) }}</span>
            </template>

            <template v-else-if="col.key === 'last_valid'" #default="{ row }">
              <span class="text-muted mono">{{ formatDateTimeFull(row.last_valid_at) }}</span>
            </template>

            <template v-else-if="col.key === 'actions'" #default="{ row }">
              <div class="action-cell">
                <!-- 「操作」「验证」这两个依赖插件的能力置灰时给出原因；编辑 / 删除是纯本地数据
                     操作，插件禁用也照常可用。
                     ⚠️ el-tooltip 不能直接包 disabled 的 el-button：disabled 控件收不到鼠标事件，
                     提示不会弹。所以各用一层非 disabled 的 span 做锚点。 -->
                <el-tooltip
                  :content="actionHint(row)"
                  placement="top"
                  :disabled="!actionHint(row)"
                >
                  <span class="action-anchor">
                    <!-- 「操作」常驻：插件没声明任何操作时也保留按钮（灰掉 + 悬停说明），
                         免得同一列里按钮忽有忽无、每行的按钮位置都对不齐 -->
                    <el-button
                      size="small"
                      :icon="Operation"
                      :disabled="!canOpenActions(row)"
                      @click="openActions(row, $event)"
                    >
                      操作
                    </el-button>
                  </span>
                </el-tooltip>
                <el-tooltip
                  content="插件已禁用"
                  placement="top"
                  :disabled="isPluginEnabled(row.credential_type)"
                >
                  <span class="action-anchor">
                    <!-- 「验证」只按**本条是否在本次批量里**禁用：批量勾了 A/B/C，D 的按钮照常可用，
                         用户想单条补验就补验。整页互斥（batchLocked）交给新增/编辑/删除那些会改数据的地方。 -->
                    <el-button
                      size="small"
                      type="primary"
                      plain
                      :icon="Check"
                      :loading="verifyingIds.includes(row.id) || isBatchVerifying(row.id)"
                      :disabled="isBatchVerifying(row.id) || !isPluginEnabled(row.credential_type)"
                      @click="verifyRow(row)"
                    >
                      验证
                    </el-button>
                  </span>
                </el-tooltip>
                <el-button size="small" :icon="Edit" :disabled="batchLocked" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" type="danger" plain :icon="Delete" :disabled="batchLocked" @click="removeRow(row)">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog
      v-model="batchPickVisible"
      title="批量验证"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="pick-toolbar">
        <el-checkbox
          :model-value="pickAllChecked"
          :indeterminate="pickIndeterminate"
          :disabled="batchRunning"
          @change="onPickAll"
        >全选</el-checkbox>
        <span class="pick-count text-muted">
          已选 {{ batchPickIds.length }} / {{ pickableCredentials.length }}
        </span>
      </div>
      <div class="pick-options">
        <span class="pick-opt">
          <span class="text-muted">并发线程数</span>
          <el-input-number
            v-model="batchConcurrency"
            :min="batchLimits.concurrency_range[0]"
            :max="batchLimits.concurrency_range[1]"
            :step="1"
            step-strictly
            size="small"
            controls-position="right"
          />
        </span>
        <span class="pick-opt">
          <span class="text-muted">超时（秒）</span>
          <el-input-number
            v-model="batchTimeout"
            :min="batchLimits.timeout_range[0]"
            :max="batchLimits.timeout_range[1]"
            :step="1"
            step-strictly
            size="small"
            controls-position="right"
          />
        </span>
        <span class="text-muted pick-opt-hint">并发越高越快，但可能触发目标限流</span>
      </div>
      <div class="pick-list">
        <el-checkbox-group v-model="batchPickIds">
          <div
            v-for="row in vaultStore.credentials"
            :key="row.id"
            class="pick-row"
            :class="{ 'is-disabled': !canBatchVerify(row.credential_type) }"
          >
            <el-checkbox :value="row.id" :disabled="batchRunning || !canBatchVerify(row.credential_type)">
              <span class="pick-name">{{ row.name }}</span>
              <span class="pick-type text-muted">{{ typeLabel(row.credential_type) }}</span>
            </el-checkbox>
            <el-tag v-if="!canBatchVerify(row.credential_type)" size="small" type="info">
              {{ isPluginEnabled(row.credential_type) ? '需手动验证' : '插件已禁用' }}
            </el-tag>
          </div>
        </el-checkbox-group>
        <el-empty
          v-if="!vaultStore.credentials.length"
          description="暂无可验证的凭据"
          :image-size="60"
        />
      </div>
      <template #footer>
        <el-button @click="batchPickVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!batchPickIds.length || batchRunning" @click="startPickedBatch">
          验证 {{ batchPickIds.length }} 条
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="deletePickVisible"
      title="批量删除"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="pick-toolbar">
        <el-checkbox
          :model-value="deleteAllChecked"
          :indeterminate="deleteIndeterminate"
          :disabled="deleteStarting"
          @change="onDeletePickAll"
        >全选</el-checkbox>
        <span class="pick-count text-muted">
          已选 {{ deletePickIds.length }} / {{ vaultStore.credentials.length }}
        </span>
      </div>
      <el-alert
        class="del-warn"
        type="warning"
        :closable="false"
        show-icon
        title="删除后不可撤销，请确认后再操作"
      />
      <div class="pick-list">
        <el-checkbox-group v-model="deletePickIds">
          <!-- 与批量验证不同：这里**不置灰任何行**。删除是纯本地数据操作，
               插件禁用与否都不影响能否删掉这条凭据。 -->
          <div v-for="row in vaultStore.credentials" :key="row.id" class="pick-row">
            <el-checkbox :value="row.id" :disabled="deleteStarting">
              <span class="pick-name">{{ row.name }}</span>
              <span class="pick-type text-muted">{{ typeLabel(row.credential_type) }}</span>
            </el-checkbox>
          </div>
        </el-checkbox-group>
        <el-empty
          v-if="!vaultStore.credentials.length"
          description="暂无可删除的凭据"
          :image-size="60"
        />
      </div>
      <template #footer>
        <el-button @click="deletePickVisible = false">取消</el-button>
        <el-button
          type="danger"
          :loading="deleteStarting"
          :disabled="!deletePickIds.length"
          @click="startBatchDelete"
        >
          删除 {{ deletePickIds.length }} 条
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="dedupeVisible"
      title="凭据去重"
      width="760px"
      :close-on-click-modal="false"
      :show-close="!dedupeResolving"
    >
      <div class="pick-toolbar">
        <el-checkbox
          :model-value="dedupeAllChecked"
          :indeterminate="dedupeIndeterminate"
          :disabled="dedupeScanning || dedupeResolving"
          @change="onDedupePickAll"
        >全选</el-checkbox>
        <span class="pick-count text-muted">
          扫描范围 {{ dedupeScopeIds.length }} / {{ vaultStore.credentials.length }}
        </span>
        <el-button
          size="small"
          type="primary"
          :loading="dedupeScanning"
          :disabled="!dedupeScopeIds.length || dedupeResolving"
          @click="scanDuplicates"
        >检测重复</el-button>
      </div>

      <div v-if="!dedupeScanned" class="pick-list">
        <el-checkbox-group v-model="dedupeScopeIds">
          <div v-for="row in vaultStore.credentials" :key="row.id" class="pick-row">
            <el-checkbox :value="row.id" :disabled="dedupeScanning">
              <span class="pick-name">{{ row.name }}</span>
              <span class="pick-type text-muted">{{ typeLabel(row.credential_type) }}</span>
            </el-checkbox>
          </div>
        </el-checkbox-group>
        <el-empty
          v-if="!vaultStore.credentials.length"
          description="暂无可去重的凭据"
          :image-size="60"
        />
      </div>

      <div v-else class="dedupe-results">
        <el-empty
          v-if="!dedupeGroups.length"
          description="未发现重复凭据"
          :image-size="60"
        />
        <div v-for="(group, gi) in dedupeGroups" :key="gi" class="dedupe-group">
          <div class="dedupe-head">
            <strong>{{ group.plugin_name }}</strong>
            <span class="dedupe-match text-muted">
              重复依据：
              <template v-if="group.match && group.match.length">
                <template v-for="(m, mi) in group.match" :key="m.key">
                  <span v-if="mi">、</span>{{ m.label }} {{ m.masked }}
                </template>
              </template>
              <template v-else>敏感字段值重合</template>
            </span>
          </div>
          <el-radio-group
            v-model="dedupeKeep[gi]"
            class="dedupe-items"
            :disabled="dedupeResolving"
          >
            <el-radio v-for="item in group.items" :key="item.id" :value="item.id" class="dedupe-item">
              <span class="dedupe-name">{{ item.name }}</span>
              <span class="dedupe-meta text-muted">
                {{ item.group_name || '未分组' }} · {{ item.notes || '无备注' }} ·
                更新于 {{ formatDateTimeFull(item.updated_at) }}
              </span>
            </el-radio>
          </el-radio-group>
          <el-checkbox
            v-model="dedupeMergeNotes[gi]"
            :disabled="dedupeResolving"
          >把其他项的「描述 / 备注」合并到保留项</el-checkbox>
        </div>
      </div>

      <template #footer>
        <el-button :disabled="dedupeResolving" @click="dedupeVisible = false">取消</el-button>
        <el-button v-if="dedupeScanned" :disabled="dedupeResolving" @click="dedupeScanned = false">重新选择范围</el-button>
        <el-button
          type="primary"
          :loading="dedupeResolving"
          :disabled="!dedupeScanned || !dedupeGroups.length"
          @click="applyDedupe"
        >应用去重</el-button>
      </template>
    </el-dialog>

    <!-- 目录树右键菜单 -->
    <div
      v-if="ctxMenu.visible"
      class="ctx-mask"
      @click="closeCtxMenu"
      @contextmenu.prevent="closeCtxMenu"
    />
    <!-- 操作选择菜单的「点击外部关闭」层：原先没有，导致不选中某一项菜单就一直挂着 -->
    <div
      v-if="actionMenu.visible"
      class="ctx-mask"
      @click="closeActionMenu"
      @contextmenu.prevent="closeActionMenu"
    />
    <!-- 多个操作时的选择菜单（单个操作会直接进，不经过这里） -->
    <div
      v-if="actionMenu.visible"
      ref="actionMenuRef"
      class="ctx-menu action-menu"
      :style="{ left: `${actionMenu.x}px`, top: `${actionMenu.y}px` }"
      @click.stop
    >
      <div
        v-for="item in actionMenu.items"
        :key="item.id"
        class="ctx-item"
        @click="pickFromMenu(item)"
      >
        <div>{{ item.name || item.id }}</div>
      </div>
      <div v-if="actionMenu.pending" class="ctx-hint">正在检测权限…</div>
      <div v-else-if="actionMenu.note" class="ctx-hint">{{ actionMenu.note }}</div>
    </div>

    <div
      v-if="ctxMenu.visible"
      ref="ctxMenuRef"
      class="ctx-menu"
      :style="{ left: `${ctxMenu.x}px`, top: `${ctxMenu.y}px` }"
    >
      <div class="ctx-item" @click="openScopeDefaults">
        <el-icon><SetUp /></el-icon>
        {{ ctxMenu.scope === 'category' ? '类别默认设置' : '插件默认设置' }}
      </div>
      <div v-if="ctxMenu.canToggle" class="ctx-item" @click="togglePluginFromTree">
        <el-icon>
          <CircleClose v-if="ctxMenu.pluginEnabled" />
          <Check v-else />
        </el-icon>
        {{ ctxMenu.pluginEnabled ? '禁用' : '启用' }}插件「{{ ctxMenu.name }}」
      </div>
    </div>

    <!-- 类别 / 插件默认设置（与「插件管理」页共用同一个对话框） -->
    <ScopeDefaultsDialog
      v-model="scopeDialog.visible"
      :scope="scopeDialog.scope"
      :scope-key="scopeDialog.scopeKey"
      :credential-type="scopeDialog.credentialType"
      :title="scopeDialog.title"
    />

    <el-dialog
      v-model="actionDialog.visible"
      :title="actionDialog.title"
      width="640px"
      :close-on-click-modal="false"
    >
      <div v-if="actionDialog.description" class="action-desc">{{ actionDialog.description }}</div>

      <template v-if="actionDialog.kind === 'chat'">
        <!-- 「模型」这一行：**操作声明了 model 字段就常驻**，不再取决于当前值是否为空。
             曾用 `model || models.length` 当条件——但本仓库 LLM 插件已不再写死默认模型
             （model 为空串），一旦列表又没拉回来（网络失败 / 插件没实现 list_models），
             整行会连「刷新模型列表」按钮一起消失，用户**连自救的入口都没有**。
             只对**没有** model 字段的操作（模型由插件自己定，如编排类应用）才不显示。 -->
        <div v-if="actionDialog.modelDeclared || actionDialog.models.length" class="chat-model">
          <span class="text-muted">模型</span>
          <el-select
            v-model="actionDialog.model"
            class="chat-model-select"
            size="small"
            filterable
            allow-create
            default-first-option
            :loading="actionDialog.modelsLoading"
            placeholder="选择或输入模型名"
          >
            <el-option v-for="m in actionDialog.models" :key="m" :label="m" :value="m" />
          </el-select>
          <!-- 常驻：随时可刷新；拉过一次后会缓存在会话里，重开对话框不必再等网络 -->
          <el-button
            size="small"
            :loading="actionDialog.modelsLoading"
            @click="loadModels(true)"
          >{{ actionDialog.models.length ? '刷新模型列表' : '获取模型列表' }}</el-button>
          <span v-if="actionDialog.modelError" class="chat-model-error">
            {{ actionDialog.modelError }}
          </span>
        </div>
        <div class="chat-box">
          <div
            v-for="(msg, index) in actionDialog.messages"
            :key="index"
            class="chat-msg"
            :class="msg.role"
          >
            <div class="chat-bubble" :class="{ 'is-error': msg.error }">{{ msg.content }}</div>
          </div>
          <el-empty
            v-if="!actionDialog.messages.length"
            description="输入一句话开始对话"
            :image-size="50"
          />
        </div>
        <div class="chat-input">
          <el-input
            v-model="actionDialog.input"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
            @keydown.enter.exact.prevent="sendChat"
          />
          <el-button type="primary" :loading="actionDialog.busy" @click="sendChat">发送</el-button>
        </div>
      </template>
      <template v-else-if="actionDialog.kind === 'form'">
        <el-form
          ref="actionFormRef"
          :model="actionDialog.values"
          :rules="actionRules"
          label-width="90px"
          @submit.prevent
        >
          <el-form-item v-for="field in actionFormFields" :key="field.key" :label="field.label" :prop="field.key">
            <el-select
              v-if="field.type === 'select'"
              v-model="actionDialog.values[field.key]"
              style="width: 100%"
            >
              <el-option
                v-for="opt in normalizeOptions(field.options)"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
            <el-input
              v-else-if="field.type === 'textarea'"
              v-model="actionDialog.values[field.key]"
              type="textarea"
              :rows="3"
              :placeholder="field.placeholder || ''"
            />
            <el-input
              v-else
              v-model="actionDialog.values[field.key]"
              :type="field.type === 'password' ? 'password' : 'text'"
              :show-password="field.type === 'password'"
              :placeholder="field.placeholder || ''"
            />
          </el-form-item>
        </el-form>
        <div class="actions-row">
          <el-button type="primary" :loading="actionDialog.busy" @click="runActionForm">
            {{ actionDialog.submitLabel }}
          </el-button>
          <!-- 带进度的操作本质是个后台任务：关掉弹窗它照样跑，可以在「任务」页看进度、随时取消 -->
          <el-button
            v-if="actionDialog.busy && actionDialog.progressEnabled"
            @click="actionDialog.visible = false"
          >转到后台</el-button>
        </div>
        <div v-if="actionDialog.busy && actionDialog.progressEnabled" class="bg-hint">
          也可以关掉本窗口，任务会继续在后台运行——在左侧「任务」里看进度或取消。
        </div>
        <div v-if="actionDialog.progress" class="action-progress">
          <el-progress
            :percentage="progressPercent"
            :indeterminate="!actionDialog.progress.total"
            :stroke-width="14"
            :text-inside="true"
          />
          <div v-if="actionDialog.progress.text" class="progress-text">
            {{ actionDialog.progress.text }}
          </div>
        </div>
        <div v-if="actionDialog.error" class="chat-error">{{ actionDialog.error }}</div>
        <pre v-if="actionDialog.output" class="action-output">{{ actionDialog.output }}</pre>
        <el-table
          v-if="actionDialog.formRows.length"
          :data="actionDialog.formRows"
          size="small"
          max-height="300"
          class="mt-10"
        >
          <el-table-column
            v-for="col in actionDialog.listColumns"
            :key="col.key"
            :prop="col.key"
            :label="col.label"
            :min-width="120"
            show-overflow-tooltip
          />
        </el-table>
      </template>
      <!-- 列表类操作：表格 + 行内动作 + 下钻 -->
      <template v-else-if="actionDialog.kind === 'list'">
        <div class="list-toolbar">
          <el-button
            v-if="actionDialog.listStack.length"
            size="small"
            :icon="Back"
            @click="popList"
          >返回</el-button>
          <span class="list-crumb">
            <template v-for="(crumb, index) in actionDialog.listCrumb" :key="index">{{ crumb }} / </template>
            <strong>{{ actionDialog.listTitle }}</strong>
          </span>
          <span v-if="actionDialog.listTotal !== null" class="text-muted">
            共 {{ actionDialog.listTotal }} 条{{ actionDialog.listHasMore ? '（仅显示部分）' : '' }}
          </span>
          <el-button
            size="small"
            :icon="Refresh"
            :loading="actionDialog.listLoading"
            @click="refreshList"
          >刷新</el-button>
        </div>

        <!-- 列表参数：例如「收件箱」要选拉取封数/文件夹 -->
        <el-form
          v-if="actionFormFields.length"
          ref="actionFormRef"
          class="list-fields"
          size="small"
          :inline="true"
          :model="actionDialog.values"
          :rules="actionRules"
          @submit.prevent
        >
          <el-form-item
            v-for="field in actionFormFields"
            :key="field.key"
            :label="field.label"
            :prop="field.key"
          >
            <el-select
              v-if="field.type === 'select'"
              v-model="actionDialog.values[field.key]"
              :placeholder="field.placeholder || '请选择'"
              clearable
            >
              <el-option
                v-for="option in normalizeOptions(field.options)"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
            <el-input
              v-else
              v-model="actionDialog.values[field.key]"
              :placeholder="field.placeholder || ''"
              :style="{ width: field.type === 'text' ? '160px' : '200px' }"
            />
          </el-form-item>
          <el-form-item>
            <el-button
              type="primary"
              :loading="actionDialog.listLoading"
              @click="refreshList"
            >{{ actionDialog.submitLabel }}</el-button>
          </el-form-item>
        </el-form>

        <el-alert v-if="actionDialog.listError" type="error" :closable="false" show-icon class="list-error">
          <template #title>
            {{ actionDialog.listError }}
            <el-button link size="small" @click="refreshList">重试</el-button>
          </template>
        </el-alert>

        <el-table
          v-else
          v-loading="actionDialog.listLoading"
          :data="actionDialog.listRows"
          size="small"
          max-height="380"
        >
          <el-table-column
            v-for="col in actionDialog.listColumns"
            :key="col.key"
            :prop="col.key"
            :label="col.label"
            :width="col.width || ''"
            :min-width="col.width ? 0 : 120"
            show-overflow-tooltip
          >
            <template #default="{ row }">
              <code v-if="col.mono" class="mono">{{ row[col.key] ?? '—' }}</code>
              <el-tag v-else-if="col.type === 'tag'" size="small" effect="plain">
                {{ row[col.key] ?? '—' }}
              </el-tag>
              <span v-else>{{ row[col.key] ?? '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column
            v-if="actionDialog.listRowActions.length"
            label="操作"
            width="190"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                v-for="rowAction in actionDialog.listRowActions"
                :key="rowAction.id"
                size="small"
                text
                type="primary"
                :disabled="rowActionDisabled(rowAction, row)"
                @click="runRowAction(rowAction, row)"
              >{{ rowAction.label || rowAction.id }}</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty :description="actionDialog.listEmptyText || '暂无数据'" :image-size="50" />
          </template>
        </el-table>
      </template>

      <el-empty v-else description="该操作暂无可视化界面" :image-size="60" />
    </el-dialog>

    <CredentialForm v-model="formVisible" :credential="editing" :default-type="createDefaultType" @saved="load" />
  </div>
</template>

<style scoped>
.section-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-search {
  width: 260px;
}

.filter-item {
  width: 170px;
}

.content-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.tree-card {
  flex-shrink: 0;
  position: relative;
  transition: width 0.2s ease;
}

.tree-card.resizing {
  transition: none;
}

.tree-card.collapsed {
  width: 48px;
}

.tree-card.collapsed :deep(.el-card__body) {
  padding: 8px 6px;
}

.tree-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.tree-card.collapsed .tree-header {
  justify-content: center;
  margin-bottom: 0;
}

.tree-title {
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.tree-search {
  flex: 1;
  min-width: 0;
}

.tree-search :deep(.el-input__wrapper) {
  padding: 0 8px;
}

.tree-path-hint {
  margin-bottom: 6px;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-resizer {
  position: absolute;
  top: 0;
  right: 0;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 5;
}

.tree-resizer:hover,
.tree-card.resizing .tree-resizer {
  background: var(--el-color-primary-light-7);
}

.tree-card :deep(.el-card__body) {
  padding: 8px;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  overflow: hidden;
}

/* 目录树支持拖拽排序：给可拖动的行一个抓手光标，让「这里能拖」这件事自解释。
   注意 el-tree 把 draggable 与 data-key 都落在 `.el-tree-node` 上（不是 __content），
   所以按属性选择、并用子选择器把光标加到内容行上。
   根节点「全部凭据」（node-key 取到 id=all）不参与排序，单独排除，
   免得它显示抓手却怎么拖都没反应。 */
.tree-card :deep(.el-tree-node[draggable="true"]:not([data-key="all"]) > .el-tree-node__content) {
  cursor: grab;
}

.tree-card :deep(.el-tree-node[draggable="true"]:not([data-key="all"]) > .el-tree-node__content:active) {
  cursor: grabbing;
}

.tree-icon {
  line-height: 1;
}

.tree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.table-card {
  flex: 1;
  min-width: 0;
}

.credential-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.credential-icon {
  font-size: 20px;
  line-height: 1;
}

.credential-icon-img {
  width: 22px;
  height: 22px;
  object-fit: contain;
  flex-shrink: 0;
}

.tree-icon-img {
  width: 16px;
  height: 16px;
  object-fit: contain;
  flex-shrink: 0;
}

.credential-name {
  font-weight: 600;
}

.credential-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}

/* 单元格一律单行省略：长备注/长分组名不再把行高撑大（内容再长行高也固定），
   鼠标悬停时由元素自带的 title 属性显示全文。 */
.credential-cell,
.credential-text {
  min-width: 0;
}

.credential-name,
.credential-sub,
.cell-ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-ellipsis {
  display: block;
  max-width: 100%;
}

/* ── 行高三档（--cred-row-lines 只绑在凭据表上，见模板 class="cred-table"）──
   绝不能提到 .page：el-dialog 渲染在 .page 内部，会连带改掉操作弹窗里表格的行高。
   变量的语义是「该行能容纳的文字行数」，绑定时写成 rowLines + 1（档位 1/2/3 → 2/3/4）：
   「名称」列天生占两行（名称 + 下面的子行），第一档必须等于现状，故最低就是 2 行。
   min-height 让**数据行**等高；只作用于 body，表头另有固定高度（见下），别再把它撑高。
   line-clamp 用同一个变量，把超出档位的内容截断（悬停看 title 全文）。 */
.cred-table :deep(.el-table__body-wrapper .cell) {
  min-height: calc(1.5em * var(--cred-row-lines, 3));
}

/* 表头固定 38px 单行高度：不参与行高变量，padding 归零后由 vertical-align 居中。 */
.cred-table :deep(.el-table__header-wrapper th.el-table__cell) {
  height: 38px;
  padding-top: 0;
  padding-bottom: 0;
}

.cred-table .cell-ellipsis {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: var(--cred-row-lines, 3);
  white-space: normal;
  word-break: break-word;
  overflow: hidden;
}

/* 附加信息：一个字段一行 */
.extra-line {
  display: block;
}

/* el-tag 的文字在 .el-tag__content 里，得单独压；否则分组名长了照样撑开 */
.cell-tag {
  max-width: 100%;
}

.cell-tag :deep(.el-tag__content) {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mono {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
}

.pick-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.pick-count {
  font-size: 12px;
}

.del-warn {
  margin-bottom: 8px;
}

.pick-options {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 8px;
}

.pick-opt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.pick-opt .el-input-number {
  width: 104px;
}

.pick-opt-hint {
  font-size: 12px;
}

.pick-list {
  max-height: 46vh;
  overflow-y: auto;
  padding: 4px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.pick-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}

.pick-row.is-disabled {
  opacity: 0.6;
}

.pick-name {
  font-weight: 500;
}

.pick-type {
  margin-left: 8px;
  font-size: 12px;
}

.dedupe-results {
  max-height: 52vh;
  overflow-y: auto;
}

.dedupe-group {
  padding: 10px 12px;
  margin-bottom: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.dedupe-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 6px;
}

.dedupe-match {
  font-size: 12px;
}

.dedupe-items {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  margin-bottom: 6px;
}

.dedupe-item {
  height: auto;
  margin-right: 0;
  padding: 4px 0;
}

.dedupe-item :deep(.el-radio__label) {
  display: flex;
  flex-direction: column;
  line-height: 1.5;
  white-space: normal;
}

.dedupe-name {
  font-weight: 500;
}

.dedupe-meta {
  font-size: 12px;
  word-break: break-all;
}

.ctx-mask {
  position: fixed;
  inset: 0;
  z-index: 3000;
}

.ctx-menu {
  position: fixed;
  z-index: 3001;
  min-width: 180px;
  padding: 4px;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  box-shadow: var(--el-box-shadow-light);
}

.ctx-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 13px;
  border-radius: 4px;
  cursor: pointer;
}

.ctx-item:hover {
  background: var(--el-fill-color-light);
}

.chat-model {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

/* 模型列表拉取失败的提示：占满一行落在下拉框下方，不挤压「获取模型列表」按钮 */
.chat-model-error {
  flex-basis: 100%;
  font-size: 12px;
  color: var(--el-color-danger);
}

.chat-model-select {
  width: 300px;
}

.chat-box {
  height: 320px;
  overflow-y: auto;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
}

.chat-msg {
  display: flex;
  margin-bottom: 8px;
}

.chat-msg.user {
  justify-content: flex-end;
}

.chat-bubble {
  max-width: 78%;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--el-fill-color-light);
}

.chat-msg.user .chat-bubble {
  background: var(--el-color-primary-light-8);
}

/* 报错气泡：与普通回复同形，仅换红字红底，便于在对话流中一眼看出 */
.chat-bubble.is-error {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}

.chat-error {
  margin-top: 8px;
  font-size: 13px;
  color: var(--el-color-danger);
}

.chat-input {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.chat-input :deep(.el-textarea) {
  flex: 1;
}

.actions-row {
  display: flex;
  justify-content: flex-end;
}

.action-output {
  max-height: 320px;
  margin-top: 10px;
  padding: 10px;
  overflow: auto;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

/* 操作列按钮：flex 布局对齐，清除 Element Plus 相邻按钮的默认左间距 */
/* 四个按钮按 2×2 排布（两两一行）：单行横排会把操作列拉得很宽 */
.action-cell {
  display: grid;
  grid-template-columns: repeat(2, max-content);
  align-items: center;
  justify-content: start;
  gap: 6px;
  white-space: nowrap;
}

.action-cell .el-button {
  margin-left: 0;
}

/* 「操作」「验证」各自包一层非 disabled 的 span 作 el-tooltip 的锚点（disabled 的
   el-button 收不到鼠标事件，直接包它提示不弹）。inline-flex 让锚点贴合按钮、
   不额外撑出高度，也不影响 .action-cell 的 flex 间距。 */
.action-anchor {
  display: inline-flex;
  align-items: center;
}

.action-anchor .el-button {
  margin-left: 0;
}

/* 列设置弹层 */
.column-settings-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.column-settings-item {
  display: flex;
  margin-right: 0;
  height: 28px;
}

.column-settings-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 表头拖拽提示样式 */
.table-card :deep(.el-table__header-wrapper th.is-draggable) {
  cursor: grab;
  user-select: none;
}

.table-card :deep(.el-table__header-wrapper th.is-dragging) {
  opacity: 0.5;
}

.table-card :deep(.el-table__header-wrapper th.is-drag-over) {
  border-left: 2px solid var(--el-color-primary);
}

.list-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.list-crumb {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.list-crumb strong {
  color: var(--el-text-color-primary);
}

.list-error {
  margin-bottom: 10px;
}

.list-fields {
  margin-bottom: 4px;
}

.action-desc {
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 带进度的操作（下载/打包）：进度条 + 一行说明（如「12.3 MB / 45.0 MB」） */
.action-progress {
  margin-top: 12px;
}

.bg-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.action-progress .progress-text {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ctx-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mt-10 {
  margin-top: 10px;
}

.action-menu {
  min-width: 160px;
  max-height: calc(100vh - 16px);
  overflow-y: auto;
}

.ctx-hint {
  padding: 4px 10px 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.sort-field {
  width: 132px;
}

.sort-dir {
  width: 96px;
}

.row-lines {
  width: 88px;   /* 选项 label 只有两个字（紧凑/标准/宽松），无需更宽 */
}

/* ── 分页（在筛选框 .filter-bar 内整体靠右，见该处模板） ── */

.pager {
  display: flex;
  align-items: center;
  gap: 6px;
  /* 把分页推到筛选框右边缘；比塞一个自撑开的 spacer 元素更省事，
     且筛选框换行（flex-wrap）时也不会多出一个孤立元素 */
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

.pager-page-input {
  width: 64px;   /* 只放页码数字：去掉上下箭头后无需更宽 */
}

.pager-page-input :deep(.el-input__inner) {
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.pager-size {
  width: 78px;   /* 选项 label 只是数字（单位在框外的 .pager-unit），无需更宽 */
}

.pager-unit {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
