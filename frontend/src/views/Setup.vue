<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'
import AuthBackdrop from '@/components/AuthBackdrop.vue'

const router = useRouter()
const appStore = useAppStore()

const formRef = ref(null)
const submitting = ref(false)

const form = reactive({ password: '', confirm: '' })

const rules = {
  password: [
    { required: true, message: '请输入主密码', trigger: 'blur' },
    { min: 8, message: '主密码至少需要 8 个字符', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入主密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

/* 强度配色一律取主题令牌，这样切主题时进度条跟着换色，不会留在 Element Plus 的默认色上。 */
const strength = computed(() => {
  const value = form.password
  if (!value) return { score: 0, label: '未输入', color: 'var(--v-text-3)' }
  let score = 0
  if (value.length >= 8) score += 1
  if (value.length >= 12) score += 1
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1
  if (/\d/.test(value)) score += 1
  if (/[^A-Za-z0-9]/.test(value)) score += 1
  score = Math.min(score, 4)

  const table = [
    { label: '很弱', color: 'var(--v-danger)' },
    { label: '较弱', color: 'var(--v-danger)' },
    { label: '一般', color: 'var(--v-warning)' },
    { label: '较强', color: 'var(--v-success)' },
    { label: '很强', color: 'var(--v-primary)' },
  ]
  return { score, ...table[score] }
})

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch (fields) {
    // 校验失败不只标红字段，额外给一条全局提示，避免只有小字提示被忽略
    const first = fields && Object.values(fields)[0]
    const msg = (Array.isArray(first) ? first[0]?.message : first?.message) || '请检查主密码输入'
    ElMessage.error(msg)
    return
  }

  submitting.value = true
  try {
    await appStore.setupMasterPassword(form.password)
    ElMessage.success('保险库创建成功，请妥善保管主密码')
    router.push('/')
  } catch (err) {
    ElMessage.error(err.message || '创建失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-screen">
    <AuthBackdrop />

    <el-card class="auth-card auth-card--tactical" shadow="never">
      <div class="auth-brand">
        <span class="auth-mark">
          <img src="/favicon.svg" class="auth-mark-img" alt="Keyrealm" />
        </span>
        <div class="auth-kicker"><span class="auth-dot" aria-hidden="true" />INITIALIZE VAULT</div>
        <h1>创建保险库</h1>
        <p class="auth-subtitle">设置主密码（首次使用）</p>
      </div>

      <el-alert type="info" :closable="false" show-icon class="auth-alert">
        <template #title>主密码用于派生加密密钥，一旦遗忘将无法恢复任何凭据。</template>
      </el-alert>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="主密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            size="large"
            show-password
            placeholder="至少 8 个字符"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-form-item label="确认主密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            size="large"
            show-password
            placeholder="再次输入以确认"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <div class="strength">
        <div class="strength-bars">
          <span
            v-for="index in 4"
            :key="index"
            class="strength-bar"
            :style="{ background: index <= strength.score ? strength.color : 'var(--v-border)' }"
          />
        </div>
        <span class="strength-label" :style="{ color: strength.color }">
          密码强度：{{ strength.label }}
        </span>
      </div>

      <el-button
        type="primary"
        size="large"
        class="auth-submit"
        :loading="submitting"
        :icon="Lock"
        @click="submit"
      >
        创建保险库
      </el-button>
    </el-card>
  </div>
</template>

<style scoped>
/* 与 Unlock.vue 同一套外壳：全屏居中，背景由 AuthBackdrop 静态铺开。 */
.auth-screen {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  overflow: hidden;
  background: transparent;
}

/* 见 Unlock.vue 同处说明：挂 .el-card 提高优先级，内边距改由这里独占控制。 */
.auth-card.el-card {
  --el-card-padding: 0;
  position: relative;
  z-index: 1;
  width: 460px;
  padding: 28px 28px 24px;
  box-shadow: var(--v-shadow-lg);
  /* 不加 both：填充模式会留下末帧 transform，使本卡片成为 position: fixed 后代的包含块（见 base.css 同名注释） */
  animation: v-fade-up 0.5s var(--v-ease);
}

.auth-brand {
  text-align: center;
  margin-bottom: 20px;
}

/* 与 Unlock.vue 同：描边做在容器自身（padding + border），不用绝对定位兄弟元素。 */
.auth-mark {
  display: inline-grid;
  place-items: center;
  padding: 6px;
  border: 2px solid color-mix(in srgb, var(--v-accent) 40%, transparent);
  border-radius: 17px;
  filter: drop-shadow(0 8px 18px rgba(6, 10, 22, 0.35));
}

.auth-mark-img {
  display: block;
  width: 56px;
  height: 56px;
}

.auth-brand h1 {
  margin: 14px 0 5px;
  font-family: var(--v-font-display);
  font-size: 23px;
  font-weight: 750;
  letter-spacing: var(--v-title-tracking);
}

.auth-subtitle {
  margin: 0;
  color: var(--v-text-2);
  font-size: 13px;
}

.auth-alert {
  margin-bottom: 18px;
}

/* ── 强度计 ─────────────────────────────────────────────────────── */

.strength {
  margin: 2px 0 20px;
}

.strength-bars {
  display: flex;
  gap: 6px;
}

.strength-bar {
  height: 6px;
  flex: 1;
  border-radius: 3px;
  transition: background var(--v-base) var(--v-ease), box-shadow var(--v-base) var(--v-ease);
}

.strength-label {
  display: inline-block;
  margin-top: 8px;
  font-size: 12px;
  transition: color var(--v-base) var(--v-ease);
}

.auth-submit {
  width: 100%;
}

@media (prefers-reduced-motion: reduce) {
  .auth-card {
    animation: none;
  }
}

/* ── 静态战术风格（与开屏密码面板一致，无动态元素） ───────────── */
.auth-card.auth-card--tactical.el-card {
  --el-card-padding: 0;
  position: relative;
  width: 420px;
  padding: 24px 24px 20px;
  border: 1px solid rgba(163, 230, 53, 0.2);
  border-radius: 16px;
  background:
    radial-gradient(120% 90% at 12% 0%, rgba(163, 230, 53, 0.07), transparent 55%),
    linear-gradient(180deg, rgba(10, 13, 18, 0.92), rgba(6, 8, 12, 0.86));
  box-shadow:
    0 26px 70px rgba(0, 0, 0, 0.5),
    inset 0 1px 0 rgba(255, 255, 255, 0.05);
  color: #f4f4f5;
  animation: none;
  overflow: hidden;
}
.auth-card--tactical::before,
.auth-card--tactical::after {
  content: "";
  position: absolute;
  inset: auto;
  width: 16px;
  height: 16px;
  background: none;
  opacity: 1;
  pointer-events: none;
  border-color: rgba(163, 230, 53, 0.5);
  border-style: solid;
}
.auth-card--tactical::before {
  top: 9px;
  left: 9px;
  border-width: 1px 0 0 1px;
}
.auth-card--tactical::after {
  right: 9px;
  bottom: 9px;
  border-width: 0 1px 1px 0;
}
.auth-kicker {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 12px;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9px;
  letter-spacing: 0.26em;
  color: rgba(163, 230, 53, 0.72);
  text-transform: uppercase;
}
.auth-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #a3e635;
  box-shadow: 0 0 10px rgba(163, 230, 53, 0.9);
}
.auth-card--tactical .auth-brand h1 {
  margin: 8px 0 4px;
  color: #f4f4f5;
  font-size: 20px;
  font-weight: 650;
  letter-spacing: 0.02em;
}
.auth-card--tactical .auth-subtitle {
  color: rgba(244, 244, 245, 0.5);
  font-size: 11.5px;
}
.auth-card--tactical :deep(.el-form-item__label) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9.5px;
  letter-spacing: 0.2em;
  color: rgba(244, 244, 245, 0.48);
  text-transform: uppercase;
}
.auth-card--tactical :deep(.el-input__wrapper) {
  background: rgba(3, 5, 8, 0.58);
  box-shadow: 0 0 0 1px rgba(244, 244, 245, 0.13) inset;
  border-radius: 8px;
}
.auth-card--tactical :deep(.el-input__wrapper.is-focus) {
  box-shadow:
    0 0 0 1px rgba(163, 230, 53, 0.62) inset,
    0 0 0 3px rgba(163, 230, 53, 0.1);
}
.auth-card--tactical :deep(.el-input__inner) {
  height: 40px;
  color: #f4f4f5;
}
.auth-card--tactical :deep(.el-input__inner::placeholder) {
  color: rgba(244, 244, 245, 0.28);
}
.auth-card--tactical :deep(.el-input__suffix) {
  color: rgba(244, 244, 245, 0.4);
}
.auth-card--tactical :deep(.el-form-item__error) {
  color: #fca5a5;
}
.auth-card--tactical :deep(.el-alert) {
  border: 1px solid rgba(163, 230, 53, 0.16);
  border-radius: 8px;
  background: rgba(163, 230, 53, 0.06);
}
.auth-card--tactical :deep(.el-alert__title) {
  color: rgba(244, 244, 245, 0.6);
  font-size: 11px;
}
.auth-card--tactical :deep(.el-alert .el-alert__icon) {
  color: #a3e635;
}
.auth-card--tactical :deep(.el-button--primary) {
  height: 42px;
  border: 0;
  border-radius: 8px;
  background: linear-gradient(90deg, #a3e635, #4ade80);
  color: #071009;
  font-weight: 700;
  box-shadow: none;
}
.auth-card--tactical :deep(.el-button--primary:hover) {
  filter: brightness(1.07);
  background: linear-gradient(90deg, #a3e635, #4ade80);
}
.auth-card--tactical .auth-foot {
  color: rgba(244, 244, 245, 0.3);
}
.auth-card--tactical .auth-foot kbd {
  border-color: rgba(244, 244, 245, 0.14);
  background: rgba(244, 244, 245, 0.06);
  color: rgba(244, 244, 245, 0.5);
}
</style>
