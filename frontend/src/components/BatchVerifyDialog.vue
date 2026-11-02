<script setup>
/**
 * 批量验证对话框（凭据管理 / 仪表盘共用）。
 *
 * 行为与凭据管理原来的「批量验证」完全一致：列出候选凭据、默认全选可批量验证的条目、
 * 回填并发数 / 超时（随时可改，点「验证」时随后端保存），点按钮后走同一个
 * `vaultStore.batchVerify('all', …, ids, concurrency, timeout)`，进度横幅由 AppLayout 统一展示。
 *
 * `candidates` 由父组件决定范围：凭据管理传当前筛选后的列表；仪表盘传全量凭据。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { useVaultStore } from '@/stores/vault'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  candidates: { type: Array, default: () => [] },
  title: { type: String, default: '批量验证' },
})

const emit = defineEmits(['update:modelValue', 'started'])

const vaultStore = useVaultStore()

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

const pickedIds = ref([])
const concurrency = ref(8)
const timeout = ref(20)
const limits = ref({ concurrency_range: [1, 32], timeout_range: [5, 300] })
const starting = ref(false)

const batchRunning = computed(() => vaultStore.batch.running)

const pluginFor = (type) => vaultStore.pluginByType[type] || null

/** 插件记录缺失（孤儿凭据）时视为启用，不置灰。 */
function isPluginEnabled(type) {
  const plugin = pluginFor(type)
  return plugin ? !!plugin.enabled : true
}

/** manifest 里 batch_verify=false 的类型只能手动单条验证；插件被禁用的也排除。 */
function canBatchVerify(type) {
  const plugin = pluginFor(type)
  return isPluginEnabled(type) && (!plugin || plugin.batch_verify !== false)
}

function typeLabel(type) {
  const plugin = pluginFor(type)
  return plugin ? plugin.name : type
}

const pickable = computed(() =>
  props.candidates.filter((row) => canBatchVerify(row.credential_type)))

const allChecked = computed(
  () => pickable.value.length > 0 && pickedIds.value.length === pickable.value.length)
const indeterminate = computed(
  () => pickedIds.value.length > 0 && pickedIds.value.length < pickable.value.length)

function selectAll(checked) {
  pickedIds.value = checked ? pickable.value.map((row) => row.id) : []
}

function loadOptions() {
  vaultStore.fetchBatchOptions()
    .then((opt) => {
      if (!opt || opt.ok === false) return
      concurrency.value = Number(opt.concurrency) || concurrency.value
      timeout.value = Number(opt.timeout) || timeout.value
      if (opt.concurrency_range) limits.value.concurrency_range = opt.concurrency_range
      if (opt.timeout_range) limits.value.timeout_range = opt.timeout_range
    })
    .catch(() => {})
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    pickedIds.value = pickable.value.map((row) => row.id)
    loadOptions()
  },
)

async function start() {
  const ids = [...pickedIds.value]
  if (!ids.length) return
  visible.value = false
  starting.value = true
  try {
    await vaultStore.batchVerify('all', null, null, ids, concurrency.value, timeout.value)
    emit('started', ids.length)
  } catch (err) {
    ElMessage.error(err.message || '批量验证启动失败')
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="560px" :close-on-click-modal="false">
    <div class="pick-toolbar">
      <el-checkbox
        :model-value="allChecked"
        :indeterminate="indeterminate"
        :disabled="batchRunning"
        @change="selectAll"
      >全选</el-checkbox>
      <span class="pick-count text-muted">
        已选 {{ pickedIds.length }} / {{ pickable.length }}
      </span>
    </div>

    <div class="pick-options">
      <span class="pick-opt">
        <span class="text-muted">并发线程数</span>
        <el-input-number
          v-model="concurrency"
          :min="limits.concurrency_range[0]"
          :max="limits.concurrency_range[1]"
          :step="1"
          step-strictly
          size="small"
          controls-position="right"
        />
      </span>
      <span class="pick-opt">
        <span class="text-muted">超时（秒）</span>
        <el-input-number
          v-model="timeout"
          :min="limits.timeout_range[0]"
          :max="limits.timeout_range[1]"
          :step="1"
          step-strictly
          size="small"
          controls-position="right"
        />
      </span>
      <span class="text-muted pick-opt-hint">并发越高越快，但可能触发目标限流</span>
    </div>

    <div class="pick-list">
      <el-checkbox-group v-model="pickedIds">
        <div
          v-for="row in candidates"
          :key="row.id"
          class="pick-row"
          :class="{ 'is-disabled': !canBatchVerify(row.credential_type) }"
        >
          <el-checkbox
            :value="row.id"
            :disabled="batchRunning || !canBatchVerify(row.credential_type)"
          >
            <span class="pick-name">{{ row.name }}</span>
            <span class="pick-type text-muted">{{ typeLabel(row.credential_type) }}</span>
          </el-checkbox>
          <el-tag v-if="!canBatchVerify(row.credential_type)" size="small" type="info">
            {{ isPluginEnabled(row.credential_type) ? '需手动验证' : '插件已禁用' }}
          </el-tag>
        </div>
      </el-checkbox-group>
      <el-empty
        v-if="!candidates.length"
        description="暂无可验证的凭据"
        :image-size="60"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :disabled="!pickedIds.length || batchRunning"
        :loading="starting"
        @click="start"
      >
        验证 {{ pickedIds.length }} 条
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pick-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.pick-count {
  font-size: 12px;
}

.pick-options {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 8px;
}

.pick-opt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.pick-opt :deep(.el-input-number) {
  width: 104px;
}

.pick-opt-hint {
  font-size: 12px;
}

.pick-list {
  max-height: 46vh;
  overflow-y: auto;
  padding: 4px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.pick-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}

.pick-row.is-disabled {
  opacity: 0.6;
}

.pick-name {
  font-weight: 500;
}

.pick-type {
  margin-left: 8px;
  font-size: 12px;
}
</style>
