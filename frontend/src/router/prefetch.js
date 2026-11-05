/**
 * 非首屏页面的懒加载表与预取。
 *
 * 启动时只解析首屏代码；其余页面在悬停侧栏时立即预取，解锁后再利用空闲时间
 * 逐个加载。这样既保留首屏体积收益，又避免首次切换功能卡时等待 chunk 解析。
 */
export const LAZY_VIEWS = {
  '/credentials': () => import('@/views/Credentials.vue'),
  '/plugins': () => import('@/views/PluginManager.vue'),
  '/groups': () => import('@/views/Groups.vue'),
  '/mcp': () => import('@/views/McpSettings.vue'),
  '/logs': () => import('@/views/Logs.vue'),
  '/tasks': () => import('@/views/Tasks.vue'),
  '/settings': () => import('@/views/Settings.vue'),
  '/backup': () => import('@/views/Backup.vue'),
  '/about': () => import('@/views/About.vue'),
}

// 按使用频率排序，空闲预取时先加载最常去的页面
const PREFETCH_ORDER = [
  '/credentials', '/logs', '/settings', '/tasks', '/plugins',
  '/groups', '/mcp', '/backup', '/about',
]

function onIdle(fn) {
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(fn, { timeout: 2500 })
  } else {
    setTimeout(fn, 300)
  }
}

/** 预取单个页面（侧栏悬停/聚焦时调用）。重复调用会命中模块缓存，开销可忽略。 */
export function prefetchRoute(path) {
  const load = LAZY_VIEWS[path]
  if (load) load().catch(() => {})
}

/** 解锁后逐个空闲时预取全部非首屏页面，避免切页时才临时解析。 */
export function prefetchAllRoutes() {
  const queue = PREFETCH_ORDER.map((path) => LAZY_VIEWS[path]).filter(Boolean)
  const step = () => {
    const load = queue.shift()
    if (!load) return
    load().catch(() => {})
    if (queue.length) onIdle(step)
  }
  onIdle(step)
}
