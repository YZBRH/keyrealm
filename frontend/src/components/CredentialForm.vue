<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useVaultStore } from '@/stores/vault'
import { api } from '@/api'
import { matchPresets, normalizeOptions, visibleFields } from '@/utils/format'
import ProxySelect from '@/components/ProxySelect.vue'
import TlsSelect from '@/components/TlsSelect.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  credential: { type: Object, default: null },
  defaultGroupId: { type: [Number, String], default: null },
  defaultType: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const vaultStore = useVaultStore()

const formRef = ref(null)
const submitting = ref(false)

const form = reactive({
  name: '',
  credential_type: '',
  group_id: null,
  auto_verify: true,
  data: {},
})

const isEdit = computed(() => !!(props.credential && props.credential.id))

// **仅已启用**的插件：只有「新建」路径该用它（挑首个可选类型、切换类型时套默认值）。
// 渲染 schema 不要用它——见下面 currentPlugin 的说明。
const plugins = computed(() => vaultStore.enabledPlugins)

const groupOptions = computed(() => vaultStore.groupOptions)

// 类型选项按插件类别分组（el-option-group），下拉支持关键词搜索
const typeGroups = computed(() => vaultStore.typeGroups)

// ⚠️ 这里必须用 pluginByType（**含已禁用插件**），不能用 plugins（= enabledPlugins，仅已启用）。
// 插件被禁用只是关掉功能闸门，插件的数据形状（fields_schema）没有任何变化，编辑一条
// 「插件已禁用」的凭据时照样要按 schema 把字段渲染出来。曾用 enabledPlugins 查，结果：
// 找不到插件 → currentPlugin 为 null → schema 为空 → 表单一个字段都不渲染，
// 数据明明已经装进 form.data 了却「看不见凭据值」。
// 新建路径不受影响：类型下拉由 typeGroups（enabledPlugins 派生）提供，选不到禁用插件。
const currentPlugin = computed(
  () => vaultStore.pluginByType[form.credential_type] || null,
)

const schema = computed(() => (currentPlugin.value && currentPlugin.value.fields_schema) || [])

// 条件显隐（manifest 的 show_if）：渲染与提交都用这份，保证「看到什么就提交什么」。
// 被隐藏字段的值不再提交——否则切模式后另一组的密钥会一直留在库里。
const visibleSchema = computed(() => visibleFields(schema.value, form.data))

/** 端点字段的候选（manifest 的 presets）；输入本身不受限，候选只是补全。 */
function fetchPresets(field, query, callback) {
  callback(matchPresets(field, query))
}

const rules = computed(() => {
  const result = {
    name: [
      { required: true, message: '请输入凭据名称', trigger: 'blur' },
      { min: 1, max: 100, message: '名称长度需在 1-100 个字符之间', trigger: 'blur' },
    ],
    credential_type: [{ required: true, message: '请选择凭据类型', trigger: 'change' }],
  }
  schema.value.forEach((field) => {
    if (!field.required) return
    const trigger = field.type === 'select' || field.type === 'checkbox' ? 'change' : 'blur'
    result[`data.${field.key}`] = [
      { required: true, message: `请填写${field.label}`, trigger },
    ]
  })
  return result
})

