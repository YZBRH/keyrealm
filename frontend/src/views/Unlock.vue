<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Unlock as UnlockIcon } from '@element-plus/icons-vue'

import { useAppStore } from '@/stores/app'

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
    <div class="auth-halo" aria-hidden="true" />

    <el-card class="auth-card" shadow="never">
      <div class="auth-brand">
        <span class="auth-mark">
          <img src="/favicon.svg" class="auth-mark-img" alt="Keyrealm" />
        </span>
        <h1 class="v-grad-text">Keyrealm</h1>
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
/* 登录/解锁页是「无外壳」的全屏页：背景完全透明，让 App.vue 铺的极光透上来。 */
.auth-screen {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  overflow: hidden;
  background: transparent;
}

/* 卡背后两团柔光（品牌色 + 强调色），把视线收拢到卡片上。
   颜色走 --v-halo-* 令牌，主题可以换色或整体压淡 —— paper 的主色近乎黑，
   直接铺开只会在米白纸上留一圈灰印。

   尺寸必须明显大于卡片，这点踩过坑：渐变在 closest-side 的 72% 处就到透明，
   660px 时半径 330px、透明边界约 237px，恰好压在卡片边缘（半宽 230px）上，
   于是光晕几乎全被卡片盖住，只剩轮廓外一圈极窄的灰边，看着像块脏底板。
   放到 900px 后透明边界约 324px，渐隐带拉到 90px 上下，才散得开。 */
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
  .auth-card,
  .auth-halo,
  .auth-ring {
    animation: none;
  }
}
</style>
