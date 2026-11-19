/* ============================================================
   开屏动画 JS（2026-10-26，CONTRACT §31）

   ── 为什么在 public/ 而不是 src/ ─────────────────────────
   主人原话：「不用纯 CSS 写加载动画，可以优先下载加载动画的 JS」。
   先试过 src/boot/boot.ts 独立入口 —— Vite 会把同页多个 module 脚本合并进
   同一个 entry chunk（实测产物只有一个 2.3MB 的 index-*.js），「先到先播」落空。
   改放 public/：原样拷进 dist 根目录、不进打包器，classic script 在解析到
   </body> 前就同步执行 —— 浏览器解析 HTML 时就会以最高优先级取它（几 KB），
   和 2.3MB 主包并行下载，秒到秒播。

  ── JS 版比 CSS 版多出来的三件事 ─────────────────────────
  ① 进度计数：底部「正在加载工作台 · N%」，缓动爬向 92%（不撒谎：100% 只在
      真正交接时出现），交接瞬间冲到 100。
  ② 完整播完一轮（2026-10-26 主人要求「至少要完整执行完一次」）：
      接管后一律从第一笔画到收笔（上色 + 扫光），绝不半途掐掉。
      CSS 兜底已不画土星（13:21 改：兜底期 = 空纸 + 光晕呼吸），接管后
      这一轮就是用户看到的唯一一次绘制 —— 消灭「CSS 画半圈 → 冻住 →
      接管重画」的三段式观感（13:21 主人实测反馈的根因）。
  ③ 进度可被探针读取：window.__gwBoot = { progress, phase, lines }。

  ── 专门衔接入口（2026-10-26 主人要求「结尾必须留专门的入口」）──
  整轮画完（扫光收笔）那一刻，本文件在 <html> 打 data-boot-done="1"，
  splash.ts 见到这个标记才给 #gw-boot 加 is-done 淡出 —— 交接镜头顺序：
  完整绘制 → 扫光收笔 → 轻推近 + 进度走满（index.html 的 is-done 镜头）
  → 淡出露出已推近悬停的 WebGL 土星 → 拉远进主内容。

   ── 兜底矩阵（三层，任何一层挂了都不黑屏）──────────────
   · boot.js 加载失败 → html 无 data-bootjs → 空纸 + 光晕呼吸等待页（静态但活）
   · 主包下载失败     → boot 层一直在，光晕/星空/进度条一直动
   · prefers-reduced-motion → 不逐笔，直接成品 + 计数 100

   ── 交接协议 ─────────────────────────────────────────────
   splash.ts 的 hideBootLayer() 给 #gw-boot 加 .is-done → MutationObserver
   收到 → 快进补笔 + 计数 100 → splash.ts 520ms 后 remove 节点 → 停 rAF。
   本文件不依赖任何项目模块，splash.ts 也不知道本文件存在 —— 单向观察，零耦合。
   本文件是唯一事实源；index.html 只保留门控结构（兜底期不画土星，见 13:21 注）。
   ============================================================ */
