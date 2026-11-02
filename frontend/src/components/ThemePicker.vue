<script setup>
/**
 * 主题选择器 —— 嵌在「系统设置 → 外观」卡片里的方案网格。
 *
 * 全部方案按明暗分两组，点一下即刻换肤：`applyTheme()` 把 id 写到 `<html data-theme>`、
 * 按 `mode` 增删 `.dark` 类，并把选择落进 localStorage。纯表现层偏好 ——
 * 不请求后端、不进数据库，与 `vault.layout.*` 同一性质。
 *
 * 增删主题只需改 `src/theme/themes.js`（登记元信息）与 `src/styles/themes/<id>.css`
 * （声明令牌），本组件按 `THEMES` 渲染，不必跟着改。
 */
import { ref } from 'vue'
import { Check } from '@element-plus/icons-vue'

import { THEMES } from '@/theme/themes'
import { applyTheme } from '@/theme/applyTheme'

/** 当前生效的主题 id。首屏由 index.html 的内联脚本先写好，这里只读不猜。 */
const current = ref(document.documentElement.dataset.theme || 'obsidian')

const groups = [
  { label: '浅色', items: THEMES.filter((t) => t.mode === 'light') },
  { label: '深色', items: THEMES.filter((t) => t.mode === 'dark') },
]

function pick(theme) {
  current.value = applyTheme(theme.id).id
}
</script>

<template>
  <div class="tp">
    <div v-for="group in groups" :key="group.label" class="tp-group">
      <div class="tp-group-label">{{ group.label }}</div>
      <div class="tp-grid">
        <button
          v-for="theme in group.items"
          :key="theme.id"
          type="button"
          class="tp-item"
          :class="{ 'is-active': current === theme.id }"
          :title="`切换到「${theme.name}」`"
          @click="pick(theme)"
        >
          <!-- 三色预览条：卡片的第一眼就是这套配色的骨架色。 -->
          <span class="tp-swatch" aria-hidden="true">
            <i v-for="(color, i) in theme.swatch" :key="i" :style="{ background: color }" />
          </span>
          <span class="tp-meta">
            <span class="tp-name">
              {{ theme.name }}
              <em>{{ theme.en }}</em>
            </span>
            <span class="tp-desc">{{ theme.desc }}</span>
          </span>
          <span class="tp-check">
            <el-icon><Check /></el-icon>
          </span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tp-group + .tp-group {
  margin-top: 18px;
}

.tp-group-label {
  margin-bottom: 8px;
  font-size: 12px;
  letter-spacing: 0.08em;
  color: var(--v-text-3);
}

.tp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(208px, 1fr));
  gap: 12px;
}

.tp-item {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
  padding: 0 0 12px;
  overflow: hidden;
  border: 1px solid var(--v-border);
  border-radius: var(--v-radius-md);
  background: var(--v-bg-elevated);
  text-align: left;
  cursor: pointer;
  transition:
    transform var(--v-fast) var(--v-ease-out),
    box-shadow var(--v-base) var(--v-ease),
    border-color var(--v-base) var(--v-ease);
}

@media (hover: hover) and (pointer: fine) {
  .tp-item:hover {
    transform: translateY(-1px);
    border-color: var(--v-border-strong);
    box-shadow: var(--v-shadow-md);
  }
}

.tp-item:active {
  transform: translateY(0) scale(0.98);
}

.tp-item:focus-visible {
  outline: 2px solid var(--v-primary);
  outline-offset: 2px;
}

.tp-item.is-active {
  border-color: color-mix(in srgb, var(--v-primary) 55%, transparent);
  box-shadow: var(--v-glow);
}

.tp-swatch {
  display: flex;
  height: 38px;
}

.tp-swatch i {
  flex: 1;
}

.tp-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 0 12px;
}

.tp-name {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-family: var(--v-font-display);
  font-size: 13.5px;
  font-weight: 650;
  letter-spacing: var(--v-title-tracking);
  color: var(--v-text);
}

.tp-name em {
  font-style: normal;
  font-size: 10.5px;
  font-weight: 400;
  color: var(--v-text-3);
}

.tp-desc {
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--v-text-2);
}

/* 选中标记钉在右上角，靠 opacity 显隐而不是 v-if —— 否则选中瞬间卡片内容会移位。 */
.tp-check {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--v-primary);
  color: var(--v-primary-ink);
  font-size: 12px;
  opacity: 0;
  transform: scale(0.6);
  transition:
    opacity var(--v-fast) var(--v-ease),
    transform var(--v-base) var(--v-spring);
}

.tp-item.is-active .tp-check {
  opacity: 1;
  transform: scale(1);
}

@media (prefers-reduced-motion: reduce) {
  .tp-item:hover,
  .tp-item:active {
    transform: none;
  }
}
</style>
