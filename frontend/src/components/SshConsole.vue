<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import api from '@/api'

const props = defineProps({
  credentialId: { type: Number, required: true },
  credentialName: { type: String, default: '' },
})

const screen = ref('')
const input = ref('')
const busy = ref(false)
const connecting = ref(false)
const connected = ref(false)
const error = ref('')
const summary = ref('')
const cwd = ref('')
const mode = ref('shell')
const sessionId = ref('')
const history = ref([])
const historyIndex = ref(-1)
const termEl = ref(null)
const inputEl = ref(null)
const copied = ref(false)

const INTERACTIVE_KEY = 'vault.ssh.interactive'
const interactive = ref((() => {
  try { return localStorage.getItem(INTERACTIVE_KEY) === '1' } catch { return false }
})())

const prompt = computed(() => {
  const base = summary.value || props.credentialName || 'ssh'
  const path = displayPath(cwd.value)
  return mode.value === 'shell' ? `${base}:${path}$ ` : `${base}$ `
})

// ── 终端高亮：命令回显、应用提示、错误 / 警告关键字 ──────────
const ERROR_WORDS = 'error|failed|failure|denied|refused|not found|no such file|permission denied|timed out|timeout|invalid|fatal|panic|exception|错误|失败|拒绝|无法|超时'
const WARN_WORDS = 'warning|warn|deprecated|警告|注意'
const HL_SPLIT = new RegExp(`(${ERROR_WORDS}|${WARN_WORDS})`, 'gi')
const ERROR_TEST = new RegExp(`^(?:${ERROR_WORDS})$`, 'i')

function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function highlightLine(text, commandBase) {
  let lineClass = ''
  if (text.startsWith('# ')) lineClass = 'hl-meta'
  else if (text.startsWith('[错误]') || text.startsWith('[退出码')) lineClass = 'hl-error'
  else if (commandBase && (text.startsWith(`${commandBase}:`) || text.startsWith(`${commandBase}$`))) {
    lineClass = 'hl-cmd'
  }
  if (lineClass === 'hl-cmd') {
    return `<span class="hl-cmd">${escapeHtml(text)}</span>`
  }

  const parts = []
  let last = 0
  for (const match of text.matchAll(HL_SPLIT)) {
    if (match.index > last) parts.push(escapeHtml(text.slice(last, match.index)))
    const token = match[0]
    const pieceClass = ERROR_TEST.test(token) ? 'hl-err-piece' : 'hl-warn-piece'
    parts.push(`<span class="${pieceClass}">${escapeHtml(token)}</span>`)
    last = match.index + token.length
  }
  if (last < text.length) parts.push(escapeHtml(text.slice(last)))
  const body = parts.join('')
  return lineClass ? `<span class="${lineClass}">${body}</span>` : body
}

const highlighted = computed(() => {
  return screen.value
    .split('\n')
    .map((line) => highlightLine(line, summary.value))
    .join('\n')
})

function displayPath(path) {
  if (!path) return '~'
  const user = summary.value.split('@')[0] || ''
  const homes = [`/home/${user}`, '/root']
  for (const home of homes) {
    if (user && path === home) return '~'
    if (user && path.startsWith(home + '/')) return '~/' + path.slice(home.length + 1)
  }
  return path
}

function append(text) {
  if (!text) return
  screen.value += (screen.value && !screen.value.endsWith('\n') ? '\n' : '') + text
  if (screen.value.length > 200000) {
    screen.value = '…（较早输出已截断）\n' + screen.value.slice(-150000)
  }
  nextTick(() => {
    if (termEl.value) termEl.value.scrollTop = termEl.value.scrollHeight
  })
}

function appendLine(text) {
  screen.value += (screen.value && !screen.value.endsWith('\n') ? '\n' : '') + text + '\n'
  if (screen.value.length > 200000) {
    screen.value = '…（较早输出已截断）\n' + screen.value.slice(-150000)
  }
  nextTick(() => {
    if (termEl.value) termEl.value.scrollTop = termEl.value.scrollHeight
  })
}

