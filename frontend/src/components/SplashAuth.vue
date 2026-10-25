<script setup>
import { computed, reactive, ref, watch } from 'vue'

import { useAppStore } from '@/stores/app'

const props = defineProps({
  // 'unlock' | 'setup'
  mode: { type: String, default: 'unlock' },
})
const emit = defineEmits(['success', 'error'])

const appStore = useAppStore()

const passwordRef = ref(null)
const form = reactive({ password: '', confirm: '' })
const showPassword = ref(false)
const busy = ref(false)
const error = ref('')

const isSetup = computed(() => props.mode === 'setup')
const errorCode = computed(() => (isSetup.value ? 'CHECK FAILED' : 'ACCESS DENIED'))

/** 统一失败出口：面板内保留详细错误，同时通知开屏做全屏级反馈。 */
function fail(message) {
  error.value = message
  emit('error', { code: errorCode.value, message })
}

const strength = computed(() => {
  const value = form.password
  if (!value) return { score: 0, label: '未输入', color: 'rgba(244,244,245,.35)' }
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1
  if (/\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  score = Math.min(score, 4)
  return [
    { score: 0, label: '很弱', color: '#f87171' },
    { score: 1, label: '较弱', color: '#f87171' },
    { score: 2, label: '一般', color: '#fbbf24' },
    { score: 3, label: '较强', color: '#4ade80' },
    { score: 4, label: '很强', color: '#a3e635' },
  ][score]
})

function reset() {
  form.password = ''
  form.confirm = ''
  error.value = ''
  showPassword.value = false
}

watch(
  () => props.mode,
  () => reset(),
)

function focus() {
  passwordRef.value?.focus()
}

defineExpose({ focus })

async function submit() {
  if (busy.value) return
  error.value = ''
  const password = form.password
  if (!password) {
    fail('请输入主密码')
    return
  }
  if (isSetup.value) {
    if (password.length < 8) {
      fail('主密码至少需要 8 个字符')
      return
    }
    if (password !== form.confirm) {
      fail('两次输入的密码不一致')
      return
    }
  }
  busy.value = true
  try {
    if (isSetup.value) await appStore.setupMasterPassword(password)
    else await appStore.unlock(password)
    reset()
    emit('success')
  } catch (err) {
    const message = err.message || (isSetup.value ? '创建失败' : '解锁失败')
    form.password = ''
    form.confirm = ''
    focus()
    fail(message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="sa" :class="{ 'has-error': !!error }">
    <div class="sa-corner sa-corner-tl" aria-hidden="true" />
    <div class="sa-corner sa-corner-br" aria-hidden="true" />

    <div class="sa-kicker">
      <span class="sa-dot" aria-hidden="true" />
      {{ isSetup ? 'INITIALIZE VAULT' : 'VAULT SEALED' }}
    </div>
    <h2 class="sa-title">{{ isSetup ? '创建保险库' : '解锁保险库' }}</h2>
    <p class="sa-sub">
      {{ isSetup ? '首次使用，请设置主密码。它不会被上传或离开本机。' : '输入主密码解开本地保险库。' }}
    </p>

    <form class="sa-form" @submit.prevent="submit">
      <label class="sa-field">
        <span class="sa-label">主密码</span>
        <span class="sa-input-wrap">
          <input
            ref="passwordRef"
            v-model="form.password"
            :type="showPassword ? 'text' : 'password'"
            :autocomplete="isSetup ? 'new-password' : 'current-password'"
            :placeholder="isSetup ? '至少 8 个字符' : '输入主密码'"
            spellcheck="false"
            :disabled="busy"
            @input="error = ''"
          />
          <button class="sa-eye" type="button" tabindex="-1" @click="showPassword = !showPassword">
            {{ showPassword ? '隐藏' : '显示' }}
          </button>
        </span>
      </label>

      <template v-if="isSetup">
        <label class="sa-field">
          <span class="sa-label">确认主密码</span>
          <span class="sa-input-wrap">
            <input
              v-model="form.confirm"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="再次输入以确认"
              spellcheck="false"
              :disabled="busy"
              @input="error = ''"
            />
          </span>
        </label>

        <div class="sa-strength">
          <span class="sa-bars">
            <i
              v-for="index in 4"
              :key="index"
              :style="{ background: index <= strength.score ? strength.color : 'rgba(244,244,245,.12)' }"
            />
          </span>
          <span class="sa-strength-label" :style="{ color: strength.color }">
            密码强度：{{ strength.label }}
          </span>
        </div>
      </template>

      <p v-if="error" class="sa-error" role="alert">
        <span class="sa-error-code">{{ isSetup ? 'CHECK FAILED' : 'ACCESS DENIED' }}</span>
        <span class="sa-error-msg">{{ error }}</span>
      </p>

      <button class="sa-submit" type="submit" :disabled="busy">
        <span>{{ busy ? '处理中…' : isSetup ? '创建保险库' : '解锁' }}</span>
        <span class="sa-enter" aria-hidden="true">ENTER ↵</span>
      </button>
    </form>

    <p class="sa-foot">本地派生密钥 · 数据不离开本机</p>
  </div>
</template>

<style scoped>
.sa {
  position: relative;
  width: 100%;
  padding: 22px 22px 16px;
  border: 1px solid rgba(163, 230, 53, 0.2);
  border-radius: 16px;
  background:
    radial-gradient(120% 90% at 12% 0%, rgba(163, 230, 53, 0.07), transparent 55%),
    linear-gradient(180deg, rgba(10, 13, 18, 0.82), rgba(6, 8, 12, 0.72));
  box-shadow:
    0 26px 70px rgba(0, 0, 0, 0.5),
    inset 0 1px 0 rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  color: #f4f4f5;
  font-family: 'Inter', 'SF Pro Display', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
  overflow: hidden;
}

.sa-corner {
  position: absolute;
  width: 16px;
  height: 16px;
  pointer-events: none;
  border-color: rgba(163, 230, 53, 0.5);
  border-style: solid;
}
.sa-corner-tl {
  top: 9px;
  left: 9px;
  border-width: 1px 0 0 1px;
}
.sa-corner-br {
  right: 9px;
  bottom: 9px;
  border-width: 0 1px 1px 0;
}

.sa-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9px;
  letter-spacing: 0.28em;
  color: rgba(163, 230, 53, 0.72);
  text-transform: uppercase;
}
.sa-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #a3e635;
  box-shadow: 0 0 10px rgba(163, 230, 53, 0.9);
  animation: sa-pulse 1.8s ease-in-out infinite;
}
@keyframes sa-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.7); }
}

