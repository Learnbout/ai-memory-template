/* ============================================================
   ai-memory — saturn.js
   登录页 / 全局背景用的 WebGL 土星（无贴图、无渲染库、单文件）

   ── 从哪来 ──────────────────────────────────────────────
   移植自 Graphite Workspace 的 SaturnBackground.tsx + saturnShader.ts
   （主人 2026-10-15 起自研的那颗，不是 CodePen 的贴图版）。
   那边是 React 组件，这里去掉了 React/TS，只留：
     ① 一次 compile/link、② 一个全屏三角形、③ 一个 rAF 循环。
   着色器源码两边**同一口径**（2026-10-26 重新对齐）：登录页先修了
   「星球拼接」三处（ringDensity 角向细纹 + 内径柔化 + 前景环压盘面
   alpha 0.55）与球面贴图接缝（lon 周期化），主人拍板「同步」后已把
   环的三处搬回工作台 saturnShader.ts —— 工作台侧的贴图缝 2026-10-25
   就已修过（lp 圆周采样），两边现在逐字等价，再改请两边一起改。

   ── 为什么不把 GLSL 拆成 .glsl 文件再 fetch ────────────────
   登录页要求在**未登录**状态下就能加载本文件；多一次网络往返就多
   一个「首屏先黑一下再出星球」的窗口。单文件 28KB（gzip 后约 8KB）
   比「HTML + 两次 fetch」更省，也不用给服务器加 .glsl 的 MIME。

   ── 用法 ────────────────────────────────────────────────
     <canvas id="saturn-bg" class="saturn-bg" aria-hidden="true"></canvas>
     <script src="/assets/saturn.js" defer></script>
   自动挂到 #saturn-bg 上；没有该元素则什么都不做（不会报错，方便别处复用）。
   WebGL 不可用时给 canvas 加 .saturn-bg--fallback，由 CSS 兜一层静态
   「球 + 环」渐变 —— 它不需要好看，只需要看得出来不是坏了。

   ── 性能口径（同 Graphite Workspace，实测过的数字）─────────
   像素比夹到 1.5、上限 30fps。背景自转周期 72s，30fps 与 60fps
   肉眼无差别，但 GPU 占用减半；上层卡片用了 backdrop-filter，
   这个减半直接等于「磨砂重算次数减半」。
   prefers-reduced-motion：只把相位冻在 0，**不停止渲染** ——
   停渲染会让用户中途改系统设置时背景整块消失。

   ── 探针 ────────────────────────────────────────────────
   window.__aiMemorySaturn = { ok, fps, width, height, fallback, reason }
   「看起来有星星」是主观判断，不能拿来验收；探针读这个对象。
   ============================================================ */