async function closeSession() {
  const id = sessionId.value
  sessionId.value = ''
  connected.value = false
  if (!id) return
  try {
    await api.sshClose(id)
  } catch { /* 断开失败无需打扰用户 */ }
}

async function connect() {
  if (connecting.value) return
  await closeSession()
  connecting.value = true
  error.value = ''
  screen.value = ''
  cwd.value = ''
  try {
    const res = await api.sshOpen(props.credentialId, interactive.value)
    if (!res || res.ok === false) {
      error.value = (res && res.error) || '连接失败'
      appendLine(`[错误] ${error.value}`)
      return
    }
    sessionId.value = res.session_id
    summary.value = res.summary || ''
    mode.value = res.mode || 'shell'
    connected.value = true
    appendLine(`# 已连接 ${summary.value}`)
    if (res.shell_kind === 'pty') {
      appendLine('# 交互模式：已申请 PTY，使用登录 shell 环境')
    } else if (interactive.value) {
      appendLine('# PTY 不可用，已回退静默会话')
    }
    if (res.history_off) appendLine('# 已在目标端临时关闭命令历史（HISTFILE=/dev/null）')
    if (res.host_key) appendLine(`# 主机密钥 ${res.host_key}`)
    if (res.banner) appendLine(res.banner)
    if (mode.value !== 'shell') {
      appendLine('# 服务器不支持交互 shell，当前为逐条命令模式（不保留 cd / 环境变量）')
    } else {
      appendLine('# 输入命令后按 Enter 执行；关闭窗口自动断开。')
    }
    await nextTick()
    inputEl.value?.focus()
  } catch (err) {
    error.value = err.message || '连接失败'
    appendLine(`[错误] ${error.value}`)
  } finally {
    connecting.value = false
  }
}

async function send() {
  const command = input.value
  if (!connected.value || busy.value) return
  if (!command.trim()) return
  history.value.push(command)
  historyIndex.value = -1
  appendLine(prompt.value + command)
  input.value = ''
  busy.value = true
  try {
    const res = await api.sshExec(sessionId.value, command)
    if (res && res.ok) {
      if (res.output) append(res.output)
      if (res.cwd) cwd.value = res.cwd
      if (res.exit_code !== null && res.exit_code !== undefined && res.exit_code !== 0) {
        appendLine(`[退出码 ${res.exit_code}]`)
      }
    } else {
      const message = (res && res.error) || '执行失败'
      appendLine(`[错误] ${message}`)
      if (res && res.closed) {
        connected.value = false
        sessionId.value = ''
      }
    }
  } catch (err) {
    appendLine(`[错误] ${err.message || '执行失败'}`)
  } finally {
    busy.value = false
    await nextTick()
    inputEl.value?.focus()
  }
}

function clearScreen() {
  screen.value = ''
  if (connected.value) appendLine('# 已清屏')
}

