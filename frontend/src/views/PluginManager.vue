<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowRight, Check, CircleClose, Refresh, Search, SetUp } from '@element-plus/icons-vue'

import ScopeDefaultsDialog from '@/components/ScopeDefaultsDialog.vue'
import { useVaultStore } from '@/stores/vault'

const vaultStore = useVaultStore()
const loading = ref(false)
const pluginSearch = ref('')

const installedGroups = computed(() => {
  const groups = {}
  vaultStore.plugins.forEach((p) => {
    const key = p.category || '(根目录)'
    if (!groups[key]) {
      groups[key] = { key, name: p.category_name || key, category_icon: p.category_icon || null, items: [] }
    }
    groups[key].items.push(p)
  })
  return Object.values(groups)
})

// 搜索词按空格拆分为多个关键词，需全部命中（AND）
const searchTerms = computed(() =>
  pluginSearch.value.trim().toLowerCase().split(/\s+/).filter(Boolean),
)

function matchesSearch(item) {
  if (!searchTerms.value.length) return true
  const haystack = [item.name, item.credential_type, item.description, item.category_name]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return searchTerms.value.every((term) => haystack.includes(term))
}

// 过滤只影响展示：组内无匹配项时整组丢弃，不触碰任何选中集合（本页无选择状态）
const filteredInstalledGroups = computed(() =>
  installedGroups.value
    .map((group) => ({ ...group, items: group.items.filter(matchesSearch) }))
    .filter((group) => group.items.length),
)

// ── 类别折叠：状态持久化到 localStorage ──
const COLLAPSED_CATS_KEY = 'vault.plugins.collapsedCats'

function readCollapsedCats() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLLAPSED_CATS_KEY) || '[]')
    return Array.isArray(saved) ? saved : []
  } catch {
    return []
  }
}

const collapsedCats = ref(readCollapsedCats())

function persistCollapsedCats() {
  try {
    localStorage.setItem(COLLAPSED_CATS_KEY, JSON.stringify(collapsedCats.value))
  } catch {
    // 隐私模式等写入失败时静默降级，仅影响持久化
  }
}

// 搜索时忽略折叠状态、强制展开命中类别，避免「搜到了却看不见」
function isCatCollapsed(key) {
  if (pluginSearch.value.trim()) return false
  return collapsedCats.value.includes(key)
}

function toggleCat(key) {
  const index = collapsedCats.value.indexOf(key)
  if (index === -1) collapsedCats.value.push(key)
  else collapsedCats.value.splice(index, 1)
  persistCollapsedCats()
}

// 有插件但全被过滤掉时给空状态提示（避免出现只有标题的空卡片）
const noInstalledMatch = computed(
  () => searchTerms.value.length > 0 && vaultStore.plugins.length > 0 && !filteredInstalledGroups.value.length,
)

async function load() {
  loading.value = true
  try {
    await vaultStore.fetchPlugins()
  } catch (err) {
    ElMessage.error(err.message || '加载插件失败')
  } finally {
    loading.value = false
  }
}

// 正在切换启用状态的插件类型：防止连点导致重复请求 / 连续弹窗
const togglingTypes = ref([])

async function toggleEnabled(plugin) {
  const type = plugin.credential_type
  if (togglingTypes.value.includes(type)) return
  const next = !plugin.enabled
  togglingTypes.value = [...togglingTypes.value, type]
  try {
    await vaultStore.setPluginEnabled(type, next)
    ElMessage.success(`已${next ? '启用' : '禁用'}「${plugin.name}」`)
    await load()
  } catch (err) {
    ElMessage.error(err.message || (next ? '启用失败' : '禁用失败'))
  } finally {
    togglingTypes.value = togglingTypes.value.filter((item) => item !== type)
  }
}

onMounted(load)

// ── 类别 / 插件默认设置（与「凭据目录」右键共用同一个对话框）──
const scopeDialog = ref({ visible: false, scope: 'plugin', scopeKey: '', credentialType: '', title: '' })

function openDefaults(scope, scopeKey, credentialType, name) {
  if (!scopeKey) return
  scopeDialog.value = {
    visible: true,
    scope,
    scopeKey,
    credentialType,
    title: `${scope === 'category' ? '类别' : '插件'}默认设置：${name}`,
  }
}
</script>

