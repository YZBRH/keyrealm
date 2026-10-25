/*!
 * Keyrealm 开屏动画 —— 「UNSEAL / 解封」
 * ------------------------------------------------------------
 * 自包含 Web Component，零依赖、零网络请求，可直接用于 Electron / Tauri / 浏览器。
 *
 *   <keyrealm-splash autoplay skippable></keyrealm-splash>
 *
 * 事件（均为 bubbles + composed，宿主可直接监听）：
 *   splash:play    动画开始
 *   splash:reveal  点击后的转场开始（主界面此时应已渲染）
 *   splash:done    开屏结束，组件自动隐藏
 *
 * 等待阶段（开场结束后）不自动跳转：点击任意处 / Enter / Space / ESC 进入；
 * 主界面应在此之前渲染好。
 *
 * 方法：play() / enter() / skip() / replay() / seek(ms)
 *
 * 属性：
 *   autoplay         连接后自动播放
 *   duration         开场时段基准（毫秒，默认 3500；内部节奏按比例缩放）
 *   skippable        "false" 时隐藏跳过按钮 / 禁用 ESC
 *   sound            开启合成音效（WebAudio，无音频资源；默认关闭）
 *   reveal           "false" 时转场退化为直接淡出
 *   theme            aurora | mint | nebula（默认 obsidian 黑曜 + 青柠）
 *
 * 点击转场固定为「钥匙孔隧道」：盘面强推进 + 锁孔虹膜 + 4 层同心环 + 柔光。
 *
 * 主题通过 --kr-* CSS 变量控制，宿主可在 :root 或元素上覆盖。
 */
