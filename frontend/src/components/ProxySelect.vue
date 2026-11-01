<script setup>
import { computed, onMounted } from 'vue'

import { useProxyStore } from '@/stores/proxy'

/**
 * 「使用代理」三选一：跟随全局 / 直连 / 指定某个已保存的代理。
 *
 * 值同时写进两个保留键：`_proxy_mode`（global | proxy | direct）与 `_proxy_id`。
 * 用两个 v-model 而不是把两者揉成一个对象，是因为它们最终要落进凭据的 data 字典里，
 * 分开传可以让 `CredentialForm` / `ScopeDefaultsDialog` 直接绑到各自的键上。
 */
const props = defineProps({
  mode: { type: String, default: 'global' },
  proxyId: { type: [Number, String], default: null },
  disabled: { type: Boolean, default: false },
  // 「跟随」那一项怎么写。凭据里写「跟随上层设置」，作用域预设里写「不在此层指定」。
  followLabel: { type: String, default: '跟随上层设置' },
  // 括注：跟随之后实际会走哪个出口（由后端按「子模块预设 > 上层模块预设 > 全局」解析好）
  inheritHint: { type: String, default: '' },
})

const emit = defineEmits(['update:mode', 'update:proxyId'])

const proxyStore = useProxyStore()

onMounted(() => {
  if (!proxyStore.items.length) proxyStore.fetch().catch(() => {})
})

// 用一个字符串承载三选一：「跟随全局」/「直连」是特殊值，具体代理是 `proxy:<id>`
const value = computed({
  get() {
    if (props.mode === 'direct') return 'direct'
    if (props.mode === 'proxy' && props.proxyId) return `proxy:${props.proxyId}`
    return 'global'
  },
  set(next) {
    if (next === 'direct') {
      emit('update:mode', 'direct')
      emit('update:proxyId', null)
    } else if (typeof next === 'string' && next.startsWith('proxy:')) {
      emit('update:mode', 'proxy')
      // 代理 id 是 UUID 字符串，**不要** Number() 转换（转了就是 NaN/不存在的代理）
      emit('update:proxyId', next.slice('proxy:'.length))
    } else {
      emit('update:mode', 'global')
      emit('update:proxyId', null)
    }
  },
})

const globalLabel = computed(() => {
  const hint = props.inheritHint || proxyStore.globalDisplay || proxyStore.globalUrl || '直连'
  return `${props.followLabel}（${hint}）`
})
</script>

<template>
  <el-select
    v-model="value"
    :disabled="disabled"
    class="full-width"
    :placeholder="followLabel"
  >
    <el-option value="global" :label="globalLabel" />
    <el-option value="direct" label="直连（不使用代理）" />
    <el-option-group v-if="proxyStore.items.length" label="指定代理">
      <el-option
        v-for="item in proxyStore.items"
        :key="item.id"
        :value="`proxy:${item.id}`"
        :label="`${item.name}（${item.url}）`"
      />
    </el-option-group>
  </el-select>
</template>
