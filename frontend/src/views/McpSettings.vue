<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, VideoPause, VideoPlay } from '@element-plus/icons-vue'

import { api } from '@/api'
import { useMcpStore } from '@/stores/mcp'
import { useAppStore } from '@/stores/app'
import { copyText } from '@/utils/clipboard'
import TokenDialog from '@/components/TokenDialog.vue'

const mcpStore = useMcpStore()
const appStore = useAppStore()

// 轮换结果（明文）展示弹窗
const revealDialog = ref(false)
const revealedToken = ref(null)

onMounted(() => {
  mcpStore.refreshAll().catch((err) => ElMessage.error(err.message || '加载 MCP 状态失败'))
  loadListen().catch(() => {})
})

const serverUrl = computed(() => mcpStore.serverUrl)

// ── 监听地址 ─────────────────────────────────────────
const listenHost = ref('127.0.0.1')
const listenPort = ref(8765)
const savingListen = ref(false)

async function loadListen() {
  const options = await api.getMcpOptions()
  listenHost.value = (options && options.host) || '127.0.0.1'
  listenPort.value = (options && options.port) || 8765
}

async function saveListen() {
  savingListen.value = true
  try {
    const result = await api.setMcpOptions(listenHost.value, listenPort.value)
    if (result && result.ok === false) {
      ElMessage.error(result.message || '保存失败')
      return
    }
    await mcpStore.fetchStatus()
    ElMessage.success(
      result.restarted
        ? `已在 ${result.host}:${result.port} 重启服务`
        : `监听地址已保存为 ${result.host}:${result.port}（服务未运行，下次启动生效）`,
    )
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    savingListen.value = false
  }
}

async function toggleServer() {
  try {
    if (mcpStore.running) {
      await mcpStore.stop()
      ElMessage.success('MCP 服务已停止')
    } else {
      const result = await mcpStore.start()
      if (result && result.ok === false) {
        ElMessage.error(result.message || '启动失败')
        return
      }
      ElMessage.success(`MCP 服务已启动：${result.url}`)
    }
    await appStore.refreshTray()
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  }
}

// 高危工具：开启前需二次确认。
// 代理那几个也在列：**代理决定了「凭据的验证流量发到哪」**，被 Agent 改掉就等于把
// 全部跟随全局的凭据出口交给它指定的地址（改/删全局默认还会让所有凭据静默换出口）。
const SENSITIVE_TOOLS = new Set([
  'vault_get_secret',
  'vault_create_credential',
  'vault_update_credential',
  'vault_delete_credential',
  'vault_export_backup',
  'vault_import_credentials',
  'vault_create_proxy',
  'vault_update_proxy',
  'vault_delete_proxy',
  'vault_set_global_proxy',
  'vault_test_proxy',
])

async function togglePermission(toolName, enabled) {
  // 开启高危工具时要求确认，避免误点导致 Agent 获得敏感能力
  if (enabled && SENSITIVE_TOOLS.has(toolName)) {
    try {
      await ElMessageBox.confirm(
        `「${toolName}」是高危工具，开启后持有有效 Token 的 Agent 均可调用。确认开启？`,
        '开启高危工具',
        { type: 'warning', confirmButtonText: '确认开启', cancelButtonText: '取消' },
      )
    } catch {
      // 用户取消：回滚开关显示
      await mcpStore.fetchPermissions()
      return
    }
  }
  try {
    await mcpStore.setPermission(toolName, enabled)
    ElMessage.success(enabled ? `已开启 ${toolName}` : `已关闭 ${toolName}`)
  } catch (err) {
    ElMessage.error(err.message || '设置失败')
    await mcpStore.fetchPermissions()
  }
}

