<script setup>
/**
 * 仪表盘中央核心：状态环 + 轨道粒子 + 健康度数字。
 *
 * 零图表依赖：环用 SVG（颜色直接吃 --v-* 主题令牌），粒子用 Canvas（从主题令牌现读颜色）。
 * 6 套主题切换时通过 MutationObserver 重新取色；侧栏折叠不会触发 window.resize，
 * 因此尺寸用 ResizeObserver 跟踪；prefers-reduced-motion 下只画静态帧、不跑 RAF。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  health: { type: Object, default: () => ({ rate: 0, valid: 0, total: 0 }) },
  status: { type: Object, default: () => ({}) },
  generatedAt: { type: String, default: '' },
})

const STATUS_ORDER = ['valid', 'invalid', 'error', 'unknown']
const VIEW = 360
const RADIUS = 132
const CIRC = 2 * Math.PI * RADIUS

const rootRef = ref(null)
const canvasRef = ref(null)
const rate = ref(0)

const counts = computed(() => props.status || {})
const total = computed(() => STATUS_ORDER.reduce((sum, key) => sum + (Number(counts.value[key]) || 0), 0))

const segments = computed(() => {
  let offset = 0
  return STATUS_ORDER
    .map((key) => {
      const value = Number(counts.value[key]) || 0
      const length = total.value ? (value / total.value) * CIRC : 0
      const segment = { key, value, length, offset }
      offset += length
      return segment
    })
    .filter((segment) => segment.length > 0)
})

const ticks = Array.from({ length: 72 }, (_, index) => {
  const angle = (index / 72) * Math.PI * 2
  const major = index % 6 === 0
  const inner = major ? 148 : 152
  const outer = 158
  return {
    x1: VIEW / 2 + Math.cos(angle) * inner,
    y1: VIEW / 2 + Math.sin(angle) * inner,
    x2: VIEW / 2 + Math.cos(angle) * outer,
    y2: VIEW / 2 + Math.sin(angle) * outer,
    major,
  }
})

// ── 健康度数字动画 ────────────────────────────────────────
let rateRaf = 0

function animateRate(target) {
  cancelAnimationFrame(rateRaf)
  if (prefersReduced()) {
    rate.value = target
    return
  }
  const from = rate.value
  const started = performance.now()
  const step = (now) => {
    const progress = Math.min(1, (now - started) / 700)
    const eased = 1 - Math.pow(1 - progress, 3)
    rate.value = from + (target - from) * eased
    if (progress < 1) rateRaf = requestAnimationFrame(step)
  }
  rateRaf = requestAnimationFrame(step)
}

watch(() => Number(props.health && props.health.rate) || 0, (value) => animateRate(value),
  { immediate: true })

// ── Canvas 轨道粒子 ───────────────────────────────────────
let ctx = null
let raf = 0
let dpr = 1
let width = 0
let height = 0
let particles = []
let colors = []
let startedAt = 0
let running = false
let resizeObserver = null
let themeObserver = null
let reducedMedia = null

function prefersReduced() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function readColors() {
  const el = rootRef.value
  if (!el) return
  const styles = getComputedStyle(el)
  colors = ['--v-primary', '--v-accent', '--v-accent-2', '--v-success']
    .map((key) => styles.getPropertyValue(key).trim())
    .filter(Boolean)
  if (!colors.length) colors = ['#a3e635']
}

function seedParticles() {
  const shortSide = Math.min(width, height)
  const count = Math.max(26, Math.min(120, Math.round((width * height) / 16000)))
  particles = Array.from({ length: count }, () => ({
    angle: Math.random() * Math.PI * 2,
    radius: (0.30 + Math.random() * 0.62) * (shortSide / 2),
    speed: (Math.random() < 0.5 ? -1 : 1) * (0.00008 + Math.random() * 0.00022),
    size: 0.6 + Math.random() * 1.6,
    phase: Math.random() * Math.PI * 2,
    twinkle: 0.0012 + Math.random() * 0.0028,
    alpha: 0.35 + Math.random() * 0.55,
    color: Math.floor(Math.random() * 4),
  }))
}

function resize() {
  const canvas = canvasRef.value
  const host = canvas && canvas.parentElement
  if (!canvas || !host) return
  const rect = host.getBoundingClientRect()
  const nextWidth = Math.max(1, Math.round(rect.width))
  const nextHeight = Math.max(1, Math.round(rect.height))
  if (nextWidth === width && nextHeight === height) return
  width = nextWidth
  height = nextHeight
  dpr = Math.min(2, window.devicePixelRatio || 1)
  canvas.width = Math.round(width * dpr)
  canvas.height = Math.round(height * dpr)
  canvas.style.width = `${width}px`
  canvas.style.height = `${height}px`
  ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  seedParticles()
  if (prefersReduced()) draw(0)
}

function draw(now) {
  if (!ctx) return
  ctx.clearRect(0, 0, width, height)
  const cx = width / 2
  const cy = height / 2
  const elapsed = now - startedAt
  const reduced = prefersReduced()
  for (const particle of particles) {
    const angle = particle.angle + (reduced ? 0 : elapsed * particle.speed)
    const x = cx + Math.cos(angle) * particle.radius
    const y = cy + Math.sin(angle) * particle.radius * 0.86
    const twinkle = reduced ? 1 : (0.55 + 0.45 * Math.sin(elapsed * particle.twinkle + particle.phase))
    ctx.globalAlpha = particle.alpha * twinkle
    ctx.fillStyle = colors[particle.color % colors.length]
    ctx.beginPath()
    ctx.arc(x, y, particle.size, 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.globalAlpha = 1
}

function frame(now) {
  if (!running) return
  draw(now)
  raf = requestAnimationFrame(frame)
}

function startLoop() {
  if (running || prefersReduced()) return
  running = true
  startedAt = performance.now()
  raf = requestAnimationFrame(frame)
}

function stopLoop() {
  running = false
  cancelAnimationFrame(raf)
}

function onVisibility() {
  if (document.hidden) stopLoop()
  else startLoop()
}

function onReducedChange() {
  stopLoop()
  if (prefersReduced()) draw(0)
  else startLoop()
}

onMounted(() => {
  readColors()
  resize()
  startLoop()
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => resize())
    if (canvasRef.value && canvasRef.value.parentElement) {
      resizeObserver.observe(canvasRef.value.parentElement)
    }
  }
  // 主题切换只改 data-theme / class，不触发任何事件——直接观察这两个属性
  themeObserver = new MutationObserver(() => {
    readColors()
    if (prefersReduced()) draw(0)
  })
  themeObserver.observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme', 'class'],
  })
  document.addEventListener('visibilitychange', onVisibility)
  reducedMedia = window.matchMedia('(prefers-reduced-motion: reduce)')
  if (reducedMedia.addEventListener) reducedMedia.addEventListener('change', onReducedChange)
})

onBeforeUnmount(() => {
  stopLoop()
  cancelAnimationFrame(rateRaf)
  if (resizeObserver) resizeObserver.disconnect()
  if (themeObserver) themeObserver.disconnect()
  document.removeEventListener('visibilitychange', onVisibility)
  if (reducedMedia && reducedMedia.removeEventListener) {
    reducedMedia.removeEventListener('change', onReducedChange)
  }
})
</script>

<template>
  <div ref="rootRef" class="dash-core" :class="{ 'is-empty': !total }">
    <canvas ref="canvasRef" class="dash-core-particles" aria-hidden="true" />

    <svg class="dash-core-svg" :viewBox="`0 0 ${VIEW} ${VIEW}`" role="img"
      :aria-label="`健康度 ${Math.round(((props.health && props.health.rate) || 0) * 100)}%，` +
        `有效 ${props.health && props.health.valid || 0} / ${props.health && props.health.total || 0}`">
      <!-- 刻度环 -->
      <g class="core-ticks">
        <line
          v-for="(tick, index) in ticks"
          :key="index"
          :x1="tick.x1" :y1="tick.y1" :x2="tick.x2" :y2="tick.y2"
          :class="{ 'is-major': tick.major }"
        />
      </g>

      <!-- 外层虚线轨道与扫描弧 -->
      <circle class="core-orbit" :cx="VIEW / 2" :cy="VIEW / 2" :r="148"
        stroke-dasharray="2 9" />
      <circle class="core-scan" :cx="VIEW / 2" :cy="VIEW / 2" :r="142"
        stroke-dasharray="54 838" />

      <!-- 状态环 -->
      <circle class="core-ring-base" :cx="VIEW / 2" :cy="VIEW / 2" :r="RADIUS" />
      <circle
        v-for="segment in segments"
        :key="segment.key"
        class="core-ring-seg"
        :class="`is-${segment.key}`"
        :cx="VIEW / 2" :cy="VIEW / 2" :r="RADIUS"
        :stroke-dasharray="`${segment.length} ${Math.max(0, CIRC - segment.length)}`"
        :stroke-dashoffset="-segment.offset"
      />

      <!-- 内圈装饰 -->
      <circle class="core-ring-inner" :cx="VIEW / 2" :cy="VIEW / 2" r="104" />
      <circle class="core-ring-inner is-dashed" :cx="VIEW / 2" :cy="VIEW / 2" r="92"
        stroke-dasharray="1 7" />

      <text class="core-label" :x="VIEW / 2" y="144" text-anchor="middle">VAULT HEALTH</text>
      <text class="core-rate" :x="VIEW / 2" y="204" text-anchor="middle">
        {{ Math.round(rate * 100) }}<tspan class="core-rate-unit">%</tspan>
      </text>
      <text class="core-sub" :x="VIEW / 2" y="236" text-anchor="middle">
        {{ props.health && props.health.valid || 0 }} / {{ total }} 有效
      </text>
    </svg>

    <div class="dash-core-corners" aria-hidden="true">
      <span /><span /><span /><span />
    </div>

    <div v-if="generatedAt" class="dash-core-stamp">
      SYNC {{ generatedAt }}
    </div>
  </div>
</template>

<style scoped>
.dash-core {
  position: relative;
  width: 100%;
  max-width: 460px;
  aspect-ratio: 1 / 1;
  margin: 0 auto;
  display: grid;
  place-items: center;
  color: var(--v-primary);
}

.dash-core-particles {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.dash-core-svg {
  position: relative;
  z-index: 1;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.core-ticks line {
  stroke: var(--v-border-strong);
  stroke-width: 1;
  opacity: 0.55;
}

.core-ticks line.is-major {
  stroke: var(--v-primary);
  opacity: 0.65;
  stroke-width: 1.4;
}

.core-orbit {
  fill: none;
  stroke: var(--v-primary);
  stroke-width: 1;
  opacity: 0.34;
  transform-origin: 50% 50%;
  animation: core-spin 46s linear infinite;
}

.core-scan {
  fill: none;
  stroke: var(--v-accent-2);
  stroke-width: 2;
  stroke-linecap: round;
  opacity: 0.75;
  filter: drop-shadow(0 0 6px var(--v-accent-2));
  transform-origin: 50% 50%;
  animation: core-spin 8s linear infinite;
}

.core-ring-base {
  fill: none;
  stroke: var(--v-border);
  stroke-width: 10;
  opacity: 0.55;
}

.core-ring-seg {
  fill: none;
  stroke-width: 10;
  stroke-linecap: butt;
  transform: rotate(-90deg);
  transform-origin: 50% 50%;
  transition: stroke-dasharray var(--v-slow) var(--v-ease);
}

.core-ring-seg.is-valid { stroke: var(--v-success); }
.core-ring-seg.is-invalid { stroke: var(--v-danger); }
.core-ring-seg.is-error { stroke: var(--v-warning); }
.core-ring-seg.is-unknown { stroke: var(--v-info); opacity: 0.65; }

.core-ring-inner {
  fill: none;
  stroke: var(--v-primary);
  stroke-width: 1;
  opacity: 0.22;
}

.core-ring-inner.is-dashed {
  opacity: 0.3;
}

.core-label {
  fill: var(--v-text-2);
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  letter-spacing: 0.26em;
}

.core-rate {
  fill: var(--v-text);
  font-family: var(--v-font-display);
  font-size: 54px;
  font-weight: 700;
  letter-spacing: -0.04em;
  filter: drop-shadow(0 0 16px var(--v-halo-1));
}

.core-rate-unit {
  fill: var(--v-primary);
  font-size: 24px;
  font-weight: 600;
}

.core-sub {
  fill: var(--v-text-2);
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 12px;
  letter-spacing: 0.08em;
}

.dash-core-corners {
  position: absolute;
  inset: 8px;
  pointer-events: none;
}

.dash-core-corners span {
  position: absolute;
  width: 18px;
  height: 18px;
  border-color: var(--v-primary);
  border-style: solid;
  border-width: 0;
  opacity: 0.5;
}

.dash-core-corners span:nth-child(1) { top: 0; left: 0; border-top-width: 1px; border-left-width: 1px; }
.dash-core-corners span:nth-child(2) { top: 0; right: 0; border-top-width: 1px; border-right-width: 1px; }
.dash-core-corners span:nth-child(3) { bottom: 0; left: 0; border-bottom-width: 1px; border-left-width: 1px; }
.dash-core-corners span:nth-child(4) { bottom: 0; right: 0; border-bottom-width: 1px; border-right-width: 1px; }

.dash-core-stamp {
  position: absolute;
  bottom: 4px;
  left: 50%;
  transform: translateX(-50%);
  font-family: 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--v-text-3);
  white-space: nowrap;
}

.is-empty .core-ring-seg,
.is-empty .core-scan {
  display: none;
}

@keyframes core-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .core-orbit,
  .core-scan {
    animation: none;
  }
  .core-ring-seg {
    transition: none;
  }
}
</style>