function coerce(field, value) {
  if (field.type === 'checkbox') return value === true || value === 'true'
  if (field.type === 'number') {
    if (value === '' || value === null || value === undefined) return null
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return value === null || value === undefined ? '' : value
}

function schemaDefaults(list, source = {}) {
  const data = {}
  list.forEach((field) => {
    if (Object.prototype.hasOwnProperty.call(source, field.key)) {
      data[field.key] = coerce(field, source[field.key])
      return
    }
    if (field.type === 'checkbox') {
      data[field.key] = field.default === true || field.default === 'true'
      return
    }
    if (field.type === 'number') {
      data[field.key] = field.default === null || field.default === undefined ? null : Number(field.default)
      return
    }
    data[field.key] = field.default === null || field.default === undefined ? '' : field.default
  })
  return data
}

// ── 代理选择 + 类别/插件默认设置 ──────────────────────────

const proxyMode = ref('global')
const proxyId = ref(null)
// 该插件的请求是否受框架的「代理 / TLS 版本」管辖（决定要不要显示这两项）。
// 注意：邮箱虽然用标准库 smtplib，但它读 plugin_sdk.proxy() 穿 SOCKS / HTTP CONNECT 隧道、
// 也用 plugin_sdk.ssl_context() 取框架的 TLS 版本——**两项都适用**，故照常显示。
// 只有真正无出网请求的插件才会经后端 USES_PROXY=False 把它们隐藏（目前仓库里没有这类插件）。
const usesProxy = ref(true)
// 后端按「子模块预设 > 上层模块预设 > 全局」算好的实际出口，用于下拉里那项的括注
const inheritedProxy = ref('')

// ── TLS 版本（空串 = 跟随上层设置） ──────────────────────
const tlsVersion = ref('')
const inheritedTls = ref('')
const tlsVersions = ref(['auto', '1.0', '1.1', '1.2', '1.3'])

/**
 * 拉取该插件的默认设置。
 * `apply=true`（新建）时才把默认值注入表单——编辑时套默认会把用户显式清空的字段复活。
 */
async function loadDefaults(credentialType, { apply = false } = {}) {
  if (!credentialType) return
  try {
    const res = await api.getCredentialDefaults(credentialType)
    usesProxy.value = !res || res.uses_proxy !== false
    inheritedProxy.value = (res && res.inherited_proxy) || ''
    inheritedTls.value = (res && res.inherited_tls) || ''
    if (res && Array.isArray(res.tls_versions) && res.tls_versions.length) {
      tlsVersions.value = res.tls_versions
    }
    const values = (res && res.values) || {}
    if (apply) {
      Object.entries(values).forEach(([key, value]) => {
        if (key.startsWith('_')) return          // 保留键单独处理
        const current = form.data[key]
        if (current === undefined || current === null || current === '') form.data[key] = value
      })
      // **代理不从默认设置预填**：预填等于把它烤成「条目设置」，从此永远压着插件/类别预设，
      // 以后改预设对已有凭据就不生效了。凭据的默认状态就该是「跟随上层设置」，
      // 由后端在验证时按 凭据 > 插件预设 > 类别预设 > 全局 实时解析。
    }
  } catch {
    usesProxy.value = true
  }
}

async function resetForm() {
  const existing = props.credential
  const targetId = existing && existing.id ? existing.id : null

  if (targetId) {
    form.name = existing.name || ''
    form.credential_type = existing.credential_type || ''
    form.group_id = existing.group_id ?? null
    form.auto_verify = true
    const plugin = vaultStore.pluginByType[existing.credential_type]
    const schema = (plugin && plugin.fields_schema) || []
    // 先用非敏感字段同步预填，再拉取完整明文补全敏感字段（避免空值覆盖密钥）
    form.data = schemaDefaults(schema, existing.data_plain || {})
    const plain = existing.data_plain || {}
    proxyMode.value = plain._proxy_mode || 'global'
    proxyId.value = plain._proxy_id ?? null
    tlsVersion.value = plain._tls_version || ''
    try {
      const full = await api.getCredential(targetId)
      // 防止快速切换时把旧结果写进新表单
      if (props.credential && props.credential.id === targetId && full && full.data) {
        form.data = schemaDefaults(schema, full.data)
        proxyMode.value = full.data._proxy_mode || 'global'
        proxyId.value = full.data._proxy_id ?? null
        tlsVersion.value = full.data._tls_version || ''
      }
    } catch {
      // 拉取失败则回退到 data_plain（非敏感字段）
    }
    // 编辑时不套默认（否则会把用户显式清空的字段复活），只取 uses_proxy 决定要不要显示代理项
    await loadDefaults(existing.credential_type, { apply: false })
  } else {
    const initialType = props.defaultType || (plugins.value[0] && plugins.value[0].credential_type) || ''
    form.name = ''
    form.credential_type = initialType
    form.group_id = props.defaultGroupId ?? null
    form.auto_verify = true
    const plugin = plugins.value.find((item) => item.credential_type === initialType)
    form.data = schemaDefaults((plugin && plugin.fields_schema) || [])
    proxyMode.value = 'global'
    proxyId.value = null
    tlsVersion.value = ''
    await loadDefaults(initialType, { apply: true })
  }
  if (formRef.value) formRef.value.clearValidate()
}

// ── 模型字段（manifest 里 type: "model"）─────────────────
// 候选来自该凭据（或表单里尚未保存的草稿）的模型列表；目标站取不到时就退回自由输入。

const modelOptions = ref([])
const modelsLoading = ref(false)

const hasModelField = computed(() => schema.value.some((f) => f.type === 'model'))

let modelTimer = null

async function loadModelOptions() {
  if (!hasModelField.value || !form.credential_type) return
  modelsLoading.value = true
  try {
    const list = await api.listDraftModels(form.credential_type, { ...form.data })
    modelOptions.value = Array.isArray(list) ? list : []
  } catch {
    modelOptions.value = []
  } finally {
    modelsLoading.value = false
  }
}

/** 输入站点/密钥后再延迟拉取，免得每敲一个字符就打一次目标站 */
function scheduleModelLoad() {
  if (!hasModelField.value) return
  clearTimeout(modelTimer)
  modelTimer = setTimeout(loadModelOptions, 600)
}

watch(
  () => [form.credential_type, form.data.base_url, form.data.api_key],
  () => scheduleModelLoad(),
)

watch(
  () => props.modelValue,
  async (visible) => {
    if (!visible) return
    await resetForm()
    modelOptions.value = []
    loadModelOptions()
  },
  { immediate: true },
)

// 创建模式下切换凭据类型时，按新插件的 fields_schema 重建字段，并重新套用该插件的默认设置。
watch(
  () => form.credential_type,
  async (next, prev) => {
    if (!props.modelValue || isEdit.value) return
    if (!prev || next === prev) return
    const plugin = plugins.value.find((item) => item.credential_type === next)
    form.data = schemaDefaults((plugin && plugin.fields_schema) || [])
    proxyMode.value = 'global'
    proxyId.value = null
    await loadDefaults(next, { apply: true })
    if (formRef.value) formRef.value.clearValidate()
  },
)

function buildData() {
  const data = {}
  // 只提交当前可见的字段：show_if 未命中的字段（另一种模式的字段）不带上，
  // 免得切换模式后旧密钥仍留在库里。后端插件的 validate() 也是按模式校验的。
  visibleSchema.value.forEach((field) => {
    data[field.key] = coerce(field, form.data[field.key])
  })
  // 保留键不在 manifest 字段里，得单独带上：后端按这两个键决定该凭据走哪个代理
  data._proxy_mode = proxyMode.value || 'global'
  if (proxyMode.value === 'proxy' && proxyId.value) data._proxy_id = proxyId.value
  // TLS 版本：空 = 跟随上层，不写键
  if (tlsVersion.value) data._tls_version = tlsVersion.value
  return data
}

function close() {
  emit('update:modelValue', false)
}

/** 创建前重复检测：敏感字段值与既有凭据任一字段值相同即提示。 */
async function confirmIfDuplicate(payload) {
  let matches = []
  try {
    matches = await api.checkDuplicateCredential(payload.credential_type, payload.data)
  } catch {
    return true // 检测失败不阻断保存
  }
  if (!Array.isArray(matches) || !matches.length) return true
  const lines = matches
    .map((item) => `「${item.name}」（${item.credential_type}）`)
    .join('、')
  try {
    await ElMessageBox.confirm(
      `检测到相同凭据已存在：${lines}。确定要继续添加吗？`,
      '重复凭据提醒',
      { type: 'warning', confirmButtonText: '继续添加', cancelButtonText: '取消' },
    )
    return true
  } catch {
    return false
  }
}

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  submitting.value = true
  try {
    const payload = {
      name: form.name.trim(),
      credential_type: form.credential_type,
      group_id: form.group_id ?? null,
      data: buildData(),
      auto_verify: form.auto_verify,
    }

    if (!isEdit.value) {
      const confirmed = await confirmIfDuplicate(payload)
      if (!confirmed) return
    }

    let saved
    if (isEdit.value) {
      saved = await vaultStore.updateCredential(props.credential.id, {
        name: payload.name,
        group_id: payload.group_id,
        data: payload.data,
      })
      if (form.auto_verify) {
        try {
          await vaultStore.verifyCredential(props.credential.id)
        } catch (err) {
          ElMessage.warning(`凭据已保存，但验证失败：${err.message}`)
        }
      }
    } else {
      saved = await vaultStore.createCredential(payload)
    }

    ElMessage.success(isEdit.value ? '凭据已更新' : '凭据已创建')
    emit('saved', saved)
    close()
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="isEdit ? '编辑凭据' : '添加凭据'"
    width="640px"
    top="6vh"
    class="credential-dialog"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="credential-form">
      <el-form-item label="凭据名称" prop="name">
        <el-input v-model="form.name" placeholder="例如：OpenAI 生产账号" clearable maxlength="100" />
      </el-form-item>

      <el-form-item label="服务" prop="credential_type">
        <el-select
          v-model="form.credential_type"
          placeholder="输入关键词搜索服务"
          :disabled="isEdit"
          filterable
          class="full-width"
        >
          <el-option-group v-for="group in typeGroups" :key="group.label" :label="group.label">
            <el-option v-for="option in group.options" :key="option.value" :label="option.label" :value="option.value" />
          </el-option-group>
        </el-select>
      </el-form-item>

      <el-form-item label="所属分组">
        <el-select v-model="form.group_id" placeholder="未分组" clearable class="full-width">
          <el-option v-for="option in groupOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>

      <el-divider v-if="schema.length" content-position="left">
        <span class="divider-text">以下字段由插件 {{ currentPlugin?.name }} 定义</span>
      </el-divider>

      <template v-for="field in visibleSchema" :key="field.key">
        <el-form-item :label="field.label" :prop="`data.${field.key}`">
          <!-- 端点地址（Base URL / 服务端点等）：始终可手输，有官方预设时可下拉补全 -->
          <el-autocomplete
            v-if="(field.type === 'text' || field.type === 'url') && field.presets?.length"
            v-model="form.data[field.key]"
            :fetch-suggestions="(query, cb) => fetchPresets(field, query, cb)"
            :trigger-on-focus="true"
            :placeholder="field.placeholder || ''"
            clearable
            class="full-width"
          >
            <template #default="{ item }">
              <!-- URL 是主信息，说明文字弱化并放在下方；上下留白把相邻候选拉开 -->
              <div style="display: flex; flex-direction: column; gap: 2px; padding: 4px 0; line-height: 1.35">
                <span style="font-size: 14px; color: var(--el-text-color-primary); word-break: break-all">{{ item.value }}</span>
                <span style="font-size: 12px; color: var(--el-text-color-secondary)">{{ item.label }}</span>
              </div>
            </template>
          </el-autocomplete>

          <el-input
            v-else-if="field.type === 'text' || field.type === 'url'"
            v-model="form.data[field.key]"
            :placeholder="field.placeholder || ''"
            clearable
          />

          <el-input
            v-else-if="field.type === 'password' || field.sensitive"
            v-model="form.data[field.key]"
            type="password"
            show-password
            :placeholder="field.placeholder || '请输入'"
          />

          <el-input
            v-else-if="field.type === 'textarea'"
            v-model="form.data[field.key]"
            type="textarea"
            :rows="3"
            :placeholder="field.placeholder || ''"
          />

          <el-select
            v-else-if="field.type === 'select'"
            v-model="form.data[field.key]"
            :placeholder="field.placeholder || '请选择'"
            filterable
            class="full-width"
          >
            <el-option
              v-for="option in normalizeOptions(field.options)"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>

          <el-input-number
            v-else-if="field.type === 'number'"
            v-model="form.data[field.key]"
            :controls-position="'right'"
            class="full-width"
          />

          <el-switch v-else-if="field.type === 'checkbox'" v-model="form.data[field.key]" />

          <!-- 模型名：候选来自该凭据的模型列表，取不到也能直接输入（allow-create） -->
          <el-select
            v-else-if="field.type === 'model'"
            v-model="form.data[field.key]"
            :placeholder="field.placeholder || '留空自动选择'"
            filterable
            allow-create
            default-first-option
            clearable
            :loading="modelsLoading"
            class="full-width"
            @visible-change="(open) => open && !modelOptions.length && loadModelOptions()"
          >
            <el-option v-for="opt in modelOptions" :key="opt" :label="opt" :value="opt" />
          </el-select>

          <el-input v-else v-model="form.data[field.key]" :placeholder="field.placeholder || ''" clearable />

          <div v-if="field.hint" class="field-hint">{{ field.hint }}</div>
          <div v-if="field.sensitive" class="field-hint">敏感字段，加密存储且不会在列表中展示</div>
        </el-form-item>
      </template>

      <el-form-item v-if="usesProxy" label="使用代理">
        <ProxySelect
          v-model:mode="proxyMode"
          v-model:proxyId="proxyId"
          :inherit-hint="inheritedProxy"
        />
        <div class="field-hint">
          该凭据验证时走哪个出口。优先级：<strong>本条凭据 &gt; 该插件预设 &gt; 所属类别预设 &gt; 全局默认</strong>
          ——「跟随上层设置」就是按这个顺序往上找
        </div>
      </el-form-item>

      <el-form-item v-if="usesProxy" label="TLS 版本">
        <TlsSelect v-model="tlsVersion" :inherit-hint="inheritedTls" :versions="tlsVersions" />
        <div class="field-hint">
          该凭据验证时用哪个 TLS 版本，优先级同上。<strong>auto</strong> 先按标准协商，
          仅当握手失败才回退 TLS 1.2 重试。只开<b>旧版 TLS</b> 的老服务器（如老邮件服务器）
          请直接选<strong>固定 1.1 / 1.0</strong>。
        </div>
      </el-form-item>

      <el-form-item>
        <el-checkbox v-model="form.auto_verify">
          {{ isEdit ? '保存后重新验证' : '保存后自动验证' }}
        </el-checkbox>
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full-width {
  width: 100%;
}

/* 内容超长时让弹窗内部滚动，避免「保存」按钮被挤出视口（默认窗口高度下也能点到保存） */
.credential-dialog {
  display: flex;
  flex-direction: column;
  max-height: 88vh;      /* 与 top="6vh" 配合：上下各留 6vh */
  margin-bottom: 6vh;
}

.credential-dialog :deep(.el-dialog__body) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.divider-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.field-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  margin-top: 2px;
}

.credential-form :deep(.el-form-item) {
  margin-bottom: 16px;
}
</style>