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

const prompt = computed(() => {
  const base = summary.value || props.credentialName || 'ssh'
  const path = displayPath(cwd.value)
  return mode.value === 'shell' ? `${base}:${path}$ ` : `${base}$ `
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
    const res = await api.sshOpen(props.credentialId)
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
      <el-button size="small" :disabled="!connected || busy" @click="clearScreen">清屏</el-button>
      <el-button v-if="connected" size="small" :disabled="busy" @click="closeSession">断开</el-button>
      <el-button v-else size="small" type="primary" :loading="connecting" @click="connect">连接</el-button>
    </div>

    <pre ref="termEl" class="ssh-term">{{ screen }}</pre>

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
  gap: 10px;
  font-size: 12px;
  color: var(--v-text-2);
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
}
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
