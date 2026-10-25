<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
// 按需引入后不再有 app.use(ElementPlus, { locale })，中文语言包改由根级
// <el-config-provider> 提供（ElConfigProvider 用 renderSlot 渲染，不产生额外 DOM）。
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import api, { EVENTS, onEvent } from '@/api'
import SplashAuth from '@/components/SplashAuth.vue'
// 开屏动画：自包含 Web Component（Shadow DOM，零网络请求）。
import '@/splash/keyrealm-splash.js'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'
import { useMcpStore } from '@/stores/mcp'
import { useProxyStore } from '@/stores/proxy'

const router = useRouter()
const appStore = useAppStore()
const vaultStore = useVaultStore()
const mcpStore = useMcpStore()
const proxyStore = useProxyStore()

// ── 开屏动画 ────────────────────────────────────────────
// 桌面按进程、Web 按标签页会话只播一次；禁用开关存在后端 settings。
// 窗口过小时直接跳过：动画按大屏构图，窄窗口下观感差且拖慢启动。
const SPLASH_MIN_WIDTH = 1024
const SPLASH_MIN_HEIGHT = 600
const splashHost = ref(null)
const splashEl = ref(null)
const splashAuthTarget = ref(null)
const splashAuthRef = ref(null)
const splashRevealing = ref(false)
const splashMounted = ref(true)
const splashPlaying = ref(false)
// 从首帧起就用开屏层盖住底层页面（此时开屏组件只是一块深色底），
// 避免等待 claim/bootstrap 的几十毫秒里闪出旧解锁页的白字。
const splashBlocking = ref(true)

const splashAuthMode = computed(() => (appStore.initialized ? 'unlock' : 'setup'))
// 是否需要在开屏里嵌密码表单：只在引导完成时判定一次。
// 不能用 `!appStore.unlocked` 实时判断 —— 解锁成功后 store 会立刻变 true，
// 表单会在 emit('success') 之前被卸载，宿主收不到成功事件。
const splashAuthNeeded = ref(false)
const showSplashAuth = computed(
  () => splashPlaying.value && splashAuthNeeded.value,
)
watch(
  () => appStore.bootstrapped,
  (ready) => {
    if (ready) splashAuthNeeded.value = !appStore.unlocked
  },
  { immediate: true },
)

const isWebMode = !!(window.__KEYREALM_WEB__ || {}).token
const SPLASH_SEEN_KEY = 'keyrealm.splash.seen'

function viewportTooSmall() {
  // 取不到尺寸（0）时不要误判成「小窗口」；正常桌面/浏览器都会有值。
  const w = window.innerWidth || document.documentElement.clientWidth || 0
  const h = window.innerHeight || document.documentElement.clientHeight || 0
  return w > 0 && h > 0 && (w < SPLASH_MIN_WIDTH || h < SPLASH_MIN_HEIGHT)
}

async function createSplashElement() {
  // 自定义元素用 JS 主动实例化，避开「先渲染成 HTMLUnknownElement、再升级」的时序问题。
  let Ctor = window.KeyrealmSplash || window.customElements?.get?.('keyrealm-splash')
  if (!Ctor) {
    try {
      await window.customElements?.whenDefined?.('keyrealm-splash')
      Ctor = window.KeyrealmSplash || window.customElements?.get?.('keyrealm-splash')
    } catch { /* ignore */ }
  }
  if (!Ctor || !splashHost.value) {
    splashMounted.value = false
    splashPlaying.value = false
    splashBlocking.value = false
    return
  }
  const el = new Ctor()
  splashEl.value = el
  splashHost.value.appendChild(el)
  splashAuthTarget.value = el
  splashRevealing.value = false
  await nextTick()
  el.addEventListener('splash:done', onSplashDone, { once: true })
  el.addEventListener('splash:reveal', () => { splashRevealing.value = true })
  el.addEventListener('splash:auth', () => splashAuthRef.value?.focus?.())
  try {
    el.play()
  } catch {
    // 动画初始化失败不能把底层页面一直锁在 inert 状态
    splashPlaying.value = false
    splashMounted.value = false
    splashBlocking.value = false
  }
}

/** 挂载并播放一次开屏。启动与「总是展示」的托盘唤出共用。 */
async function startSplash() {
  if (viewportTooSmall()) {
    splashMounted.value = false
    splashBlocking.value = false
    return
  }
  // 先拿到保险库状态：决定开场是「锁住等待密码」还是普通开锁动画。
  try {
    await appStore.bootstrap()
  } catch {
    splashMounted.value = false
    splashBlocking.value = false
    return
  }
  splashAuthNeeded.value = !appStore.unlocked
  splashRevealing.value = false
  splashMounted.value = true
  splashBlocking.value = true
  splashPlaying.value = true
  await nextTick()
  await createSplashElement()
}