.sa-title {
  margin: 10px 0 4px;
  font-size: 20px;
  font-weight: 650;
  letter-spacing: 0.02em;
  color: #f4f4f5;
}
.sa-sub {
  margin: 0 0 16px;
  font-size: 11.5px;
  line-height: 1.6;
  color: rgba(244, 244, 245, 0.5);
}

.sa-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sa-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sa-label {
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9.5px;
  letter-spacing: 0.2em;
  color: rgba(244, 244, 245, 0.48);
  text-transform: uppercase;
}
.sa-input-wrap {
  position: relative;
  display: block;
}
.sa-input-wrap input {
  width: 100%;
  height: 40px;
  padding: 0 48px 0 12px;
  border: 1px solid rgba(244, 244, 245, 0.13);
  border-radius: 8px;
  outline: none;
  background: rgba(3, 5, 8, 0.58);
  color: #f4f4f5;
  font-size: 13px;
  font-family: inherit;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.sa-input-wrap input::placeholder {
  color: rgba(244, 244, 245, 0.28);
}
.sa-input-wrap input:focus {
  border-color: rgba(163, 230, 53, 0.62);
  box-shadow: 0 0 0 3px rgba(163, 230, 53, 0.1);
}
.sa-input-wrap input:disabled {
  opacity: 0.6;
}
.sa-eye {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  padding: 2px 4px;
  border: 0;
  background: transparent;
  color: rgba(244, 244, 245, 0.4);
  font-family: inherit;
  font-size: 10px;
  cursor: pointer;
}
.sa-eye:hover {
  color: #a3e635;
}

.sa-strength {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: -2px;
}
.sa-bars {
  display: flex;
  gap: 4px;
  flex: 1;
}
.sa-bars i {
  display: block;
  flex: 1;
  height: 3px;
  border-radius: 999px;
  transition: background 0.2s;
}
.sa-strength-label {
  flex: none;
  font-size: 10px;
  color: rgba(244, 244, 245, 0.4);
}

.sa.has-error {
  border-color: rgba(248, 113, 113, 0.38);
  box-shadow:
    0 26px 70px rgba(0, 0, 0, 0.5),
    inset 0 1px 0 rgba(255, 255, 255, 0.05),
    inset 0 0 0 1px rgba(248, 113, 113, 0.06);
  animation: sa-shake 0.4s cubic-bezier(0.36, 0.07, 0.19, 0.97);
}
@keyframes sa-shake {
  10%, 90% { transform: translateX(-1px); }
  20%, 80% { transform: translateX(2px); }
  30%, 50%, 70% { transform: translateX(-3px); }
  40%, 60% { transform: translateX(3px); }
}

.sa-error {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 0;
  padding: 8px 11px;
  border: 1px solid rgba(248, 113, 113, 0.2);
  border-left: 2px solid rgba(248, 113, 113, 0.72);
  border-radius: 6px;
  background: linear-gradient(90deg, rgba(248, 113, 113, 0.13), rgba(248, 113, 113, 0.03));
  animation: sa-error-in 0.28s ease-out both;
}
.sa-error-code {
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 8.5px;
  letter-spacing: 0.22em;
  color: #f87171;
}
.sa-error-msg {
  color: rgba(254, 202, 202, 0.92);
  font-size: 11px;
  line-height: 1.4;
}
@keyframes sa-error-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.sa-submit {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  height: 42px;
  margin-top: 2px;
  padding: 0 14px;
  border: 0;
  border-radius: 8px;
  background: linear-gradient(90deg, #a3e635, #4ade80);
  color: #071009;
  font-family: inherit;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  cursor: pointer;
  transition: filter 0.2s, opacity 0.2s;
}
.sa-submit:hover:not(:disabled) {
  filter: brightness(1.07);
}
.sa-submit:disabled {
  opacity: 0.65;
  cursor: default;
}
.sa-enter {
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.12em;
  opacity: 0.6;
}

.sa-foot {
  margin: 12px 0 0;
  text-align: center;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 8.5px;
  letter-spacing: 0.14em;
  color: rgba(244, 244, 245, 0.26);
}
</style>
