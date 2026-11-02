<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import api from '@/api'

const props = defineProps({
  credentialId: { type: Number, required: true },
  credentialName: { type: String, default: '' },
})

const PREVIEW_LIMIT = 256 * 1024
const DOWNLOAD_LIMIT = 20 * 1024 * 1024
const UPLOAD_LIMIT = 20 * 1024 * 1024
const isWeb = !!(window.__KEYREALM_WEB__ || {}).token

const sessionId = ref('')
const summary = ref('')
const path = ref('')
const home = ref('')
const parent = ref('')
const entries = ref([])
const truncated = ref(false)
const loading = ref(false)
const opening = ref(false)
const uploading = ref(false)
const error = ref('')
const preview = ref({
  visible: false,
  name: '',
  path: '',
  kind: '',
  text: '',
  dataUrl: '',
  mime: '',
  size: 0,
  truncated: false,
})
const previewLoading = ref(false)
const fileInput = ref(null)

function b64ToBytes(value) {
  const binary = atob(value || '')
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

function bytesToB64(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

function formatSize(size) {
  const value = Number(size) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatTime(seconds) {
  const value = Number(seconds) || 0
  if (!value) return ''
  return new Date(value * 1000).toLocaleString()
}

function decodeText(bytes) {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    try {
      return new TextDecoder('gb18030').decode(bytes)
    } catch {
      return new TextDecoder('utf-8').decode(bytes)
    }
  }
}

async function closeSession() {
  const id = sessionId.value
  sessionId.value = ''
  if (!id) return
  try {
    await api.sshClose(id)
  } catch { /* 断开失败无需打扰用户 */ }
}

async function open() {
  if (opening.value) return
  await closeSession()
  opening.value = true
  error.value = ''
  entries.value = []
  preview.value.visible = false
  try {
    const res = await api.sshFilesOpen(props.credentialId)
    if (!res || res.ok === false) {
      error.value = (res && res.error) || '打开文件会话失败'
      return
    }
    sessionId.value = res.session_id
    summary.value = res.summary || props.credentialName || ''
    home.value = ''
    await load('')
  } catch (err) {
    error.value = err.message || '打开文件会话失败'
  } finally {
    opening.value = false
  }
}

async function load(target) {
  if (!sessionId.value) return
  loading.value = true
  error.value = ''
  try {
    const res = await api.sshFilesList(sessionId.value, target || '')
    if (!res || res.ok === false) {
      error.value = (res && res.error) || '读取目录失败'
      return
    }
    entries.value = Array.isArray(res.entries) ? res.entries : []
    path.value = res.path || target || ''
    home.value = res.home || home.value
    parent.value = res.parent || ''
    truncated.value = !!res.truncated
    preview.value.visible = false
  } catch (err) {
    error.value = err.message || '读取目录失败'
  } finally {
    loading.value = false
  }
}

function refresh() {
  load(path.value)
}

function goUp() {
  if (!parent.value || parent.value === path.value) return
  load(parent.value)
}

function onRowClick(row) {
  if (!row) return
  if (row.is_dir) {
    load(row.path)
    return
  }
  openPreview(row)
}

function closePreview() {
  preview.value.visible = false
}

async function openPreview(row) {
  if (!sessionId.value) return
  preview.value = {
    visible: true, name: row.name, path: row.path, kind: '', text: '',
    dataUrl: '', mime: '', size: row.size || 0, truncated: false,
  }
  previewLoading.value = true
  try {
    const res = await api.sshFilesRead(sessionId.value, row.path, PREVIEW_LIMIT)
    if (!res || res.ok === false) {
      ElMessage.error((res && res.error) || '读取文件失败')
      preview.value.visible = false
      return
    }
    const bytes = b64ToBytes(res.content_b64)
    const kind = res.kind === 'image' && res.truncated ? 'binary' : (res.kind || 'binary')
    preview.value = {
      visible: true,
      name: res.name || row.name,
      path: res.path || row.path,
      kind,
      text: kind === 'text' ? decodeText(bytes) : '',
      dataUrl: kind === 'image'
        ? `data:${res.mime || 'application/octet-stream'};base64,${res.content_b64}`
        : '',
      mime: res.mime || '',
      size: res.size || 0,
      truncated: !!res.truncated,
    }
  } catch (err) {
    ElMessage.error(err.message || '读取文件失败')
    preview.value.visible = false
  } finally {
    previewLoading.value = false
  }
}

async function download(row) {
  const target = row && row.path
  if (!target || !sessionId.value) return
  const name = (row && row.name) || 'download'
  if (!isWeb) {
    try {
      const picked = await api.pickSavePath(name)
      const localPath = (picked && picked.path) || ''
      if (!localPath) return
      const res = await api.sshFilesDownload(sessionId.value, target, localPath)
      if (!res || res.ok === false) {
        ElMessage.error((res && res.error) || '下载失败')
        return
      }
      ElMessage.success(`已保存到 ${res.path}`)
    } catch (err) {
      ElMessage.error(err.message || '下载失败')
    }
    return
  }
  try {
    const res = await api.sshFilesRead(sessionId.value, target, DOWNLOAD_LIMIT)
    if (!res || res.ok === false) {
      ElMessage.error((res && res.error) || '下载失败')
      return
    }
    if (res.truncated) {
      ElMessage.error(`文件超过下载上限（${DOWNLOAD_LIMIT / 1024 / 1024}MB）`)
      return
    }
    const bytes = b64ToBytes(res.content_b64)
    const blob = new Blob([bytes], { type: res.mime || 'application/octet-stream' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = res.name || 'download'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 5000)
  } catch (err) {
    ElMessage.error(err.message || '下载失败')
  }
}

function pickUpload() {
  fileInput.value?.click()
}

async function onFileChange(event) {
  const file = event.target.files && event.target.files[0]
  event.target.value = ''
  if (!file || !sessionId.value) return
  if (file.size > UPLOAD_LIMIT) {
    ElMessage.error(`文件超过上传上限（${UPLOAD_LIMIT / 1024 / 1024}MB）`)
    return
  }
  const exists = entries.value.some((item) => !item.is_dir && item.name === file.name)
  if (exists) {
    try {
      await ElMessageBox.confirm(`当前目录已存在「${file.name}」，是否覆盖？`, '确认覆盖', {
        type: 'warning',
        confirmButtonText: '覆盖',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
  }
  uploading.value = true
  try {
    const buffer = await file.arrayBuffer()
    const res = await api.sshFilesUpload(
      sessionId.value, path.value, file.name, bytesToB64(buffer),
    )
    if (!res || res.ok === false) {
      ElMessage.error((res && res.error) || '上传失败')
      return
    }
    ElMessage.success(`已上传 ${file.name}`)
    await load(path.value)
  } catch (err) {
    ElMessage.error(err.message || '上传失败')
  } finally {
    uploading.value = false
  }
}

onMounted(open)
onBeforeUnmount(closeSession)

defineExpose({ open })
</script>

<template>
  <div class="ssh-files">
    <div class="sf-toolbar">
      <el-button size="small" :disabled="!parent || parent === path" @click="goUp">上一级</el-button>
      <el-input
        v-model="path"
        class="sf-path"
        size="small"
        placeholder="/home/user"
        @keyup.enter="load(path)"
      />
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      <el-button size="small" type="primary" :disabled="!sessionId" :loading="uploading" @click="pickUpload">
        上传
      </el-button>
      <input ref="fileInput" type="file" class="sf-file-input" @change="onFileChange" />
      <span class="sf-meta">{{ summary }}</span>
    </div>

    <div v-if="error" class="sf-error">
      <span>{{ error }}</span>
      <el-button size="small" :loading="opening" @click="open">重试</el-button>
    </div>

    <div class="sf-body">
      <div class="sf-list">
        <el-table
          v-loading="loading"
          :data="entries"
          size="small"
          height="360"
          empty-text="空目录"
          @row-click="onRowClick"
        >
          <el-table-column label="名称" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="sf-name" :class="{ 'is-dir': row.is_dir }">
                {{ row.is_dir ? '📁' : '📄' }} {{ row.name }}
                <span v-if="row.is_link" class="sf-link">↗</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="大小" width="100" align="right">
            <template #default="{ row }">{{ row.is_dir ? '-' : formatSize(row.size) }}</template>
          </el-table-column>
          <el-table-column label="修改时间" width="170">
            <template #default="{ row }">{{ formatTime(row.mtime) }}</template>
          </el-table-column>
        </el-table>
        <div v-if="truncated" class="sf-hint">目录条目过多，仅显示前 2000 项。</div>
      </div>

      <div v-if="preview.visible" class="sf-preview">
        <div class="sf-preview-head">
          <span class="sf-preview-name" :title="preview.path">{{ preview.name }}</span>
          <el-button size="small" text @click="closePreview">关闭</el-button>
        </div>
        <div class="sf-preview-meta">
          {{ formatSize(preview.size) }}<span v-if="preview.mime"> · {{ preview.mime }}</span>
        </div>
        <div v-if="previewLoading" class="sf-preview-empty">加载中…</div>
        <pre v-else-if="preview.kind === 'text'" class="sf-preview-text">{{ preview.text }}</pre>
        <img
          v-else-if="preview.kind === 'image'"
          class="sf-preview-image"
          :src="preview.dataUrl"
          :alt="preview.name"
        />
        <div v-else class="sf-preview-empty">该文件为二进制或暂不支持预览。</div>
        <div v-if="preview.truncated" class="sf-hint">仅显示前 256KB，下载可获取完整文件。</div>
        <div class="sf-preview-actions">
          <el-button size="small" type="primary" @click="download(preview)">下载</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ssh-files {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sf-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.sf-path {
  flex: 1;
  min-width: 220px;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
}
.sf-file-input {
  display: none;
}
.sf-meta {
  font-size: 12px;
  color: var(--v-text-3);
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
}
.sf-error {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid rgba(248, 113, 113, 0.35);
  border-radius: 8px;
  background: rgba(248, 113, 113, 0.08);
  color: #fca5a5;
  font-size: 12px;
}
.sf-body {
  display: flex;
  gap: 12px;
  align-items: stretch;
  min-height: 360px;
}
.sf-list {
  flex: 1;
  min-width: 0;
}
.sf-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--v-text-3);
}
.sf-name {
  cursor: pointer;
}
.sf-name.is-dir {
  color: #a3e635;
}
.sf-link {
  color: var(--v-text-3);
  font-size: 11px;
}
.sf-preview {
  width: 42%;
  min-width: 280px;
  max-width: 480px;
  display: flex;
  flex-direction: column;
  padding: 10px;
  border: 1px solid rgba(163, 230, 53, 0.16);
  border-radius: 10px;
  background: #05070a;
}
.sf-preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.sf-preview-name {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sf-preview-meta {
  margin: 4px 0 8px;
  font-size: 11px;
  color: #71717a;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
}
.sf-preview-text {
  flex: 1;
  margin: 0;
  padding: 8px;
  overflow: auto;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.03);
  color: #d4d4d8;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  user-select: text;
  max-height: 300px;
}
.sf-preview-image {
  max-width: 100%;
  max-height: 300px;
  object-fit: contain;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.03);
}
.sf-preview-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--v-text-3);
  font-size: 12px;
}
.sf-preview-actions {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}
</style>