async function initSplash() {
  let show = true
  if (isWebMode) {
    // Web：默认一次标签页会话只播一次。开启「总是展示」后每次刷新都播。
    try {
      const cfg = await api.getSplashDisabled()
      const always = Boolean(cfg && cfg.always) && !(cfg && cfg.disabled)
      if (cfg && cfg.disabled) show = false
      else if (!always && sessionStorage.getItem(SPLASH_SEEN_KEY)) show = false
      // 即使本次因禁用不播，也记下会话标记：当前标签页里重新启用不会补播。
      sessionStorage.setItem(SPLASH_SEEN_KEY, '1')
    } catch { /* sessionStorage / 后端不可用：按默认（播放）处理 */ }
  } else {
    // 桌面：由后端按进程认领；「总是展示」开启时后端每次加载都会返回 show=true。
    let claim = null
    try {
      claim = await api.claimSplash()
    } catch { /* 后端暂不可用：按默认（播放）处理，不因开屏失败影响主流程 */ }
    if (claim && claim.show === false) show = false
  }
  if (!show) {
    splashMounted.value = false
    splashBlocking.value = false
    return
  }
  await startSplash()
}

/** 桌面端从托盘唤出：按「总是展示开屏动画」设置决定是否重播。 */
function onWindowRestored() {
  if (isWebMode) return
  api.getSplashDisabled()
    .then((cfg) => {
      if (!cfg || cfg.disabled || !cfg.always) return
      if (splashPlaying.value) return
      return startSplash()
    })
    .catch(() => {})
}

function onSplashDone() {
  splashPlaying.value = false
  splashMounted.value = false
  splashBlocking.value = false
  splashAuthTarget.value = null
  splashEl.value = null
}

function onSplashAuthSuccess() {
  // 主界面先渲染在遮罩下方；开屏自己播放「回中 → 放大 → 开锁 → 任意键进入」。
  router.push('/').catch(() => {})
  splashEl.value?.authSuccess?.()
}

function onSplashAuthError(detail) {
  // 表单内的详细错误照常显示；这里再触发开屏的全屏级反馈。
  splashEl.value?.authError?.(detail)
}

const disposers = []

// 关闭窗口确认弹窗（由后端关闭事件 vault:close_confirm 触发）
const closeDialog = ref(false)
const closeRemember = ref(false)
const closeBusy = ref(false)

async function handleCloseChoice(action) {
  closeBusy.value = true
  try {
    await api.confirmCloseAction(action, closeRemember.value)
    closeDialog.value = false
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  } finally {
    closeBusy.value = false
  }
}

function guard(promise) {
  if (promise && typeof promise.catch === 'function') promise.catch(() => {})
}

// 更新准备完成 → 全局询问是否立即重启。
// 放在根组件而不是设置页：用户可能在准备期间切到别的页面，弹窗照样要出现。
let updateReadyBusy = false
async function promptUpdateReady(detail) {
  if (updateReadyBusy) return
  updateReadyBusy = true
  const info = detail || {}
  const version = info.version ? `（v${info.version}）` : ''
  const reused = info.reused ? '将直接使用上次已准备好的本地产物。' : ''
  try {
    await ElMessageBox.confirm(
      `更新已准备完成${version}。${reused}是否立即重启完成安装？`
      + '选择「稍后」不会重新下载，下次更新会直接复用本地准备产物。',
      '更新已就绪',
      {
        type: 'success',
        confirmButtonText: '立即重启',
        cancelButtonText: '稍后',
        distinguishCancelAndClose: true,
        closeOnClickModal: false,
      },
    )
  } catch {
    ElMessage.info('已暂缓更新；下次更新将复用本地准备产物')
    updateReadyBusy = false
    return
  }
  try {
    const res = await api.applyPreparedUpdate(true)
    if (res && res.ok === false) {
      ElMessage.error(res.message || '无法开始安装更新')
    } else {
      ElMessage.success('正在重启完成更新…')
    }
  } catch (err) {
    ElMessage.error(err.message || '无法开始安装更新')
  } finally {
    updateReadyBusy = false
  }
}

/** 窗口重新获得焦点/可见时刷新数据（覆盖后台 MCP 操作后前台未实时更新的场景） */
function refreshOnActive() {
  if (!appStore.unlocked) return
  guard(vaultStore.fetchCredentials())
  guard(vaultStore.fetchGroups())
}
function handleVisibility() {
  if (document.visibilityState !== 'visible') return
  handleActivity()
  refreshOnActive()
}

