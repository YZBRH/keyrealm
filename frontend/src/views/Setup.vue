<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'

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
  } catch {
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
    <div class="auth-halo" aria-hidden="true" />

    <el-card class="auth-card" shadow="never">
      <div class="auth-brand">
        <span class="auth-mark">
          <span class="auth-ring" aria-hidden="true" />
          <img src="/favicon.svg" class="auth-mark-img" alt="Keyrealm" />
        </span>
        <h1 class="v-grad-text">Keyrealm</h1>
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
/* 与 Unlock.vue 同一套外壳：全屏居中、背景透明，内容压在 App.vue 的极光之上。 */
.auth-screen {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  overflow: hidden;
  background: transparent;
}

/* 与 Unlock.vue 同款双色柔光，尺寸/配色的取舍见那边的注释。静态，不呼吸。 */
.auth-halo {
  position: absolute;
  width: 900px;
  height: 900px;
  pointer-events: none;
  filter: blur(64px);
  opacity: var(--v-halo-opacity);
  background: radial-gradient(closest-side, var(--v-halo-1), transparent 72%),
    radial-gradient(closest-side at 68% 38%, var(--v-halo-2), transparent 72%);
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

.auth-mark {
  position: relative;
  display: inline-grid;
  place-items: center;
  width: 56px;
  height: 56px;
}

.auth-mark-img {
  display: block;
  width: 56px;
  height: 56px;
  filter: drop-shadow(0 8px 18px rgba(6, 10, 22, 0.35));
}

/* 与 Unlock.vue 同：不做旋转、也不做扩散，就一圈静态描边（理由见那边的注释）。 */
.auth-ring {
  position: absolute;
  inset: -6px;
  border-radius: 15px;
  border: 2px solid color-mix(in srgb, var(--v-accent) 40%, transparent);
  opacity: 0.85;
  pointer-events: none;
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
  .auth-card,
  .auth-halo,
  .auth-ring {
    animation: none;
  }
}
</style>
