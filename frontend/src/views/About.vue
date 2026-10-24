<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '@/api'
import { useAppStore } from '@/stores/app'
import { useVaultStore } from '@/stores/vault'

// 项目主页：点击后交给后端用**系统浏览器**打开（pywebview 内直接跳转会顶掉应用界面）
const PROJECT_URL = 'https://github.com/YZBRH/keyrealm'

// 版本号与「构建版本」（commit 短 hash）都从**后端**取——前端不再写死，
// 免得界面显示的和正在跑的 exe 对不上。格式：1.0.0 (fe15e3b)
const versionInfo = ref({ display: '—', schemaVersion: '' })

const appStore = useAppStore()
const vaultStore = useVaultStore()

const isWebMode = computed(
  () => typeof window !== 'undefined' && !!(window.__KEYREALM_WEB__ || {}).token,
)
const isDesktop = computed(() => !isWebMode.value && typeof window !== 'undefined' && !!window.pywebview)
const runtimeLabel = computed(() => {
  if (isWebMode.value) return 'Web 模式（浏览器）'
  return isDesktop.value ? '桌面端（pywebview）' : '浏览器预览（开发模式）'
})
const categoryCount = computed(
  () => new Set(vaultStore.plugins.map((p) => p.category).filter(Boolean)).size,
)

async function openProject() {
  try {
    await api.openExternalUrl(PROJECT_URL)
  } catch (err) {
    ElMessage.error(err.message || '打开链接失败')
  }
}

onMounted(() => {
  if (!vaultStore.plugins.length) vaultStore.fetchPlugins().catch(() => {})
  api.getVersion().then((v) => {
    versionInfo.value = {
      display: (v && v.display) || '—',
      schemaVersion: (v && v.schema_version) || '',
    }
  }).catch(() => {})
})
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">关于</h2>
    </div>

    <el-card shadow="never">
      <div class="about-brand">
        <img src="/favicon.svg" alt="Keyrealm" class="about-logo" />
        <div>
          <div class="about-name">Keyrealm</div>
          <div class="about-sub text-muted">本地凭据保险库 · 版本 v{{ versionInfo.display }}</div>
        </div>
      </div>

      <el-descriptions :column="2" border size="small" class="about-desc">
        <el-descriptions-item label="作者">BR</el-descriptions-item>
        <el-descriptions-item label="项目主页">
          <el-link type="primary" :underline="false" @click="openProject">{{ PROJECT_URL }}</el-link>
        </el-descriptions-item>
        <el-descriptions-item label="版本号">v{{ versionInfo.display }}</el-descriptions-item>
        <el-descriptions-item label="数据库结构版本">v{{ versionInfo.schemaVersion || '—' }}</el-descriptions-item>
        <el-descriptions-item label="运行环境">{{ runtimeLabel }}</el-descriptions-item>
        <el-descriptions-item label="当前状态">
          {{ appStore.unlocked ? '已解锁' : '已锁定' }}
        </el-descriptions-item>
        <el-descriptions-item label="插件">
          {{ vaultStore.plugins.length }} 个 · {{ categoryCount }} 个类别
        </el-descriptions-item>
        <el-descriptions-item label="MCP 服务">
          {{ appStore.tray.mcp_running ? `运行中 · ${appStore.tray.mcp_host}:${appStore.tray.mcp_port}` : '已停止' }}
        </el-descriptions-item>
        <el-descriptions-item label="前端技术栈">
          Vue 3 · Element Plus · Pinia · Vite
        </el-descriptions-item>
        <el-descriptions-item label="后端技术栈">
          Python 3.11+ · {{ isWebMode ? 'Starlette' : 'pywebview' }} · SQLite · cryptography
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<style scoped>
.about-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.about-logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
}

.about-name {
  font-size: 17px;
  font-weight: 700;
}

.about-sub {
  font-size: 12px;
  margin-top: 2px;
}
</style>