/** 用户活动上报（供后端「空闲自动锁定」计时）。
 *  这里只做转发，节流在 store 内（30s）——本函数会被 mousemove 高频调用，
 *  所以在 store 里拦截比在这里判断更省事，也不必给每个事件各配一套节流。 */
function handleActivity() {
  appStore.reportActivity()
}

// 覆盖键鼠/滚轮/触摸四类；mousemove 与 wheel 高频，标 passive 避免拖慢滚动。
const ACTIVITY_EVENTS = [
  ['mousemove', { passive: true }],
  ['mousedown', { passive: true }],
  ['wheel', { passive: true }],
  ['touchstart', { passive: true }],
  ['keydown', undefined],
]

/** 「自动检测更新」发现新版本 → 弹一条可点击的通知（点它跳到设置页的更新卡片）。
 *  后端把通知存成「取出即清除」，所以事件与首屏各调一次也不会重复弹。 */
async function showUpdateNotice() {
  try {
    const n = await api.takeUpdateNotice()
    if (!n || !n.has_update) return
    const v = (n.latest && n.latest.version) || ''
    const c = (n.latest && n.latest.commit) || ''
    const cur = (n.current && n.current.version) || ''
    ElNotification({
      title: '发现新版本',
      message: `${v}${c ? `（${c}）` : ''}　当前 ${cur}　·　点击前往「设置 → 更新」`,
      type: 'info',
      duration: 0,
      onClick: () => router.push({ name: 'settings' }),
    })
  } catch { /* 通知失败无所谓，不影响使用 */ }
}

/** 上次更新**失败**了 → 弹一条通知（交棒阶段没有窗口，只能靠这里让用户看见）。
 *  同一份结果只弹一次（按 `finished_at` 记在 localStorage 里）。 */
async function showUpdateFailure() {
  try {
    const st = await api.updateStatus()
    if (!st || st.ok !== false || !st.finished_at) return
    const key = 'vault.update.failedSeen'
    if (localStorage.getItem(key) === st.finished_at) return
    localStorage.setItem(key, st.finished_at)
    ElNotification({
      title: '上次更新失败',
      message: `${st.message || '未知原因'}${st.log ? `　·　日志：${st.log}` : ''}`,
      type: 'error',
      duration: 0,
      onClick: () => router.push({ name: 'settings' }),
    })
  } catch { /* 通知失败无所谓，不影响使用 */ }
}

onMounted(() => {
  initSplash().catch(() => {})

  disposers.push(
    onEvent(EVENTS.credentialUpdated, (detail) => vaultStore.applyCredentialUpdated(detail)),
  )
  disposers.push(
    onEvent(EVENTS.credentialsChanged, () => guard(vaultStore.fetchCredentials())),
  )
  disposers.push(
    onEvent(EVENTS.pluginsChanged, () => guard(vaultStore.fetchPlugins())),
  )
  // 代理注册表可能被 MCP 侧改动（新增/删除/换全局），界面里开着的设置页要跟着变。
  disposers.push(
    onEvent(EVENTS.proxiesChanged, () => guard(proxyStore.fetch())),
  )
  disposers.push(onEvent(EVENTS.batchStarted, (detail) => vaultStore.applyBatchStarted(detail)))
  disposers.push(onEvent(EVENTS.batchProgress, (detail) => vaultStore.applyBatchProgress(detail)))
  disposers.push(
    onEvent(EVENTS.batchDone, (detail) => guard(vaultStore.applyBatchDone(detail))),
  )
  disposers.push(
    onEvent(EVENTS.mcpStatus, (detail) => {
      mcpStore.applyStatusEvent(detail)
      guard(appStore.refreshTray())
    }),
  )
  disposers.push(
    onEvent(EVENTS.closeConfirm, () => {
      closeRemember.value = false
      closeDialog.value = true
    }),
  )
  // 桌面端从托盘唤出：是否重播开屏由「总是展示开屏动画」决定（见 onWindowRestored）。
  disposers.push(onEvent(EVENTS.windowRestored, () => { onWindowRestored() }))
  // 更新准备完成（后端不再自动退出）→ 询问是否立即重启。
  disposers.push(onEvent(EVENTS.updateReady, (detail) => { promptUpdateReady(detail) }))
  // 后端加锁（界面按钮 / 托盘 / 空闲自动锁）都会广播此事件，统一在这里清空明文。
  // 随后的 watch(appStore.unlocked) 会把路由推回 /unlock。
  disposers.push(onEvent(EVENTS.vaultLocked, () => appStore.applyLocked()))

  // 自动检测更新发现新版本 → 弹通知；同时补查一次（覆盖「检查在首屏前就完成」的情况）。
  disposers.push(onEvent(EVENTS.updateAvailable, () => { showUpdateNotice() }))
  showUpdateNotice()
  // 上次更新**失败**也要让人看见（交棒阶段没有窗口）。
  showUpdateFailure()

  const handleFocus = () => {
    handleActivity()
    refreshOnActive()
  }
  window.addEventListener('focus', handleFocus)
  document.addEventListener('visibilitychange', handleVisibility)
  disposers.push(() => window.removeEventListener('focus', handleFocus))
  disposers.push(() => document.removeEventListener('visibilitychange', handleVisibility))

  for (const [name, opts] of ACTIVITY_EVENTS) {
    window.addEventListener(name, handleActivity, opts)
    disposers.push(() => window.removeEventListener(name, handleActivity, opts))
  }

  guard(appStore.refreshTray())
  // 页面重载会丢掉已推送的批量验证事件；这里从任务列表兜一次底，避免横幅「凭空消失」
  guard(vaultStore.resumeBatchFromTasks())
})

