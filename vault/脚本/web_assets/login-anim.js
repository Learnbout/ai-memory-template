/* ============================================================
   ai-memory — login-anim.js
   登录页入场编排（GSAP 3，本地 vendored：web_assets/gsap.min.js）

   ── 为什么把动画单独放一个文件 ──────────────────────────────
   ① 登录页是纯字符串拼接的 HTML，动画逻辑放进去会让 web_auth.py 里
      出现大段 JS，可读性和可测性都变差；
   ② 两个 `<script defer>` 按文档顺序执行，所以本文件天然能保证
      「GSAP 已加载」—— 不需要 DOMContentLoaded 里再赌一次。

   ── 失败必须可见，不能把页面锁成空白 ──────────────────────
   初值用 gsap.set/from 在 JS 里设，**不写进 CSS**。
   反例：CSS 里先 `.card{opacity:0}` 再由 JS 淡入 —— 一旦 gsap.min.js
   加载失败（断网、被拦、路径写错），卡片永久 opacity:0，用户看到一片
   星空却登不了录，而且报错只在控制台。当前写法下 GSAP 缺失时本文件
   直接 return，页面就是「没有动画但正常可用」。

   ── 编排口径（与 Graphite Workspace 的入场节奏一致）────────
   总时长约 1.5s，一条 timeline 推进，不用 setTimeout 串：
     0.00  背景（星空 + 土星）淡入并轻微推近
     0.10  暗纱淡入
     0.18  卡片升起（y 28 → 0）
     0.34  卡片内元素错峰浮入（stagger .055）
   错峰而不是整块一起动：整块一起动像「弹出对话框」，错峰才像页面在加载。

   ── prefers-reduced-motion ─────────────────────────────────
   系统开了「减少动态效果」就完全跳过入场（元素本来就是可见的），
   只保留按钮按下的一点点反馈 —— 那不是装饰，是操作确认。
   ============================================================ */
(function () {
  'use strict';

  function boot() {
    var gsap = window.gsap;
    if (!gsap) return;                       /* 没有 GSAP：保持静态可用 */

    var reduce = window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false;

    /* 按钮按下反馈：与动效偏好无关，属操作确认 */
    var form = document.querySelector('form.card');
    var btn = form && form.querySelector('button[type="submit"]');
    if (btn) {
      form.addEventListener('submit', function () {
        if (form.dataset.busy === '1') { return; }   /* 防连点重复提交 */
        form.dataset.busy = '1';
        btn.textContent = '验证中…';
        gsap.to(btn, { scale: 0.985, opacity: 0.82, duration: 0.14, ease: 'power2.out' });
      });
      btn.addEventListener('pointerenter', function () {
        gsap.to(btn, { y: -1, duration: 0.18, ease: 'power2.out' });
      });
      btn.addEventListener('pointerleave', function () {
        gsap.to(btn, { y: 0, duration: 0.22, ease: 'power2.out' });
      });
    }

    if (reduce) return;                      /* 少动效：不播入场 */

    /* 探针：截图/验收时要能「停在某一帧」看效果，不能靠手快。
       window.__aiMemoryLoginAnim.tl.pause(0.35) 就停在入场中段。 */
    window.__aiMemoryLoginAnim = { gsap: gsap.version, tl: null, reduced: false };

    /* ── 全部用 fromTo，不用 from ────────────────────────────────
       实测（2026-10-23，Chromium）：用 `from()` 给 `.card > *` 做错峰入场，
       之后把时间轴 seek 到 progress(1)，提交按钮的 inline style 仍停在
       `opacity:0; translate(0,10px)`，另外 9 个兄弟元素却正常归位 ——
       按钮永久不可见，等于把登录入口弄没了。换成显式 fromTo（两端都写死）
       后，同样 seek 到 progress(1)，10 个子元素全部 opacity:1。
       `from()` 的终点是「创建那一刻读到的当前值」，多目标 + stagger 时这个
       读数既依赖写入顺序，也会被 CSS transition 拖走（本页 button 原本带
       `transition:opacity`，已同时摘掉）。fromTo 不读任何当前值，天然免疫。

       ⚠ 验收手法：不要「等两秒再看」。后台窗口 / 被遮挡的窗口里 rAF 会被
       节流到 1~2 帧每秒，时间轴几乎不动，看起来像「动画没跑完」。
       正确做法是 seek：`__aiMemoryLoginAnim.tl.progress(1).pause()`，
       这是确定性的，与帧率无关。 */
    var tl = gsap.timeline({ defaults: { ease: 'power3.out' } });
    window.__aiMemoryLoginAnim.tl = tl;

    tl.fromTo('.saturn-bg', { opacity: 0, scale: 1.045 },
                            { opacity: 1, scale: 1, duration: 1.5 }, 0)
      .fromTo('.veil', { opacity: 0 }, { opacity: 1, duration: 0.9 }, 0.1)
      .fromTo('.card', { y: 28, opacity: 0 }, { y: 0, opacity: 1, duration: 0.85 }, 0.18)
      .fromTo('.card > *', { y: 10, opacity: 0 },
                           { y: 0, opacity: 1, duration: 0.5, stagger: 0.045 }, 0.30)
      .fromTo('.kicker .pip', { scale: 0 },
                              { scale: 1, duration: 0.5, ease: 'back.out(2.2)' }, 0.46);

    /* 密码错 / 被限流时，错误条自己再抖一下 —— 页面是整页重载的，
       用户此刻只会盯着卡片，光有文案不够醒目 */
    var err = document.querySelector('.card .err');
    if (err) {
      tl.fromTo(err, { x: -9, opacity: 0 },
                     { x: 0, opacity: 1, duration: 0.5, ease: 'power2.out' }, 0.5)
        .fromTo(err, { x: -5 }, { x: 0, duration: 0.75, ease: 'elastic.out(1, 0.4)' }, 0.72);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
