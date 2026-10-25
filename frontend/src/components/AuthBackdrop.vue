<template>
  <!-- 旧版独立解锁/创建页的静态背景：星野 + 极光 + 刻度环，与开屏同一套视觉，
       但没有任何动画（旧页面按「不使用动态元素」处理）。 -->
  <div class="ab" aria-hidden="true">
    <div class="ab-sky" />
    <div class="ab-stars" />
    <div class="ab-grid" />
    <div class="ab-glow" />
    <div class="ab-rings"><span /><span /><span /></div>
    <div class="ab-vignette" />
    <div class="ab-frame" aria-hidden="true">
      <i /><i /><i /><i />
    </div>
    <div class="ab-hud ab-hud-tl"><b>KEYREALM</b><i>//</i>LOCAL VAULT</div>
    <div class="ab-hud ab-hud-br">AES-256-GCM <i>·</i> LOCAL ONLY</div>
  </div>
</template>

<style scoped>
.ab {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  background: #07080c;
}

/* 极光：左上冷蓝、左下紫、右侧一点绿，和开屏的配色一致 */
.ab-sky {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(70% 62% at 16% 6%, rgba(52, 84, 178, 0.34), transparent 62%),
    radial-gradient(58% 56% at 6% 94%, rgba(112, 60, 168, 0.26), transparent 64%),
    radial-gradient(52% 58% at 94% 70%, rgba(58, 128, 92, 0.14), transparent 62%),
    radial-gradient(66% 62% at 74% 16%, rgba(28, 64, 128, 0.18), transparent 60%),
    linear-gradient(180deg, #06070b, #07080c 55%, #05060a);
}

/* 星野：四层平铺的点阵，尺寸互质避免出现明显重复 */
.ab-stars {
  position: absolute;
  inset: 0;
  opacity: 0.85;
  background-image:
    radial-gradient(1px 1px at 24px 36px, rgba(255, 255, 255, 0.62), transparent 100%),
    radial-gradient(1px 1px at 118px 82px, rgba(255, 255, 255, 0.42), transparent 100%),
    radial-gradient(1.4px 1.4px at 64px 148px, rgba(210, 230, 255, 0.5), transparent 100%),
    radial-gradient(1px 1px at 176px 22px, rgba(255, 255, 255, 0.3), transparent 100%),
    radial-gradient(1.2px 1.2px at 196px 126px, rgba(190, 220, 255, 0.38), transparent 100%),
    radial-gradient(1px 1px at 96px 196px, rgba(255, 255, 255, 0.26), transparent 100%);
  background-size: 260px 240px, 300px 280px, 340px 320px, 380px 360px, 420px 400px, 460px 440px;
}

/* 细网格：只在中部淡出，边缘留黑 */
.ab-grid {
  position: absolute;
  inset: -10%;
  opacity: 0.55;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.035) 1px, transparent 1px);
  background-size: 72px 72px;
  -webkit-mask-image: radial-gradient(ellipse at 50% 50%, #000 20%, transparent 72%);
  mask-image: radial-gradient(ellipse at 50% 50%, #000 20%, transparent 72%);
}

/* 卡片背后的淡绿光，把视线收拢到中间 */
.ab-glow {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 780px;
  height: 780px;
  transform: translate(-50%, -50%);
  background: radial-gradient(
    closest-side,
    rgba(163, 230, 53, 0.085),
    rgba(163, 230, 53, 0.02) 46%,
    transparent 72%
  );
}

/* 同心刻度环：实线 + 虚线 + 内圈，静态 */
.ab-rings {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 820px;
  height: 820px;
  transform: translate(-50%, -50%);
}
.ab-rings span {
  position: absolute;
  display: block;
  border-radius: 50%;
  border: 1px solid rgba(163, 230, 53, 0.1);
}
.ab-rings span:nth-child(1) {
  inset: 0;
}
.ab-rings span:nth-child(2) {
  inset: 62px;
  border-style: dashed;
  border-color: rgba(255, 255, 255, 0.06);
}
.ab-rings span:nth-child(3) {
  inset: 150px;
  border-color: rgba(163, 230, 53, 0.06);
}

/* 四周压暗，避免亮部顶到边缘 */
.ab-vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(
    ellipse at 50% 48%,
    transparent 34%,
    rgba(0, 0, 0, 0.42) 76%,
    rgba(0, 0, 0, 0.72) 100%
  );
}

/* 四角取景框，与开屏的边角 HUD 一致 */
.ab-frame {
  position: absolute;
  inset: 14px;
}
.ab-frame i {
  position: absolute;
  width: 24px;
  height: 24px;
  border: 0 solid rgba(163, 230, 53, 0.16);
}
.ab-frame i:nth-child(1) {
  top: 0;
  left: 0;
  border-top-width: 1px;
  border-left-width: 1px;
}
.ab-frame i:nth-child(2) {
  top: 0;
  right: 0;
  border-top-width: 1px;
  border-right-width: 1px;
}
.ab-frame i:nth-child(3) {
  bottom: 0;
  left: 0;
  border-bottom-width: 1px;
  border-left-width: 1px;
}
.ab-frame i:nth-child(4) {
  bottom: 0;
  right: 0;
  border-bottom-width: 1px;
  border-right-width: 1px;
}

/* 四角 HUD 文案，呼应开屏 */
.ab-hud {
  position: absolute;
  font-family: 'JetBrains Mono', 'SFMono-Regular', 'Cascadia Code', monospace;
  font-size: 9px;
  letter-spacing: 0.24em;
  color: rgba(255, 255, 255, 0.26);
  text-transform: uppercase;
  white-space: nowrap;
}
.ab-hud b {
  margin-right: 6px;
  font-weight: 600;
  color: rgba(163, 230, 53, 0.5);
}
.ab-hud i {
  margin: 0 6px;
  font-style: normal;
  color: rgba(255, 255, 255, 0.16);
}
.ab-hud-tl {
  top: 22px;
  left: 26px;
}
.ab-hud-br {
  right: 26px;
  bottom: 22px;
}

@media (max-width: 900px) {
  .ab-rings {
    width: 640px;
    height: 640px;
  }
  .ab-rings span:nth-child(2) {
    inset: 48px;
  }
  .ab-rings span:nth-child(3) {
    inset: 116px;
  }
  .ab-hud {
    display: none;
  }
}
</style>