(function () {
  'use strict';

  var MAX_DPR = 1.5;
  var MAX_FPS = 30;

  var VERT = [
    'attribute vec2 aPos;',
    'void main() { gl_Position = vec4(aPos, 0.0, 1.0); }'
  ].join('\n');

  var FRAG = `
precision highp float;

uniform vec2  uRes;
uniform float uRot;        // 自转相位（弧度）
uniform float uTime;       // 全局时间（秒）
uniform vec3  uEye;
uniform mat3  uBasis;      // 列向量依次为 right / up / fwd
uniform float uTanHalf;
uniform vec2  uShift;      // 画面内平移（行星不一定要正中）

const float PLANET_R = 1.0;
const float RING_IN  = 1.283;
const float RING_OUT = 2.349;
const float PI = 3.1415926;

float h21(vec2 p){ p = fract(p * vec2(127.1, 311.7)); p += dot(p, p + 34.23); return fract(p.x * p.y); }
float vn(vec2 p){
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(h21(i), h21(i + vec2(1.0, 0.0)), f.x),
             mix(h21(i + vec2(0.0, 1.0)), h21(i + vec2(1.0, 1.0)), f.x), f.y);
}
float fbm(vec2 p){
  float s = 0.0, a = 0.5;
  for (int i = 0; i < 5; i++) { s += a * vn(p); p = p * 2.07 + vec2(13.7, 7.3); a *= 0.5; }
  return s;
}
float h31(vec3 p){
  p = fract(p * 0.1031);
  p += dot(p, p.yzx + 33.33);
  return fract((p.x + p.y) * p.z);
}

float starLayer(vec3 d, float scale, float dens){
  vec3 p = d * scale;
  vec3 i = floor(p), f = p - i;
  float h = h31(i);
  if (h < 1.0 - dens) return 0.0;
  vec3 c = vec3(h31(i + 13.1), h31(i + 27.7), h31(i + 41.3)) * 0.7 + 0.15;
  return smoothstep(0.17, 0.0, length(f - c)) * (0.55 + 0.45 * h31(i + 7.7));
}

float meteorScreen(vec2 uv, float time){
  float m = 0.0;
  for (int i = 0; i < 3; i++) {
    float fi = float(i);
    float T = 11.0 + 6.0 * fi;
    float s = fract(time / T + fi * 0.41);
    vec2 P0 = vec2(-0.90 + 0.50 * fi, 0.72 - 0.42 * fi);
    vec2 D = normalize(vec2(0.85 - 0.28 * fi, -(0.55 + 0.24 * fi)));
    float travel = 1.7 + 0.3 * fi;
    vec2 head = P0 + D * (s * travel);
    float len = 0.16 + 0.04 * fi;

    vec2 q = uv - head;
    float along = dot(q, D);
    float perp2 = max(dot(q, q) - along * along, 0.0);
    float core = exp(-perp2 * 12000.0);
    float tail = along < 0.0 ? exp(along * (14.0 / len)) : 0.0;
    float headGlow = along > 0.0 ? exp(-along * along * 6000.0) : 0.0;
    float env = smoothstep(0.0, 0.10, s) * (1.0 - smoothstep(0.60, 1.0, s));
    m += (headGlow * 1.3 + tail * 0.55) * core * env;
  }
  return m;
}

float starTwinkle(float seed, float time){ return 0.72 + 0.28 * sin(time * (0.8 + 1.6 * seed) + seed * 31.0); }

vec3 sky(vec3 d, float time){
  float neb = pow(clamp(fbm(d.xz * 2.1 + d.y * 1.9 + 4.0), 0.0, 1.0), 3.0);
  vec3 col = vec3(0.014, 0.016, 0.028) + vec3(0.038, 0.033, 0.058) * neb * 0.8;
  float s = starLayer(d, 220.0, 0.012)
          + starLayer(d, 120.0, 0.016) * 0.8
          + starLayer(d, 60.0, 0.020) * 0.55 * starTwinkle(h31(floor(d * 60.0)), time);
  return col + vec3(0.90, 0.92, 1.00) * s * 1.3;
}

float rockRadius(float fi){ return 0.026 + 0.016 * fract(fi * 0.37 + 0.21); }

vec3 rockPos(float fi, float time){
  float Ror = 3.15 + 0.55 * fi;
  float w = (0.055 + 0.016 * fi) * (mod(fi, 2.0) < 0.5 ? 1.0 : -1.0);
  float ang = w * time + fi * 2.39;
  float y0 = 0.55 * sin(fi * 2.1 + 0.8);
  return vec3(Ror * cos(ang), y0 + 0.06 * sin(time * 0.5 + fi * 1.3), Ror * sin(ang));
}

vec3 rockVel(float fi, float time){
  float Ror = 3.15 + 0.55 * fi;
  float w = (0.055 + 0.016 * fi) * (mod(fi, 2.0) < 0.5 ? 1.0 : -1.0);
  float ang = w * time + fi * 2.39;
  return vec3(-Ror * w * sin(ang), 0.03 * cos(time * 0.5 + fi * 1.3), Ror * w * cos(ang));
}

vec3 shadeRock(vec3 ro, vec3 rd, float t, vec3 C, float rad, vec3 L, vec3 V, float time){
  vec3 p = ro + rd * t;
  vec3 N = normalize(p - C);
  float day = clamp(dot(N, L), 0.0, 1.0);
  float alb = 0.45 + 0.35 * fbm(N.xy * 8.0 + N.z * 4.0);
  vec3 col = vec3(0.44, 0.40, 0.36) * alb * (0.10 + 1.15 * day);
  float rim = pow(1.0 - max(dot(N, -rd), 0.0), 2.0);
  col += vec3(0.35, 0.42, 0.60) * rim * 0.15;

  vec3 vd = V / max(length(V), 1e-5);
  float lead = clamp(dot(N, vd), 0.0, 1.0);
  float flick = 0.7 + 0.3 * sin(time * 11.0 + C.x * 5.0 + C.z * 3.0);
  col += vec3(1.0, 0.50, 0.16) * pow(lead, 2.0) * flick * 1.05;
  col += vec3(1.0, 0.85, 0.60) * pow(lead, 6.0) * flick * 0.75;
  return col;
}

vec3 planetAlbedo(vec3 n, float rot){
  float lat = asin(clamp(n.y, -1.0, 1.0));
  float lon = atan(n.z, n.x) + rot;

  /* 2026-10-26 修「贴图接缝」（主人：球的贴图重合度没搞好导致有条缝）：
     lon = atan(z,x) 在 ±π 处跳变，直接喂 fbm 会在球面留一条随自转
     移动的竖直接缝 —— 缝两侧亮暗对不上，看起来像球面特效「脱节」。
     把 lon 嵌进圆周坐标 (cos, sin)（对 lon 天然周期），半径取原角频率
     保持纹理尺度基本不变；三处 lon 采样全部改掉，缝就消失了。 */
  vec2 cl = vec2(cos(lon), sin(lon));

  float warp = fbm(cl * 1.6 + vec2(lat * 4.0));
  float band = lat * 11.0 + 2.2 * sin(lat * 5.0 + 1.2) + warp * 1.6;
  float st = 0.5 + 0.5 * sin(band * PI);
  st = mix(st, fbm(vec2(lat * 26.0 + 3.0 * cl.x, 3.0 * cl.y)), 0.40);
  st = 0.24 + 0.76 * st;

  vec3 c = mix(vec3(0.50, 0.38, 0.26), vec3(0.97, 0.91, 0.77), st);
  float eq = exp(-pow(lat / 0.14, 2.0));
  c = mix(c, vec3(0.98, 0.93, 0.80), eq * 0.22);
  c *= vec3(1.05, 1.00, 0.90);
  c *= 0.93 + 0.15 * fbm(vec2(7.0 * cl.x, 7.0 * cl.y + lat * 30.0));

  float pole = smoothstep(0.75, 0.995, abs(n.y));
  return mix(c, vec3(0.45, 0.53, 0.64), pole * 0.55);
}

vec3 shadePlanet(vec3 ro, vec3 rd, float t, vec3 L, float rot){
  vec3 p = ro + rd * t;
  vec3 N = normalize(p);
  float day = smoothstep(-0.18, 0.42, dot(N, L));
  vec3 alb = planetAlbedo(N, rot);

  if (abs(L.y) > 1e-4) {
    float tr = -p.y / L.y;
    if (tr > 0.0) {
      float r = length((p + L * tr).xz);
      if (r > RING_IN && r < RING_OUT) {
        float dens = 0.55 + 0.45 * fbm(vec2(r * 22.0, 0.0));
        dens *= smoothstep(RING_IN, RING_IN + 0.05, r) * smoothstep(RING_OUT, RING_OUT - 0.12, r);
        alb *= 1.0 - 0.62 * clamp(dens, 0.0, 1.0);
      }
    }
  }

  vec3 lit = alb * (0.070 + 1.22 * day);
  float rim = pow(1.0 - max(dot(N, -rd), 0.0), 3.0);
  return lit + vec3(0.30, 0.36, 0.52) * rim * day * 0.55;
}

/* 2026-10-26 修「星球拼接」（主人反馈）：ringDensity 从只沿半径变
   （fbm(r, 0)，角向均匀）改为带角向细纹 —— 只沿半径变的前景环压在
   球面上是一片均匀灰雾，内径边缘又是一条锐利直线，看起来就像
   「上半个球和下半个球是两块料拼的」。角向用整数角频 28 做周期映射
   （th∈[-π,π] → 0..28 格，边界对齐），不会在 ±π 处出现接缝线；
   内径边缘 smoothstep 0.045 → 0.09 柔化，消掉横切球面的直线感。 */
float ringDensity(float r, float th){
  float t = (r - RING_IN) / (RING_OUT - RING_IN);
  float d = 0.55 + 0.45 * fbm(vec2(r * 24.0, 0.0));
  d *= 0.40 + 0.60 * fbm(vec2(r * 6.5, 11.0));
  d *= 0.62 + 0.38 * fbm(vec2(r * 30.0, (th / 6.2831853 + 0.5) * 28.0));
  d *= 0.86 + 0.14 * fbm(vec2(r * 18.0, (th / 6.2831853 + 0.5) * 9.0) + 7.3);
  d *= smoothstep(0.0, 0.09, t) * smoothstep(1.0, 0.94, t);
  d *= 1.0 - 0.93 * exp(-pow((r - 2.058) / 0.042, 2.0));
  d *= 1.0 - 0.55 * exp(-pow((r - 2.263) / 0.013, 2.0));
  return clamp(d, 0.0, 1.0);
}

float ringShadow(vec3 pr, vec3 L){
  float t = dot(-pr, L);
  if (t <= 0.0) return 0.0;
  return 1.0 - smoothstep(PLANET_R * 0.97, PLANET_R * 1.20, length(pr + L * t));
}

float ringSparkle(vec2 xz, float time){
  float th = atan(xz.y, xz.x);
  vec2 cell = vec2(length(xz) * 44.0, th * 19.0);
  vec2 id = floor(cell);
  float h = h21(id);
  if (h < 0.84) return 0.0;
  vec2 c = (vec2(h21(id + 7.3), h21(id + 3.1)) - 0.5) * 0.7;
  float d = length(fract(cell) - 0.5 - c);
  float tw = 0.5 + 0.5 * sin(time * (1.5 + 3.0 * h) + h * 40.0);
  float g = smoothstep(0.20, 0.0, d);
  return g * g * tw * (0.55 + 0.45 * h);
}

vec4 ringColor(float r, vec3 q, vec3 L){
  float th = atan(q.z, q.x);
  float a = ringDensity(r, th);
  a *= 1.0 - ringShadow(q, L) * 0.88;
  vec3 base = mix(vec3(0.78, 0.75, 0.68), vec3(0.99, 0.96, 0.90),
                  0.5 + 0.5 * fbm(vec2(r * 40.0, 3.0)));
  return vec4(base, a);
}

vec3 rockTrailGlow(vec2 uv, float time){
  vec3 acc = vec3(0.0);
  for (int i = 0; i < 4; i++) {
    float fi = float(i);
    vec3 C = rockPos(fi, time);
    vec3 V = rockVel(fi, time);
    vec3 rel = C - uEye;
    float cz = dot(rel, uBasis[2]);
    if (cz < 0.2) continue;
    vec2 sp = vec2(dot(rel, uBasis[0]), dot(rel, uBasis[1])) / (cz * 2.0 * uTanHalf) - uShift;
    vec2 sv = vec2(dot(V, uBasis[0]), dot(V, uBasis[1])) / cz;
    float sl = length(sv);
    if (sl < 1e-6) continue;
    vec2 D = sv / sl;
    float len = 0.06 + 0.015 * fi;
    vec2 q = uv - sp;
    float along = dot(q, D);
    float perp2 = max(dot(q, q) - along * along, 0.0);
    float core = exp(-perp2 * 26000.0);
    float tail = (along < 0.0 && along > -len) ? exp(along * 16.0 / len) : 0.0;
    float flick = 0.75 + 0.25 * sin(time * 13.0 + fi * 7.0);
    acc += vec3(1.0, 0.55, 0.20) * tail * core * flick * 0.7;
  }
  return acc;
}

void insertHit(float t, int k, inout float ta, inout int ka, inout float tb, inout int kb){
  if (t <= 0.0) return;
  if (ta < 0.0 || t < ta) { tb = ta; kb = ka; ta = t; ka = k; }
  else if (tb < 0.0 || t < tb) { tb = t; kb = k; }
}

/* ringA：前景环压在**球面上**时的 alpha 折减（main 里按命中顺序判定）。
   原版环在盘面上几乎不透明，球面暖条纹被整块盖死 —— 观感就是
   「上半球和下半球两块料拼的」。真实照片里环横过盘面是半透明的，
   能透出球面；压在星空上的环段保持原 alpha。 */
vec3 overlay(int k, float t, vec3 col, vec3 ro, vec3 rd, vec3 L, float rot, float time, float ringA){
  if (k == 1) return shadePlanet(ro, rd, t, L, rot);
  if (k == 2) {
    vec3 q = ro + rd * t;
    float r = length(q.xz);
    float th = atan(q.z, q.x);
    vec4 rc = ringColor(r, q, L);
    vec3 c = mix(col, rc.rgb, rc.a * ringA);
    float sh = 1.0 - ringShadow(q, L) * 0.88;
    c += vec3(1.0, 0.97, 0.88) * ringSparkle(q.xz, time) * sh * (0.45 + 0.55 * ringDensity(r, th)) * ringA;
    return c;
  }
  float fi = float(k - 3);
  return shadeRock(ro, rd, t, rockPos(fi, time), rockRadius(fi), L, rockVel(fi, time), time);
}

void main(){
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / uRes.y + uShift;
  vec3 rd = normalize(uBasis * vec3(uv * uTanHalf * 2.0, 1.0));
  vec3 ro = uEye;
  vec3 L  = normalize(vec3(-0.58, 0.30, 0.76));

  float tS = -1.0;
  {
    float b = dot(ro, rd);
    float disc = b * b - (dot(ro, ro) - PLANET_R * PLANET_R);
    if (disc > 0.0) { float t0 = -b - sqrt(disc); if (t0 > 0.0) tS = t0; }
  }

  float tR = -1.0;
  if (abs(rd.y) > 1e-6) {
    float t = -ro.y / rd.y;
    if (t > 0.0) {
      float r = length((ro + rd * t).xz);
      if (r > RING_IN && r < RING_OUT) tR = t;
    }
  }

  float tA = -1.0;
  float rockId = 0.0;
  for (int i = 0; i < 4; i++) {
    float fi = float(i);
    vec3 C = rockPos(fi, uTime);
    vec3 oc = ro - C;
    float b = dot(oc, rd);
    float rad = rockRadius(fi);
    float disc = b * b - (dot(oc, oc) - rad * rad);
    if (disc > 0.0) {
      float t0 = -b - sqrt(disc);
      if (t0 > 0.0 && (tA < 0.0 || t0 < tA)) { tA = t0; rockId = fi; }
    }
  }

  vec3 col = sky(rd, uTime);
  col += vec3(0.85, 0.92, 1.00) * meteorScreen(uv, uTime) * 1.1;
  col += rockTrailGlow(uv, uTime);

  if (tS < 0.0) {
    float b = dot(ro, rd);
    float h = sqrt(max(dot(ro, ro) - b * b, 0.0));
    float glow = exp(-(h - 1.0) * 7.0);
    if (glow > 0.004) {
      vec3 P = normalize(ro + rd * (-b));
      float day = smoothstep(-0.05, 0.45, dot(P, L));
      col += (vec3(0.50, 0.60, 0.85) * 0.30 + vec3(0.95, 0.80, 0.55) * 0.18) * glow * day;
    }
  }

  float tn1 = -1.0, tn2 = -1.0;
  int kn1 = 0, kn2 = 0;
  insertHit(tR, 2, tn1, kn1, tn2, kn2);
  insertHit(tS, 1, tn1, kn1, tn2, kn2);
  if (tA > 0.0) insertHit(tA, 3 + int(rockId), tn1, kn1, tn2, kn2);

  /* 前景环压在球面上（近=环、远=球）→ alpha 折减让球面透出来（见 overlay 注释） */
  float ringA = (kn1 == 2 && kn2 == 1) ? 0.55 : 1.0;
  if (kn2 != 0) col = overlay(kn2, tn2, col, ro, rd, L, uRot, uTime, ringA);
  if (kn1 != 0) col = overlay(kn1, tn1, col, ro, rd, L, uRot, uTime, ringA);

  gl_FragColor = vec4(col, 1.0);
}
`;

  /* ── 相机 ────────────────────────────────────────────────────────────
     与 Graphite Workspace 的 SATURN_CAM 同口径，只有 dist 与 shift 随
     屏宽做响应式：那边是「铺满背景、卡片压在上面」，这边是「一张卡片 +
     一颗要看得清的星球」，视口里要留白，所以拉远了一档。 */
  var CAM = {
    az: -0.62,
    el: 0.325,
    roll: -0.2,
    dist: 9.2,
    fov: 0.62,
    spinPeriod: 72
  };

  /** 按宽高比决定「星球站哪、多大」。返回 {dist, shiftX, shiftY} */
  function layout(w, h) {
    var aspect = w / Math.max(h, 1);
    if (aspect >= 1.6) {          /* 宽屏：星球靠右下，左边留给卡片 */
      return { dist: 9.2, shiftX: -0.30, shiftY: 0.05 };
    }
    if (aspect >= 1.15) {         /* 中等：偏右一档 */
      return { dist: 9.8, shiftX: -0.16, shiftY: 0.04 };
    }
    if (aspect >= 0.85) {         /* 近似方形：居中偏下 */
      return { dist: 10.6, shiftX: 0, shiftY: 0.02 };
    }
    /* 竖屏：星球沉到底部，卡片在上方 —— 不然卡片会把球整个盖住 */
    return { dist: 11.4, shiftX: 0, shiftY: 0.26 };
  }

  function compile(gl, type, src) {
    var sh = gl.createShader(type);
    if (!sh) return null;
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      /* 编译失败必须看得见原因，否则表现就是「背景静静地不出现」 */
      console.error('[saturn] shader 编译失败：', gl.getShaderInfoLog(sh));
      gl.deleteShader(sh);
      return null;
    }
    return sh;
  }

  function mount(canvas) {
    var report = function (d) { window.__aiMemorySaturn = d; };
    var fallback = function (reason) {
      canvas.classList.add('saturn-bg--fallback');
      report({ ok: false, fps: 0, width: 0, height: 0, fallback: true, reason: reason });
    };

    var gl = canvas.getContext('webgl', {
      alpha: false, antialias: false, depth: false, stencil: false,
      powerPreference: 'high-performance'
    }) || canvas.getContext('experimental-webgl');
    if (!gl) { fallback('WebGL 上下文不可用'); return; }

    var vs = compile(gl, gl.VERTEX_SHADER, VERT);
    var fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    var prog = gl.createProgram();
    if (!vs || !fs || !prog) { fallback('shader 不可用'); return; }
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.error('[saturn] program 链接失败：', gl.getProgramInfoLog(prog));
      fallback('program 链接失败');
      return;
    }
    gl.useProgram(prog);

    /* 全屏大三角形：比两个三角形少一条插值边界 */
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var aPos = gl.getAttribLocation(prog, 'aPos');
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    var U = function (n) { return gl.getUniformLocation(prog, n); };
    var uRes = U('uRes'), uRot = U('uRot'), uEye = U('uEye'), uBasis = U('uBasis'),
        uTanHalf = U('uTanHalf'), uShift = U('uShift'), uTime = U('uTime');

    /* 相机基：世界系里环面 = y0 平面、自转轴 = y 轴；相机在 eye 看向原点 */
    var eye = [
      CAM.dist * Math.cos(CAM.el) * Math.sin(CAM.az),
      CAM.dist * Math.sin(CAM.el),
      CAM.dist * Math.cos(CAM.el) * Math.cos(CAM.az)
    ];
    var cross = function (a, b) {
      return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
    };
    var norm = function (a) {
      var l = Math.hypot(a[0], a[1], a[2]) || 1;
      return [a[0] / l, a[1] / l, a[2] / l];
    };
    var fwd = norm([-eye[0], -eye[1], -eye[2]]);
    var r0 = norm(cross(fwd, [0, 1, 0]));
    var u0 = cross(r0, fwd);
    var cs = Math.cos(CAM.roll), sn = Math.sin(CAM.roll);
    var right = r0.map(function (v, i) { return v * cs + u0[i] * sn; });
    var up = r0.map(function (v, i) { return -v * sn + u0[i] * cs; });
    /* uniformMatrix3fv 按列主序读，所以 right / up / fwd 依次排 */
    gl.uniformMatrix3fv(uBasis, false, new Float32Array([
      right[0], right[1], right[2], up[0], up[1], up[2], fwd[0], fwd[1], fwd[2]
    ]));
    gl.uniform3fv(uEye, new Float32Array(eye));
    gl.uniform1f(uTanHalf, Math.tan(CAM.fov * 0.5));

    var W = 0, H = 0, eyeD = 0;
    var place = function (w, h) {
      var L = layout(w, h);
      if (uShift) gl.uniform2f(uShift, L.shiftX, L.shiftY);
      /* dist 只影响星球在画面里的大小：改的是相机沿同一条视线的距离。
         基（right/up/fwd）不用重算 —— fwd = normalize(-eye)，同方向缩放
         不变，叉积出来的 right/up 也就不变。只有 uEye 要跟着走。 */
      if (L.dist !== eyeD) {
        eyeD = L.dist;
        eye = [
          L.dist * Math.cos(CAM.el) * Math.sin(CAM.az),
          L.dist * Math.sin(CAM.el),
          L.dist * Math.cos(CAM.el) * Math.cos(CAM.az)
        ];
        gl.uniform3fv(uEye, new Float32Array(eye));
      }
    };
    var resize = function () {
      var dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      var cw = canvas.clientWidth || window.innerWidth;
      var ch = canvas.clientHeight || window.innerHeight;
      var w = Math.max(1, Math.round(cw * dpr));
      var h = Math.max(1, Math.round(ch * dpr));
      if (w === W && h === H) return;
      W = w; H = h;
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
      if (uRes) gl.uniform2f(uRes, w, h);
      place(cw, ch);
    };
    resize();
    var onResize = function () { resize(); };
    window.addEventListener('resize', onResize, { passive: true });
    var ro = (typeof ResizeObserver !== 'undefined') ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(canvas);

    /* 动效偏好：只冻相位，不停止渲染 */
    var motionQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    var reduce = function () { return !!(motionQuery && motionQuery.matches); };
    var spinRate = CAM.spinPeriod > 0 ? (Math.PI * 2) / CAM.spinPeriod : 0;

    var raf = 0, last = 0, elapsed = 0, t0 = performance.now(), frames = [];
    var tick = function (now) {
      raf = requestAnimationFrame(tick);
      if (now - last < 1000 / MAX_FPS - 1) return;   /* 30fps 上限 */
      last = now;
      if (!document.hidden) elapsed = (now - t0) / 1000;
      var tAnim = reduce() ? 0 : elapsed;
      if (uRot) gl.uniform1f(uRot, tAnim * spinRate);
      if (uTime) gl.uniform1f(uTime, tAnim);
      gl.drawArrays(gl.TRIANGLES, 0, 3);

      frames.push(now);
      while (frames.length && now - frames[0] > 1000) frames.shift();
      report({ ok: true, fps: frames.length, width: W, height: H, fallback: false });
    };
    raf = requestAnimationFrame(tick);

    /* 页面切走时彻底停掉：后台标签页继续烧 GPU 是笔记本上的电费 */
    var onVis = function () {
      if (document.hidden) { cancelAnimationFrame(raf); raf = 0; }
      else if (!raf) { t0 = performance.now() - elapsed * 1000; last = 0; raf = requestAnimationFrame(tick); }
    };
    document.addEventListener('visibilitychange', onVis, { passive: true });
  }

  function boot() {
    var canvas = document.getElementById('saturn-bg');
    if (!canvas) return;
    try {
      mount(canvas);
    } catch (e) {
      canvas.classList.add('saturn-bg--fallback');
      window.__aiMemorySaturn = { ok: false, fps: 0, width: 0, height: 0, fallback: true, reason: String(e) };
      console.error('[saturn] 挂载失败：', e);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
