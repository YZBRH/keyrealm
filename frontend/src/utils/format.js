/**
 * 时间格式化工具。
 *
 * 后端返回的 datetime 由 pydantic 序列化为 ISO 字符串（可能不带时区），
 * 这里统一按本地时间解析。
 */

export function parseDate(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  const text = String(value).trim()
  if (!text) return null
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

function pad(n) {
  return String(n).padStart(2, '0')
}

/** 2026-10-03 14:22 */
export function formatDateTime(value, fallback = '—') {
  const date = parseDate(value)
  if (!date) return fallback
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 2026-10-03 14:22:07 */
export function formatDateTimeFull(value, fallback = '—') {
  const date = parseDate(value)
  if (!date) return fallback
  return `${formatDateTime(date)}:${pad(date.getSeconds())}`
}

/** 刚刚 / 5 分钟前 / 3 小时前 / 2 天前 / 2026-10-03 */
export function formatRelative(value, fallback = '从未') {
  const date = parseDate(value)
  if (!date) return fallback
  const diff = Date.now() - date.getTime()
  if (diff < 0) {
    const ahead = Math.abs(diff)
    if (ahead < 60_000) return '即将'
    if (ahead < 3_600_000) return `${Math.floor(ahead / 60_000)} 分钟后`
    if (ahead < 86_400_000) return `${Math.floor(ahead / 3_600_000)} 小时后`
    return `${Math.floor(ahead / 86_400_000)} 天后`
  }
  if (diff < 10_000) return '刚刚'
  if (diff < 60_000) return `${Math.floor(diff / 1000)} 秒前`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return formatDateTime(date)
}

const STATUS_TEXT = {
  valid: '有效',
  invalid: '失效',
  error: '错误',
  unknown: '未验证',
  // 派生状态：凭据所属的**插件**被禁用。它不是 DB 里 status 列的取值——插件启用/禁用只是
  // plugins/.state.json 里的一个开关，重新启用后状态必须能自动回到原值，所以这个状态由前端
  // 按插件启用状态实时派生（见 Credentials.vue 的 effectiveStatus()），绝不写进库。
  plugin_disabled: '插件禁用',
}

export function statusText(status) {
  return STATUS_TEXT[status] || status || '未验证'
}

export function statusTagType(status) {
  switch (status) {
    case 'valid':
      return 'success'
    case 'invalid':
      return 'danger'
    case 'error':
      return 'warning'
    // 「未验证」与「插件禁用」都是「没有结论」，共用灰色标签；插件禁用另有 tooltip 说明缘由
    default:
      return 'info'
  }
}

// 注意：这里**不含** plugin_disabled。它是前端派生状态，不是 DB 里的值，而这个下拉是直接
// 递给后端 list_credentials(status=…) 做 SQL 过滤的，加进来只会永远筛出 0 条。
export const STATUS_OPTIONS = [
  { label: '有效', value: 'valid' },
  { label: '失效', value: 'invalid' },
  { label: '错误', value: 'error' },
  { label: '未验证', value: 'unknown' },
]

/**
 * 归一化 manifest 里 `select` 字段的 options。
 *
 * 插件的 options 允许两种写法：
 *   - 纯字符串数组：`["api_key", "chatgpt"]` → 值与显示都是它自己；
 *   - 对象数组：`[{ value: "chatgpt", label: "ChatGPT 登录态" }]` → 显示 label、存 value。
 * 早期只支持字符串，导致「值必须可读」——想让用户看懂就只能把中文当值存库（既难看又难匹配）。
 * 统一在这里归一，渲染处一律用 value/label。
 */
export function normalizeOptions(options) {
  if (!Array.isArray(options)) return []
  return options.map((option) => {
    if (option && typeof option === 'object') {
      return { value: option.value, label: option.label ?? String(option.value ?? '') }
    }
    return { value: option, label: String(option) }
  })
}

/**
 * Base URL / 端点字段的下拉候选（manifest 的 `presets`，形如 `[{label, value}]`）。
 *
 * 返回 Element Plus autocomplete 需要的 `[{value, label}]`：输入框始终可自由输入，
 * 候选只是辅助。按 label 与 value 同时做不区分大小写的包含匹配；空查询返回全部。
 */
export function matchPresets(field, query) {
  const list = Array.isArray(field && field.presets) ? field.presets : []
  const q = String(query || '').trim().toLowerCase()
  return list
    .filter((preset) => preset && preset.value)
    .map((preset) => ({
      value: String(preset.value),
      label: String(preset.label ?? preset.value),
    }))
    .filter((preset) => !q
      || preset.label.toLowerCase().includes(q)
      || preset.value.toLowerCase().includes(q))
}

/**
 * 单个字段此刻该不该显示（manifest 的 show_if 条件显隐）。
 *
 * `show_if` 形如 `{"auth_mode": "chatgpt"}` 或 `{"auth_mode": ["api_key","chatgpt"]}`：
 * 只有当那个字段的**当前值**命中时才显示本字段；没有 `show_if` 的一律显示。
 * 用于「同厂商不同模式」——例如 OpenAI 选 API Key 时不必看到登录态那组字段。
 */
export function fieldVisible(field, values) {
  const cond = field && field.show_if
  if (!cond || typeof cond !== 'object' || Array.isArray(cond)) return true
  const vals = values || {}
  return Object.entries(cond).every(([key, want]) => {
    const allowed = Array.isArray(want) ? want : [want]
    return allowed.some((v) => String(vals[key] ?? '') === String(v))
  })
}

/** 按 show_if 过滤出当前该显示的字段（渲染与提交都用它，保证「看到什么就提交什么」） */
export function visibleFields(fields, values) {
  const list = Array.isArray(fields) ? fields : []
  return list.filter((f) => fieldVisible(f, values))
}