onBeforeUnmount(() => {
  disposers.forEach((dispose) => dispose())
  disposers.length = 0
})

// 保险库被锁定（例如托盘菜单触发）时，强制回到解锁页。
watch(
  () => appStore.unlocked,
  (unlocked) => {
    if (appStore.initialized && !unlocked && router.currentRoute.value.name !== 'unlock') {
      router.push('/unlock')
    }
  },
)
</script>

<template>
  <!-- locale 由根级 config-provider 统一提供：按需引入后没有 app.use(ElementPlus) 了。
       该组件内部用 renderSlot 渲染，不会多包一层 DOM，不影响 #app 的布局。 -->
  <el-config-provider :locale="zhCn" :z-index="100000">
    <!-- 背景极光：三个缓慢漂移的光斑，纯装饰。内容层靠 #app 的 z-index 压在上面。 -->
    <div class="v-aurora" aria-hidden="true">
      <span class="blob b1" />
      <span class="blob b2" />
      <span class="blob b3" />
    </div>

    <router-view v-slot="{ Component }">
      <!-- 开屏遮罩期间锁住底层页面，避免密码表单被重复聚焦 / 误触。 -->
      <component :is="Component" :inert="splashBlocking || null" />
    </router-view>

    <!-- 开屏动画：由 JS 实例化的 Web Component，外层先铺深色底防旧页面闪回。 -->
    <div
      v-if="splashMounted"
      ref="splashHost"
      class="splash-host"
      :class="{ 'is-revealing': splashRevealing }"
    />
    <Teleport v-if="splashAuthTarget" :to="splashAuthTarget">
      <!-- 密码表单作为开屏的一部分；禁用开屏 / 已解锁时不挂载，走原有独立页面。 -->
      <SplashAuth
        v-if="showSplashAuth"
        ref="splashAuthRef"
        :mode="splashAuthMode"
        @success="onSplashAuthSuccess"
        @error="onSplashAuthError"
      />
    </Teleport>

    <el-dialog
      v-model="closeDialog"
      title="关闭窗口"
      width="440px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <p class="close-ask">要直接退出程序，还是让程序继续在后台运行？</p>
      <p class="close-hint">后台运行时窗口会最小化到系统托盘，MCP 服务与定时任务继续执行。</p>
      <el-checkbox v-model="closeRemember">记住我的选择（可在"系统设置"中修改）</el-checkbox>
      <template #footer>
        <el-button :disabled="closeBusy" @click="handleCloseChoice('background')">
          后台运行
        </el-button>
        <el-button type="primary" :disabled="closeBusy" @click="handleCloseChoice('quit')">
          直接退出
        </el-button>
      </template>
    </el-dialog>
  </el-config-provider>
</template>

<style>
/* 全局基座（html/body/#app、滚动条、.page、.page-toolbar、.text-muted）已移到
   `src/styles/base.css`，那里按主题令牌统一控制。此处只留本组件独有的部分。
   注意：本块是**非 scoped** 的全局样式。 */

/* 开屏宿主在自定义元素升级完成前也要盖住底层页面（防止旧解锁页白字闪回）。
   data-revealing 由组件在进入转场时设置，让钥匙孔能透出主界面。 */
keyrealm-splash {
  position: fixed;
  inset: 0;
  z-index: 99999;
  background: #07080c;
}
keyrealm-splash[data-revealing] {
  background: transparent;
}
.splash-host {
  position: fixed;
  inset: 0;
  z-index: 99999;
  background: #07080c;
}
.splash-host.is-revealing {
  background: transparent;
}

/* 关闭确认弹窗里的两段文案 */
.close-ask {
  margin: 0 0 6px;
  font-size: 14px;
  color: var(--v-text);
}

.close-hint {
  margin: 0 0 14px;
  color: var(--v-text-2);
  font-size: 13px;
  line-height: 1.6;
}
</style>