"""Web 端登录与会话（阶段 4，2026-10-23）。

**为什么需要这个文件**：`memory_web.py` 在阶段 4 之前零鉴权，`POST /save` 是裸写库
接口。一旦按阶段 13 挂到服务器，等于把整个记忆库对内网开放成「谁都能改」。

设计取舍（主人 2026-10-23 裁决选 B：登录页 + 签名 Cookie）：

- **不用 HTTP Basic Auth**：浏览器那个原生小框没有登出、页面上不知道「我是谁」，
  阶段 5 加账号体系时还得推倒重写。
- **不复用 MCP 的 Bearer 令牌**：浏览器不方便带请求头，令牌得塞进 URL 或手设 Cookie，
  会漏进历史记录和访问日志 —— 属于倒退。
- **Cookie 是 HMAC 签名的无状态会话**：服务端不存 session 表，重启不掉线，
  水平扩容也不用粘性会话。代价是「立即踢人」做不到，只能靠短 TTL（默认 12 小时）。

**密钥分离**：会话签名密钥不是直接拿 `token.key` 用，而是用
`HMAC(token_key, "ai-memory/web-session/v1")` 派生一把。这样 Web 会话签名一旦被
攻破，也推不出 MCP 的 JWT 签发密钥 —— 两套凭证不该共用一把钥匙。这是密钥分离的
最小实践（KDF 用的就是 HMAC，够用且不需要额外依赖）。

**已知局限（写在这里，别以后当 bug 查）**：
1. 无状态会话无法即时吊销。要「立刻下线某人」得改 `token.key`（会连带废掉 MCP 票）
   或引入黑名单表 —— 留给阶段 8（审计）。
2. 登录限流在进程内存里，重启即清空，多进程各算一份。当前单进程，够用。
3. 只有 Cookie，没有 CSRF token。靠 `SameSite=Lax` 挡住跨站表单提交；
   要更强的防护得加 double-submit token，等真的有多用户再上。
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()

# `secret_io` 住在代码根（库根），而本文件在 脚本/ 子目录里。被 memory_web 导入时
# 路径已经由它准备好；单独用 CLI 跑（python 脚本/web_auth.py）时 sys.path[0] 是
# 脚本目录，找不到 secret_io —— 所以这里自己补一次，保证 CLI 可用。
_CODE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(VAULT), str(_CODE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

COOKIE_NAME = "ai_memory_sess"
COOKIE_VERSION = "v1"
DEFAULT_TTL = 12 * 3600
PBKDF2_ROUNDS = 260_000

# 登录失败限流：同一 (用户名, 来源) 连错 5 次锁 5 分钟
MAX_FAILS = 5
LOCK_SECONDS = 300

_LOGIN_FAILS: dict[str, list[float]] = {}


# ── 路径 ────────────────────────────────────────────────────────────────────


def config_path() -> Path:
    return VAULT / ".workbuddy" / "web_auth.json"


def secret_path() -> Path:
    return VAULT / ".workbuddy" / "token.key"


# ── 密码哈希 ────────────────────────────────────────────────────────────────


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。PBKDF2-HMAC-SHA256，26 万轮。

    为什么是 PBKDF2 而不是 bcrypt/argon2：标准库自带，零依赖。这个项目的部署环境
    （隔离 venv + 无网 pip）里少一个依赖就少一类故障。26 万轮在本机约 100ms，
    对登录场景完全够，也足够让离线爆破变慢。
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    _s, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, hash_hex)


# ── 用户配置 ────────────────────────────────────────────────────────────────


def load_config() -> dict[str, Any]:
    p = config_path()
    if not p.is_file():
        return {"users": {}, "ttl": DEFAULT_TTL}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置不是对象")
        data.setdefault("users", {})
        data.setdefault("ttl", DEFAULT_TTL)
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return {"users": {}, "ttl": DEFAULT_TTL}


def save_config(cfg: dict[str, Any]) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def set_user(name: str, password: str, *, note: str = "") -> Path:
    cfg = load_config()
    salt, digest = hash_password(password)
    session_version = int(cfg.get("users", {}).get(name, {}).get("session_version") or 0) + 1
    cfg["users"][name] = {
        "salt": salt,
        "hash": digest,
        "note": note,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_version": session_version,
    }
    return save_config(cfg)


def list_users() -> list[dict[str, Any]]:
    cfg = load_config()
    out = []
    for name, rec in sorted(cfg.get("users", {}).items()):
        out.append({"name": name, "note": rec.get("note", ""), "updated": rec.get("updated", "")})
    return out


def del_user(name: str) -> bool:
    cfg = load_config()
    if name not in cfg.get("users", {}):
        return False
    cfg["users"].pop(name)
    save_config(cfg)
    return True


def rename_user(old: str, new: str) -> bool:
    """改用户名：整条记录（salt/hash/note）原样搬过去，密码不变。

    冲突即拒绝（返回 False）：旧名不存在、或新名已被**其它**账号占用。
    上层（web_gate 的 /api/gate/username）必须先做过同样的检查再调用；
    这里再挡一道是兜底 —— check-then-write 之间没有锁，单进程下够用，
    但两个入口都查比只查一处少一类「绕过网关直调」的漏。

    改名后旧名的会话票立即失效（user_exists(old) 不再命中），
    网关必须用新名重签一张票回给浏览器，否则人被自己踢下线。
    """
    cfg = load_config()
    users = cfg.get("users", {})
    if old not in users or new in users:
        return False
    rec = users.pop(old)
    rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    users[new] = rec
    save_config(cfg)
    return True


def has_users() -> bool:
    return bool(load_config().get("users"))


def user_exists(name: str) -> bool:
    """账号还在不在。

    为什么每个请求都要查一下（2026-10-23 补的洞）：本会话是**无状态** HMAC Cookie，
    `sub` 只是被签名，签发之后就没人再核对它还有没有对应账号 —— 于是
    「删掉一个账号」并不能让他手上的 Cookie 失效，最长还能再用满 12 小时。
    鉴权闸门每次比对一下用户名，删号即等于立刻踢人（比 TTL 到期快，比维护
    服务端会话表便宜）。代价是每请求读一次几十字节的 JSON，这里可以接受。

    注意能力边界：它只解决「账号被删」，不解决「改密码后旧 Cookie 仍有效」——
    后者要绑密码哈希指纹进 payload，属阶段 8（审计与会话治理）。
    """
    return name in load_config().get("users", {})


# ── 会话密钥与会话值 ────────────────────────────────────────────────────────


def session_key() -> bytes:
    """从 token.key 派生**独立的**会话签名密钥（密钥分离，见模块 docstring）。"""
    from secret_io import load_secret

    base = load_secret(secret_path())
    return hmac.new(base, b"ai-memory/web-session/v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_session(user: str, *, ttl: int | None = None, key: bytes | None = None) -> tuple[str, int]:
    """签发会话值，返回 (cookie_value, max_age)。"""
    key = key or session_key()
    ttl = int(ttl or load_config().get("ttl") or DEFAULT_TTL)
    now = int(time.time())
    user_record = load_config().get("users", {}).get(user, {})
    payload = {"sub": user, "iat": now, "exp": now + ttl, "v": 1,
               "sv": int(user_record.get("session_version") or 0), "jti": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{COOKIE_VERSION}.{body}.{sig}", ttl


def verify_session(value: str, *, key: bytes | None = None) -> dict[str, Any] | None:
    """校验会话值。任何异常都按「无效」处理（fail-closed），绝不抛给调用方。"""
    if not value or not value.startswith(COOKIE_VERSION + "."):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    _v, body, sig = parts
    try:
        key = key or session_key()
    except (OSError, ValueError):
        return None
    expect = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) <= int(time.time()):
        return None
    user = str(payload.get("sub") or "")
    record = load_config().get("users", {}).get(user)
    if record and int(payload.get("sv") or 0) != int(record.get("session_version") or 0):
        return None
    return payload


def parse_cookie(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not header:
        return out
    for chunk in header.split(";"):
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()
    return out


def session_from_headers(headers) -> dict[str, Any] | None:
    """从请求头取会话。`headers` 是 BaseHTTPRequestHandler.headers 那样的映射。"""
    try:
        raw = headers.get("Cookie")
    except Exception:  # noqa: BLE001
        return None
    return verify_session(parse_cookie(raw).get(COOKIE_NAME, ""))


# ── 登录限流 ────────────────────────────────────────────────────────────────


def _fail_key(user: str, source: str) -> str:
    return f"{user.strip().lower()}|{source}"


def login_locked(user: str, source: str) -> int:
    """返回还需等待的秒数，0 表示没锁。"""
    rec = _LOGIN_FAILS.get(_fail_key(user, source))
    if not rec:
        return 0
    count, until = rec[0], rec[1]
    if count < MAX_FAILS:
        return 0
    left = int(until - time.time())
    return max(0, left)


def note_login_fail(user: str, source: str) -> None:
    k = _fail_key(user, source)
    rec = _LOGIN_FAILS.get(k, [0.0, 0.0])
    count = float(rec[0]) + 1
    until = time.time() + LOCK_SECONDS if count >= MAX_FAILS else 0.0
    _LOGIN_FAILS[k] = [count, until]


def clear_login_fail(user: str, source: str) -> None:
    _LOGIN_FAILS.pop(_fail_key(user, source), None)


# ── 登录页 ──────────────────────────────────────────────────────────────────
#
# 视觉口径来自 Graphite Workspace（主人自己那套），不另起一套配色：
#   页面底 #0b0c0d · 玻璃板 rgba(12,13,15,.70) + blur(20px) saturate(1.1)
#   描边 #1f2123 / #2b2e31 · 主字 #e9ebec · 次字 #95999d · 弱字 #62666a
#   近白强调 #dfe4e7（那边唯一的高亮就是它，没有彩色主色）
#   字体 Inter / Segoe UI / Microsoft YaHei；等宽 JetBrains Mono / Cascadia Code
#   圆角 10px 面板 / 6px 控件
#
# 星球 = Graphite Workspace 的 SaturnBackground（自研 WebGL，零贴图），
# 抽成静态资源 /assets/saturn.js 复用，不在本文件里再画一颗 CSS 假球。
# 为什么不做成图片：那张图不存在 —— 它本来就是算出来的，换分辨率不糊。
#
# 分层（从下到上）：
#   canvas#saturn-bg (z0) → .veil 暗纱 (z1) → .stage 卡片 (z2)
# 暗纱是「左浓右淡」的径向渐变：左边压暗保证卡片上的字读得清，
# 右边留亮让星球可见。这一步不做，卡片透明度一降字就糊在星球亮面上
# （Graphite Workspace 里同样踩过，见 theme.css 的 --page-veil 注释）。

_ASSET_SATURN = "/assets/saturn.js"
# GSAP 3.13.0 本地 vendored（标准授权，文件头自带许可声明）：
# 不放 CDN —— 这套东西最终跑在内网 / 服务器上，外网一断动画就整块没了，
# 而「没动画」还只是降级；若把初值写进 CSS，就变成「页面空白登不了录」。
_ASSET_GSAP = "/assets/gsap.min.js"
_ASSET_ANIM = "/assets/login-anim.js"

_CSS = """
*{box-sizing:border-box}
html,body{height:100%}
:root{
  --bg:#0b0c0d;--panel-glass:rgba(12,13,15,.70);--panel-blur:blur(20px) saturate(1.1);
  --border:#1f2123;--border-strong:#2b2e31;
  --text:#e9ebec;--text-dim:#95999d;--text-mute:#62666a;
  --accent:#dfe4e7;
  --mono:'JetBrains Mono','Cascadia Code',Consolas,monospace;
  --sans:'Inter','Segoe UI','Microsoft YaHei',system-ui,sans-serif;
  --radius:10px;--radius-sm:6px;
}
body{
  margin:0;background:var(--bg);color:var(--text);font-size:14px;line-height:1.7;
  font-family:var(--sans);overflow:hidden;-webkit-font-smoothing:antialiased;
}
/* 背景层：永不参与命中测试，否则它会吃掉整页点击 */
.saturn-bg{
  position:fixed;inset:0;z-index:0;display:block;width:100%;height:100%;
  pointer-events:none;background:#04050a;
}
/* WebGL 起不来时的静态兜底：一颗球 + 一道环，糊得出来就行，别留一片黑 */
.saturn-bg--fallback{
  background:
    radial-gradient(ellipse 25% 4.5% at 74% 64%,rgba(196,188,172,.30) 0 46%,transparent 72%),
    radial-gradient(circle 12% at 74% 61%,#cfc0a4 0 62%,#7d6d54 84%,transparent 88%),
    radial-gradient(120% 90% at 18% 0%,#12141a 0%,#0b0c0d 55%,#05060a 100%);
}
/* 暗纱：左浓右淡。文字区的对比度靠它，不靠卡片自身加深 —— 
   卡片再加深就把星球整块挡住了（Graphite Workspace 第九轮的结论）。 */
.veil{
  position:fixed;inset:0;z-index:1;pointer-events:none;
  background:
    radial-gradient(78% 68% at 16% 50%,rgba(11,12,13,.92) 0%,rgba(11,12,13,.72) 42%,rgba(11,12,13,.30) 74%,rgba(11,12,13,.18) 100%),
    linear-gradient(180deg,rgba(11,12,13,.55) 0%,rgba(11,12,13,0) 28%,rgba(11,12,13,0) 72%,rgba(11,12,13,.62) 100%);
}
/* 星座锚点（2026-10-26 主人拍板：登录页只加星座这一样，其它星空元素不加）。
   纯装饰：不响应交互、不动（减动效友好）、窄屏让位给卡片直接隐藏。 */
.constellation{
  position:fixed;right:7vw;top:9vh;z-index:1;pointer-events:none;opacity:.85;
}
.constellation line{stroke:rgba(190,205,240,.30);stroke-width:1}
.constellation circle{fill:#cfd9f5}
.constellation circle.hub{fill:#ffffff}
@media (max-width:820px){.constellation{display:none}}

/* 流星与陨石（2026-10-26 主人补拍板：登录页要有流星 + 陨石）。
   流星 = 纯光线（细、快、冷白）；陨石 = 岩石头 + 暖色尾焰（慢、带弧度下坠）。
   全部 fixed + pointer-events:none，z-index 1（暗纱上、卡片下，不挡登录）；
   减动效直接隐藏；位移用 translate 合成层，不碰 layout。
   2026-10-26 二修（主人：「流星滑落的方向和陨石动效也有问题」）：
   两处几何错误 ——
   ① 头尾颠倒：光线亮头必须朝运动方向。路径往左下走 → 亮头在左端、
      尾巴向右上渐隐（原版亮头在右端 = 尾巴冲前）。
   ② 光条与轨迹不共线：transform 里 rotate 的角度必须等于路径斜率
      （流星路径 (-620,388) → 斜率 32°，rotate(-32deg) 才与轨迹同线；
      原版 rotate(+32deg) 是镜像的，光条斜着横在轨迹上）。 */
.meteor,.meteorite{position:fixed;z-index:1;pointer-events:none}
.meteor{
  width:150px;height:1.6px;border-radius:2px;
  background:linear-gradient(90deg,#ffffff,rgba(238,228,204,.85) 38%,rgba(238,228,204,0));
  transform:rotate(-32deg);opacity:0;
  animation:gw-shoot 7s ease-in infinite;
}
.meteor.m2{width:110px;animation-delay:2.4s;animation-duration:8.2s;opacity:0}
.meteor.m3{width:90px;animation-delay:4.8s;animation-duration:9.4s;opacity:0}
@keyframes gw-shoot{
  0%{transform:translate(0,0) rotate(-32deg);opacity:0}
  3%{opacity:1}
  14%{transform:translate(-620px,388px) rotate(-32deg);opacity:0}
  100%{transform:translate(-620px,388px) rotate(-32deg);opacity:0}
}
/* 陨石：rock（岩石头）在左端 = 运动前方；尾焰从岩石头向右上渐隐。
   路径 (0,-40)→(-560,460) 斜率 ≈ 39.4° → rotate(-39deg) 与轨迹同线。 */
.meteorite{width:120px;height:26px;opacity:0;animation:gw-fall 11s linear infinite}
.meteorite .rock{
  position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:14px;height:11px;
  background:radial-gradient(circle at 35% 35%,#8a7a63,#4a3f30 60%,#241d14);
  border-radius:58% 42% 55% 45% / 55% 48% 52% 45%;
  box-shadow:0 0 10px rgba(243,222,186,.35);
}
.meteorite .tail{
  position:absolute;left:10px;top:50%;transform:translateY(-50%);
  width:104px;height:9px;border-radius:9px;
  background:linear-gradient(90deg,rgba(255,236,200,.75),rgba(214,178,120,.35) 55%,rgba(243,222,186,0));
  filter:blur(1.5px);
}
@keyframes gw-fall{
  0%{transform:translate(0,-40px) rotate(-39deg);opacity:0}
  5%{opacity:1}
  30%{transform:translate(-560px,420px) rotate(-39deg);opacity:.9}
  38%{transform:translate(-680px,520px) rotate(-39deg);opacity:0}
  100%{transform:translate(-680px,520px) rotate(-39deg);opacity:0}
}
@media (prefers-reduced-motion:reduce){.meteor,.meteorite{display:none}}
.stage{
  position:relative;z-index:2;height:100%;
  display:flex;align-items:center;
  padding:0 clamp(24px,7vw,110px);
}
.card{
  width:min(388px,100%);padding:30px 30px 24px;border-radius:var(--radius);
  background:var(--panel-glass);border:1px solid var(--border);
  -webkit-backdrop-filter:var(--panel-blur);backdrop-filter:var(--panel-blur);
  box-shadow:0 30px 90px rgba(0,0,0,.66),0 1px 0 rgba(255,255,255,.03) inset;
}
.kicker{
  display:flex;align-items:center;gap:8px;margin:0 0 20px;
  font:500 10.5px/1 var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--text-mute);
}
.kicker::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--border-strong),transparent)}
.pip{width:5px;height:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 10px rgba(223,228,231,.55)}
h1{font-size:21px;line-height:1.35;margin:0 0 6px;font-weight:600;letter-spacing:-.2px}
.sub{color:var(--text-dim);font-size:12.5px;margin:0 0 24px}
label{
  display:block;margin:0 0 7px;
  font:500 10.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--text-mute);
}
input{
  width:100%;padding:10px 12px;margin:0 0 16px;border-radius:var(--radius-sm);
  background:#101112;border:1px solid var(--border);color:var(--text);
  font-size:13.5px;font-family:var(--sans);outline:none;
  transition:border-color .15s,box-shadow .15s,background .15s;
}
input:hover{border-color:var(--border-strong)}
input:focus{border-color:#3b4046;background:#131416;box-shadow:0 0 0 3px rgba(223,228,231,.07)}
button{
  width:100%;margin-top:4px;padding:10px 14px;border-radius:var(--radius-sm);
  border:1px solid var(--accent);background:var(--accent);color:#0b0c0d;
  font-size:13.5px;font-weight:600;font-family:var(--sans);letter-spacing:.02em;
  cursor:pointer;
  /* 只过渡颜色。transform / opacity 由 GSAP 逐帧写 inline style，
     这里再挂 transition 就是两套动画抢同一属性（实测把按钮卡在 opacity:0）。 */
  transition:background .15s,border-color .15s;
}
button:hover{background:#eef1f3;border-color:#eef1f3}
.pw-field{position:relative;display:block}

.pw-field input{padding-right:46px}
.pw-eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:34px;height:34px;z-index:3;pointer-events:auto;display:grid;place-items:center;padding:0;border:0;border-radius:8px;background:transparent;color:#95999d;cursor:pointer;line-height:0}
.pw-eye:hover{background:rgba(255,255,255,.07);border-color:transparent;color:#e9ebec}
.pw-eye:focus-visible{outline:2px solid #3b4046;outline-offset:1px}
.pw-eye svg{width:18px;height:18px}
/* 眼睛垂直居中（最终）：父容器 line-height:0 + input display:block，高度才等于输入框 */
.pw-field{position:relative;display:block;line-height:0}
.pw-field input{display:block;width:100%;padding-right:44px;box-sizing:border-box}
.pw-eye{position:absolute;top:50%;right:6px;transform:translateY(-50%);width:30px;height:30px;display:flex;align-items:center;justify-content:center;padding:0;margin:0;border:0;border-radius:7px;background:transparent;color:#95999d;cursor:pointer;line-height:0;z-index:4}

/* 小眼睛最终口径：装在输入框内部右侧（2026-10-23 主人两次纠正） */
.pw-field{position:relative!important;display:block!important}
.pw-field input{width:100%!important;padding-right:44px!important;box-sizing:border-box}
.pw-eye,.pw-eye-btn{position:absolute!important;right:6px!important;top:50%!important;transform:translateY(-50%)!important;width:30px!important;height:30px!important;display:grid!important;place-items:center;padding:0!important;margin:0!important;border:0!important;border-radius:7px;background:transparent!important;color:#95999d!important;cursor:pointer;line-height:0;z-index:4!important;pointer-events:auto!important}
.pw-eye:hover,.pw-eye-btn:hover{background:rgba(255,255,255,.08)!important;color:#e9ebec!important}
.pw-eye svg,.pw-eye-btn svg{width:17px;height:17px}

button:active{transform:translateY(1px)}
button:focus-visible{outline:2px solid rgba(223,228,231,.55);outline-offset:2px}
.err{
  display:flex;gap:8px;align-items:flex-start;
  background:rgba(224,113,111,.08);border:1px solid rgba(224,113,111,.34);
  color:#e9a3a1;padding:9px 11px;border-radius:var(--radius-sm);
  font-size:12.5px;line-height:1.55;margin:0 0 16px;
}
.err::before{content:"";flex:none;width:3px;height:3px;margin-top:7px;border-radius:50%;background:#e0716f}
.meta{
  display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin-top:20px;padding-top:14px;border-top:1px solid var(--border);
  font:400 10.5px/1.5 var(--mono);letter-spacing:.04em;color:var(--text-mute);
}
/* 窄屏：卡片居中、暗纱改成均匀压暗 —— 这时星球沉在下方，
   左右分层的暗纱会让上半部分过亮、下半部分没意义 */
@media (max-width:820px){
  .stage{padding:0 20px;justify-content:center}
  .veil{
    background:radial-gradient(120% 90% at 50% 40%,rgba(11,12,13,.80) 0%,rgba(11,12,13,.66) 60%,rgba(11,12,13,.44) 100%);
  }
  .card{width:min(400px,100%)}
}
@media (prefers-reduced-motion:reduce){button{transition:none}}
"""


def login_page(
    *, error: str = "", next_url: str = "/", user: str = "", assets_prefix: str = "/assets"
) -> str:
    """登录页 HTML。

    `user` 会被回填进输入框（输错密码时不用重敲），必须先转义。
    `next` 只允许站内相对路径；调用方（Handler._handle_login）已经挡过一次，
    这里再挡一次 —— 漏一处就是开放重定向。

    `assets_prefix`（2026-10-23 新增）：静态资源前缀。默认 `/assets` 给 L3
    （memory_web）用；反向代理网关必须传别的值 —— Graphite 工作台自己就用
    `/assets` 放前端 bundle，两者同域冲突，网关那一侧改用 `/gate-assets`。
    """
    nxt = next_url or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    # 只接受站内绝对路径前缀；挡掉 "//evil.com" 与绝对 URL
    prefix = (assets_prefix or "/assets").strip() or "/assets"
    if not prefix.startswith("/") or prefix.startswith("//"):
        prefix = "/assets"
    prefix = prefix.rstrip("/")
    err_html = f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex,nofollow">
<title>登录 · ai-memory</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='15' r='6.5' fill='%23cfc0a4'/%3E%3Cellipse cx='16' cy='16.5' rx='13' ry='3.6' fill='none' stroke='%23cfc0a4' stroke-opacity='.65' stroke-width='1.8' transform='rotate(-17 16 16.5)'/%3E%3C/svg%3E">
<style>{_CSS}</style>
</head><body>
<canvas id="saturn-bg" class="saturn-bg" aria-hidden="true"></canvas>
<div class="veil"></div>
<svg class="constellation" width="300" height="180" viewBox="0 0 300 180" aria-hidden="true">
  <line x1="20" y1="140" x2="95" y2="60"/><line x1="95" y1="60" x2="185" y2="95"/>
  <line x1="185" y1="95" x2="268" y2="30"/><line x1="185" y1="95" x2="240" y2="160"/>
  <circle cx="20" cy="140" r="2.5"/><circle cx="95" cy="60" r="3" class="hub"/>
  <circle cx="185" cy="95" r="2.5"/><circle cx="268" cy="30" r="3.5" class="hub"/>
  <circle cx="240" cy="160" r="2"/>
</svg>
<div class="meteor" style="left:70%;top:12%" aria-hidden="true"></div>
<div class="meteor m2" style="left:42%;top:4%" aria-hidden="true"></div>
<div class="meteor m3" style="left:88%;top:34%" aria-hidden="true"></div>
<div class="meteorite" style="left:74%;top:22%" aria-hidden="true"><span class="tail"></span><span class="rock"></span></div>
<div class="meteorite" style="left:40%;top:2%;animation-delay:5.5s" aria-hidden="true"><span class="tail"></span><span class="rock"></span></div>
<div class="stage">
  <form class="card" method="post" action="/login">
    <p class="kicker"><span class="pip"></span>ai-memory · 中央节点</p>
    <h1>登录记忆库</h1>
    <p class="sub">多人协作知识库 · 本节点内容需登录后访问</p>
    {err_html}
    <input type="hidden" name="next" value="{html.escape(nxt)}">
    <label for="u">用户名</label>
    <input id="u" name="user" autocomplete="username" spellcheck="false"
           autocapitalize="none" value="{html.escape(user)}" autofocus>
    <label for="p">密码</label>
    <span class="pw-field">
      <input id="p" name="password" type="password" autocomplete="current-password">
      <button type="button" class="pw-eye" id="pw-eye" aria-label="显示密码" aria-pressed="false" title="显示密码" onpointerdown="var i=document.getElementById('p'),on=this.getAttribute('aria-pressed')==='true';i.type=on?'password':'text';this.setAttribute('aria-pressed',on?'false':'true');this.setAttribute('aria-label',on?'显示密码':'隐藏密码');this.title=on?'显示密码':'隐藏密码';event.preventDefault();return false;" onclick="return false;"">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-6.2 10-6.2S22 12 22 12s-3.6 6.2-10 6.2S2 12 2 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>
      </button>
    </span>
    <button type="submit">进入记忆库</button>
    <div class="meta"><span>登录状态保持 12 小时</span><span>v1</span></div>
  </form>
</div>
<noscript><div class="veil"></div></noscript>
<script>
/* 链接 Graphite 设置里的面板透明度/模糊度（2026-10-26 主人要求）。
   网关与工作台同源，localStorage 共享 gw.frost.v1；登录页读同一个键覆写
   --panel-glass / --panel-blur，卡片就跟着设置滑杆走。
   没有该键（首次访问 / 其它宿主）→ 保持上面 :root 的默认值，零副作用。
   注意：本页是 f-string 模板，JS 的花括号必须双写转义。 */
try{{var _f=JSON.parse(localStorage.getItem("gw.frost.v1")||"null");
if(_f&&_f.blur!=null){{document.documentElement.style.setProperty("--panel-blur","blur("+_f.blur+"px) saturate(1.1)")}}
if(_f&&_f.opacity!=null){{document.documentElement.style.setProperty("--panel-glass","rgba(12, 13, 15, "+_f.opacity+")")}}}}
catch(e){{}}
</script>
<script src="{prefix}/saturn.js" defer></script>
<script src="{prefix}/gsap.min.js" defer></script>
    <script src="{prefix}/login-anim.js" defer></script>
</body></html>"""


# ── 命令行 ──────────────────────────────────────────────────────────────────


def _gen_password(n: int = 16) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="web_auth.py", description="Web 端账号管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="创建/重置一个账号；不传密码则随机生成并打印一次")
    p_init.add_argument("--user", required=True)
    p_init.add_argument("--password", default="")
    p_init.add_argument("--note", default="")

    p_add = sub.add_parser("adduser", help="新增账号")
    p_add.add_argument("--user", required=True)
    p_add.add_argument("--password", default="")
    p_add.add_argument("--note", default="")

    p_pw = sub.add_parser("passwd", help="改密码")
    p_pw.add_argument("--user", required=True)
    p_pw.add_argument("--password", default="")

    sub.add_parser("list", help="列出账号")

    p_del = sub.add_parser("deluser", help="删账号")
    p_del.add_argument("--user", required=True)

    args = ap.parse_args(argv)

    if args.cmd in ("init", "adduser"):
        cfg = load_config()
        exists = args.user in cfg.get("users", {})
        if args.cmd == "adduser" and exists:
            print(f"账号已存在：{args.user}（改密码请用 passwd）", file=sys.stderr)
            return 1
        pw = args.password
        shown = False
        if not pw:
            if sys.stdin.isatty():
                a = getpass.getpass("设置密码：")
                b = getpass.getpass("再输一次：")
                if a != b or not a:
                    print("两次输入不一致或为空", file=sys.stderr)
                    return 1
                pw = a
            else:
                pw = _gen_password()
                shown = True
        if len(pw) < 8:
            print("密码太短，至少 8 位", file=sys.stderr)
            return 1
        path = set_user(args.user, pw, note=args.note)
        print(f"{'已重置' if exists else '已创建'}账号：{args.user}  ->  {path}")
        if shown:
            print(f"随机密码（只显示这一次）：{pw}")
        return 0

    if args.cmd == "passwd":
        cfg = load_config()
        if args.user not in cfg.get("users", {}):
            print(f"没有这个账号：{args.user}", file=sys.stderr)
            return 1
        pw = args.password
        shown = False
        if not pw:
            if sys.stdin.isatty():
                pw = getpass.getpass("新密码：")
            else:
                pw = _gen_password()
                shown = True
        if len(pw) < 8:
            print("密码太短，至少 8 位", file=sys.stderr)
            return 1
        set_user(args.user, pw, note=cfg["users"][args.user].get("note", ""))
        print(f"已改密码：{args.user}")
        if shown:
            print(f"随机密码（只显示这一次）：{pw}")
        return 0

    if args.cmd == "list":
        rows = list_users()
        if not rows:
            print("还没有任何 Web 账号。用 `init --user <名字>` 创建。")
            return 0
        print(f"{'用户名':<16}{'备注':<20}{'更新时间'}")
        print("-" * 64)
        for r in rows:
            print(f"{r['name']:<16}{r['note']:<20}{r['updated']}")
        return 0

    if args.cmd == "deluser":
        print("已删除" if del_user(args.user) else "没有这个账号")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
