<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Unlock as UnlockIcon } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'
import AuthBackdrop from '@/components/AuthBackdrop.vue'

const router = useRouter()
const appStore = useAppStore()

const formRef = ref(null)
const submitting = ref(false)
const errorMessage = ref('')
const passwordRef = ref(null)

const form = reactive({ password: '' })

const rules = {
  password: [{ required: true, message: '请输入主密码', trigger: 'blur' }],
}

const mockHint = window.__VAULT_MOCK__ ? `开发模式 Mock 主密码：${window.__VAULT_MOCK__.password}` : ''

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  submitting.value = true
  errorMessage.value = ''
  try {
    await appStore.unlock(form.password)
    form.password = ''
    router.push('/')
  } catch (err) {
    errorMessage.value = err.message || '解锁失败'
    form.password = ''
    ElMessage.error(errorMessage.value)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  if (passwordRef.value && passwordRef.value.focus) passwordRef.value.focus()
})
</script>

<template>
  <div class="auth-screen">
    <AuthBackdrop />

    <el-card class="auth-card auth-card--tactical" shadow="never">
      <div class="auth-brand">
        <span class="auth-mark">
          <img src="/favicon.svg" class="auth-mark-img" alt="Keyrealm" />
        </span>
        <div class="auth-kicker"><span class="auth-dot" aria-hidden="true" />VAULT SEALED</div>
        <h1>解锁保险库</h1>
        <p class="auth-subtitle">保险库已锁定，请输入主密码解锁</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="主密码" prop="password">
          <el-input
            ref="passwordRef"
            v-model="form.password"
            type="password"
            size="large"
            show-password
            placeholder="输入主密码"
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <el-alert v-if="errorMessage" type="error" :closable="false" show-icon class="auth-alert">
        <template #title>{{ errorMessage }}</template>
      </el-alert>

      <el-button
        type="primary"
        size="large"
        class="auth-submit"
        :loading="submitting"
        :icon="UnlockIcon"
        @click="submit"
      >
        解锁
      </el-button>

      <p class="auth-foot">
        <kbd>Enter</kbd> 快速解锁
      </p>

      <p v-if="mockHint" class="mock-hint">{{ mockHint }}</p>
    </el-card>
  </div>
</template>

<style scoped>
/* 登录/解锁页是「无外壳」的全屏页：背景由 AuthBackdrop 静态铺开。 */
.auth-screen {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  overflow: hidden;
  background: transparent;
}

/* 同时挂 .el-card 是为了压过 Element Plus 的 .el-card.is-never-shadow（同为 2 类选择器，
   加上 scoped 的属性选择器后本规则胜出），不必用 !important。
   --el-card-padding 归零，卡片内边距统一由这里控制，免得和基座的 18/20px 叠加。 */
.auth-card.el-card {
  --el-card-padding: 0;
  position: relative;
  z-index: 1;
  width: 420px;
  padding: 28px 28px 24px;
  box-shadow: var(--v-shadow-lg);
  /* 不加 both：填充模式会留下末帧 transform，使本卡片成为 position: fixed 后代的包含块（见 base.css 同名注释） */
  animation: v-fade-up 0.5s var(--v-ease);
}

/* ── 品牌区 ─────────────────────────────────────────────────────── */

.auth-brand {
  text-align: center;
  margin-bottom: 22px;
}

/* 描边直接做在容器上：图标 56px + 6px 留白 + 2px 描边，
   外圆角 17px = 图标自身圆角 9px + 留白 6px + 描边 2px，三者同心。
   不要再用绝对定位的兄弟元素画环——负 inset 叠上图片的 filter 阴影后，
   部分 WebView 内核会按层叠上下文把顶部那段描边绘制到阴影之下，看起来像缺了一条边。 */
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

/* ── 表单与按钮 ─────────────────────────────────────────────────── */

.auth-alert {
  margin-bottom: 14px;
}

.auth-submit {
  width: 100%;
  margin-top: 4px;
}

.auth-foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 14px 0 0;
  font-size: 12px;
  color: var(--v-text-3);
}

.auth-foot kbd {
  padding: 1px 5px;
  border: 1px solid var(--v-border-strong);
  border-bottom-width: 2px;
  border-radius: 5px;
  background: var(--v-bg-sunken);
  font-family: inherit;
  font-size: 11px;
  color: var(--v-text-2);
}

.mock-hint {
  margin: 10px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--v-text-3);
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
