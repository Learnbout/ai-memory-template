"""FastAPI 实例 + CORS + 路由挂载（路径前缀 /api）。"""

from __future__ import annotations

import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .paths import describe as describe_paths
from .paths import ensure_data_dir, is_local, mode as runtime_mode, resource_path
from .routers import (
    admin,
    admin_mcp,
    agent,
    audit_api,
    chat_api,
    dashboard,
    data_api,
    events_api,
    me,
    notes,
    notices_api,
    ops_api,
    plan_api,
    plugin_api,
    runs,
    settings,
    tasks_api,
    tools,
    update_api,
)
from . import chat_stream, data_stream, updater
from .vault import VaultUnavailable

app = FastAPI(title="Graphite Workspace API", version="1.0.0")


@app.on_event("startup")
async def _wire_stream_loop() -> None:
    """把主事件循环交给 chat_stream（阶段 10）。

    写接口跑在线程池里，asyncio.Queue 不许跨线程摸 —— 广播必须经
    `loop.call_soon_threadsafe` 转投，而那需要循环引用。SSE 没人用时
    这行也无害（publish 侧对空循环静默跳过）。
    """
    import asyncio

    # 2026-10-27（R2）：数据目录在这里建一次并实测可写。
    #
    # 为什么放在启动钩子而不是 import：`data_dir()` 刻意保持纯函数（不 mkdir），
    # 这样「目录不存在」才能当「全新安装」的信号；建目录/探权限是启动期的一次性动作。
    # 失败直接抛 → uvicorn 起不来 → 日志里有明确报错。**不回落**到别的目录：
    # 静默换目录比启动失败危险得多，数据会写到一个谁也想不到的地方。
    ensure_data_dir()

    # 阶段 26：安装器写下的管理员凭据，第一次启动时消费掉（只在中心化形态生效）。
    # 失败只打日志，绝不阻断启动 —— 少个账号是小事，服务起不来是大事。
    try:
        from .bootstrap_admin import bootstrap_once

        _msg = bootstrap_once()
        if _msg:
            import logging

            logging.getLogger("graphite").info("[bootstrap] %s", _msg)
            print("[bootstrap] %s" % _msg, flush=True)
    except Exception as _exc:  # noqa: BLE001
        print("[bootstrap] 跳过：%s: %s" % (type(_exc).__name__, _exc), flush=True)

    chat_stream.set_loop(asyncio.get_running_loop())
    # 2026-10-26：数据变更巡检线程（12s 一次，变了推给 SSE 订阅者）——
    # 替代原来「每个标签页各自 20s 轮询」，开多了也不堆请求。
    data_stream.start()