(function () {
  'use strict'

  /* ── 缓动 ────────────────────────────────────────────── */

  /* 与 CSS 版同一支贝塞尔（0.4, 0.1, 0.3, 1）：慢起笔、中段快、收笔稳。
     牛顿迭代解 x→t，6 次足够到 1e-5 精度。 */
  function cubicBezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx
    var cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by
    function sampleX(t) { return ((ax * t + bx) * t + cx) * t }
    function sampleY(t) { return ((ay * t + by) * t + cy) * t }
    function sampleDX(t) { return (3 * ax * t + 2 * bx) * t + cx }
    return function (x) {
      if (x <= 0) return 0
      if (x >= 1) return 1
      var t = x
      for (var i = 0; i < 6; i++) {
        var dx = sampleX(t) - x
        if (Math.abs(dx) < 1e-5) break
        var d = sampleDX(t)
        if (Math.abs(d) < 1e-6) break
        t -= dx / d
      }
      return sampleY(t)
    }
  }
  var easeStroke = cubicBezier(0.4, 0.1, 0.3, 1)
  function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v }

  /* ── 逐笔编排（顺序 = 人画土星的顺序，与 CSS 兜底版一致）── */
  var STROKES = [
    { sel: '.l0', start: 0.1, dur: 0.75 }, // 环后半段
    { sel: '.l1', start: 0.45, dur: 0.85 }, // 球轮廓
    { sel: '.l2', start: 0.95, dur: 0.55 }, // 云带 ×4
    { sel: '.l3', start: 1.08, dur: 0.55 },
    { sel: '.l4', start: 1.21, dur: 0.55 },
    { sel: '.l5', start: 1.32, dur: 0.55 },
    { sel: '.l6', start: 1.38, dur: 0.9 }, // 环前半段
    { sel: '.l7', start: 1.5, dur: 0.5 }, // 高光弧
  ]
  var DRAW_TOTAL = 2.0 // 最后一笔收笔时刻
  var BLOOM_AT = 1.9 // 上色 + 外辉（is-drawn）
  var SWEEP_AT = 2.35 // 扫光（is-swept）
  var SPEEDUP = 5 // 交接快进倍速

  /* ── 主流程 ──────────────────────────────────────────── */
  function boot() {
    var layer = document.getElementById('gw-boot')
    if (!layer) return
    var box = layer.querySelector('.gw-draw')
    var tip = layer.querySelector('.gw-boot-tip')
    if (!box) return

    var svg = box.querySelector('svg')
    var strokes = STROKES.map(function (p) {
      return {
        start: p.start,
        dur: p.dur,
        el: svg ? svg.querySelector(p.sel) : null,
      }
    })

    var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    var api = { progress: 0, phase: 'draw', lines: strokes.map(function () { return 0 }) }
    window.__gwBoot = api

    var exiting = false // 收到 is-done 之后：5 倍速补完 + 计数冲 100
    var removed = false
    var sweepDone = false
    /* 接管状态（2026-10-26 性能体检 + §31.5 完整播完 + 13:21 兜底不画）：
       JS 逐笔动画和 CSS 描边动画一样，都吃主线程 —— 主包求值把线程堵死的
       那段里两者一起冻住。既然如此，就**不要在这段里接管**：等 load + 线程
       空闲了再接管。接管后从第一笔完整画一轮（主人要求「至少完整执行完一次」）。
       CSS 兜底已不再画土星（index.html 13:21 改）—— 兜底期是空纸 + 光晕呼吸，
       因此不存在「CSS 画了半圈被接管掐掉重画」的三段式观感。 */
    var takenOver = false
    var strokeBase = 0
    var doneFlagged = false // 整轮画完后打 data-boot-done（splash.ts 的交接闸门）
    var snapped = false // is-done 抢先到：描边停笔、计数落 100（不再靠帧追赶）

    function takeover() {
      if (takenOver || removed) return
      takenOver = true
      document.documentElement.setAttribute('data-bootjs', '1')
      /* 2026-10-26 主人要求：加载动画至少要完整执行完一次。
         所以接管不再分快慢机 —— 一律归零描边、从第一笔画到收笔。
         CSS 兜底已不画图（空纸 + 光晕），这一轮就是用户看到的唯一一次绘制。 */
      for (var j = 0; j < strokes.length; j++) {
        if (!strokes[j].el) continue
        strokes[j].el.style.animation = 'none'
        strokes[j].el.style.strokeDashoffset = '1'
      }
      strokeBase = performance.now()
    }

    function scheduleTakeover() {
      var go = function () {
        if (document.visibilityState !== 'visible') {
          window.setTimeout(go, 250)
          return
        }
        takeover()
      }
      var idle = function (cb) {
        if (window.requestIdleCallback) window.requestIdleCallback(cb, { timeout: 900 })
        else window.setTimeout(cb, 300)
      }
      if (document.readyState === 'complete') idle(go)
      else window.addEventListener('load', function () { idle(go) }, { once: true })
      /* 兜底：load 迟迟不来（接口/图片拖住）4s 也接管，别让空纸永远干等 */
      window.setTimeout(function () { if (!takenOver) takeover() }, 4000)
    }

    /* 交接观察：splash.ts 只认识 .is-done / remove，本文件只旁听这两个动作 */
    var mo = new MutationObserver(function () {
      if (!layer.isConnected) {
        removed = true
        /* phase 在这里收口，不等下一帧 —— 补笔完成时 rAF 已经歇了
           （frame 的 exit 分支 return 不再排帧），层被移除后没有帧
           会再去跑顶部的 removed 检查。旧版靠「progress 冲 100 慢于
           层移除」的竞态碰运气，探针两向都可能，13:21 修复。 */
        api.phase = 'done'
        mo.disconnect()
        return
      }
      if (layer.classList.contains('is-done') && !exiting) {
        exiting = true
        api.phase = 'exit'
        takeover() // 交接必须先停 CSS 循环，否则内联 dashoffset 会被 CSS 动画盖掉
        finishDraw()
        /* 快进收尾必须「算数」：描边时钟直接拨到终点（frame 不再重画中间值）、
           计数直接落 100。不这样做，主线程被堵死时 5 倍速补笔抢不过
           520ms 的移除定时器，会出现「层没了计数还停在 71」的中间态。 */
        snapped = true
        shownPct = 100
        api.progress = 100
        if (pctEl) pctEl.textContent = ' · 100%'
        /* 兜底路径也补标记：万一 is-done 抢在整轮画完前到达（主线程被堵死
           超过闸门上限），标记必须落，splash.ts 的时序断言才有不变式。 */
        if (!doneFlagged) {
          doneFlagged = true
          document.documentElement.setAttribute('data-boot-done', '1')
          try { window.__gwBootDone = true } catch (e) { /* 同上 */ }
        }
      }
    })
    mo.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'], childList: true })

    /* 立即画完：全部描边到位 + 上色 + 扫光（交接快进 / 减动效共用） */
    function finishDraw() {
      for (var i = 0; i < strokes.length; i++) {
        if (strokes[i].el) strokes[i].el.style.strokeDashoffset = '0'
      }
      box.classList.add('is-drawn')
      if (!sweepDone) {
        box.classList.add('is-swept')
        sweepDone = true
      }
    }

    /* 接管时机由 scheduleTakeover 决定（load + 空闲）；在此之前画面是
       空纸 + 光晕呼吸（全合成器动画），绝不在主线程被主包堵住的那段里
       抢方向盘 —— 否则手绘开头几笔就会被冻结（fix3 的教训）。 */

    var pctEl = document.createElement('span')
    pctEl.className = 'gw-boot-pct'
    if (tip) tip.appendChild(pctEl)

    var t0 = performance.now()
    var shownPct = 0
    var last = t0
    var lastPaint = 0

    function frame(now) {
      if (removed) {
        api.phase = 'done'
        return
      }
      /* 描边降帧（2026-10-26 二修，帧率实测驱动）：dashoffset 是 paint 属性，
         60fps 逐帧重绘 SVG 与 GSAP 全屏 canvas 动画同时跑时帧率掉到 48fps。
         慢速手绘 30fps 足够，砍一半 paint 压力；计数推进按真实 dt 算，速率不变。 */
      if (now - lastPaint < 30) {
        requestAnimationFrame(frame)
        return
      }
      lastPaint = now
      var speed = exiting ? SPEEDUP : 1
      var t = ((now - t0) / 1000) * speed
      /* 描边时钟：从接管那一刻起算。上色/扫光/完成标记都必须挂它 ——
         慢机接管发生在 t0 之后 1s+，若用 t0 时钟会在还没画完时就提前触发。
         snapped（is-done 抢先到）时时钟直接拨到终点：描边停笔、不再写中间值。 */
      var st = snapped ? 1e9 : ((now - (strokeBase || t0)) / 1000) * speed

      /* 描边推进：只在本文件真正接管之后才写内联值。
         未接管时描边保持隐藏（dashoffset:1），没有 CSS 动画抢属性。 */
      if (takenOver) {
        for (var i = 0; i < strokes.length; i++) {
          var s = strokes[i]
          if (!s.el) continue
          var local = clamp01((st - s.start) / s.dur)
          var eased = easeStroke(local)
          api.lines[i] = eased
          if (!exiting || local < 1) s.el.style.strokeDashoffset = String(1 - eased)
        }

        /* 上色 / 扫光（一次性 class 触发 CSS transition / forwards 动画） */
        if (st >= BLOOM_AT && !box.classList.contains('is-drawn')) box.classList.add('is-drawn')
        if (st >= SWEEP_AT && !sweepDone) {
          box.classList.add('is-swept')
          sweepDone = true
        }
        /* 整轮画完：打完成标记 —— 这是给 splash.ts 的「专门衔接入口」。
           is-done 只允许在这之后到达（scheduleBootHide 轮询本标记）。 */
        if (!doneFlagged && st >= SWEEP_AT) {
          doneFlagged = true
          document.documentElement.setAttribute('data-boot-done', '1')
          try { window.__gwBootDone = true } catch (e) { /* 探针只读不写，失败不碍事 */ }
          if (api.phase === 'draw') api.phase = 'idle'
        }
      }
      if (!exiting && doneFlagged && api.phase === 'draw') api.phase = 'idle'

      /* 计数：缓动爬向 92，交接后冲 100（100 只在真交接时出现）。
         交接段速率给 24/s：splash.ts 在 is-done 后 520ms 就会移除本层，
         冲线必须赶在那之前完成（8/s 实测会输给移除，探针抓到过）。 */
      var target = exiting ? 100 : 92 * (1 - Math.exp(-t / 1.9))
      var dt = Math.min(1, ((now - last) / 1000) * (exiting ? 24 : 3.2))
      shownPct += (target - shownPct) * dt
      last = now
      if (!exiting && shownPct > 92) shownPct = 92 // 卡在 92 档，不虚报
      api.progress = Math.round(shownPct)
      if (pctEl) pctEl.textContent = ' · ' + api.progress + '%'

      if (exiting && api.progress >= 100 && t > DRAW_TOTAL + 1.5) {
        api.phase = 'exit'
        /* 交接后的补笔与上色都已完成，rAF 可以歇了（层随后被 splash.ts 移除） */
        return
      }
      requestAnimationFrame(frame)
    }

    if (reduceMotion) {
      /* 减动效：不逐笔，成品直接在；接管标记也要落 */
      takenOver = true
      document.documentElement.setAttribute('data-bootjs', '1')
      finishDraw()
      api.phase = 'idle'
      shownPct = 100
      api.progress = 100
      if (pctEl) pctEl.textContent = ' · 100%'
      /* 完成标记也立即落：splash.ts 的交接闸门等它，不能让减动效用户
           在黑屏前多干等 3.2s 兜底 */
      document.documentElement.setAttribute('data-boot-done', '1')
      try { window.__gwBootDone = true } catch (e) { /* 同上 */ }
    } else {
      scheduleTakeover()
      requestAnimationFrame(frame)
    }
  }

  boot()
})()