async function handleRotate() {
  try {
    await ElMessageBox.confirm(
      '轮换将立即作废旧 Token，使用旧 Token 的客户端需更新为新 Token。确认轮换？',
      '轮换 Token',
      { type: 'warning', confirmButtonText: '确认轮换', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await mcpStore.rotateToken()
    revealedToken.value = result
    revealDialog.value = true
    await appStore.refreshTray()
  } catch (err) {
    ElMessage.error(err.message || '轮换失败')
  }
}

async function copyUrl() {
  const ok = await copyText(serverUrl.value)
  if (ok) ElMessage.success('服务地址已复制')
}

async function copyClientConfig() {
  const ok = await copyText(mcpStore.clientConfig())
  if (ok) ElMessage.success('客户端配置已复制')
}
</script>

<template>
  <div class="page">
    <!-- 服务状态 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>MCP 服务</span>
          <el-tag :type="mcpStore.running ? 'success' : 'danger'" effect="dark" round>
            {{ mcpStore.running ? '运行中' : '已停止' }}
          </el-tag>
        </div>
      </template>

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="服务地址">
          <code class="mono">{{ mcpStore.running ? serverUrl : '—' }}</code>
          <el-button v-if="mcpStore.running" text size="small" @click="copyUrl">复制</el-button>
        </el-descriptions-item>
        <el-descriptions-item label="传输协议">SSE (Server-Sent Events)</el-descriptions-item>
        <el-descriptions-item label="鉴权方式">Bearer Token</el-descriptions-item>
      </el-descriptions>

      <!-- 监听地址：改完保存即生效；服务在运行则按新地址重启 -->
      <div class="listen-row">
        <span class="token-label">监听地址</span>
        <el-input v-model="listenHost" class="listen-host" placeholder="127.0.0.1" />
        <span class="listen-colon">:</span>
        <el-input-number
          v-model="listenPort"
          :min="1"
          :max="65535"
          :controls="false"
          class="listen-port"
        />
        <el-button
          type="primary"
          size="small"
          :loading="savingListen"
          @click="saveListen"
        >保存并重启</el-button>
        <span class="text-muted token-hint">
          默认 127.0.0.1:8765；端口被占用会报错且不自动更换
        </span>
      </div>

      <div class="token-row">
        <span class="token-label">访问 Token</span>
        <code class="mono">{{ mcpStore.token ? mcpStore.token.token_prefix + '…' : '—' }}</code>
        <span class="text-muted token-hint">明文仅在轮换时展示一次，数据库只保存哈希与前缀</span>
        <el-button size="small" :icon="Refresh" @click="handleRotate">轮换 Token</el-button>
      </div>

      <div class="server-actions">
        <el-button
          :type="mcpStore.running ? 'danger' : 'success'"
          :icon="mcpStore.running ? VideoPause : VideoPlay"
          :loading="mcpStore.busy"
          @click="toggleServer"
        >
          {{ mcpStore.running ? '停止服务' : '启动服务' }}
        </el-button>
        <el-button text @click="copyClientConfig">复制客户端配置</el-button>
      </div>
      <div class="text-muted server-hint">开启后下次启动会自动开启；点「停止服务」则保持关闭。</div>

      <el-alert type="info" :closable="false" class="mt-12">
        <template #title>
          客户端配置示例（Claude Desktop / Cursor）：将下方 JSON 合并到 MCP 配置文件中，并替换 Token。
        </template>
        <pre class="config-pre">{{ mcpStore.clientConfig() }}</pre>
      </el-alert>
    </el-card>

    <!-- 工具权限 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>工具权限（全局开关）</span>
        </div>
      </template>

      <el-table :data="mcpStore.permissions" size="small" stripe>
        <el-table-column prop="tool_name" label="工具名" min-width="200">
          <template #default="{ row }">
            <code class="mono">{{ row.tool_name }}</code>
            <el-tag
              v-if="SENSITIVE_TOOLS.has(row.tool_name)"
              type="danger"
              size="small"
              effect="plain"
              class="ml-6"
            >
              高危
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="240" />
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.enabled"
              size="small"
              @change="(val) => togglePermission(row.tool_name, val)"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- Token 轮换结果（明文仅展示一次） -->
    <TokenDialog v-model="revealDialog" :token="revealedToken" />
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.server-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  flex-wrap: wrap;
}

.server-hint {
  margin-top: 8px;
  font-size: 12px;
}

.config-pre {
  margin: 8px 0 0;
  padding: 10px;
  background: var(--el-fill-color-dark);
  border-radius: 6px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-x: auto;
  white-space: pre;
}

.mt-12 {
  margin-top: 12px;
}

.ml-6 {
  margin-left: 6px;
}

.token-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.token-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.token-hint {
  font-size: 12px;
}

.token-row .el-button {
  margin-left: auto;
}

.listen-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 16px;
}

.listen-row .token-label {
  min-width: 60px;
}

.listen-host {
  width: 170px;
}

.listen-port {
  width: 96px;
}

.listen-colon {
  color: var(--el-text-color-secondary);
}
</style>