@app.exception_handler(VaultUnavailable)
async def _on_vault_unavailable(request: Request, exc: VaultUnavailable) -> JSONResponse:
    """库读不出来 → 503，且把原因原样交给前端。

    2026-10-20 主人裁定：库是唯一原件，md 只是可导出快照，「库坏了就读 md」
    等于让页面展示可能已过期的数据。所以这里不再降级，直接把故障暴露出来，
    由前端整站进「记忆库不可用」页（`VaultGate`）。
    """
    return JSONResponse(status_code=503, content={"detail": f"记忆库不可用：{exc}"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
#  阶段 25（2026-10-25）：公网前的安全基线
#
#  两条都放在这里，是因为它们必须覆盖**全站**（含 SPA 静态回退），
#  放在任何一个业务路由上都只能护住一部分。
#
#  ── 为什么 CSP 里 style-src 留了 unsafe-inline ──────────────
#  antd 是 CSS-in-JS，运行时注入 <style>；GSAP 会给元素写 style 属性。
#  去掉 unsafe-inline 的代价是把整个 UI 框架换掉，收益只是「少一档注入面」。
#  真正要紧的 script-src 保持 'self'：外部脚本一个都进不来——
#  插件代码也因此只能从 `/api/plugins/*/asset/*` 这个同源口子加载，不再是任意 URL。
#
#  ── CSRF 为什么查 Origin 而不是发 token ────────────────────
#  本应用的写操作全部是 fetch/XHR（没有传统表单提交），浏览器对非 GET 请求
#  必然带 Origin。校验「有 Origin 且不同源 → 拒绝」能挡住跨站伪造，
#  且不需要在前后端各加一套 token 生命周期（多一套状态就多一处能错）。
#  Origin 为空时放行：那是 curl / 服务端内部调用 / 老客户端，不是浏览器跨站。
# ============================================================================
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "media-src 'self' blob:; "
    "frame-src 'self' blob:; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_same(origin: str, host: str) -> bool:
    """Origin 的 host:port 是否与请求的 Host 一致（忽略 scheme：内网是 http，将来上 https 也不该失配）"""
    try:
        from urllib.parse import urlsplit

        return urlsplit(origin).netloc.lower() == (host or "").lower()
    except ValueError:
        return False


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    # ── CSRF：写请求的 Origin 必须同源 ──────────────────────────────────
    # 2026-10-26：接受 X-Forwarded-Host 作为第二个同源基准。web_gate 反代
    # 会剥掉原始 Host（上游只看到 127.0.0.1:8788），但浏览器 Origin 仍是
    # 用户访问用的 host:port —— 直比 Host 必然失配，所有写请求全 403
    # （主人先在聊天里撞见）。网关本来就转发 X-Forwarded-Host = 原始 Host，
    # 拿它比 Origin 安全性不降：攻击者跨站请求的 Origin 依然对不上。
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin") or ""
        host = request.headers.get("host") or ""
        # X-Forwarded-Host 只信「从本机连进来的」请求（= 网关反代）。
        # ── 2026-10-26 14:02 修正（journal 实测）───────────────────────
        # 不能拿 request.client 当「本机」判据：uvicorn 默认 proxy_headers=True，
        # 会把 client 重写成 X-Forwarded-For（真实浏览器 IP）—— 网关转发的每个
        # 请求 peer 都显示反向代理所在网段的地址，永不 loopback，上一版因此全 403。
        # 真正的安全边界是「8788 只绑 127.0.0.1」（ss 实测）：能建立 TCP 的
        # 必然是本机进程（网关），远端直连伪造 XFH 的前提是绑定回环已失守。
        # 所以有 XFH 即采信，再要求 Origin 与网关 Host（XFH）同源 —— 跨站
        # 页面的 Origin 对不上 fwd，防线不变。
        fwd = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        ok = _origin_same(origin, host) or (bool(fwd) and _origin_same(origin, fwd))
        if origin and not ok:
            return JSONResponse(
                status_code=403,
                content={"detail": "跨站请求被拒绝（Origin 与本站不同源）"},
            )

    response = await call_next(request)

    # setdefault：让个别路由（如后续插件资源）能自己覆盖更严格的策略
    h = response.headers
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(), usb=()")
    # HSTS 只在真 HTTPS 时发：内网 http 下发 HSTS 会把浏览器锁成 https，
    # 而 8766 没有证书 —— 那是把自己锁在门外（LeafWiki 的 require_secure 是同一条道理）。
    if request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") or "") == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# 阶段 26（2026-11-18）：按运行形态决定注册哪些路由。
#
# 本地版（local）**不注册**多人协作相关路由，而不是「注册了再拒绝」：
#   · admin / admin_mcp —— 单人环境没有账号可管
#   · chat_api / chat_stream —— 库内聊天是给多人用的
#   · audit_api —— 单人不存在「审计谁」
#   · ops_api —— 备份/恢复/轮转是服务端运维，本地用户不该看到
#   · notices_api —— 通知中心的未读水位来自聊天与计划，本地版没有来源
# 访问这些路径会得到 **404**（不是 403）：403 等于告诉扫描者「这里有个门」。
_LOCAL_SKIP = {"admin", "admin_mcp", "chat_api", "audit_api", "ops_api", "notices_api"}
_ALL_ROUTERS = (
    notes, tools, runs, agent, settings, dashboard, admin, admin_mcp, chat_api,
    audit_api, me, plan_api, ops_api, notices_api, update_api, tasks_api,
    plugin_api, data_api, events_api,
)
_MODE = runtime_mode()
for r in _ALL_ROUTERS:
    if _MODE == "local" and r.__name__.rsplit(".", 1)[-1] in _LOCAL_SKIP:
        continue
    app.include_router(r.router, prefix="/api")

# SSE 事件流（阶段 10）：单独文件，与 chat_api 的写接口互不干扰；
# 挂载顺序在通配 SPA 路由之前，保证 /api/chat/stream 不被吞掉。
# 本地版没有聊天，这条流自然也不该在（未注册 → 404）。
if _MODE != "local":
    app.include_router(chat_stream.router, prefix="/api")


@app.get("/api/mode")
def api_mode() -> dict:
    """当前运行形态（前端据此隐藏入口；也是安装器与验收脚本的判据）。"""
    return {"ok": True, "mode": _MODE, "local": _MODE == "local"}


_TAG_SCRIPT_RE = re.compile(r"<script\b[^>]*>", re.I)
_ATTR_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ATTR_MODULE_RE = re.compile(r"""\btype\s*=\s*["']module["']""", re.I)


def _entry_asset_name() -> str:
    """入口 JS 文件名 —— 从 index.html 的真实内容里解析，不做任何猜测。

    ## 为什么不能 glob 排序（2026-10-28 修）

    旧实现：`js = sorted(DIST.glob("assets/index-*.js")); out["frontend_asset"]
    = js[-1].name if js else ""`。取的是**字典序最大的那个**，不是入口。

    实测产物里 `assets/index-*.js` 有 10 个（Vite 的异步 chunk 也叫 `index-*`），
    字典序最大的是 `index-ZOFX89ji.js`，而 index.html 真正加载的是
    `index-CO9W8u0w.js`。这个字段的用途是「拿它跟浏览器顶栏的构建标识对一眼，
    就知道是服务端没换还是浏览器没换」—— 名字取错，这个诊断价值就是 0，
    而且比没有更糟：它会指向一个根本没被请求过的文件。

    ## 为什么按 type="module" + /assets/ 认，而不是取第一个 src

    产物里有两个 `<script src>`：head 里 Vite 注入的入口（module，在 /assets/），
    和 body 末尾的 `<script src="/boot.js">`。按文档顺序取第一个**这次**恰好对，
    但那是巧合 —— 入口反正是 Vite 注入的 module 脚本，认属性比认顺序可靠。

    优先级：module + /assets/  >  /assets/  >  module  >  其他。
    同级保持文档顺序（list.sort 是稳定排序）。

    index.html 读不到或里面没有 `<script src>` 时返回 **空字符串**，不退回 glob：
    那时候前端入口本身就不存在（`/` 也已经 404），瞎报一个名字反而是误导。
    """
    try:
        html = (DIST / "index.html").read_text(encoding="utf-8", errors="replace")
    except NameError:  # DIST 定义在文件后段，模块没走完就是没这名字
        return ""
    except OSError:
        return ""
    if not html:
        return ""

    ranked: list[tuple[int, str]] = []
    for tag_m in _TAG_SCRIPT_RE.finditer(html):
        tag = tag_m.group(0)
        src_m = _ATTR_SRC_RE.search(tag)
        if not src_m:
            continue
        src = (src_m.group(1) or src_m.group(2) or "").strip()
        # 剥掉 ?v= / #hash —— `index-xxx.js?v=1` 直接 endswith(".js") 会漏判
        src = src.split("?", 1)[0].split("#", 1)[0]
        name = src.rsplit("/", 1)[-1]
        if not name.endswith(".js"):
            continue
        is_module = bool(_ATTR_MODULE_RE.search(tag))
        in_assets = "/assets/" in src
        if is_module and in_assets:
            rank = 0
        elif in_assets:
            rank = 1
        elif is_module:
            rank = 2
        else:
            rank = 3
        ranked.append((rank, name))

    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


@app.get("/api/health")
def health() -> dict:
    out = {
        "ok": True,
        "service": "graphite-workspace",
        # 2026-10-23：端口不再写死 8787 —— 服务器上是 8788（回环），
        # 报固定值会让人拿错端口去排障。
        "port": int(os.environ.get("GRAPHITE_PORT") or 8787),
        # 阶段 12：版本从 <代码根>/VERSION 读，不在这里写常量。
        # 为什么健康检查要带版本：更新完的第一步就是打这个接口看「换没换过来」，
        # 没有它就只能靠人看页面，而页面有缓存。
        "version": updater.current_version(),
    }
    # 前端产物文件名（2026-10-25）：排「部署了但页面还是旧的」时，拿它跟浏览器顶栏
    # 显示的构建标识对一眼就知道是谁的问题 —— 服务端已换、浏览器没换。
    # 2026-10-28：改为从 index.html 解析真实入口（原 glob 排序取到的是无关 chunk）。
    out["frontend_asset"] = _entry_asset_name()
    return out


@app.get("/api/health/paths")
def health_paths() -> dict:
    """路径解析自述：每个键的值、来源（env/file/default）、是否存在。

    为什么单列一个端点：中心化后最常见的故障是「库指错 → 工作台回落扫盘 →
    页面数据是旧的」，而它在 UI 上只表现为数据不对、不报错。这里能一眼看出
    vault_root 到底解析成了什么、来自哪个来源。
    """
    return describe_paths()


# ---------------------------------------------------------------------------
# 前端静态托管（2026-10-15，常驻改造）
#
# 为什么把 dist 也交给本进程：前端 `BASE = '/api'` 是相对路径，同一端口 =
# 同源 = 零 CORS、零代理配置；于是「整个网页」只剩一个进程要养，常驻就只看住它。
# （对比用 Apache 反代：要多养一个 httpd、还要改它的 vhost/代理配置，
#   而后端进程反正照样得常驻 —— 省不掉，白搭。）
#
# 四个必须注意的点：
#   1. 本通配路由注册在 /api 之后 —— FastAPI 按注册顺序匹配，API 优先级不变；
#      /docs 与 /openapi.json 在 app 创建时就已注册，同样先于它。
#   2. 必须回退到 index.html：前端是 history 路由，直接刷新 /memory-explorer
#      这类深链时磁盘上并没有这个文件，不回退就是 404。
#   3. 防目录穿越：resolve 后必须仍位于 DIST 之下才允许读盘。
#   4. dist 不存在（未构建 / 纯 API 部署）时整段跳过，不影响 API。
# ---------------------------------------------------------------------------

DIST = resource_path("frontend", "dist")

if DIST.is_dir():
    # 带内容哈希的资源名（index-XXXX.js）永远可以长缓存。Starlette 0.37+ 的
    # StaticFiles 才支持 headers 参数，老版本会抛 TypeError —— 降级成默认行为，
    # 不影响功能（只是少了 immutable 这一层优化）。
    try:
        app.mount(
            "/assets",
            StaticFiles(directory=DIST / "assets", headers={"Cache-Control": "public, max-age=31536000, immutable"}),
            name="assets",
        )
    except TypeError:  # pragma: no cover - 老版 Starlette
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    # index.html 必须「可以存，但每次都要验证」（2026-10-25：主人遇到「部署了但页面
    # 还是旧的」，表现为新功能全都没有）。
    # FileResponse 默认只给 ETag/Last-Modified、**不给 Cache-Control**，浏览器按启发式
    # 规则自行估一个新鲜期，部署后就会拿着旧 HTML 去引旧的 <script src>。
    # 用 no-cache 而不是 no-store：不禁止缓存，也不每次重传，只强制回源验证 ——
    # 内容没变就是 304，代价最小。
    _HTML_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        # /api 下不存在的路径必须 404 JSON，不能落到 SPA 回退
        # （2026-10-25 阶段 25：之前 /api/plugins/x/asset/../main.py 这类越界请求
        #  会返回 200 + index.html，看起来像「读到了东西」，实际是 SPA 兜底 —— 既误导排障，
        #  也让「接口不存在」和「接口返回页面」混在一起）
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="接口不存在")
        target = (DIST / full_path).resolve() if full_path else DIST / "index.html"
        if full_path and target.is_file() and DIST in target.parents:
            return FileResponse(target, headers=_HTML_HEADERS)
        return FileResponse(DIST / "index.html", headers=_HTML_HEADERS)
