"""反向代理网关：给任何上游 Web 服务套同一把登录锁（2026-10-23 新增）。

## 解决什么问题

Graphite Workspace（`127.0.0.1:8788`，FastAPI + React）本身**零鉴权** —— 它原本是
「本机自用」的定位，跑在回环上都嫌多余。现在要把它放到服务器上给协作者看，
就必须有一把锁，而锁不该写进业务代码里：

| 方案 | 不选的理由 |
|---|---|
| 给 FastAPI 加鉴权中间件 | 要改上游代码、改前端（登录页/跳转）、以后换个上游就得再改一遍 |
| nginx basic auth | 要额外养一个 nginx、改 vhost；密码文件与 `.htpasswd` 是另一套账号体系，和 ai-memory 的 `web_auth` 割裂 |
| **本文件（标准库反代）** | 上游零改动；账号与会话直接复用 `web_auth`（和 L3 同一份 `.workbuddy/web_auth.json`、同一个 `token.key`）；以后换上游只改 `--upstream` |

## 边界（能力与不做什么）

- **只做 HTTP 转发**，不做 WebSocket 与 SSE 长连接。Graphite 没有这两样（已核：
  `app/main.py` 只有 StaticFiles mount + SPA 通配回退，七组 router 全是普通
  JSON 接口）。将来上游要是加了 SSE，这里必须改成流式转发。
- **整包缓冲**，不流式。所以大文件下载会占内存 → 设 `MAX_BODY` 上限。
- **不动 body**：`Content-Encoding` 可能已是 gzip，改一个字节就坏。所以错误页
  只在「没拿到上游响应」时生成，拿到响应就原样转。
- **剥掉 Cookie 双向**：请求侧不把会话票送给上游（上游不需要，且会进它的日志）；
  响应侧不转上游的 `Set-Cookie`（防止上游覆盖会话票）。同理剥 `Transfer-Encoding`
  —— `http.client` 已解掉分块，再声明分块会让浏览器挂住等下一块。

## 与 `web_auth.login_page` 的资源前缀冲突

上游 Graphite 自己用 `/assets/*` 放前端 bundle；登录页也要三个静态文件
（saturn.js / gsap.min.js / login-anim.js）。同名必撞，所以登录页走
`/gate-assets/*`：`login_page(assets_prefix="/gate-assets")`。
`/gate-assets/` 是**唯一免鉴权读通道**，必须白名单后缀 + 拒 `..` + `resolve()` 复核
（三个都做，少一个就是任意文件读取）。

## 用法

    python web_gate.py --upstream http://127.0.0.1:8788 --host 0.0.0.0 --port 8766
    python web_gate.py --upstream http://127.0.0.1:8788 --no-login   # 自测/排障

账号管理复用 `web_auth.py`：`python web_auth.py init --user 名字`。
"""

from __future__ import annotations

import argparse
import base64
import html
import http.client
import ipaddress
import json
import os
import posixpath
import socket
import socketserver
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import web_auth  # noqa: E402  —— 必须先把 脚本/ 放进 sys.path

ASSET_DIR = HERE / "web_assets"
ASSET_PREFIX = "/gate-assets"
ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}

PROXY_TIMEOUT = 300.0  # 秒。MCP 工具调用（tools/call）上游要等子进程，30s 不够
MAX_BODY = 32 * 1024 * 1024  # 32MB：上游没有上传接口，这个上限只是防误用


# ── 登录留痕（阶段 11，2026-10-25）────────────────────────────────────────
#
# 为什么要有：阶段 8 的审计只回答「谁改了数据」。有人拿管理员名字连试二十次
# 口令这件事，在库里一点痕迹都没有 —— 事后只能看到「某天起他登进来了」。
#
# 四条纪律：
# 1. **绝不抛异常**：审计写不进去不能让人登不上。登录是唯一的自救入口，
#    审计盘满就把自己锁在门外是荒谬的（audit.record 本身已兜住，这里再兜一层
#    import 失败）。
# 2. **口令一个字都不进**：detail 只放 ok / reason，连键名都不建带 password 的，
#    比依赖 `_scrub` 的键名匹配更稳。
# 3. **失败也要记**：只记成功等于「试错次数不可见」，而这正是最该看见的。
# 4. **懒加载库根**：网关只把 `脚本/` 放进了 sys.path，audit.py 在库根。
#    放到第一次写审计时再插，避免 `--no-login` 自测环境因为找不到库而起不来。
_AUDIT_ROOT_READY = False