(function () {
  'use strict';

  if (!window.customElements || customElements.get('keyrealm-splash')) return;

  var BASE = 3500;                 // 开场时段基准（ms），duration 属性按此缩放
  var HOLD_AT = 3350;              // 开场结束、进入「等待点击」的时点
  var HOLD_LEAD = 150;             // 进入后到虹膜展开的留白
  var REVEAL_DUR = 1080;           // 虹膜展开时长
  var UNLOCK_AT = 2200;            // 开锁「咔哒」时刻
  var SEEK_HOLD = 3000;            // 仅 seek 调试用的假想等待时长
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  /* 虹膜展开：慢起、后段加速，像镜头真正「穿」进钥匙孔 */
  var easeIris = function (p) {
    return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
  };

  /* ── 几何小工具 ─────────────────────────────────────────── */
  function polar(cx, cy, r, deg) {
    var a = (deg * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }
  function arcPath(r, from, to) {
    var p1 = polar(320, 320, r, from);
    var p2 = polar(320, 320, r, to);
    var large = Math.abs(to - from) > 180 ? 1 : 0;
    return 'M' + p1[0].toFixed(2) + ' ' + p1[1].toFixed(2) +
      'A' + r + ' ' + r + ' 0 ' + large + ' 1 ' +
      p2[0].toFixed(2) + ' ' + p2[1].toFixed(2);
  }

  /* 外圈刻度 / 六边形 / 扫描弧——一次生成，实例共用 */
  function buildRings() {
    var ticks = '';
    for (var i = 0; i < 72; i++) {
      var deg = i * 5 - 90;
      var major = i % 6 === 0;
      var p1 = polar(320, 320, major ? 291 : 296, deg);
      var p2 = polar(320, 320, 304, deg);
      ticks += '<line x1="' + p1[0].toFixed(2) + '" y1="' + p1[1].toFixed(2) +
        '" x2="' + p2[0].toFixed(2) + '" y2="' + p2[1].toFixed(2) +
        '" stroke="' + (major ? 'rgba(163,230,53,.42)' : 'rgba(244,244,245,.13)') +
        '" stroke-width="' + (major ? 1.4 : 1) + '"/>';
    }
    var hex = [];
    var nodes = '';
    for (var j = 0; j < 6; j++) {
      var pt = polar(320, 320, 214, j * 60 - 90);
      hex.push(pt[0].toFixed(2) + ',' + pt[1].toFixed(2));
      nodes += '<circle cx="' + pt[0].toFixed(2) + '" cy="' + pt[1].toFixed(2) +
        '" r="2.6" fill="rgba(163,230,53,.62)"/>';
    }
    return '' +
      '<g class="kr-rot-slow"><circle cx="320" cy="320" r="282" fill="none" ' +
      'stroke="rgba(34,211,238,.16)" stroke-width="1" stroke-dasharray="1 9"/></g>' +
      '<circle class="kr-draw" data-start="200" data-dur="900" cx="320" cy="320" r="304" ' +
      'fill="none" stroke="rgba(244,244,245,.07)" stroke-width="1"/>' +
      '<g class="kr-ticks">' + ticks + '</g>' +
      '<path class="kr-draw" data-start="320" data-dur="800" d="' + arcPath(252, -58, 58) +
      '" fill="none" stroke="rgba(163,230,53,.45)" stroke-width="1.4"/>' +
      '<path class="kr-draw" data-start="440" data-dur="800" d="' + arcPath(252, 122, 238) +
      '" fill="none" stroke="rgba(163,230,53,.2)" stroke-width="1"/>' +
      '<polygon class="kr-draw" data-start="650" data-dur="800" points="' + hex.join(' ') +
      '" fill="none" stroke="rgba(244,244,245,.13)" stroke-width="1"/>' +
      '<g class="kr-nodes">' + nodes + '</g>' +
      '<circle class="kr-sweep" cx="320" cy="320" r="292" fill="none" ' +
      'stroke="rgba(34,211,238,.5)" stroke-width="2" stroke-linecap="round" ' +
      'stroke-dasharray="64 1770"/>' +
      '<g stroke="rgba(244,244,245,.06)" stroke-width="1">' +
      '<line x1="10" y1="320" x2="196" y2="320"/>' +
      '<line x1="444" y1="320" x2="630" y2="320"/>' +
      '<line x1="320" y1="10" x2="320" y2="196"/>' +
      '<line x1="320" y1="444" x2="320" y2="630"/>' +
      '</g>';
  }
  var RINGS = buildRings();

  /* 大尺度轨道环：铺满画面外围，填掉空旷感，但不抢中心 */
  function buildOrbit() {
    var ticks = '';
    for (var i = 0; i < 72; i++) {
      var deg = i * 5 - 90;
      var major = i % 6 === 0;
      var p1 = polar(500, 500, major ? 454 : 462, deg);
      var p2 = polar(500, 500, 472, deg);
      ticks += '<line x1="' + p1[0].toFixed(1) + '" y1="' + p1[1].toFixed(1) +
        '" x2="' + p2[0].toFixed(1) + '" y2="' + p2[1].toFixed(1) +
        '" stroke="' + (major ? 'rgba(163,230,53,.2)' : 'rgba(244,244,245,.08)') +
        '" stroke-width="1"/>';
    }
    function arc500(r, from, to) {
      var p1 = polar(500, 500, r, from);
      var p2 = polar(500, 500, r, to);
      return 'M' + p1[0].toFixed(1) + ' ' + p1[1].toFixed(1) +
        'A' + r + ' ' + r + ' 0 ' + (Math.abs(to - from) > 180 ? 1 : 0) + ' 1 ' +
        p2[0].toFixed(1) + ' ' + p2[1].toFixed(1);
    }
    var nodes = '';
    for (var n = 0; n < 6; n++) {
      var pt = polar(500, 500, 434, n * 60 - 90);
      nodes += '<circle cx="' + pt[0].toFixed(1) + '" cy="' + pt[1].toFixed(1) +
        '" r="2.2" fill="rgba(163,230,53,.4)"/>';
    }
    var glints = '';
    [68, 158, 250, 338].forEach(function (deg, i) {
      var p = polar(500, 500, 472, deg);
      glints += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="' + (i === 0 ? 9 : 6) + '" fill="rgba(163,230,53,.07)"/>' +
        '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="' + (i === 0 ? 2.6 : 1.7) + '" fill="rgba(200,255,150,.6)"/>';
    });
    return '' +
      '<g class="kr-orbit-spin">' +
      '<circle cx="500" cy="500" r="472" stroke="rgba(244,244,245,.05)" stroke-width="1"/>' +
      '<circle cx="500" cy="500" r="512" stroke="rgba(34,211,238,.11)" stroke-width="1" stroke-dasharray="2 16"/>' +
      '<path d="' + arc500(440, -58, 58) + '" stroke="rgba(163,230,53,.10)" stroke-width="1.2"/>' +
      '<path d="' + arc500(440, 122, 238) + '" stroke="rgba(34,211,238,.08)" stroke-width="1"/>' +
      '<path d="' + arc500(408, 20, 100) + '" stroke="rgba(244,244,245,.055)" stroke-width="1"/>' +
      '<g>' + ticks + '</g><g>' + nodes + '</g><g>' + glints + '</g>' +
      '</g>' +
      '<g stroke="rgba(244,244,245,.04)" stroke-width="1">' +
      '<line x1="30" y1="500" x2="250" y2="500"/>' +
      '<line x1="750" y1="500" x2="970" y2="500"/>' +
      '<line x1="500" y1="30" x2="500" y2="250"/>' +
      '<line x1="500" y1="750" x2="500" y2="970"/>' +
      '</g>';
  }
  var ORBIT = buildOrbit();

  /* ── 样式（Shadow DOM 内部，完全隔离） ───────────────────── */
  var STYLE = `
  :host{
    --kr-bg:#07080c;
    --kr-ink:#f4f4f5;
    --kr-dim:rgba(244,244,245,.56);
    --kr-primary:#a3e635;
    --kr-green:#4ade80;
    --kr-cyan:#22d3ee;
    --kr-blue:#3b82f6;
    --kr-violet:#8b5cf6;
    --kr-font:'Inter','SF Pro Display','Segoe UI','PingFang SC','Microsoft YaHei',system-ui,sans-serif;
    --kr-mono:'JetBrains Mono','SFMono-Regular','Cascadia Code','Roboto Mono','DejaVu Sans Mono',monospace;
    position:fixed; inset:0; z-index:99999; display:block;
    color:var(--kr-ink); font-family:var(--kr-font);
    -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
    contain:layout paint;
  }
  :host([hidden]){display:none!important}
  :host([theme="aurora"]){--kr-primary:#818cf8;--kr-green:#a78bfa;--kr-cyan:#22d3ee}
  :host([theme="mint"]){--kr-primary:#34d399;--kr-green:#2dd4bf;--kr-cyan:#38bdf8}
  :host([theme="nebula"]){--kr-primary:#a78bfa;--kr-green:#f472b6;--kr-cyan:#38bdf8}
  *,*::before,*::after{box-sizing:border-box}

  .kr-root{
    position:absolute; inset:0; overflow:hidden;
    opacity:0; user-select:none; cursor:default;
  }
  .kr-mask{position:absolute; inset:0; z-index:10; background:var(--kr-bg)}
  .kr-mask.is-revealing{
    -webkit-mask-image:radial-gradient(circle at var(--kr-hx,50%) var(--kr-hy,42%),
      transparent 0, transparent var(--kr-hole,0px), #000 calc(var(--kr-hole,0px) + 2px));
    mask-image:radial-gradient(circle at var(--kr-hx,50%) var(--kr-hy,42%),
      transparent 0, transparent var(--kr-hole,0px), #000 calc(var(--kr-hole,0px) + 2px));
  }

  /* ── 背景：极光 / 光带 / 光斑 / 轨道 / 点阵 / 粒子 / 暗角 / 噪点 ── */
  .kr-bg{position:absolute; inset:0; overflow:hidden}
  .kr-aurora{position:absolute; z-index:0; border-radius:50%; will-change:transform}
  .kr-a1{width:80vmax;height:80vmax;left:-24vmax;top:-32vmax;
    background:radial-gradient(circle at 50% 50%,rgba(59,130,246,.40),rgba(59,130,246,.06) 46%,transparent 66%);
    animation:kr-drift1 24s ease-in-out infinite}
  .kr-a2{width:74vmax;height:74vmax;right:-28vmax;bottom:-32vmax;
    background:radial-gradient(circle at 50% 50%,rgba(139,92,246,.36),rgba(139,92,246,.05) 46%,transparent 66%);
    animation:kr-drift2 28s ease-in-out infinite}
  .kr-a3{width:48vmax;height:48vmax;left:50%;top:44%;
    background:radial-gradient(circle at 50% 50%,rgba(34,211,238,.22),rgba(163,230,53,.05) 45%,transparent 68%);
    opacity:.8; animation:kr-breathe 9s ease-in-out infinite}
  .kr-a4{width:58vmax;height:58vmax;left:46%;top:54%;
    background:radial-gradient(circle at 50% 50%,rgba(236,72,153,.16),rgba(139,92,246,.06) 45%,transparent 68%);
    opacity:.75; animation:kr-drift3 30s ease-in-out infinite}
  .kr-a5{width:76vmax;height:38vmax;left:50%;top:-16vmax;
    background:radial-gradient(ellipse at 50% 50%,rgba(56,189,248,.17),rgba(59,130,246,.05) 46%,transparent 70%);
    opacity:.7; animation:kr-breathe-top 12s ease-in-out infinite}
  @keyframes kr-drift1{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(5vmax,3vmax,0) scale(1.07)}}
  @keyframes kr-drift2{0%,100%{transform:translate3d(0,0,0) scale(1.03)}50%{transform:translate3d(-4vmax,-3vmax,0) scale(.96)}}
  @keyframes kr-drift3{0%,100%{transform:translate(-50%,-50%) scale(1)}50%{transform:translate(-47%,-54%) scale(1.1)}}
  @keyframes kr-breathe-top{0%,100%{transform:translate(-50%,0) scale(1);opacity:.55}50%{transform:translate(-50%,2vmax) scale(1.06);opacity:.9}}
  @keyframes kr-breathe{0%,100%{transform:translate(-50%,-50%) scale(1);opacity:.72}50%{transform:translate(-50%,-50%) scale(1.12);opacity:1}}

  /* 斜向极光带：呼应 App 图标里的虹彩 */
  .kr-ribbon{position:absolute; z-index:1; width:150vmax; height:24vmax;
    opacity:.42; pointer-events:none; will-change:transform;
    -webkit-mask-image:radial-gradient(ellipse at 50% 50%,#000 0,rgba(0,0,0,.65) 45%,transparent 76%);
    mask-image:radial-gradient(ellipse at 50% 50%,#000 0,rgba(0,0,0,.65) 45%,transparent 76%)}
  .kr-r1{left:-34vmax; top:12vmax;
    background:linear-gradient(90deg,transparent,rgba(59,130,246,.24),rgba(139,92,246,.26),rgba(34,211,238,.16),transparent);
    animation:kr-ribbon1 30s ease-in-out infinite}
  .kr-r2{right:-40vmax; bottom:4vmax;
    background:linear-gradient(90deg,transparent,rgba(244,114,182,.18),rgba(167,139,250,.22),rgba(34,211,238,.14),transparent);
    animation:kr-ribbon2 36s ease-in-out infinite}
  @keyframes kr-ribbon1{0%,100%{transform:rotate(-18deg) translate3d(0,0,0)}50%{transform:rotate(-14deg) translate3d(3vmax,-2vmax,0)}}
  @keyframes kr-ribbon2{0%,100%{transform:rotate(15deg) translate3d(0,0,0)}50%{transform:rotate(18deg) translate3d(-3vmax,2vmax,0)}}

  /* 远景光斑 */
  .kr-bokeh{position:absolute; z-index:1; border-radius:50%; opacity:.42; pointer-events:none}
  .kr-b1{width:17vmin;height:17vmin;left:7%;top:16%;
    background:radial-gradient(circle,rgba(59,130,246,.55),transparent 70%);
    animation:kr-bokeh1 20s ease-in-out infinite}
  .kr-b2{width:11vmin;height:11vmin;right:12%;top:24%;
    background:radial-gradient(circle,rgba(167,139,250,.5),transparent 70%);
    animation:kr-bokeh2 24s ease-in-out infinite}
  .kr-b3{width:9vmin;height:9vmin;left:16%;bottom:20%;
    background:radial-gradient(circle,rgba(34,211,238,.5),transparent 70%);
    animation:kr-bokeh1 28s ease-in-out infinite reverse}
  .kr-b4{width:13vmin;height:13vmin;right:8%;bottom:26%;
    background:radial-gradient(circle,rgba(244,114,182,.4),transparent 70%);
    animation:kr-bokeh2 22s ease-in-out infinite}
  .kr-b5{width:6vmin;height:6vmin;left:44%;top:8%;
    background:radial-gradient(circle,rgba(163,230,53,.35),transparent 70%);
    animation:kr-bokeh1 26s ease-in-out infinite}
  @keyframes kr-bokeh1{0%,100%{transform:translate3d(0,0,0) scale(1);opacity:.3}50%{transform:translate3d(2vmin,-2vmin,0) scale(1.12);opacity:.5}}
  @keyframes kr-bokeh2{0%,100%{transform:translate3d(0,0,0) scale(1.05);opacity:.38}50%{transform:translate3d(-2vmin,2vmin,0) scale(.95);opacity:.24}}

  /* 徽记下方的光池，给画面一点“落点” */
  .kr-floor{position:absolute; z-index:2; left:50%; top:70%; width:64vmin; height:14vmin;
    transform:translate(-50%,-50%); opacity:.8; pointer-events:none;
    background:radial-gradient(ellipse at center,rgba(163,230,53,.10),rgba(34,211,238,.06) 38%,transparent 70%);
    animation:kr-floor 7s ease-in-out infinite}
  @keyframes kr-floor{0%,100%{opacity:.55;transform:translate(-50%,-50%) scale(.96)}50%{opacity:.95;transform:translate(-50%,-50%) scale(1.04)}}

  .kr-grid{position:absolute; z-index:3; inset:-12%;
    background-image:radial-gradient(rgba(255,255,255,.055) 1px,transparent 1px);
    background-size:32px 32px; opacity:.55;
    -webkit-mask-image:radial-gradient(ellipse at 50% 46%,#000 0,rgba(0,0,0,.5) 38%,transparent 68%);
    mask-image:radial-gradient(ellipse at 50% 46%,#000 0,rgba(0,0,0,.5) 38%,transparent 68%)}
  .kr-canvas{position:absolute; z-index:4; inset:0; width:100%; height:100%}
  .kr-orbit{position:absolute; z-index:5; left:50%; top:47%; width:min(145vmin,1700px); aspect-ratio:1;
    transform:translate(-50%,-50%); opacity:.7; pointer-events:none}
  .kr-orbit-spin{transform-origin:500px 500px; transform-box:view-box; animation:kr-spin 160s linear infinite}
  .kr-scan{position:absolute; z-index:6; left:0; right:0; top:-24vh; height:30vh; opacity:.5;
    background:linear-gradient(180deg,transparent,rgba(163,230,53,.028) 44%,rgba(163,230,53,.055) 50%,rgba(163,230,53,.028) 56%,transparent);
    animation:kr-scan 8s linear infinite; pointer-events:none;
    filter:blur(2px)}
  @keyframes kr-scan{0%{transform:translateY(0)}100%{transform:translateY(124vh)}}
  .kr-vignette{position:absolute; z-index:7; inset:0;
    background:radial-gradient(ellipse at 50% 44%,transparent 26%,rgba(0,0,0,.34) 62%,rgba(0,0,0,.8) 100%)}
  .kr-grain{position:absolute; z-index:8; inset:0; opacity:.2; pointer-events:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.38'/%3E%3C/svg%3E")}

  /* 四角 HUD 微标签与边缘刻度 */
  .kr-corner{position:absolute; top:22px; z-index:20; font-family:var(--kr-mono); font-size:8.5px;
    letter-spacing:.24em; color:rgba(244,244,245,.26); opacity:0; pointer-events:none; white-space:nowrap}
  .kr-corner b{font-weight:400; color:rgba(163,230,53,.55)}
  .kr-corner-tl{left:26px}
  .kr-corner-tr{right:26px; text-align:right}
  .kr-ruler{position:absolute; z-index:20; top:50%; width:6px; height:38vh; transform:translateY(-50%);
    opacity:0; pointer-events:none;
    background:
      repeating-linear-gradient(to bottom, rgba(163,230,53,.28) 0 1px, transparent 1px 90px),
      repeating-linear-gradient(to bottom, rgba(244,244,245,.17) 0 1px, transparent 1px 18px);
    -webkit-mask-image:linear-gradient(to bottom,transparent,#000 16%,#000 84%,transparent);
    mask-image:linear-gradient(to bottom,transparent,#000 16%,#000 84%,transparent)}
  .kr-ruler-l{left:12px}
  .kr-ruler-r{right:12px}

  /* ── 舞台 ────────────────────────────────────────────── */
  .kr-stage{position:absolute; inset:0; z-index:10; display:flex; flex-direction:column;
    align-items:center; justify-content:center; padding:3vh 24px 5vh}
  .kr-dial-wrap{position:relative; flex:none; width:min(72vmin,740px); aspect-ratio:1;
    transform-origin:50% 42%; will-change:transform,filter}
  .kr-parallax{position:absolute; inset:0; will-change:transform}
  .kr-halo{position:absolute; left:50%; top:50%; width:88%; height:88%;
    transform:translate(-50%,-50%); border-radius:50%;
    background:radial-gradient(circle at 50% 45%,rgba(163,230,53,.14),rgba(34,211,238,.06) 38%,transparent 62%);
    animation:kr-halo 6s ease-in-out infinite}
  @keyframes kr-halo{0%,100%{opacity:.55;transform:translate(-50%,-50%) scale(.98)}50%{opacity:.95;transform:translate(-50%,-50%) scale(1.04)}}
  .kr-dial{position:absolute; inset:0; display:grid; place-items:center}
  .kr-rings{position:absolute; inset:0; width:100%; height:100%; overflow:visible; will-change:transform}
  .kr-rot-slow{transform-origin:320px 320px; transform-box:view-box; animation:kr-spin-rev 40s linear infinite}
  .kr-ticks{transform-origin:320px 320px; transform-box:view-box}
  .kr-sweep{transform-origin:320px 320px; transform-box:view-box; animation:kr-spin 9s linear infinite}
  @keyframes kr-spin{to{transform:rotate(360deg)}}
  @keyframes kr-spin-rev{to{transform:rotate(-360deg)}}

  .kr-emblem{position:relative; width:42%; aspect-ratio:1; display:grid; place-items:center;
    animation:kr-float 7s ease-in-out infinite}
  @keyframes kr-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
  .kr-mark{width:100%; height:100%; overflow:visible; transform-origin:50% 50%;
    filter:drop-shadow(0 0 16px rgba(163,230,53,.22)) drop-shadow(0 0 42px rgba(59,130,246,.18))}
  .kr-mark-lines{stroke-linecap:round; stroke-linejoin:round}
  .kr-s1{stop-color:var(--kr-primary)}
  .kr-s2{stop-color:var(--kr-green)}
  .kr-s3{stop-color:var(--kr-cyan)}
  .kr-shackle{transform-box:fill-box; transform-origin:0% 100%}
  .kr-mark-fill{opacity:.08; transform-box:fill-box; transform-origin:50% 50%;
    will-change:transform,opacity}
  .kr-keyhole-glow-fill{opacity:0}

  /* ── 点击转场：钥匙孔隧道 + 柔光 ──────────────────── */
  .kr-tunnel{position:absolute; inset:0; z-index:24; pointer-events:none}
  .kr-tunnel i{position:absolute; left:var(--kr-hx,50%); top:var(--kr-hy,42%);
    width:24vmin; height:24vmin; margin:-12vmin 0 0 -12vmin; border-radius:50%;
    border:1px solid rgba(163,230,53,.45); opacity:0; transform:scale(.1);
    box-shadow:0 0 18px rgba(163,230,53,.18), inset 0 0 18px rgba(34,211,238,.12);
    will-change:transform,opacity}
  .kr-flash{position:absolute; inset:0; z-index:29; pointer-events:none; opacity:0;
    background:radial-gradient(circle at var(--kr-hx,50%) var(--kr-hy,42%),
      rgba(240,255,220,.55) 0,rgba(163,230,53,.18) 22%,rgba(34,211,238,.08) 46%,transparent 76%);
    mix-blend-mode:screen}

  /* ── 等待进入：3s 倒计时 / 点击任意处 ──────────────── */
  .kr-hold{position:absolute; left:50%; bottom:clamp(44px,7.2vh,68px); z-index:22;
    display:inline-flex; align-items:center; gap:10px; height:36px; padding:0 16px;
    border-radius:999px; border:1px solid rgba(163,230,53,.22);
    background:rgba(10,12,10,.42); color:rgba(244,244,245,.78);
    font-family:var(--kr-font); font-size:11.5px; letter-spacing:.06em;
    opacity:0; pointer-events:none; cursor:pointer; backdrop-filter:blur(10px);
    transform:translate(-50%,10px); will-change:transform,opacity;
    transition:color .2s,border-color .2s,background .2s}
  .kr-hold.ready{pointer-events:auto}
  .kr-hold:hover{color:#d9f99d; border-color:rgba(163,230,53,.5); background:rgba(163,230,53,.08)}
  .kr-hold:focus-visible{outline:2px solid var(--kr-primary); outline-offset:3px}
  .kr-hold-dot{width:6px; height:6px; flex:none; border-radius:50%; background:var(--kr-primary);
    box-shadow:0 0 10px rgba(163,230,53,.8); animation:kr-dot 1.6s ease-in-out infinite}
  @keyframes kr-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.72)}}
  .kr-hold-sep{width:1px; height:12px; background:rgba(244,244,245,.16)}
  .kr-hold-hint{font-family:var(--kr-mono); font-size:9px; letter-spacing:.12em; color:rgba(244,244,245,.36)}
  .kr-hold-bar{position:absolute; left:14px; right:14px; bottom:0; height:1px; overflow:hidden; opacity:.65}
  .kr-hold-bar i{display:block; height:100%;
    background:linear-gradient(90deg,transparent,var(--kr-cyan),var(--kr-primary),transparent);
    animation:kr-hold-breathe 2.6s ease-in-out infinite}
  @keyframes kr-hold-breathe{0%,100%{opacity:.18}50%{opacity:.85}}
  .kr-root.is-reduced .kr-hold-bar{display:none}
  .kr-root.is-reduced .kr-hold-dot{animation:none}
  .kr-root.is-holding{cursor:pointer}
  .kr-root.is-holding .kr-skip{pointer-events:none}
  .kr-hole-ring{position:absolute; z-index:25; left:var(--kr-hx,50%); top:var(--kr-hy,42%);
    width:0; height:0; border-radius:50%; transform:translate(-50%,-50%); opacity:0;
    border:2px solid rgba(236,255,190,.85); pointer-events:none;
    box-shadow:0 0 34px rgba(163,230,53,.55), inset 0 0 26px rgba(34,211,238,.35);
    will-change:width,height,opacity}

  /* ── 无障碍：减少动态偏好 ─────────────────────────────── */
  .kr-root.is-reduced .kr-canvas,
  .kr-root.is-reduced .kr-boot,
  .kr-root.is-reduced .kr-pulse,
  .kr-root.is-reduced .kr-bloom,
  .kr-root.is-reduced .kr-skip,
  .kr-root.is-reduced .kr-hole-ring{display:none}
  .kr-root.is-reduced .kr-dial-wrap{filter:none; transform:none}
  .kr-root.is-reduced .kr-mark-fill{opacity:1; transform:none}
  .kr-root.is-reduced .kr-mark-lines{opacity:0}
  .kr-root.is-reduced .kr-keyhole-glow-fill{opacity:.45}
  .kr-root.is-reduced .kr-aurora,
  .kr-root.is-reduced .kr-ribbon,
  .kr-root.is-reduced .kr-bokeh,
  .kr-root.is-reduced .kr-floor,
  .kr-root.is-reduced .kr-orbit-spin,
  .kr-root.is-reduced .kr-scan,
  .kr-root.is-reduced .kr-halo,
  .kr-root.is-reduced .kr-emblem,
  .kr-root.is-reduced .kr-rot-slow,
  .kr-root.is-reduced .kr-sweep{animation:none!important}
  `;

  /* ── DOM ──────────────────────────────────────────────── */
  var MARKUP = `
  <div class="kr-root" role="dialog" aria-modal="true" aria-label="Keyrealm 正在启动">
    <div class="kr-mask">
      <div class="kr-bg" aria-hidden="true">
        <div class="kr-aurora kr-a1"></div>
        <div class="kr-aurora kr-a2"></div>
        <div class="kr-aurora kr-a3"></div>
        <div class="kr-aurora kr-a4"></div>
        <div class="kr-aurora kr-a5"></div>
        <div class="kr-ribbon kr-r1"></div>
        <div class="kr-ribbon kr-r2"></div>
        <div class="kr-bokeh kr-b1"></div>
        <div class="kr-bokeh kr-b2"></div>
        <div class="kr-bokeh kr-b3"></div>
        <div class="kr-bokeh kr-b4"></div>
        <div class="kr-bokeh kr-b5"></div>
        <div class="kr-floor"></div>
        <div class="kr-grid"></div>
        <canvas class="kr-canvas"></canvas>
        <svg class="kr-orbit" viewBox="0 0 1000 1000" fill="none">${ORBIT}</svg>
        <div class="kr-scan"></div>
        <div class="kr-vignette"></div>
        <div class="kr-grain"></div>
      </div>

      <div class="kr-corner kr-corner-tl" aria-hidden="true">KEYREALM <b>//</b> SECURE BOOT</div>
      <div class="kr-corner kr-corner-tr" aria-hidden="true">LOCAL SESSION <b>·</b> 0x1F</div>
      <div class="kr-ruler kr-ruler-l" aria-hidden="true"></div>
      <div class="kr-ruler kr-ruler-r" aria-hidden="true"></div>

      <div class="kr-stage">
        <div class="kr-dial-wrap">
          <div class="kr-parallax">
            <div class="kr-halo"></div>
            <div class="kr-dial">
              <svg class="kr-rings" viewBox="0 0 640 640" fill="none" aria-hidden="true">${RINGS}</svg>
              <div class="kr-emblem">
                <div class="kr-bloom"></div>
                <div class="kr-pulse" id="kr-p1"></div>
                <div class="kr-pulse" id="kr-p2"></div>
                <svg class="kr-mark" viewBox="0 0 240 240" fill="none" aria-hidden="true">
                  <defs>
                    <linearGradient id="krStroke" x1="60" y1="40" x2="190" y2="210" gradientUnits="userSpaceOnUse">
                      <stop class="kr-s1"/><stop class="kr-s2" offset=".55"/><stop class="kr-s3" offset="1"/>
                    </linearGradient>
                    <linearGradient id="krFill" x1="46" y1="30" x2="197" y2="216" gradientUnits="userSpaceOnUse">
                      <stop stop-color="#ffffff"/>
                      <stop offset=".55" stop-color="#f7feff"/>
                      <stop offset="1" stop-color="#dff6ff"/>
                    </linearGradient>
                    <radialGradient id="krKeyGlow">
                      <stop stop-color="rgba(226,255,170,.95)"/>
                      <stop offset=".42" stop-color="rgba(163,230,53,.38)"/>
                      <stop offset="1" stop-color="rgba(163,230,53,0)"/>
                    </radialGradient>
                    <filter id="krGlow" x="-45%" y="-45%" width="190%" height="190%">
                      <feGaussianBlur stdDeviation="2.6" result="b"/>
                      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
                    </filter>
                    <filter id="krFillGlow" x="-25%" y="-25%" width="150%" height="150%">
                      <feGaussianBlur stdDeviation="2.4" result="b"/>
                      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
                    </filter>
                    <mask id="krBodyMask" maskUnits="userSpaceOnUse" x="130" y="70" width="80" height="60">
                      <rect x="142" y="77" width="55" height="45" rx="9" fill="#fff"/>
                      <circle cx="169" cy="92" r="5.4" fill="#000"/>
                      <rect x="166.4" y="92" width="5.2" height="13.2" rx="2.6" fill="#000"/>
                    </mask>
                  </defs>

                  <!-- 线框：按真实 Logo 的骨架先描出来 -->
                  <g class="kr-mark-lines" stroke="url(#krStroke)" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round" filter="url(#krGlow)">
                    <path class="kr-draw" data-start="1000" data-dur="700" d="M58.8 38.5 V211"/>
                    <path class="kr-draw" data-start="1120" data-dur="620" d="M74 112.5 L148 88.5"/>
                    <path class="kr-draw" data-start="1180" data-dur="640" d="M74 112.5 L147 178"/>
                    <path class="kr-draw" data-start="1300" data-dur="420"
                      d="M132 173 H185 Q192 173 192 180 V182 H171 V197 H192 V209 Q192 216 185 216 H162 V203 H152 V216 H132 Z"/>
                    <rect class="kr-draw" data-start="1580" data-dur="520" x="142" y="77" width="55" height="45" rx="9"/>
                    <g class="kr-shackle">
                      <path class="kr-draw" data-start="1690" data-dur="420"
                        d="M153 77 V52 a14 14 0 0 1 28 0 V77"/>
                    </g>
                    <circle class="kr-draw" data-start="1750" data-dur="240" cx="169" cy="92" r="5.4"/>
                    <path class="kr-draw" data-start="1800" data-dur="220"
                      d="M166.4 92 H171.6 V103.2" stroke-width="1.4"/>
                  </g>

                  <!-- 实体：与 App 图标同源的白色 K + 锁 + 钥匙，1.8s 后凝实 -->
                  <g class="kr-mark-fill" filter="url(#krFillGlow)">
                    <g class="kr-keyhole-glow-fill">
                      <circle cx="169" cy="92" r="14" fill="url(#krKeyGlow)"/>
                      <rect x="164" y="90" width="10" height="20" rx="5" fill="url(#krKeyGlow)"/>
                    </g>
                    <rect x="46" y="37" width="26" height="174" rx="13" fill="url(#krFill)"/>
                    <path d="M74 112.5 L148 88.5" stroke="url(#krFill)" stroke-width="26" stroke-linecap="round"/>
                    <path d="M74 112.5 L147 178" stroke="url(#krFill)" stroke-width="26" stroke-linecap="round"/>
                    <path d="M132 173 H185 Q192 173 192 180 V182 H171 V197 H192 V209 Q192 216 185 216 H162 V203 H152 V216 H132 Z" fill="url(#krFill)"/>
                    <g class="kr-shackle kr-shackle-fill">
                      <path d="M153 77 V52 a14 14 0 0 1 28 0 V77" stroke="url(#krFill)"
                        stroke-width="12" stroke-linecap="butt" stroke-linejoin="round" fill="none"/>
                    </g>
                    <rect class="kr-lock-body" x="142" y="77" width="55" height="45" rx="9"
                      fill="url(#krFill)" mask="url(#krBodyMask)"/>
                  </g>
                </svg>
              </div>
            </div>
          </div>
        </div>

        <div class="kr-copy">
          <div class="kr-word" aria-label="KEYREALM">
            <span class="L k">K</span><span class="L">E</span><span class="L">Y</span><span class="L">R</span><span class="L">E</span><span class="L">A</span><span class="L">L</span><span class="L">M</span>
          </div>
          <div class="kr-tagline">本地加密<i>·</i>凭据保险库</div>
          <div class="kr-boot">
            <div class="kr-boot-line">
              <span class="s1">DERIVING KEY MATERIAL</span>
              <span class="s2">VERIFYING KEYRING INTEGRITY</span>
              <span class="s3">VAULT READY</span>
            </div>
            <div class="kr-bar"><i></i></div>
            <div class="kr-meta"><span>AES-256-GCM</span><span>LOCAL ONLY</span></div>
          </div>
        </div>

        <button class="kr-hold" type="button" part="enter" aria-label="进入 Keyrealm">
          <span class="kr-hold-dot" aria-hidden="true"></span>
          <span>点击任意处进入</span>
          <span class="kr-hold-sep" aria-hidden="true"></span>
          <span class="kr-hold-hint">ENTER ↵</span>
          <span class="kr-hold-bar" aria-hidden="true"><i></i></span>
        </button>
      </div>

      <div class="kr-hud" aria-hidden="true">KEYREALM <b>//</b> LOCAL VAULT</div>
    </div>

    <!-- 点击转场：钥匙孔隧道 + 柔光 -->
    <div class="kr-tunnel" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
    <div class="kr-flash" aria-hidden="true"></div>

    <div class="kr-hole-ring" aria-hidden="true"></div>
    <button class="kr-skip" type="button" part="skip" aria-label="跳过开屏动画">跳过 <kbd>ESC</kbd></button>
  </div>`;

  /* ── 组件 ─────────────────────────────────────────────── */
  class KeyrealmSplash extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.innerHTML = '<style>' + STYLE + '</style>' + MARKUP;

      this._root = this.shadowRoot.querySelector('.kr-root');
      this._mask = this.shadowRoot.querySelector('.kr-mask');
      this._ring = this.shadowRoot.querySelector('.kr-hole-ring');
      this._skipBtn = this.shadowRoot.querySelector('.kr-skip');
      this._holdEl = this.shadowRoot.querySelector('.kr-hold');
      this._tunnel = this.shadowRoot.querySelector('.kr-tunnel');
      this._flashEl = this.shadowRoot.querySelector('.kr-flash');

      this._state = 'idle';
      this._anims = [];
      this._timers = [];
      this._done = false;
      this._revealing = false;
      this._holding = false;
      this._entering = false;
      this._scale = 1;
      this._px = 0;
      this._py = 0;
      this._parallax = { x: 0, y: 0 };
      this._parallaxOn = false;
      this._audio = null;

      this._skipBtn.addEventListener('click', () => this.skip());
      /* 等待阶段：点击画面任意处立即进入（进入按钮的点击也会冒泡到这里） */
      this._root.addEventListener('click', () => {
        if (this._holding) this._enter('click');
      });
    }

    static get observedAttributes() { return ['duration', 'theme', 'skippable']; }

    connectedCallback() {
      if (this.hasAttribute('autoplay') && !this._autoStarted) {
        this._autoStarted = true;
        var self = this;
        requestAnimationFrame(function () { self.play(); });
      }
    }

    disconnectedCallback() {
      this._cleanup();
    }

    attributeChangedCallback(name, oldValue, value) {
      if (name === 'skippable' && this._skipBtn) {
        this._skipBtn.style.display = value === 'false' ? 'none' : '';
      }
    }

    /* ── 公共 API ─────────────────────────────────────── */
    play() {
      if (this._state === 'playing') return;
      this._reset();
      this.hidden = false;
      this._state = 'playing';
      this._done = false;
      this._scale = clamp((parseFloat(this.getAttribute('duration')) || BASE) / BASE, 0.35, 3);
      this._slow = clamp(parseFloat(this.getAttribute('slow')) || 1, 1, 12);
      this._startedAt = performance.now();
      this._reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

      this._bindGlobal();
      this.dispatchEvent(new CustomEvent('splash:play', { bubbles: true, composed: true }));

      if (this._reduce) { this._playReduced(); return; }

      this._setupAudio();
      this._buildAnimations();
      this._initCanvas();
      this._loop();
      this._schedule();
    }

    skip() {
      if (this._state !== 'playing' || this._entering) return;
      if (this._reduce) { this._enter('skip'); return; }
      if (this._holding) { this._enter('skip'); return; }
      this._clearTimers();
      this._startReveal(true);
    }

    /** 立即进入：播放收尾镜头并揭开主界面。等待阶段点击/回车也走这里。 */
    enter() {
      if (this._state === 'playing') this._enter('api');
    }

    replay() {
      this._reset();
      this.hidden = false;
      this.play();
    }

    /**
     * 调试用：把时间轴停在 ms（真实毫秒）。
     * 0–3350 开场；3350–6350 点击等待；之后收尾 + 虹膜。
     */
    seek(ms) {
      this._reset();
      this.hidden = false;
      this._state = 'seek';
      this._scale = clamp((parseFloat(this.getAttribute('duration')) || BASE) / BASE, 0.35, 3);
      this._reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

      if (this._reduce) {
        this._root.classList.add('is-reduced');
        this._root.style.opacity = '1';
        var hold0 = this._holdEl;
        hold0.classList.add('ready');
        hold0.style.opacity = '1';
        hold0.style.transform = 'translate(-50%, 0)';
        return;
      }

      var scale = this._scale;
      var introEnd = HOLD_AT * scale;
      var holdMs = SEEK_HOLD;
      var enterAt = introEnd + holdMs;
      var revealStart = enterAt + HOLD_LEAD;
      var introT = Math.min(ms, introEnd);

      this._buildAnimations();
      this._anims.forEach(function (a) {
        try { a.pause(); a.currentTime = introT; } catch (e) { /* ignore */ }
      });
      this._initCanvas();
      this._seekT = ms;
      this._simulateTo(introT);

      if (ms >= introEnd) {
        this._q('.kr-boot').style.opacity = '0';
        var hold = this._holdEl;
        hold.style.opacity = '1';
        hold.style.transform = 'translate(-50%, 0)';
        hold.classList.add('ready');
        this._skipBtn.style.display = 'none';

        if (ms >= enterAt) {
          var op = clamp((ms - enterAt) / 830, 0, 1);
          var copy = this._q('.kr-copy');
          copy.style.opacity = String(1 - op);
          copy.style.transform = 'translateY(' + (-14 * op).toFixed(1) + 'px)';
          copy.style.filter = 'blur(' + (6 * op).toFixed(1) + 'px)';
          var dial = this._q('.kr-dial-wrap');
          dial.style.transform = 'scale(' + (1 + 0.16 * op).toFixed(3) + ')';
          dial.style.filter = 'blur(' + (5 * op).toFixed(1) +
            'px) brightness(' + (1 + 0.18 * op).toFixed(2) + ')';
          this._q('.kr-rings').style.transform = 'rotate(' + (18 * op).toFixed(1) + 'deg)';
          this._q('.kr-hud').style.display = 'none';
          hold.style.opacity = String(Math.max(0, 1 - (ms - enterAt) / 220));
        }
      }

      this._applySeekReveal(ms, revealStart);
    }

    /* ── 内部：重置 / 清理 ─────────────────────────────── */
    _reset() {
      this._cleanup();
      this._done = false;
      this._revealing = false;
      this._holding = false;
      this._entering = false;
      this._state = 'idle';
      this._mask.classList.remove('is-revealing');
      this._root.classList.remove('is-reduced');
      this._root.classList.remove('is-holding');
      this._root.style.opacity = '';
      this.style.opacity = '';
      this.style.removeProperty('--kr-hole');
      this.style.removeProperty('--kr-hx');
      this.style.removeProperty('--kr-hy');
      this._ring.style.width = '0px';
      this._ring.style.height = '0px';
      this._ring.style.opacity = '0';
      this._skipBtn.style.display = '';
      this._skipBtn.style.pointerEvents = '';
      this._flashEl.style.opacity = '';

      var hold = this._holdEl;
      if (hold) {
        hold.classList.remove('ready');
        hold.style.opacity = '';
        hold.style.transform = '';
        hold.style.pointerEvents = '';
        var holdBar = hold.querySelector('.kr-hold-bar i');
        if (holdBar) holdBar.style.transform = '';
      }
      [this._q('.kr-boot'), this._q('.kr-copy'), this._q('.kr-dial-wrap'),
       this._q('.kr-rings'), this._q('.kr-hud')].forEach(function (el) {
        if (!el) return;
        el.style.opacity = '';
        el.style.transform = '';
        el.style.filter = '';
        el.style.display = '';
      });

      this._qAll('.kr-draw').forEach(function (el) {
        el.style.strokeDasharray = '';
        el.style.strokeDashoffset = '';
      });
      this._qAll('.kr-word .L').forEach(function (el) {
        el.style.opacity = '';
        el.style.transform = '';
        el.style.filter = '';
      });
      this._qAll('.kr-boot-line span').forEach(function (el) { el.style.opacity = ''; });
      if (this._ctx) {
        this._ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
      }
    }

    _cleanup() {
      this._clearTimers();
      if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; }
      if (this._revealRAF) { cancelAnimationFrame(this._revealRAF); this._revealRAF = 0; }
      this._anims.forEach(function (a) { try { a.cancel(); } catch (e) { /* ignore */ } });
      this._anims = [];
      this._unbindGlobal();
      if (this._ac && this._ac.state === 'running') {
        try { this._ac.suspend(); } catch (e) { /* ignore */ }
      }
    }

    _clearTimers() {
      this._timers.forEach(function (t) { clearTimeout(t); });
      this._timers = [];
    }

    _finishOut() {
      if (this._state === 'seek' || this._done) return;
      this._done = true;
      this._revealing = false;
      this._holding = false;
      this._state = 'done';
      this._root.classList.remove('is-holding');
      this._cleanup();
      this.hidden = true;
      this.dispatchEvent(new CustomEvent('splash:done', { bubbles: true, composed: true }));
    }

    /* ── 内部：动画 ───────────────────────────────────── */
    _q(s) { return this.shadowRoot.querySelector(s); }
    _qAll(s) { return Array.prototype.slice.call(this.shadowRoot.querySelectorAll(s)); }

    _anim(el, frames, opts) {
      if (!el) return null;
      opts = opts || {};
      var s = opts.real ? 1 : this._scale;
      var slow = opts.real ? (this._slow || 1) : 1;
      var dur = opts.duration != null ? opts.duration : Math.max(1, (opts.end || 0) - (opts.start || 0));
      var anim = el.animate(frames, {
        duration: Math.max(1, dur * s * slow),
        delay: (opts.start || 0) * s * slow,
        easing: opts.easing || 'cubic-bezier(.22,.61,.36,1)',
        fill: opts.fill === undefined ? 'both' : opts.fill,
        iterations: opts.iterations || 1,
      });
      this._anims.push(anim);
      return anim;
    }

    _buildAnimations() {
      var self = this;
      var q = function (s) { return self._q(s); };
      var qa = function (s) { return self._qAll(s); };
      var EASE = 'cubic-bezier(.22,.61,.36,1)';
      var SPRING = 'cubic-bezier(.34,1.4,.5,1)';

      this._anim(q('.kr-root'), [{ opacity: 0 }, { opacity: 1 }],
        { start: 0, end: 300, easing: EASE });

      this._anim(q('.kr-hud'), [{ opacity: 0 }, { opacity: 1 }], { start: 900, end: 1400 });
      qa('.kr-corner').forEach(function (el) {
        self._anim(el, [{ opacity: 0 }, { opacity: 1 }], { start: 800, end: 1400, easing: EASE });
      });
      qa('.kr-ruler').forEach(function (el) {
        self._anim(el, [{ opacity: 0 }, { opacity: .55 }], { start: 1000, end: 1600, easing: EASE });
      });
      if (this.getAttribute('skippable') !== 'false') {
        this._anim(q('.kr-skip'), [
          { opacity: 0, transform: 'translateY(6px)' },
          { opacity: 1, transform: 'translateY(0)' }
        ], { start: 700, end: 1100 });
      }

      /* 刻度环：整组淡入 + 轻微回转 */
      this._anim(q('.kr-ticks'), [{ opacity: 0 }, { opacity: 1 }],
        { start: 420, end: 1250, easing: EASE });
      this._anim(q('.kr-ticks'), [
        { transform: 'rotate(-12deg)' }, { transform: 'rotate(0deg)' }
      ], { start: 420, end: 1300, easing: EASE });

      /* 所有描边元素：按 data-start 依次画出来 */
      qa('.kr-draw').forEach(function (el) {
        var len = 0;
        try { len = el.getTotalLength(); } catch (e) { len = 0; }
        var start = parseFloat(el.dataset.start) || 1000;
        var dur = parseFloat(el.dataset.dur) || 600;
        if (!len) {
          self._anim(el, [{ opacity: 0 }, { opacity: 1 }], { start: start, end: start + 400 });
          return;
        }
        el.style.strokeDasharray = String(len);
        el.style.strokeDashoffset = String(len);
        self._anim(el, [
          { strokeDashoffset: len }, { strokeDashoffset: 0 }
        ], { start: start, end: start + dur, easing: 'cubic-bezier(.4,0,.2,1)' });
      });

      qa('.kr-nodes').forEach(function (el) {
        self._anim(el, [{ opacity: 0 }, { opacity: 1 }], { start: 900, end: 1450, easing: EASE });
      });

      /* 真实 Logo 实体凝实：线框淡出，白色徽记浮现 */
      this._anim(q('.kr-mark-fill'), [
        { opacity: .08, transform: 'scale(.985)' },
        { opacity: 1, transform: 'scale(1)' }
      ], { start: 1780, end: 2190, easing: SPRING });
      this._anim(q('.kr-mark-lines'), [
        { opacity: 1 }, { opacity: 0 }
      ], { start: 1840, end: 2080, easing: EASE });
      this._anim(q('.kr-keyhole-glow-fill'), [
        { opacity: 0 }, { opacity: .95 }, { opacity: .32 }
      ], { start: UNLOCK_AT - 40, end: UNLOCK_AT + 620, easing: EASE });

      /* 开锁：锁梁弹开（线框 + 实体同步） + 徽记脉冲 + 两道能量环 */
      qa('.kr-shackle').forEach(function (el) {
        self._anim(el, [
          { transform: 'rotate(0deg) translate(0,0)' },
          { transform: 'rotate(-21deg) translate(-1px,-2px)' }
        ], { start: UNLOCK_AT, end: UNLOCK_AT + 260, easing: SPRING });
      });
      this._anim(q('.kr-mark'), [
        { transform: 'scale(1)' }, { transform: 'scale(1.075)' }, { transform: 'scale(1)' }
      ], { start: UNLOCK_AT, end: UNLOCK_AT + 380, easing: EASE });
      this._anim(q('.kr-bloom'), [
        { opacity: 0, transform: 'translate(-50%,-50%) scale(.22)' },
        { opacity: .85, offset: .22, transform: 'translate(-50%,-50%) scale(.7)' },
        { opacity: 0, transform: 'translate(-50%,-50%) scale(1.18)' }
      ], { start: UNLOCK_AT - 40, end: UNLOCK_AT + 680, easing: 'cubic-bezier(.16,.7,.3,1)' });

      [q('#kr-p1'), q('#kr-p2')].forEach(function (el, i) {
        self._anim(el, [
          { transform: 'translate(-50%,-50%) scale(.2)', opacity: 0 },
          { opacity: .85, offset: .16 },
          { transform: 'translate(-50%,-50%) scale(1.8)', opacity: 0 }
        ], { start: UNLOCK_AT + i * 150, end: UNLOCK_AT + i * 150 + 900,
             easing: 'cubic-bezier(.16,.7,.3,1)' });
      });

      /* 品牌字：逐字母浮起 */
      qa('.kr-word .L').forEach(function (el, i) {
        var start = 2300 + i * 46;
        self._anim(el, [
          { opacity: 0, transform: 'translateY(16px)', filter: 'blur(7px)' },
          { opacity: 1, transform: 'translateY(0)', filter: 'blur(0px)' }
        ], { start: start, end: start + 640, easing: EASE });
      });
      this._anim(q('.kr-tagline'), [
        { opacity: 0, transform: 'translateY(8px)', filter: 'blur(4px)' },
        { opacity: 1, transform: 'translateY(0)', filter: 'blur(0px)' }
      ], { start: 2620, end: 3120, easing: EASE });

      /* 启动状态行 / 进度条 */
      var lineIn = function (el, a, b) {
        self._anim(el, [
          { opacity: 0 }, { opacity: 1, offset: .18 },
          { opacity: 1, offset: .82 }, { opacity: 0 }
        ], { start: a, end: b, easing: 'linear' });
      };
      lineIn(q('.s1'), 2700, 3060);
      lineIn(q('.s2'), 3060, 3260);
      lineIn(q('.s3'), 3260, 3460);
      this._anim(q('.kr-bar i'), [
        { transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }
      ], { start: 2700, end: 3350, easing: 'cubic-bezier(.4,0,.2,1)' });
      this._anim(q('.kr-meta'), [{ opacity: 0 }, { opacity: 1 }],
        { start: 2800, end: 3200, easing: EASE });

      /* 收尾镜头（文案淡出 + 推进）改由 _enter() 触发：先停留 3s 等待点击 */
    }

    _schedule() {
      var self = this;
      var s = this._scale;
      this._timers.push(setTimeout(function () { self._startHold(); }, HOLD_AT * s));
      if (this.hasAttribute('sound')) {
        this._timers.push(setTimeout(function () { self._playUnlockSound(); }, UNLOCK_AT * s));
      }
    }

    /* ── 等待进入 / 收尾 ──────────────────────────────── */
    _startHold() {
      if (this._state !== 'playing' || this._holding || this._entering) return;
      this._holding = true;
      this._root.classList.add('is-holding');

      var self = this;
      var q = function (s) { return self._q(s); };
      var hold = this._holdEl;

      /* 先收启动 HUD，再把 CTA 浮上来；此后一直等待用户点击/回车，不自动跳转 */
      this._anim(q('.kr-boot'), [
        { opacity: 1, transform: 'translateY(0)' },
        { opacity: 0, transform: 'translateY(4px)' }
      ], { start: 0, end: 240, easing: 'ease-out', real: true });
      this._anim(hold, [
        { opacity: 0, transform: 'translate(-50%,10px)' },
        { opacity: 1, transform: 'translate(-50%,0)' }
      ], { start: 180, end: 600, easing: 'cubic-bezier(.22,.61,.36,1)', real: true });
      hold.classList.add('ready');

      if (this.getAttribute('skippable') !== 'false') {
        this._fadeOut(this._skipBtn, 260, true);
        this._skipBtn.style.pointerEvents = 'none';
      }
    }

    _enter(mode) {
      if (this._state !== 'playing' || this._entering) return;
      if (this._reduce) {
        this.dispatchEvent(new CustomEvent('splash:reveal', { bubbles: true, composed: true }));
        this._finishOut();
        return;
      }
      this._entering = true;
      this._holding = false;
      this._root.classList.remove('is-holding');
      this._clearTimers();

      var hold = this._holdEl;
      hold.classList.remove('ready');
      hold.style.pointerEvents = 'none';
      this._fadeOut(hold, 200, true);
      this._skipBtn.style.display = 'none';

      if (this.hasAttribute('sound')) this._playWhoosh();
      this.dispatchEvent(new CustomEvent('splash:reveal', { bubbles: true, composed: true }));

      /* 固定使用钥匙孔隧道转场：强推进 → 锁孔虹膜 + 同心环 + 柔光 */
      this._buildOutro();
      var self = this;
      var soft = this.getAttribute('reveal') === 'false';
      this._after(HOLD_LEAD, function () {
        self._startReveal(false);
        if (!soft) self._runTunnelFX();
      });
    }

    _runTunnelFX() {
      var self = this;
      this._qAll('.kr-tunnel i').forEach(function (el, i) {
        self._anim(el, [
          { transform: 'scale(.12)', opacity: 0 },
          { opacity: .9, offset: .18 },
          { transform: 'scale(3.6)', opacity: 0 }
        ], { start: i * 90, end: 850 + i * 90,
             easing: 'cubic-bezier(.2,.6,.3,1)', real: true });
      });
      this._flash(.45, 900);
    }

    _flash(intensity, dur) {
      this._anim(this._flashEl, [
        { opacity: 0 }, { opacity: intensity, offset: .35 }, { opacity: 0 }
      ], { start: 0, end: dur, easing: 'ease-in-out', real: true });
    }

    _buildOutro() {
      var self = this;
      var q = function (s) { return self._q(s); };
      var EASE = 'cubic-bezier(.22,.61,.36,1)';

      this._fadeOut(q('.kr-boot'), 240);
      this._fadeOut(q('.kr-hud'), 300);
      if (this.getAttribute('skippable') !== 'false') this._fadeOut(q('.kr-skip'), 220);

      var copy = q('.kr-copy');
      var op = parseFloat(getComputedStyle(copy).opacity);
      if (!isFinite(op)) op = 1;
      this._anim(copy, [
        { opacity: op, transform: 'translateY(0)', filter: 'blur(0px)' },
        { opacity: 0, transform: 'translateY(-14px)', filter: 'blur(6px)' }
      ], { start: 0, end: 360, easing: EASE });

      this._anim(q('.kr-dial-wrap'), [
        { transform: 'scale(1)', filter: 'blur(0px) brightness(1)' },
        { transform: 'scale(1.3)', filter: 'blur(9px) brightness(1.24)' }
      ], { start: 0, end: 760, easing: 'cubic-bezier(.5,0,.75,1)' });
      this._anim(q('.kr-rings'), [
        { transform: 'rotate(0deg)' }, { transform: 'rotate(42deg)' }
      ], { start: 0, end: 780, easing: EASE });
    }

    _fadeOut(el, dur, real) {
      if (!el) return;
      var op = parseFloat(getComputedStyle(el).opacity);
      if (!isFinite(op)) op = 1;
      if (op <= 0.001) return;
      this._anim(el, [{ opacity: op }, { opacity: 0 }],
        { start: 0, end: dur, easing: 'ease-out', real: !!real });
    }

    _after(ms, fn) {
      var self = this;
      this._timers.push(setTimeout(fn, ms * (this._slow || 1)));
    }

    /* ── 内部：钥匙孔揭示 ─────────────────────────────── */
    _startReveal(fast) {
      if (this._revealing) return;
      this._revealing = true;

      var canMask = !!(window.CSS && CSS.supports && (
        CSS.supports('mask-image', 'radial-gradient(#000, transparent)') ||
        CSS.supports('-webkit-mask-image', 'radial-gradient(#000, transparent)')
      ));

      /* 不支持 CSS mask（或显式关闭揭示）时，退化为干净的淡出 */
      if (this.getAttribute('reveal') === 'false' || !canMask) {
        var root = this._root;
        var self = this;
        root.animate([{ opacity: 1 }, { opacity: 0 }],
          { duration: fast ? 260 : 520, fill: 'both', easing: 'ease-out' });
        this._timers.push(setTimeout(function () { self._finishOut(); }, fast ? 300 : 560));
        return;
      }

      var lock = this._q('.kr-lock-body') || this._q('.kr-mark');
      var rect = lock.getBoundingClientRect();
      var hx = rect.left + rect.width / 2;
      var hy = rect.top + rect.height / 2;

      this.style.setProperty('--kr-hx', hx + 'px');
      this.style.setProperty('--kr-hy', hy + 'px');
      this.style.setProperty('--kr-hole', '0px');
      this._mask.classList.add('is-revealing');

      var maxR = Math.hypot(
        Math.max(hx, window.innerWidth - hx),
        Math.max(hy, window.innerHeight - hy)
      ) * 1.08;
      var dur = fast ? 340 : REVEAL_DUR * this._scale * .8 * (this._slow || 1);
      var t0 = performance.now();
      var self2 = this;
      var ring = this._ring;

      function tick(now) {
        var p = Math.min(1, (now - t0) / dur);
        var e = easeIris(p);
        var r = e * maxR;
        self2.style.setProperty('--kr-hole', r.toFixed(1) + 'px');
        ring.style.width = r * 2 + 'px';
        ring.style.height = r * 2 + 'px';
        ring.style.opacity = String(0.9 * (1 - p * 0.55));
        if (p < 1) {
          self2._revealRAF = requestAnimationFrame(tick);
        } else {
          ring.style.opacity = '0';
          self2._finishOut();
        }
      }
      this._revealRAF = requestAnimationFrame(tick);
    }

    _applySeekReveal(t, revealStart) {
      if (this.getAttribute('reveal') === 'false') return;
      var start = (revealStart == null)
        ? HOLD_AT * this._scale + SEEK_HOLD + HOLD_LEAD
        : revealStart;
      var dur = REVEAL_DUR * this._scale;
      if (t < start) return;
      var lock = this._q('.kr-lock-body') || this._q('.kr-mark');
      var rect = lock.getBoundingClientRect();
      var hx = rect.left + rect.width / 2;
      var hy = rect.top + rect.height / 2;
      this.style.setProperty('--kr-hx', hx + 'px');
      this.style.setProperty('--kr-hy', hy + 'px');
      this._mask.classList.add('is-revealing');
      var p = clamp((t - start) / dur, 0, 1);
      var e = easeIris(p);
      var maxR = Math.hypot(
        Math.max(hx, window.innerWidth - hx),
        Math.max(hy, window.innerHeight - hy)
      ) * 1.08;
      var r = e * maxR;
      this.style.setProperty('--kr-hole', r.toFixed(1) + 'px');
      this._ring.style.width = r * 2 + 'px';
      this._ring.style.height = r * 2 + 'px';
      this._ring.style.opacity = String(0.9 * (1 - p * 0.55));
      if (t >= start + dur) this.style.opacity = '0';
    }

    /* ── 内部：减少动态 ───────────────────────────────── */
    _playReduced() {
      var self = this;
      var root = this._root;
      root.classList.add('is-reduced');
      root.animate([{ opacity: 0 }, { opacity: 1 }],
        { duration: 220, fill: 'both', easing: 'ease-out' });
      /* 减少动态时同样不自动进入：静态画面 + 点击/回车进入 */
      this._timers.push(setTimeout(function () {
        if (self._state !== 'playing' || self._entering) return;
        self._holding = true;
        root.classList.add('is-holding');
        var hold = self._holdEl;
        hold.classList.add('ready');
        hold.style.opacity = '1';
        hold.style.transform = 'translate(-50%, 0)';
        hold.style.pointerEvents = 'auto';
      }, 320));
    }

    /* ── 内部：Canvas 星尘 ────────────────────────────── */
    _initCanvas() {
      this._canvas = this._q('.kr-canvas');
      this._ctx = this._canvas.getContext('2d');
      this._dpr = Math.min(2, window.devicePixelRatio || 1);
      this._w = 0;
      this._h = 0;
      this._resizeCanvas();
      this._makeParticles();
      this._sparks = [];
      this._simPrev = 0;
      this._burstDone = false;
      this._lastNow = performance.now();
      this._parallax = { x: 0, y: 0 };
      this._px = 0;
      this._py = 0;
      this._parallaxOn = !!window.matchMedia &&
        matchMedia('(pointer:fine)').matches && !this._reduce;
      this._unlockAt = UNLOCK_AT * this._scale;
    }

    _resizeCanvas() {
      if (!this._canvas) return;
      var w = window.innerWidth;
      var h = window.innerHeight;
      if (w === this._w && h === this._h) return;
      this._w = w;
      this._h = h;
      this._canvas.width = Math.round(w * this._dpr);
      this._canvas.height = Math.round(h * this._dpr);
      this._canvas.style.width = w + 'px';
      this._canvas.style.height = h + 'px';
      this._ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
      if (this._parts && this._parts.length) this._makeParticles();
    }

    _makeParticles() {
      var w = this._w, h = this._h;
      var count = Math.round(Math.min(230, (w * h) / 13000));
      this._parts = [];
      for (var i = 0; i < count; i++) {
        var z = Math.random();
        var kind = Math.random();
        var p = {
          x: Math.random() * w,
          y: Math.random() * h,
          z: z,
          vx: -(0.12 + Math.random() * 0.32) * (0.35 + z),
          vy: -(0.05 + Math.random() * 0.18) * (0.35 + z),
          r: 0.35 + z * 1.5,
          a: 0.12 + z * 0.5,
          tw: 0.4 + Math.random() * 1.6,
          ph: Math.random() * Math.PI * 2,
          color: kind < 0.62 ? '240,244,255' : kind < 0.86 ? '190,255,120' : '120,230,255',
        };
        if (count > 40 && Math.random() < 0.028) {
          p.glint = true;
          p.r += 0.5 + Math.random() * 0.7;
          p.a = Math.min(0.8, p.a + 0.2);
        }
        this._parts.push(p);
      }
    }

    _loop() {
      var self = this;
      function tick(now) {
        if (self._state !== 'playing') return;
        var t = now - self._startedAt;
        var dt = Math.min(48, now - self._lastNow);
        self._lastNow = now;

        if (self._parallaxOn && !self._revealing) {
          var p = self._parallax;
          p.x += (self._px - p.x) * 0.045;
          p.y += (self._py - p.y) * 0.045;
          var par = self._q('.kr-parallax');
          if (par) {
            par.style.transform = 'perspective(1400px) rotateX(' +
              (-p.y * 2.2).toFixed(2) + 'deg) rotateY(' +
              (p.x * 2.2).toFixed(2) + 'deg)';
          }
        }

        self._step(t, dt);
        self._draw(t);
        self._raf = requestAnimationFrame(tick);
      }
      this._raf = requestAnimationFrame(tick);
    }

    _step(t, dt) {
      if (this._simPrev < this._unlockAt && t >= this._unlockAt) this._burst();
      this._simPrev = t;

      var w = this._w, h = this._h, k = dt / 16;
      for (var i = 0; i < this._parts.length; i++) {
        var p = this._parts[i];
        p.x += p.vx * k;
        p.y += p.vy * k;
        if (p.x < -4) p.x = w + 4;
        if (p.x > w + 4) p.x = -4;
        if (p.y < -4) p.y = h + 4;
        if (p.y > h + 4) p.y = -4;
      }
      for (var j = 0; j < this._sparks.length; j++) {
        var s = this._sparks[j];
        s.x += s.vx * k;
        s.y += s.vy * k;
        s.vx *= 0.986;
        s.vy = s.vy * 0.986 + 0.01 * k;
        s.life -= dt;
      }
      this._sparks = this._sparks.filter(function (s) { return s.life > 0; });
    }

    _burst() {
      var mark = this._q('.kr-mark');
      if (!mark) return;
      var r = mark.getBoundingClientRect();
      var cx = r.left + r.width * 0.6;
      var cy = r.top + r.height * 0.3;
      for (var i = 0; i < 64; i++) {
        var a = Math.random() * Math.PI * 2;
        var sp = 1.1 + Math.random() * 5.4;
        var kind = Math.random();
        this._sparks.push({
          x: cx, y: cy,
          vx: Math.cos(a) * sp,
          vy: Math.sin(a) * sp,
          life: 500 + Math.random() * 700,
          max: 1200,
          r: 0.6 + Math.random() * 1.7,
          a: 0.55 + Math.random() * 0.45,
          color: kind < 0.55 ? '210,255,140' : kind < 0.8 ? '120,230,255' : '245,245,245',
          tail: Math.random() < 0.45,
        });
      }
    }

    _draw(t) {
      if (!this._ctx) return;
      var ctx = this._ctx;
      var w = this._w, h = this._h;
      ctx.clearRect(0, 0, w, h);

      for (var i = 0; i < this._parts.length; i++) {
        var p = this._parts[i];
        var tw = 0.62 + 0.38 * Math.sin(t * 0.001 * p.tw + p.ph);
        var pa = Math.max(0, Math.min(1, p.a * tw));
        ctx.globalAlpha = pa;
        ctx.fillStyle = 'rgb(' + p.color + ')';
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, 6.2832);
        ctx.fill();
        if (p.glint) {
          ctx.globalAlpha = pa * 0.16;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r * 4.5, 0, 6.2832);
          ctx.fill();
          ctx.globalAlpha = pa * 0.45;
          ctx.strokeStyle = 'rgb(' + p.color + ')';
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(p.x - 4, p.y); ctx.lineTo(p.x + 4, p.y);
          ctx.moveTo(p.x, p.y - 4); ctx.lineTo(p.x, p.y + 4);
          ctx.stroke();
        }
      }

      ctx.globalCompositeOperation = 'lighter';
      for (var j = 0; j < this._sparks.length; j++) {
        var s = this._sparks[j];
        var alpha = Math.max(0, s.life / s.max) * s.a;
        ctx.globalAlpha = alpha;
        if (s.tail) {
          ctx.strokeStyle = 'rgb(' + s.color + ')';
          ctx.lineWidth = s.r * 0.8;
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(s.x - s.vx * 7, s.y - s.vy * 7);
          ctx.stroke();
        }
        ctx.fillStyle = 'rgb(' + s.color + ')';
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, 6.2832);
        ctx.fill();
      }
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
    }

    _simulateTo(t) {
      var step = 16;
      var start = t > 1900 ? t - 1250 : 0;
      var burstAt = this._unlockAt;
      var fired = false;
      if (burstAt < start) {
        this._burst();
        this._advance(Math.min(320, t - start));
        fired = true;
      }
      for (var s = start; s < t; s += step) {
        var dt = Math.min(step, t - s);
        if (!fired && s < burstAt && s + dt >= burstAt) {
          this._advance(burstAt - s);
          this._burst();
          this._advance(s + dt - burstAt);
          fired = true;
        } else {
          this._advance(dt);
        }
      }
      if (!fired && t >= burstAt) { this._burst(); this._advance(240); }
      this._simPrev = t;
      this._draw(t);
    }

    _advance(dt) {
      var k = dt / 16;
      for (var i = 0; i < this._parts.length; i++) {
        var p = this._parts[i];
        p.x += p.vx * k;
        p.y += p.vy * k;
      }
      for (var j = 0; j < this._sparks.length; j++) {
        var s = this._sparks[j];
        s.x += s.vx * k;
        s.y += s.vy * k;
        s.vx *= 0.986;
        s.vy = s.vy * 0.986 + 0.01 * k;
        s.life -= dt;
      }
      this._sparks = this._sparks.filter(function (s) { return s.life > 0; });
    }

    /* ── 内部：全局事件 ───────────────────────────────── */
    _bindGlobal() {
      var self = this;
      this._onKey = function (e) {
        if (self._holding && (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar')) {
          e.preventDefault();
          self._enter('key');
          return;
        }
        if ((e.key === 'Escape' || e.key === 'Esc') &&
            self.getAttribute('skippable') !== 'false') {
          e.preventDefault();
          self.skip();
        }
      };
      this._onResize = function () {
        self._resizeCanvas();
        if (self._state === 'seek') self._applySeekReveal(self._seekT || 0);
      };
      this._onPointer = function (e) {
        if (!self._parallaxOn || self._revealing) return;
        self._px = (e.clientX / window.innerWidth - 0.5) * 2;
        self._py = (e.clientY / window.innerHeight - 0.5) * 2;
      };
      window.addEventListener('keydown', this._onKey);
      window.addEventListener('resize', this._onResize);
      window.addEventListener('pointermove', this._onPointer, { passive: true });
    }

    _unbindGlobal() {
      if (this._onKey) window.removeEventListener('keydown', this._onKey);
      if (this._onResize) window.removeEventListener('resize', this._onResize);
      if (this._onPointer) window.removeEventListener('pointermove', this._onPointer);
      this._onKey = this._onResize = this._onPointer = null;
    }

    /* ── 内部：合成音效（无外部资源） ─────────────────── */
    _setupAudio() {
      if (!this.hasAttribute('sound')) return;
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        if (!this._ac) this._ac = new AC();
        if (this._ac.state === 'suspended') this._ac.resume().catch(function () {});
      } catch (e) { this._ac = null; }
    }

    _playUnlockSound() {
      var ac = this._ac;
      if (!ac || ac.state !== 'running') return;
      var t0 = ac.currentTime;
      var o = ac.createOscillator();
      var g = ac.createGain();
      o.type = 'sine';
      o.frequency.setValueAtTime(120, t0);
      o.frequency.exponentialRampToValueAtTime(42, t0 + 0.35);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(0.2, t0 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.5);
      o.connect(g).connect(ac.destination);
      o.start(t0);
      o.stop(t0 + 0.52);

      var o2 = ac.createOscillator();
      var g2 = ac.createGain();
      o2.type = 'triangle';
      o2.frequency.setValueAtTime(1600, t0);
      o2.frequency.exponentialRampToValueAtTime(420, t0 + 0.09);
      g2.gain.setValueAtTime(0.0001, t0);
      g2.gain.exponentialRampToValueAtTime(0.055, t0 + 0.008);
      g2.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.12);
      o2.connect(g2).connect(ac.destination);
      o2.start(t0);
      o2.stop(t0 + 0.14);
    }

    _playWhoosh() {
      var ac = this._ac;
      if (!ac || ac.state !== 'running') return;
      var dur = 0.9;
      var buf = ac.createBuffer(1, Math.floor(ac.sampleRate * dur), ac.sampleRate);
      var data = buf.getChannelData(0);
      for (var i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
      var src = ac.createBufferSource();
      src.buffer = buf;
      var bp = ac.createBiquadFilter();
      bp.type = 'bandpass';
      bp.Q.value = 0.9;
      bp.frequency.setValueAtTime(220, ac.currentTime);
      bp.frequency.exponentialRampToValueAtTime(2400, ac.currentTime + dur * 0.7);
      bp.frequency.exponentialRampToValueAtTime(500, ac.currentTime + dur);
      var g = ac.createGain();
      g.gain.setValueAtTime(0.0001, ac.currentTime);
      g.gain.exponentialRampToValueAtTime(0.05, ac.currentTime + 0.12);
      g.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + dur);
      src.connect(bp).connect(g).connect(ac.destination);
      src.start();
    }
  }

  customElements.define('keyrealm-splash', KeyrealmSplash);
  window.KeyrealmSplash = KeyrealmSplash;
})();
