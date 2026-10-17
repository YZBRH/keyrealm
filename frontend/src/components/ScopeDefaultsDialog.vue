<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '@/api'
import { matchPresets, normalizeOptions, visibleFields } from '@/utils/format'
import ProxySelect from '@/components/ProxySelect.vue'
import TlsSelect from '@/components/TlsSelect.vue'

/**
 * 类别 / 插件的「默认设置」对话框（两处入口共用：凭据目录右键、插件管理页）。
 *
 * 框架只负责存与继承；这里渲染哪些字段由后端按 manifest 的可预设判定给出
 * （非敏感 + 未标 `preset: false`），敏感字段永远不出现。
 */
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  scope: { type: String, default: 'plugin' },      // 'category' | 'plugin'
  scopeKey: { type: String, default: '' },         // 类别目录名 / credential_type
  credentialType: { type: String, default: '' },   // 仅 plugin 作用域需要（用于取字段）
  title: { type: String, default: '默认设置' },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const loading = ref(false)
const saving = ref(false)
const fields = ref([])
const values = ref({})
const proxyMode = ref('global')
const proxyId = ref(null)
// 该作用域下有没有走代理的插件（没有就不显示代理项——SMTP 这类插件本就不走 HTTP 代理）
const usesProxy = ref(true)
// TLS 版本（空 = 不在此层指定）
const tlsVersion = ref('')
const tlsVersions = ref(['auto', '1.0', '1.1', '1.2', '1.3'])

// 条件显隐：预设表单里也按 show_if 只显示当前模式需要的字段
const visibleFieldList = computed(() => visibleFields(fields.value, values.value))

/** 端点字段的候选（manifest 的 presets）；输入本身不受限，候选只是补全。 */
function fetchPresets(field, query, callback) {
  callback(matchPresets(field, query))
}

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

const scopeLabel = computed(() => (props.scope === 'category' ? '类别' : '插件'))

async function load() {
  if (!props.scopeKey) return
  loading.value = true
  try {
    const res = await api.getScopeOptions(props.scope, props.scopeKey, props.credentialType)
    fields.value = (res && res.fields) || []
    const raw = { ...((res && res.values) || {}) }
    usesProxy.value = !res || res.uses_proxy !== false
    if (res && Array.isArray(res.tls_versions) && res.tls_versions.length) {
      tlsVersions.value = res.tls_versions
    }
    proxyMode.value = raw._proxy_mode || 'global'
    proxyId.value = raw._proxy_id ?? null
    tlsVersion.value = raw._tls_version || ''
    delete raw._proxy_mode
    delete raw._proxy_id
    delete raw._tls_version
    values.value = raw
  } catch (err) {
    ElMessage.error(err.message || '加载默认设置失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) load()
  },
  { immediate: true },
)

/** 只展示「已设过值」的字段名，避免把没设过的空字段当成"设成了空" */
function hasValue(field) {
  const value = values.value[field.key]
  return value !== undefined && value !== null && value !== ''
}

async function save() {
  saving.value = true
  try {
    const payload = { ...values.value }
    payload._proxy_mode = proxyMode.value || 'global'
    if (proxyMode.value === 'proxy' && proxyId.value) payload._proxy_id = proxyId.value
    // TLS 版本：空 = 不在此层指定，不带该键
    if (tlsVersion.value) payload._tls_version = tlsVersion.value
    const res = await api.setScopeOptions(props.scope, props.scopeKey, payload, props.credentialType)
    if (res && res.ok === false) {
      ElMessage.error(res.message || '保存失败')
      return
    }
    ElMessage.success(`${scopeLabel.value}默认设置已保存`)
    emit('saved')
    visible.value = false
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function clearAll() {
  try {
    await ElMessageBox.confirm(
      '清空后，新建凭据将不再带这些默认值。确定吗？',
      '清空默认设置',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.clearScopeOptions(props.scope, props.scopeKey)
    values.value = {}
    proxyMode.value = 'global'
    proxyId.value = null
    tlsVersion.value = ''
    ElMessage.success('已清空')
    emit('saved')
  } catch (err) {
    ElMessage.error(err.message || '清空失败')
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="560px" :close-on-click-modal="false">
    <div v-loading="loading">
      <el-alert type="info" :closable="false" show-icon class="mb-12">
        <template #title>
          这里设的是<strong>新建凭据时的预填值</strong>，不会改动已有凭据。
        </template>
        插件级覆盖类别级；敏感字段（密钥、令牌）不参与默认设置。
      </el-alert>

      <el-form label-position="top">
        <el-form-item v-if="usesProxy" label="使用代理">
          <ProxySelect
            v-model:mode="proxyMode"
            v-model:proxyId="proxyId"
            follow-label="不在此层指定"
          />
          <div class="field-hint">
            在这层指定出口，或「不在此层指定」让其继续往上找（插件 → 类别 → 全局）。
            新建凭据时按 凭据 &gt; 插件 &gt; 类别 &gt; 全局 的优先级解析
          </div>
        </el-form-item>

        <el-form-item v-if="usesProxy" label="TLS 版本">
          <TlsSelect
            v-model="tlsVersion"
            :versions="tlsVersions"
            follow-label="不在此层指定"
          />
          <div class="field-hint">
            在这层固定 TLS 版本，或「不在此层指定」让其继续往上找（插件 → 类别 → 全局）。
          </div>
        </el-form-item>

        <el-form-item v-for="field in visibleFieldList" :key="field.key" :label="field.label">
          <el-switch v-if="field.type === 'checkbox'" v-model="values[field.key]" />
          <el-input
            v-else-if="field.type === 'textarea'"
            v-model="values[field.key]"
            type="textarea"
            :rows="2"
            :placeholder="field.placeholder || ''"
          />
          <el-select
            v-else-if="field.type === 'select'"
            v-model="values[field.key]"
            :placeholder="field.placeholder || '请选择'"
            clearable
            class="full-width"
          >
            <el-option
              v-for="option in normalizeOptions(field.options)"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <el-autocomplete
            v-else-if="field.presets?.length"
            v-model="values[field.key]"
            :fetch-suggestions="(query, cb) => fetchPresets(field, query, cb)"
            :trigger-on-focus="true"
            :placeholder="field.placeholder || ''"
            clearable
            class="full-width"
          >
            <template #default="{ item }">
              <div style="display: flex; flex-direction: column; line-height: 1.3">
                <span>{{ item.label }}</span>
                <span style="font-size: 12px; color: var(--el-text-color-secondary); word-break: break-all">{{ item.value }}</span>
              </div>
            </template>
          </el-autocomplete>
          <el-input
            v-else
            v-model="values[field.key]"
            :placeholder="field.placeholder || ''"
            clearable
          />
          <div v-if="field.hint" class="field-hint">{{ field.hint }}</div>
          <div v-else-if="!hasValue(field)" class="field-hint">留空 = 不预设（走插件自身的默认）</div>
        </el-form-item>

        <el-empty
          v-if="!fields.length"
          description="该作用域没有可预设的字段"
          :image-size="60"
        />
      </el-form>
    </div>

    <template #footer>
      <el-button :disabled="loading" @click="clearAll">清空</el-button>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.mb-12 {
  margin-bottom: 12px;
}

.full-width {
  width: 100%;
}
</style>