async function copyScreen() {
  if (!screen.value) return
  try {
    await navigator.clipboard.writeText(screen.value)
  } catch {
    // WebView 未授权剪贴板时退回 execCommand
    const ta = document.createElement('textarea')
    ta.value = screen.value
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
    } finally {
      document.body.removeChild(ta)
    }
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

function onInteractiveChange(value) {
  try { localStorage.setItem(INTERACTIVE_KEY, value ? '1' : '0') } catch { /* 仅本次会话生效 */ }
  // 会话建立后模式不可切换，已连接时直接按新偏好重连。
  if (connected.value) connect()
}

function onHistory(step) {
  if (!history.value.length) return
  if (historyIndex.value < 0) historyIndex.value = history.value.length
  historyIndex.value = Math.max(0, Math.min(history.value.length, historyIndex.value + step))
  input.value = historyIndex.value >= history.value.length ? '' : history.value[historyIndex.value]
}

onMounted(connect)
onBeforeUnmount(closeSession)

defineExpose({ connect })
</script>

<template>
  <div class="ssh-console">
    <div class="ssh-toolbar">
      <span class="ssh-status">
        <i class="ssh-dot" :class="{ on: connected, busy: connecting || busy }" />
        {{ connected ? (busy ? '执行中…' : '已连接') : (connecting ? '连接中…' : '未连接') }}
        <span v-if="summary" class="ssh-target">{{ summary }}</span>
      </span>
      <span class="spacer" />
      <el-tooltip
        placement="top"
        content="申请伪终端：加载登录 shell 环境，部分程序需要 TTY；关闭则使用静默 sh 会话。切换后会重新连接。"
      >
        <span class="ssh-mode">
          <span class="ssh-mode-label">交互模式（PTY）</span>
          <el-switch
            v-model="interactive"
            size="small"
            :disabled="busy || connecting"
            @change="onInteractiveChange"
          />
        </span>
      </el-tooltip>
      <el-button size="small" :disabled="!screen" @click="copyScreen">{{ copied ? '已复制' : '复制' }}</el-button>
      <el-button size="small" :disabled="!connected || busy" @click="clearScreen">清屏</el-button>
      <el-button v-if="connected" size="small" :disabled="busy" @click="closeSession">断开</el-button>
      <el-button v-else size="small" type="primary" :loading="connecting" @click="connect">连接</el-button>
    </div>

    <pre ref="termEl" class="ssh-term" v-html="highlighted"></pre>

    <div class="ssh-input-row">
      <span class="ssh-prompt">{{ connected ? prompt : '$' }}</span>
      <input
        ref="inputEl"
        v-model="input"
        class="ssh-input"
        :disabled="!connected || busy"
        spellcheck="false"
        autocomplete="off"
        :placeholder="connected ? '输入命令，Enter 执行' : '未连接'"
        @keydown.enter.prevent="send"
        @keydown.up.prevent="onHistory(-1)"
        @keydown.down.prevent="onHistory(1)"
      />
      <el-button type="primary" size="small" :loading="busy" :disabled="!connected" @click="send">
        执行
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.ssh-console {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ssh-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: var(--v-text-2);
}
.ssh-mode {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ssh-mode-label {
  font-size: 12px;
  color: var(--v-text-3);
  white-space: nowrap;
}
.ssh-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}
.ssh-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(244, 244, 245, 0.25);
}
.ssh-dot.on {
  background: #4ade80;
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.7);
}
.ssh-dot.busy {
  animation: ssh-pulse 1s ease-in-out infinite;
}
@keyframes ssh-pulse {
  50% { opacity: 0.35; }
}
.ssh-target {
  color: var(--v-text-3);
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
}
.spacer {
  flex: 1;
}
.ssh-term {
  margin: 0;
  min-height: 300px;
  max-height: 46vh;
  padding: 12px 14px;
  overflow: auto;
  border: 1px solid rgba(163, 230, 53, 0.16);
  border-radius: 10px;
  background: #05070a;
  color: #d4d4d8;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 12.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  user-select: text;
  -webkit-user-select: text;
  cursor: text;
}
.ssh-term :deep(.hl-cmd) { color: #a3e635; }
.ssh-term :deep(.hl-meta) { color: #7dd3fc; }
.ssh-term :deep(.hl-error) { color: #f87171; }
.ssh-term :deep(.hl-err-piece) { color: #f87171; font-weight: 600; }
.ssh-term :deep(.hl-warn-piece) { color: #fbbf24; }
.ssh-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ssh-prompt {
  flex: none;
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 12px;
  color: #a3e635;
}
.ssh-input {
  flex: 1;
  height: 32px;
  padding: 0 10px;
  border: 1px solid rgba(244, 244, 245, 0.14);
  border-radius: 7px;
  outline: none;
  background: rgba(3, 5, 8, 0.6);
  color: #f4f4f5;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 12.5px;
}
.ssh-input:focus {
  border-color: rgba(163, 230, 53, 0.55);
  box-shadow: 0 0 0 3px rgba(163, 230, 53, 0.08);
}
.ssh-input:disabled {
  opacity: 0.55;
}
</style>