<template>
  <div class="page">
    <div class="page-toolbar">
      <h2 class="section-title">插件管理</h2>
      <span class="text-muted">共 {{ vaultStore.plugins.length }} 个</span>
      <div class="spacer" />
      <el-input
        v-model="pluginSearch"
        class="plugin-search"
        placeholder="搜索插件名 / 标识 / 描述"
        clearable
        :prefix-icon="Search"
      />
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card v-loading="loading" shadow="never">
      <template #header><span>插件列表</span></template>

      <div v-for="group in filteredInstalledGroups" :key="group.key" class="pm-group">
        <div class="pm-cat" @click="toggleCat(group.key)">
          <el-icon class="pm-cat-arrow">
            <ArrowDown v-if="!isCatCollapsed(group.key)" />
            <ArrowRight v-else />
          </el-icon>
          <span class="pm-cat-icon">
            <img
              v-if="!group.category_icon && group.items[0].icon_image"
              :class="{ 'icon-lift': group.items[0].icon_lift }"
              :src="group.items[0].icon_image"
              alt=""
            />
            <template v-else>{{ group.category_icon || group.items[0].icon || '📁' }}</template>
          </span>
          <span class="pm-cat-name">{{ group.name }}（{{ group.items.length }}）</span>
          <span class="pm-cat-actions" @click.stop>
            <el-tooltip content="类别默认设置（新建凭据时预填）" placement="top">
              <el-button
                text
                size="small"
                :icon="SetUp"
                @click="openDefaults('category', group.key, '', group.name)"
              />
            </el-tooltip>
          </span>
        </div>
        <template v-if="!isCatCollapsed(group.key)">
          <div v-for="plugin in group.items" :key="plugin.credential_type" class="pm-row" :class="{ 'is-disabled': !plugin.enabled }">
            <!-- 启用状态写在条目**最左侧**，且「已启用」「已禁用」两种都显示：
                 右边按钮上的「禁用」/「启用」是**动作**（点了会发生什么），不是状态，
                 靠它反推当前状态容易看反。左侧固定宽度的一列状态标签，竖着扫一遍就清楚了。 -->
            <el-tag
              :type="plugin.enabled ? 'success' : 'info'"
              size="small"
              class="pm-state"
            >{{ plugin.enabled ? '已启用' : '已禁用' }}</el-tag>
            <img
              v-if="plugin.icon_image"
              class="pm-icon"
              :class="{ 'icon-lift': plugin.icon_lift }"
              :src="plugin.icon_image"
              alt=""
            />
            <span v-else class="pm-icon-emoji">{{ plugin.icon }}</span>
            <div class="pm-main">
              <div class="pm-name">{{ plugin.name }}</div>
              <div class="pm-sub text-muted">
                {{ plugin.credential_type }} · {{ plugin.credential_count || 0 }} 条凭据
              </div>
            </div>
            <el-tooltip content="插件默认设置（覆盖类别级）" placement="top">
              <el-button
                text
                size="small"
                :icon="SetUp"
                @click="openDefaults('plugin', plugin.credential_type, plugin.credential_type, plugin.name)"
              />
            </el-tooltip>
            <el-button
              :type="plugin.enabled ? 'danger' : 'primary'"
              plain
              size="small"
              :icon="plugin.enabled ? CircleClose : Check"
              :loading="togglingTypes.includes(plugin.credential_type)"
              :disabled="togglingTypes.includes(plugin.credential_type)"
              @click="toggleEnabled(plugin)"
            >
              {{ plugin.enabled ? '禁用' : '启用' }}
            </el-button>
          </div>
        </template>
      </div>

      <el-empty v-if="!vaultStore.plugins.length" description="暂无插件" :image-size="60" />
      <el-empty
        v-else-if="noInstalledMatch"
        description="没有匹配的插件"
        :image-size="60"
      />
    </el-card>

    <ScopeDefaultsDialog
      v-model="scopeDialog.visible"
      :scope="scopeDialog.scope"
      :scope-key="scopeDialog.scopeKey"
      :credential-type="scopeDialog.credentialType"
      :title="scopeDialog.title"
    />
  </div>
</template>

<style scoped>
.pm-group + .pm-group {
  margin-top: 12px;
}

.pm-cat {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  padding: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  user-select: none;
  border-radius: 4px;
}

.pm-cat:hover {
  background: var(--el-fill-color-light);
}

.pm-cat-arrow {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.pm-cat-icon {
  display: inline-flex;
  align-items: center;
}

.pm-cat-icon img {
  width: 16px;
  height: 16px;
  object-fit: contain;
}

.pm-cat-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
}

.pm-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.pm-row:last-child {
  border-bottom: none;
}

/* 禁用行的弱化**只压文字**，不再整行 opacity。
   整行 opacity 会连右侧的「启用」按钮一起压灰，看着像不可点——其实可点，而且用户正是该点它
   来恢复。状态已经由左侧固定宽度的那枚「已禁用」标签说清楚了，不需要再拿按钮去暗示。 */
.pm-row.is-disabled .pm-name,
.pm-row.is-disabled .pm-sub {
  opacity: .55;
}

/* 条目左侧的状态标签：定宽，让「已启用」/「已禁用」竖着扫下来对齐成一列 */
.pm-state {
  flex-shrink: 0;
  min-width: 54px;
}

.pm-icon {
  width: 22px;
  height: 22px;
  object-fit: contain;
  flex-shrink: 0;
}

.pm-icon-emoji {
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}

.pm-main {
  flex: 1;
  min-width: 0;
}

.pm-name {
  font-weight: 500;
}

.pm-sub {
  font-size: 12px;
  margin-top: 2px;
}

.plugin-search {
  width: 240px;
}
</style>