def _audit(action: str, actor: str, *, detail: dict[str, Any] | None = None, ip: str = "") -> None:
    """写一条登录审计。任何异常都只打 stderr，不外抛。"""
    global _AUDIT_ROOT_READY
    try:
        if not _AUDIT_ROOT_READY:
            root = str(HERE.parent)
            if root not in sys.path:
                sys.path.append(root)
            _AUDIT_ROOT_READY = True
        import audit  # noqa: PLC0415 —— 见纪律 4

        audit.record(actor or "（空用户名）", action, target=actor, detail=detail or {}, ip=ip)
    except Exception as e:  # noqa: BLE001
        print(f"[web_gate] 登录审计写入失败 action={action}: {type(e).__name__}: {e}", file=sys.stderr)


# 逐跳头（RFC 7230 §6.1）：不能原样转发
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# 上游不可达时给人看的页（不是 traceback）
_FALLBACK_502 = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>上游不可达</title><style>
body{background:#0b0c0d;color:#e9ebec;font:15px/1.7 "Inter","Segoe UI","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{max-width:520px;padding:28px 32px;border:1px solid #1f2123;border-radius:10px;
background:rgba(12,13,15,.7)}
h1{font-size:16px;margin:0 0 10px;color:#dfe4e7;font-weight:600}
p{margin:0 0 8px;color:#95999d}code{font-family:"JetBrains Mono",monospace;color:#cfd6e4}
</style></head><body><div class="box">
<h1>上游服务不可达</h1>
<p>网关本身正常，但它代理的服务没有响应。</p>
<p>目标：<code>%s</code></p>
<p>%s</p>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# 上游地址
# --------------------------------------------------------------------------- #
# ── 真实来源地址：谁能替客户端说话 ──────────────────────────────────────────
#
# 网关可能被反代/隧道挡在前面（nginx、cloudflared、frpc 都算）。这时
# `client_address` 对所有访客都是同一个地址 —— 2026-10-26 实测：经 Cloudflare
# 隧道进来的人，在网关眼里全长着隧道主机的 IP。后果不是「日志不好看」，而是
# 登录限流的键 `(user, source)` 被合并成一个：陌生人在公网敲 5 次错密码，
# 就能把管理员锁在门外，管理员自己也进不去。
#
# 但反过来「一律信任 X-Forwarded-For」更糟：那个头客户端自己就能写，等于把
# 限流和审计的判据交给攻击者伪造。所以只认「上一跳是我们自己人」的那份转发头。
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = ("127.0.0.1", "::1")


def _is_ip(value: str) -> bool:
    """转发头里的值必须是合法 IP 才采信。

    审计与限流都拿它当键：放行 `1.2.3.4\\nfake` 这种值等于让人往审计里伪造行，
    或者用无限多不同字符串把限流表撑爆。非法就丢弃，不做「清洗后凑合用」。
    """
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class Upstream:
    """把 `http://host:port/prefix` 拆成连接参数。

    `prefix` 留着是为了将来把工作台挂在子路径下（如 `/graphite/`），现在恒空。
    """

    def __init__(self, raw: str):
        text = (raw or "").strip()
        # 容忍运维常写的三种简写：`8788` / `:8788` / `127.0.0.1:8788`
        if text.isdigit():
            text = "http://127.0.0.1:" + text
        elif text.startswith(":"):
            text = "http://127.0.0.1" + text
        if "://" not in text:
            text = "http://" + text
        u = urllib.parse.urlsplit(text)
        if u.scheme not in ("http", "https"):
            raise ValueError(f"--upstream 只支持 http/https：{raw}")
        self.scheme = u.scheme
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.prefix = (u.path or "").rstrip("/")
        self.label = f"{self.scheme}://{self.host}:{self.port}{self.prefix or ''}/"

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=PROXY_TIMEOUT)


# --------------------------------------------------------------------------- #
# 静态资源（登录页那三个文件）
# --------------------------------------------------------------------------- #
def resolve_asset(name: str) -> tuple[Path, str] | None:
    """把 URL 里的文件名解析成磁盘路径与 Content-Type；不合法一律 None。

    三道卡子缺一不可（2026-10-23 与 memory_web 同一套写法）：
      1. 拒 `.` 开头（隐藏文件）、`/` `\\`（跨目录）、`..`（上跳）；
      2. 后缀白名单（不是任意文件都能当静态资源吐出去，尤其 `.py`）；
      3. `resolve()` 后复核父目录 —— 挡符号链接与 `%2e%2e%2f` 这类编码绕过。
    """
    if not name or name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        return None
    suffix = Path(name).suffix.lower()
    if suffix not in ASSET_TYPES:
        return None
    target = (ASSET_DIR / name).resolve()
    if ASSET_DIR.resolve() not in target.parents:
        return None
    if not target.is_file():
        return None
    return target, ASSET_TYPES[suffix]


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
class GateHandler(BaseHTTPRequestHandler):
    server_version = "ai-memory-gate/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"  # keep-alive：代理场景每条请求都重连会很浪费

    # 由 main() 注入
    upstream: Upstream = Upstream("http://127.0.0.1:8788")
    auth_on: bool = True
    # 可信的上一跳：只有这些地址送来的转发头才采信（默认只信本机回环）
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES

    # ── 日志 ────────────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认实现会带客户端地址与时间，够用；这里只补上耗时与状态，且绝不打印 Cookie
        sys.stderr.write(
            "[gate] %s %s -> %s\n"
            % (self.command, _strip_query(self.path or ""), fmt % args)
        )

    # ── 真实来源地址 ────────────────────────────────────────────────────────
    def _client_ip(self) -> str:
        """这条请求到底是谁发的 —— 限流与审计的唯一依据。

        三条分支，顺序不能换：

        1. **上一跳不可信** → 直接用 socket 地址，转发头整条当空气。
           客户端能自己写 `X-Forwarded-For`，信它等于把限流交给攻击者。
        2. **上一跳可信且带 `CF-Connecting-IP`** → 采信它。Cloudflare 边缘会覆写
           这个头，客户端伪造不进来；而且它是单值，比 XFF 链好判。
        3. **否则从 XFF 最右侧往左走**，跳过可信跳，第一个不可信的就是客户端。
           从右往左是关键：左边的值是客户端自己塞的，右边才是最近一跳写的。

        取到的值一律过 `_is_ip`；不合法就退回 socket 地址，绝不把半截字符串
        写进审计。
        """
        peer = (self.client_address[0] or "").strip()
        if peer not in self.trusted_proxies:
            return peer

        cf = (self.headers.get("CF-Connecting-IP") or "").strip()
        if _is_ip(cf):
            return cf

        hops: list[str] = []
        for raw in self.headers.get_all("X-Forwarded-For") or []:
            hops.extend(part.strip() for part in raw.split(",") if part.strip())
        for hop in reversed(hops):
            if _is_ip(hop) and hop not in self.trusted_proxies:
                return hop
        return peer

    def _forwarded_proto(self) -> str:
        """给上游的真实协议。同样只在上一跳可信时才采信转发头。

        网关自己只会说裸 HTTP，`http` 是默认值；经 TLS 终止型隧道/反代进来时
        （Cloudflare、nginx、frp + https）真实协议是 https，这里要透上去，
        否则上游按 http 生成绝对链接、Cookie 的 Secure 判断也会被带偏。
        """
        if (self.client_address[0] or "").strip() not in self.trusted_proxies:
            return "http"
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto if proto in {"http", "https"} else "http"

    # ── 会话判定（与 L3 同一条判据）────────────────────────────────────────
    def _session_user(self) -> str | None:
        """签名有效 **且** 账号还在，才算登录。

        只用签名有效是不够的：无状态 Cookie 的 `sub` 签发之后没人核对账号是否
        还存在，删号不能立刻踢人（最长还能用满 12 小时）。三处（本方法、`/login`
        反跳、日志）必须共用这一个入口 —— 各写一套判据的下场是
        `ERR_TOO_MANY_REDIRECTS`（`/` 认为没登录跳 `/login`，`/login` 认为登录了跳 `/`）。
        """
        if not self.auth_on:
            return "anonymous"
        payload = web_auth.session_from_headers(self.headers)
        if not payload:
            return None
        sub = str(payload.get("sub") or "")
        if not sub or not web_auth.user_exists(sub):
            return None
        # 阶段 7：「账号还在」之外还要「账号没被停用」。停用改的是 users.status，
        # 无状态 Cookie 不会自己失效 —— 不查这一步，停用要等 Cookie 到寿（最长
        # 12 小时）才生效，等于假锁。判据与 `/login` 共用 `_account_active`。
        if not _account_active(sub):
            return None
        return sub

    # ── 响应小工具 ──────────────────────────────────────────────────────────
    def _send(
        self,
        status: int,
        body: bytes | str,
        ctype: str = "text/html; charset=utf-8",
        headers: dict | None = None,
    ) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 网关自己出的页面（登录页、502）不许被任何站点 iframe 嵌 —— 否则攻击者
        # 套一层透明框就能骗到点击（点击劫持）。登录页是最该挡的那一个。
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # HSTS 只在真的走 HTTPS 时下发（2026-10-26 经隧道实测：公网侧登录页缺它）。
        # 局域网裸 HTTP 下发 HSTS 没意义还会把浏览器锁死在该域名的 https 上。
        if self._forwarded_proto() == "https":
            self.send_header("Strict-Transport-Security", "max-age=15552000")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _redirect(self, location: str, extra: dict | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(
        self,
        status: int,
        obj: dict,
        session: tuple[str, int] | None = None,
    ) -> None:
        """发 JSON。`session` = (cookie_value, max_age) 时顺带重签会话票
        （改密码/改用户名成功后用：无状态票里烙着用户名，改名不重签就是自己踢自己下线）。"""
        headers = None
        if session is not None:
            value, ttl = session
            headers = {
                "Set-Cookie": (
                    f"{web_auth.COOKIE_NAME}={value}; Path=/; Max-Age={int(ttl)}; "
                    f"HttpOnly; SameSite=Lax"
                )
            }
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def _read_json(self) -> dict | None:
        """读请求体并解析成对象；不是合法 JSON 对象就返回 None。"""
        try:
            raw = self._read_body()
        except ValueError:
            return None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ── 请求体 ──────────────────────────────────────────────────────────────
    def _read_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)  # 吃掉 trailer 后的空行
                    break
                total += size
                if total > MAX_BODY:
                    raise ValueError("请求体过大")
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(chunks)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(n)

    # ── HTTP 方法 ───────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    # ── 路由 ────────────────────────────────────────────────────────────────
    def _dispatch(self) -> None:
        try:
            raw_path = self.path or "/"
            parts = urllib.parse.urlsplit(raw_path)
            path = _normalize(parts.path)
            query = parts.query

            if path == "/healthz":
                return self._handle_health()

            if path == ASSET_PREFIX or path.startswith(ASSET_PREFIX + "/"):
                return self._handle_asset(path[len(ASSET_PREFIX) :].lstrip("/"))

            if path == "/login":
                if self.command in ("GET", "HEAD"):
                    return self._handle_login_page(query)
                if self.command == "POST":
                    return self._handle_login_submit()
                return self._json(405, {"ok": False, "error": "method not allowed"})

            if path == "/logout":
                return self._handle_logout()

            user = self._session_user()
            if user is None:
                # API 请求给 401 JSON，页面请求给 302 登录页：
                # 全给 302 会让前端 fetch 拿到一整页 HTML，排查起来像「接口返回了网页」。
                if path.startswith("/api/") or _wants_json(self.headers):
                    return self._json(401, {"ok": False, "error": "未登录"})
                nxt = path + (("?" + query) if query else "")
                return self._redirect("/login?" + urllib.parse.urlencode({"next": nxt}))

            # 账号自助管理（网关自己处理，不打到上游）：改密码 / 改用户名 / 我是谁。
            # 放在会话检查之后 = 未登录拿不到任何账号信息；放在代理之前 = 上游零改动，
            # 账号体系只有一个事实源（web_auth.json），不往 Graphite 里漏第二份。
            if path == "/api/gate/me" and self.command in ("GET", "HEAD"):
                return self._handle_gate_me(user)
            if path == "/api/gate/password" and self.command == "POST":
                return self._handle_gate_password(user)
            if path == "/api/gate/username" and self.command == "POST":
                return self._handle_gate_username(user)

            return self._proxy(path, query, user)
        except ValueError as e:
            self._send(413, f"请求被拒绝：{html.escape(str(e))}")
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端先断开，不算错误
        except Exception as e:  # noqa: BLE001
            # 兜底：任何未预期异常都别把 traceback 甩给浏览器
            try:
                self._send(500, f"网关内部错误：{html.escape(f'{type(e).__name__}: {e}')}")
            except Exception:  # noqa: BLE001
                pass

    # ── /healthz ────────────────────────────────────────────────────────────
    def _handle_health(self) -> None:
        self._json(
            200,
            {
                "ok": True,
                "service": "ai-memory-gate",
                "auth": bool(self.auth_on),
                # 只给 host:port，不给完整 URL —— 排障够用，别人扫到也拿不到内网路径结构
                "upstream": f"{self.upstream.scheme}://{self.upstream.host}:{self.upstream.port}",
            },
        )

    # ── /gate-assets/* ──────────────────────────────────────────────────────
    def _handle_asset(self, name: str) -> None:
        found = resolve_asset(name)
        if found is None:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        target, ctype = found
        try:
            data = target.read_bytes()
        except OSError:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 与 L3 一致：静态资源可以短缓存，登录页本身不缓存
        self.send_header("Cache-Control", "public, max-age=600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ── /login ──────────────────────────────────────────────────────────────
    def _handle_login_page(self, query: str) -> None:
        if not self.auth_on:
            return self._redirect("/")
        # 已经登录的人不该再看到登录页（也防「登录页 ↔ 首页」来回跳）
        if self._session_user() is not None:
            nxt = _safe_next(urllib.parse.parse_qs(query).get("next", ["/"])[0])
            return self._redirect(nxt)
        nxt = _safe_next(urllib.parse.parse_qs(query).get("next", ["/"])[0])
        self._send(200, web_auth.login_page(next_url=nxt, assets_prefix=ASSET_PREFIX))

    def _handle_login_submit(self) -> None:
        if not self.auth_on:
            return self._redirect("/")
        try:
            body = self._read_body().decode("utf-8", "replace")
        except ValueError as e:
            return self._send(413, f"请求被拒绝：{html.escape(str(e))}")
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        user = (form.get("user", [""])[0] or "").strip()
        password = form.get("password", [""])[0] or ""
        nxt = _safe_next(form.get("next", ["/"])[0])
        source = self._client_ip()

        wait = web_auth.login_locked(user, source)
        if wait > 0:
            _audit(
                "auth.login.fail",
                user,
                detail={"ok": False, "reason": "rate_limited", "wait": wait},
                ip=source,
            )
            return self._send(
                429,
                web_auth.login_page(
                    error=f"尝试次数过多，请 {wait} 秒后再试",
                    next_url=nxt,
                    user=user,
                    assets_prefix=ASSET_PREFIX,
                ),
            )

        rec = web_auth.load_config().get("users", {}).get(user)
        ok = bool(rec) and web_auth.verify_password(
            password, str(rec.get("salt", "")), str(rec.get("hash", ""))
        )
        if not ok:
            web_auth.note_login_fail(user, source)
            # 「用户不存在」与「口令错」在页面上故意同一句话（防用户名枚举），
            # 但审计里必须分开 —— 前者是有人在扫号，后者是有人记错口令。
            _audit(
                "auth.login.fail",
                user,
                detail={"ok": False, "reason": "bad_password" if rec else "no_such_user"},
                ip=source,
            )
            return self._send(
                401,
                web_auth.login_page(
                    error="用户名或密码不正确",
                    next_url=nxt,
                    user=user,
                    assets_prefix=ASSET_PREFIX,
                ),
            )

        # 阶段 7：口令对了但账号被停用 —— 在这里就拦，不发 cookie。
        # 先发再靠 _session_user 弹回来，用户体验是「登进去又被踢」，会一直重试。
        if not _account_active(user):
            _audit("auth.login.fail", user, detail={"ok": False, "reason": "account_disabled"}, ip=source)
            return self._send(
                403,
                web_auth.login_page(
                    error="该账号已停用，请联系管理员",
                    next_url=nxt,
                    user=user,
                    assets_prefix=ASSET_PREFIX,
                ),
            )

        web_auth.clear_login_fail(user, source)
        value, ttl = web_auth.issue_session(user)
        cookie = (
            f"{web_auth.COOKIE_NAME}={value}; Path=/; Max-Age={int(ttl)}; "
            f"HttpOnly; SameSite=Lax"
        )
        _audit("auth.login", user, detail={"ok": True}, ip=source)
        self._redirect(nxt, {"Set-Cookie": cookie})

    # ── /logout ─────────────────────────────────────────────────────────────
    def _handle_logout(self) -> None:
        # 先记「谁退出的」再清票：_session_user 读的是 cookie，清完就问不出来了
        try:
            who = self._session_user()
        except Exception:  # noqa: BLE001
            who = None
        if who:
            _audit("auth.logout", who, detail={"ok": True}, ip=self._client_ip())
        # 清票必须同时写 Max-Age=0；只发空值在某些浏览器上等于「没设置」
        cookie = f"{web_auth.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        self._redirect("/login", {"Set-Cookie": cookie})

    # ── /api/gate/*：账号自助管理 ───────────────────────────────────────────
    #
    # 为什么挂在网关而不是上游：账号与会话本来就是网关的（web_auth.json 在这边），
    # 上游 Graphite 零鉴权、也不该知道账号的存在。设置页的 UI 只是壳，
    # fetch 走同源相对路径，经网关时被这三个口子拦下，不经代理。
    #
    # 改密码/改用户名都要验「当前密码」—— 会话票被拷走时（无状态票的已知边界），
    # 至少改敏感信息还得有真密码，拷票的人改不了账号也夺不了号。
    # 失败尝试复用登录限流（同一 (用户, IP) 连错 5 次锁 5 分钟），
    # 否则拿到拷贝票的人可以无限次猜当前密码。

    def _handle_gate_me(self, user: str) -> None:
        role = self._user_role(user)
        self._json(200, {"ok": True, "user": user, "auth": bool(self.auth_on), "role": role})

    @staticmethod
    def _user_role(user: str) -> str:
        cfg = web_auth.load_config()
        if user in set(cfg.get("admin_users", [])):
            return "admin"
        role = str(cfg.get("users", {}).get(user, {}).get("role") or "editor")
        return role if role in {"editor", "readonly"} else "editor"

    def _gate_change_locked(self, user: str) -> int:
        return web_auth.login_locked(user, self._client_ip())

    def _handle_gate_password(self, user: str) -> None:
        if not self.auth_on:
            return self._json(400, {"ok": False, "error": "鉴权未开启，无账号可改"})
        wait = self._gate_change_locked(user)
        if wait > 0:
            return self._json(429, {"ok": False, "error": f"尝试次数过多，请 {wait} 秒后再试"})
        data = self._read_json()
        if data is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        current = str(data.get("current_password") or "")
        new = str(data.get("new_password") or "")
        rec = web_auth.load_config().get("users", {}).get(user)
        if not rec or not web_auth.verify_password(current, str(rec.get("salt", "")), str(rec.get("hash", ""))):
            web_auth.note_login_fail(user, self._client_ip())
            return self._json(401, {"ok": False, "error": "当前密码不正确"})
        if len(new) < 8:
            return self._json(400, {"ok": False, "error": "新密码太短，至少 8 位"})
        web_auth.clear_login_fail(user, self._client_ip())
        web_auth.set_user(user, new, note=str(rec.get("note", "")))
        try:  # 管理员页要能看到用户自己改的新口令；失败不影响改密本身
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import admin_api as _admin_api
            _admin_api.record_password(user, new)
        except Exception:  # noqa: BLE001
            pass
        # 已知边界（与 web_auth 模块注释同口径）：无状态票不绑密码哈希，
        # 改密码不会让旧票失效；这里重签一张只是续期，不是吊销。
        self._json(200, {"ok": True, "user": user}, session=web_auth.issue_session(user))

    def _handle_gate_username(self, user: str) -> None:
        if not self.auth_on:
            return self._json(400, {"ok": False, "error": "鉴权未开启，无账号可改"})
        wait = self._gate_change_locked(user)
        if wait > 0:
            return self._json(429, {"ok": False, "error": f"尝试次数过多，请 {wait} 秒后再试"})
        data = self._read_json()
        if data is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        current = str(data.get("current_password") or "")
        new_name = str(data.get("new_username") or "").strip()
        rec = web_auth.load_config().get("users", {}).get(user)
        if not rec or not web_auth.verify_password(current, str(rec.get("salt", "")), str(rec.get("hash", ""))):
            web_auth.note_login_fail(user, self._client_ip())
            return self._json(401, {"ok": False, "error": "当前密码不正确"})
        if (
            not new_name
            or len(new_name) > 32
            or any(ch.isspace() or ord(ch) < 32 for ch in new_name)
        ):
            return self._json(400, {"ok": False, "error": "用户名不合法：1~32 位，不能含空白或控制字符"})
        if new_name == user:
            return self._json(400, {"ok": False, "error": "新用户名与当前相同"})
        # 冲突检查（主人 2026-10-23 要求：不能和其它用户的账号冲突）。
        # rename_user 里还有一道兜底，但错误信息在这里才分得清「占用」和「不存在」。
        if web_auth.user_exists(new_name):
            return self._json(409, {"ok": False, "error": "该用户名已被其它账号占用"})
        if not web_auth.rename_user(user, new_name):
            return self._json(409, {"ok": False, "error": "改名失败：账号状态已变化，请刷新后重试"})
        try:  # 权限目录与令牌记录跟到新名下，避免管理员页出现「有票无账号」
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import admin_api as _admin_api
            _admin_api.rename_account(user, new_name)
        except Exception:  # noqa: BLE001
            pass
        web_auth.clear_login_fail(user, self._client_ip())
        # 旧名票据已随 user_exists(old) 失效，必须用新名重签，否则人被自己踢下线
        self._json(200, {"ok": True, "user": new_name}, session=web_auth.issue_session(new_name))

    # ── 反向代理 ────────────────────────────────────────────────────────────
    def _stream_sse(self, resp: http.client.HTTPResponse) -> None:
        """SSE 流式透传（阶段 10，2026-10-24）。

        借鉴 nginx / AWS 的 SSE 反代清单（proxy_buffering off 那一套）：

        - **不缓冲**：读到多少推多少 + 逐块 flush。原来的 `resp.read()` 整包
          语义对 SSE 是致命的 —— 上游一直写、邮差一直等，客户端什么都收不到。
        - **不带 Content-Length**：改发 `Connection: close`，用「读到断开为止」
          表达流结束（等价 chunked 的语义，省掉自己分帧）。
        - **不改上游超时**：`PROXY_TIMEOUT=300s` 留着当本线程的「上游活性
          看门狗」—— 上游每 20s 有心跳帧，正常永不触发；真静默 300s 说明
          上游死了，断流让浏览器带 Last-Event-ID 重连补发（服务端有补发）。
        - **客户端断开**（关页 / 刷新）：BrokenPipe 当正常结束，回收上游连接。
        """
        self.send_response(resp.status, resp.reason)
        for key, value in resp.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("content-length", "set-cookie"):
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            while True:
                # 必须 read1 不是 read：read(8192) 在 chunked 流上会「凑满 8192
                # 才返回」，不足就跨 chunk 继续等 —— 心跳 20s 一帧，等于把流
                # 又攥回手里。read1 只做一次底层读，有多少给多少。
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            # 客户端走了或上游死了：两边都会自己重连 / 回收，这里不打扰任何人
            pass

    def _proxy(self, path: str, query: str, user: str | None = None) -> None:
        if user and self._user_role(user) == "readonly" and self.command not in {"GET", "HEAD", "OPTIONS"}:
            return self._json(403, {"ok": False, "error": "只读用户不能修改内容"})
        try:
            body = self._read_body()
        except ValueError as e:
            return self._send(413, f"请求被拒绝：{html.escape(str(e))}")

        target = self.upstream.prefix + path + (("?" + query) if query else "")
        headers = {}
        for key, value in self.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "cookie", "content-length"):
                continue
            headers[key] = value
        # 上游要知道真实来源（它可能在日志/生成绝对链接时用到）。这里必须写
        # `_client_ip()` 而不是 socket 地址 —— 否则反代场景下上游看到的是隧道主机。
        headers["X-Forwarded-For"] = self._client_ip()
        # 协议同理：经 TLS 终止型隧道进来时，真实协议是 https，写死 http 会让上游
        # 生成 http 绝对链接、把 Cookie 的 Secure 判断带偏。
        headers["X-Forwarded-Proto"] = self._forwarded_proto()
        headers["X-Forwarded-Host"] = self.headers.get("Host") or self.upstream.host
        # 上游只监听回环，身份由网关会话产生；客户端不能直接伪造这个头。
        # HTTP headers are latin-1 in http.client; encode the Chinese login name.
        headers["X-AI-Memory-User-B64"] = base64.urlsafe_b64encode((user or "").encode("utf-8")).decode("ascii")

        conn = None
        try:
            conn = self.upstream.connect()
            conn.request(self.command, target, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return self._send(
                502,
                _FALLBACK_502 % (html.escape(self.upstream.label), html.escape(f"{type(e).__name__}: {e}")),
            )

        # SSE 流式透传（阶段 10，2026-10-24）：text/event-stream 不整包缓冲，
        # 读到多少推多少 —— 否则聊天事件会被「邮差」攥在手里永远出不去。
        if (resp.headers.get("Content-Type") or "").strip().lower().startswith("text/event-stream"):
            try:
                return self._stream_sse(resp)
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            data = resp.read()
        except (socket.timeout, OSError) as e:
            return self._send(
                502,
                _FALLBACK_502 % (html.escape(self.upstream.label), html.escape(f"{type(e).__name__}: {e}")),
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        self.send_response(resp.status, resp.reason)
        for key, value in resp.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low == "content-length" or low == "set-cookie":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _account_active(username: str) -> bool:
    """账号是否可用（阶段 7）。判据只此一份，`/login` 与 `_session_user` 共用。

    `admin_api` 不可用（模块缺失/库故障）时返回 True —— 库坏了是另一条故障链，
    此时把所有人锁在门外并不能让事情变好，而笔记本来也读不出来。
    """
    try:
        import admin_api as _admin_api

        return _admin_api.account_active(username)
    except Exception:  # noqa: BLE001
        return True


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0]


def _normalize(path: str) -> str:
    """归一化 URL 路径，并挡下目录穿越。

    客户端一般会自己把 `/a/../b` 归一化后再发，所以光靠「看到 `..` 就拒」
    测不出问题（curl 要 `--path-as-is` 才发得出去）。这里两种都覆盖：
    先归一化，再检查结果里是否还残留 `..` 段。
    """
    if not path.startswith("/"):
        path = "/" + path
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if ".." in norm.split("/"):
        raise ValueError("非法路径")
    return norm


def _safe_next(raw: str) -> str:
    """只允许站内相对路径，挡开放重定向（`//evil.com`、`http://evil.com`）。"""
    nxt = (raw or "/").strip()
    if not nxt.startswith("/") or nxt.startswith("//"):
        return "/"
    if "\r" in nxt or "\n" in nxt:
        return "/"
    return nxt


def _wants_json(headers) -> bool:
    accept = (headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _parse_trusted_proxies(cli_values: list[str] | None) -> tuple[str, ...]:
    """命令行 > 环境变量 `GATE_TRUSTED_PROXIES` > 默认 127.0.0.1,::1。

    非法项**显式丢弃并告警**，不静默全失效 —— 白名单写错一个字符就整条不管用，
    表现是「限流又变成全站共用一个 IP」，那种失灵没有任何报错可循，只能靠这句告警。
    """
    raw: list[str] = []
    for item in cli_values or []:
        raw.extend(item.split(","))
    if not cli_values:
        raw.extend((os.environ.get("GATE_TRUSTED_PROXIES") or "").split(","))
    values = [v.strip() for v in raw if v.strip()]
    if not values:
        return DEFAULT_TRUSTED_PROXIES
    kept: list[str] = []
    for value in values:
        if _is_ip(value):
            if value not in kept:
                kept.append(value)
        else:
            print(f"警告：--trusted-proxy 里的 {value!r} 不是合法 IP，已忽略", file=sys.stderr)
    return tuple(kept) or DEFAULT_TRUSTED_PROXIES


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="web_gate.py", description="登录网关 + 反向代理")
    ap.add_argument("--upstream", default=os.environ.get("GATE_UPSTREAM") or "http://127.0.0.1:8788")
    ap.add_argument("--host", default=os.environ.get("GATE_HOST") or "0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GATE_PORT") or 8766))
    ap.add_argument("--no-login", action="store_true", help="关闭鉴权（自测/排障用，勿用于生产）")
    ap.add_argument(
        "--trusted-proxy",
        action="append",
        default=None,
        metavar="IP",
        help=(
            "可信的上一跳地址（可重复，也可逗号分隔）。只有这些地址送来的 "
            "X-Forwarded-For / CF-Connecting-IP 才被采信，用来还原真实访客 IP。"
            "默认 127.0.0.1,::1；经 cloudflared / frpc / nginx 进来时，把那一跳的地址加进来。"
        ),
    )
    args = ap.parse_args(argv)

    up = Upstream(args.upstream)
    GateHandler.upstream = up
    GateHandler.auth_on = not args.no_login
    GateHandler.trusted_proxies = _parse_trusted_proxies(args.trusted_proxy)

    if GateHandler.auth_on and not web_auth.has_users():
        print(
            "警告：还没有任何账号，登录页将无法通过。先建一个：\n"
            f"  python {HERE / 'web_auth.py'} init --user 你的名字",
            file=sys.stderr,
        )
    if not ASSET_DIR.is_dir():
        print(f"警告：静态资源目录不存在，登录页会缺背景与动画：{ASSET_DIR}", file=sys.stderr)

    socketserver.TCPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((args.host, args.port), GateHandler)
    httpd.daemon_threads = True
    print(
        f"ai-memory gate  listening on http://{args.host}:{args.port}  ->  {up.label}"
        f"  (auth={'off' if args.no_login else 'on'}"
        f"  可信代理={','.join(GateHandler.trusted_proxies)})",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
