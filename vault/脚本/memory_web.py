#!/usr/bin/env python3
"""L3 可写 Web：知识库数据库化的最小可用原型。

三层 Web 路线的第三层。约束来自 2026-09-10 的主源定性：

    Markdown = 唯一事实源（写路径只走这里）
    SQLite   = 查询索引 + 渲染层（可随时从 md 重建，损坏不构成数据事故）

因此本服务：
- 索引库放 `数据库/index.sqlite3`（2026-10-19 起它**是记忆库本体**，不再「随时可删」）
- 「编辑保存」走 memory_store 的写入函数，与 MCP 通道完全同一条写路径
- 每个页面都显示库内相对路径（rel_path），一个页面对应 notes 表的一行

零第三方依赖：http.server + sqlite3 + 自写 Markdown 渲染。

用法：
    python 脚本/memory_web.py                 # http://127.0.0.1:8766/
    python 脚本/memory_web.py --port 9000
    python 脚本/memory_web.py --rebuild-only  # 只重建索引后退出

端点：
    /                    首页（统计 + 目录导航 + 最近更新 + 搜索）
    /list?dir=&type=     列表（可按目录/类型筛选）
    /note/<标题>          详情（Markdown 渲染 + 反向链接 + 库内路径）
    /edit/<标题>          编辑（写回 md）
    /search?q=           搜索
    /rebuild             重建索引
    /api/notes?q=        JSON 接口
    /api/note/<标题>      JSON 单条
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import html
import json
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
VAULT = Path(os.environ.get("AI_MEMORY_DIR") or CODE_ROOT).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
# `AI_MEMORY_DIR` 指定的是**数据目录**，代码永远跟着脚本文件走。常规部署里两者是
# 同一棵树（阶段 1 的硬事实：代码与库必须同树，否则 vault_db 的 scripts 路径会断）。
# 但测试需要一个独立的数据目录来避免污染真实库，此时两组路径不同 —— 把代码根补到
# sys.path 末尾兜底。放末尾而不是开头：万一同名模块两边都有，优先用数据目录那份，
# 保持「同树优先」的既有语义。
if str(CODE_ROOT) != str(VAULT):
    sys.path.append(str(CODE_ROOT))
# 2026-10-19：库文件收口到「数据库/」。位置解析只有一处（db_paths），
# 带「旧位置 .workbuddy/ 回落 + 告警」，避免代码改了文件没搬时读写分裂。
import db_paths  # noqa: E402
import web_auth  # noqa: E402 - 阶段 4：登录与会话（同目录，纯标准库 + secret_io）
import acl  # noqa: E402 - 阶段 6：归属库常量与权限判定
import note_grade  # noqa: E402 - 阶段 6：写入时给新笔记定级

INDEX_PATH = db_paths.resolve("index.sqlite3")

# 写路径是否可用。启动时由 write_path_status() 探一次，之后 /healthz 直接读这里
# （healthz 要便宜，不能每次请求都 import 一遍）。None = 还没探。
WRITE_OK: bool | None = None

# 2026-10-23 分层后：AUTH_ON 与 _CURRENT_USER 的**单源**在 web_views（wv.*），
# 这里不再另定义 —— 两处各写一份就会出现「--no-login 只关了一层」的裂缝。
# 无需登录即可访问的路径。刻意只放这几个：
# /login 是入口；/logout 要能清 Cookie（否则登录过期后连退都退不了）；
# /healthz 留给 systemd / 监控探活，不含任何库内容。
# /assets/* 是登录页自己的静态资源（土星背景脚本）—— 登录页在未登录状态下
# 就要加载它，所以必须免鉴权。它只含渲染代码，读不到任何笔记内容。
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico"}

# 静态资源目录：跟着**代码根**走（不跟数据目录），因为它是程序的一部分，
# 测试时把 AI_MEMORY_DIR 指到临时目录也仍然要能取到。
ASSET_DIR = CODE_ROOT / "脚本" / "web_assets"
ASSET_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


import web_views as wv  # noqa: E402 - 视图层（AUTH_ON/_CURRENT_USER 单源在此）
import web_store  # noqa: E402 - 阶段 10：只读模式开关（readonly_mode / ReadOnlyReplica）
from web_store import connect, save_note, rebuild_derived  # noqa: E402
from web_views import (  # noqa: E402
    GRADE_LABELS, GRADE_RANK_SET, _can_set_grade, _esc, _is_replica,
    detail, edit_form, graph, graph_data, home, json_notes, listing, page, search,
)


# ────────────────────────── HTTP ──────────────────────────


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code: int = 200, *, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _html(self, text: str, code: int = 200) -> None:
        self._send(text.encode("utf-8"), "text/html; charset=utf-8", code)

    def _json(self, data: object, code: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _route(self) -> tuple[str, dict]:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        return path, {k: v[0] for k, v in qs.items()}

    # ── 阶段 4：登录相关 ────────────────────────────────────────────────

    def _redirect(self, location: str, *, set_cookie: str = "", clear_cookie: bool = False) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        if clear_cookie:
            # Max-Age=0 是让浏览器立刻删掉；同时带 HttpOnly/SameSite 与签发时一致，
            # 否则某些浏览器会因为「属性不一致」拒绝覆盖同名 Cookie。
            self.send_header(
                "Set-Cookie",
                f"{web_auth.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
        self.end_headers()

    @staticmethod
    def _sess_cookie(value: str, max_age: int) -> str:
        # 不加 Secure：当前是内网 http。将来上反向代理走 https 时再加，
        # 现在就加会导致浏览器根本不保存这个 Cookie（症状是「登录成功但立刻又跳回登录页」）。
        return (
            f"{web_auth.COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={max_age}"
        )

    def _source(self) -> str:
        try:
            return self.client_address[0] or "-"
        except Exception:  # noqa: BLE001
            return "-"

    def _handle_login(self, form: dict) -> None:
        user = (form.get("user") or "").strip()
        pw = form.get("password") or ""
        nxt = form.get("next") or "/"
        # 只接受站内相对路径，挡开放重定向（//evil.com 也会被浏览器当成外站）
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"

        src = self._source()
        wait = web_auth.login_locked(user, src)
        if wait:
            self._html(
                web_auth.login_page(
                    error=f"尝试次数过多，请 {wait} 秒后再试", next_url=nxt, user=user
                ),
                429,
            )
            return

        rec = web_auth.load_config().get("users", {}).get(user)
        if rec:
            ok = web_auth.verify_password(pw, rec.get("salt", ""), rec.get("hash", ""))
        else:
            # 用户名不存在时也走一遍同样开销的哈希，避免「有没有这个账号」被响应时间枚举出来。
            web_auth.verify_password(pw, "00" * 16, "00" * 32)
            ok = False

        if not ok:
            web_auth.note_login_fail(user, src)
            self._html(
                web_auth.login_page(error="用户名或密码不对", next_url=nxt, user=user), 401
            )
            return

        # 阶段 7：口令对了但账号已被停用 —— 必须在这里拦住，不能先发 cookie 再让
        # `_guard` 把人弹回来。后者在浏览器里的观感是「登进去又被踢出」，用户会
        # 一直重试，而登录失败计数还会被这些重试带乱。
        try:
            import admin_api as _admin_api

            _active = _admin_api.account_active(user)
        except Exception:  # noqa: BLE001 —— 模块不可用时不阻断登录（库故障另有提示）
            _active = True
        if not _active:
            self._html(
                web_auth.login_page(error="该账号已停用，请联系管理员", next_url=nxt, user=user), 403
            )
            return

        web_auth.clear_login_fail(user, src)
        value, ttl = web_auth.issue_session(user)
        self._redirect(nxt, set_cookie=self._sess_cookie(value, ttl))

    def _session_user(self) -> str:
        """会话可用 → 返回用户名；否则空串。

        「可用」= 签名有效 **且** 账号还在。这个判定必须在**所有**地方一致：
        `_guard`、`/login` 的「已登录就别再给我看登录页」、以及状态条。
        2026-10-23 实测踩到的反例：只在 `_guard` 里加了「账号还在」的检查，
        `/login` 仍按「签名有效」放行 —— 于是 `/` 认为没登录 → 跳 `/login`，
        `/login` 认为已登录 → 跳 `/`，浏览器直接 ERR_TOO_MANY_REDIRECTS。
        删除账号后任何一处判定不同步，都会变成死循环。
        """
        sess = web_auth.session_from_headers(self.headers)
        if not sess:
            return ""
        sub = sess.get("sub", "")
        if not sub or not web_auth.user_exists(sub):
            return ""
        # 阶段 7：「签名有效 + 账号还在」之外还要「账号没被停用」。停用改的是
        # users.status，无状态 Cookie 不会自己失效 —— 不在这里查，停用要等
        # Cookie 到寿（最长 12 小时）才生效。判定同样必须与 `/login` 共用本方法，
        # 否则又是上面那个重定向死循环。
        try:
            import admin_api as _admin_api

            if not _admin_api.account_active(sub):
                return ""
        except Exception:  # noqa: BLE001
            pass
        return sub

    def _guard(self, path: str) -> bool:
        """返回 True 表示已放行；False 表示已经发出 302 跳登录页，调用方应直接 return。"""
        if not wv.AUTH_ON or path in PUBLIC_PATHS or path.startswith("/assets/"):
            return True
        sub = self._session_user()
        if sub:
            # 给 _auth_bar() 用：状态条要显示真实用户名，不能写死。
            wv._CURRENT_USER.sub = sub
            return True
        target = "/login"
        if self.path not in ("/", ""):
            target += "?next=" + urllib.parse.quote(self.path)
        self._redirect(target)
        return False

    def _serve_asset(self, name: str) -> None:
        """`/assets/<文件名>` —— 只服务 web_assets 顶层的白名单后缀文件。

        为什么要白名单后缀 + 拒绝任何分隔符：`unquote` 过的路径里 `%2e%2e%2f`
        会变成 `../`，`Path(base)/"../x"` 直接逃出目录。这里先按「名字里不许出现
        任何路径分隔符、不许以点开头」判死，再 `resolve()` 复核一次仍在 base 内 ——
        两道都过才发文件。这类目录穿越在公开仓库里是必被扫的第一个洞。
        """
        if (not name or name.startswith(".") or "/" in name or "\\" in name
                or ".." in name):
            self._html(page("<h1>404</h1>"), 404)
            return
        ctype = ASSET_TYPES.get(Path(name).suffix.lower())
        if not ctype:
            self._html(page("<h1>404</h1>"), 404)
            return
        try:
            base = ASSET_DIR.resolve()
            target = (base / name).resolve()
        except OSError:
            self._html(page("<h1>404</h1>"), 404)
            return
        if target != base and base not in target.parents:
            self._html(page("<h1>404</h1>"), 404)
            return
        if not target.is_file():
            self._html(page("<h1>404</h1>"), 404)
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._html(page("<h1>500</h1>"), 500)
            return
        # 短缓存 + ETag 交给浏览器协商：登录页每次都要可用，但没必要每次都重下 28KB
        self._send(body, ctype, cache="public, max-age=600")

    def do_GET(self) -> None:  # noqa: N802
        path, qs = self._route()
        try:
            if path == "/healthz":
                # 探活用：不泄露任何库内容。
                # write 必须实时求值（2026-10-23 实测踩到）：sync_push 的窗口期
                # 「起服务时库还没挂触发器」会让启动时算好的 WRITE_OK 永远停留在 true，
                # 推送完也不更新。write_path_status 只做一次 import 缓存命中 + 一次
                # sqlite_master 读，每请求现算不心疼。
                self._json({"ok": True, "auth": wv.AUTH_ON,
                            "write": bool(write_path_status()[0])})
                return
            if path == "/login":
                # 已登录就别再给看登录页。注意这里必须用 _session_user()，
                # 不能用「签名有效」—— 否则删号后与 _guard 判定不一致会死循环。
                if self._session_user():
                    self._redirect(qs.get("next") or "/")
                else:
                    self._html(web_auth.login_page(next_url=qs.get("next", "/")))
                return
            if path == "/logout":
                self._redirect("/login", clear_cookie=True)
                return
            if path.startswith("/assets/"):
                # 登录页在未登录状态下就要加载土星脚本，所以这里在 _guard 之前。
                self._serve_asset(path[len("/assets/"):])
                return
            if not self._guard(path):
                return
            if path == "/":
                self._html(home())
            elif path == "/list":
                self._html(listing(qs.get("dir", ""), qs.get("type", ""), qs.get("grade", "")))
            elif path.startswith("/note/"):
                self._html(detail(path[len("/note/"):]))
            elif path.startswith("/edit/"):
                self._html(edit_form(path[len("/edit/"):]))
            elif path == "/search":
                self._html(search(qs.get("q", "")))
            elif path == "/graph":
                self._html(graph(qs.get("dir", ""), qs.get("type", "")))
            elif path == "/api/graph":
                self._json(graph_data(qs.get("dir", ""), qs.get("type", "")))
            elif path == "/rebuild":
                if _is_replica():
                    # 只读镜像上 DELETE links 会撞触发器，与其 500 不如直说
                    self._html(page('<h1>只读镜像</h1><p class="muted">这台服务器上是库的只读镜像，'
                                    '派生表随 sync_push 推送自然一致，不需要也不允许在这里重算。</p>'))
                    return
                # 2026-10-20：库模式下 `build_index()` 是 build_index_from_md 的别名，
                # 它会 DROP notes 再从 md 重建 —— 一个页面按钮不该有这种能力。
                # 这里只重算派生表（links / FTS），notes 一行不动。
                info = rebuild_derived()
                self._html(home(f'派生表已重算：notes {info["notes"]} 行未动 / links {info["links"]} 条 / '
                                f'{info["elapsed_ms"]} ms（库为源，未读任何 md 文件）'))
            elif path == "/api/notes":
                self._json(json_notes(qs.get("q", "")))
            elif path.startswith("/api/note/"):
                title = path[len("/api/note/"):]
                conn = connect()
                try:
                    row = conn.execute("SELECT * FROM notes WHERE title=?", (title,)).fetchone()
                finally:
                    conn.close()
                self._json(dict(row) if row else {"error": "not found"})
            else:
                self._html(page('<h1>404</h1><p><a href="/">返回首页</a></p>'), 404)
        except Exception as e:  # 页面崩了也要给出可读原因
            self._html(page(f'<h1>500</h1><pre>{_esc(str(e))}</pre>'), 500)

    def do_POST(self) -> None:  # noqa: N802
        path, _qs = self._route()
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            if path == "/login":
                self._handle_login(form)
                return
            if not self._guard(path):
                return
            if path == "/save":
                title = form.get("title", "")
                msg = save_note(title, form.get("body", ""))
                self._html(detail(title, msg))
            elif path == "/set_grade":
                # 单篇改级（阶段 6.1）：只动 notes.grade，一行 UPDATE，md 投影不受影响
                title = form.get("title", "")
                g = (form.get("grade", "") or "").upper()
                if wv.AUTH_ON and not self._session_user():
                    self._html(page('<h1>403</h1><p>会话已失效，请重新登录。</p>'), 403)
                elif not _can_set_grade():
                    self._html(page('<h1>403</h1><p>只有管理员可以改级。</p>'), 403)
                elif g not in GRADE_RANK_SET:
                    self._html(page(f'<h1>400</h1><p>未知等级 {_esc(g)}。</p>'), 400)
                else:
                    conn = connect()
                    try:
                        cur = conn.execute("UPDATE notes SET grade=? WHERE title=?",
                                           (g, title))
                        conn.commit()
                        changed = cur.rowcount
                    finally:
                        conn.close()
                    msg = (f'已把「{title}」改为 {GRADE_LABELS.get(g, g)} {g}。'
                           if changed else f'没找到「{title}」，等级未改。')
                    self._html(detail(title, msg))
            else:
                self._html(page('<h1>404</h1>'), 404)
        except Exception as e:
            self._html(page(f'<h1>500</h1><pre>{_esc(str(e))}</pre>'), 500)

    def log_message(self, *args) -> None:
        return


def write_path_status() -> tuple[bool, str]:
    """写路径自检：探一次「保存」这条链路能不能真的走通。

    为什么需要它（2026-10-23 阶段 13 实测的静默故障）：
      保存链路是 memory_store._write_memory → vault_db.save → 本文件
      upsert_row → _refresh_fts_row → `import memory_tools`，而 memory_tools
      顶部就 `from pydantic import Field`。
      服务器上若用系统 /usr/bin/python3（没有 pydantic）起本服务，首页照样
      列出 130 条、每条都能打开 —— **读取全对，保存每次 500**。
      只看首页的人会以为部署成功了。所以启动时探一次，把它变成一条显眼的告警。

    判定方式只 import 一次，不写任何数据。
    2026-10-23（阶段 6.2）：只读镜像上依赖齐备也写不进去（_ro_guard_* 触发器闸），
    所以先判副本再判依赖，healthz 的 write 字段不许对镜像报 true。
    2026-10-29（阶段 10）：只读模式的判据要排在**最前** —— 它的判据是环境变量，
    比「触发器在不在」更根本；不先判它，本地库没触发器时这里会误报「OK（保存可用）」，
    而实际写请求会被 _deny_write 拒绝。
    """
    if web_store.readonly_mode():
        return False, "只读副本模式（AI_MEMORY_READONLY=1，写请求会被明确拒绝）"
    if _is_replica():
        return False, "只读镜像库（正本在本地，写请走本地 Web/MCP 后 sync_push 同步）"
    try:
        import memory_tools  # noqa: F401
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - 缺包/路径错都要报出来，不许静默
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    global WRITE_OK
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    # 阶段 13 前置：允许绑定到 0.0.0.0 供跨机访问。默认仍是回环 —— 不写就不暴露。
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址，跨机访问填 0.0.0.0")
    # 阶段 4：默认要求登录。--no-login 显式关闭，只用于本地调试。
    ap.add_argument("--no-login", action="store_true", help="关闭登录校验（仅本地调试）")
    # 2026-10-20：默认**不弹浏览器**。程序化启动（脚本、hook、自动化）时
    # 一次 webbrowser.open 会在用户桌面上凭空弹一个窗口，而启动方根本不知道。
    # 要开就显式 --open。
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器（默认不打开）")
    ap.add_argument("--no-open", action="store_true", help="兼容旧参数，等价于默认行为")
    ap.add_argument("--rebuild-only", action="store_true")
    args = ap.parse_args()

    if args.no_login:
        wv.AUTH_ON = False

    # 安全默认：要开门就得先有锁。没有账号时拒绝启动，并给出可直接复制的创建命令。
    # 与阶段 2「--require-token 装不上锁就不许跑」是同一个原则：不要静默降级成无认证。
    if wv.AUTH_ON and not web_auth.has_users():
        print("[ai-memory Web] 还没有任何登录账号，拒绝在无锁状态启动。", file=sys.stderr)
        print("  创建账号：", file=sys.stderr)
        print(f'    "{sys.executable}" "{Path(__file__).with_name("web_auth.py")}" '
              f'init --user 你的名字', file=sys.stderr)
        print("  仅本地调试可加 --no-login 跳过（不要在有网络可达的地址上这么做）。",
              file=sys.stderr)
        return 3

    # 写路径自检：读得到不等于写得进（见 write_path_status 的注释）。
    # 只告警、不拒绝启动 —— 只读浏览也是有价值的降级，不该整个宕掉；
    # 但必须在启动日志里，并且能从 /healthz 的 write 字段看到。
    ok, detail = write_path_status()
    WRITE_OK = ok
    if ok:
        print("写路径自检：OK（保存可用）")
    elif web_store.readonly_mode() or detail.startswith("只读镜像"):
        # 2026-10-29（阶段 10）：只读副本是**部署形态**，不是故障 ——
        # AI_MEMORY_READONLY=1 + index 文件 440 + 服务降权三者一起落实。
        # 这时再喊「你可能用错了 python」会把正常部署说成事故，一天刷几遍日志。
        # 分流成一条平静的状态行，写请求由 web_store._deny_write 当场拒绝。
        print("只读副本模式：保存已禁用（预期）。%s" % detail, file=sys.stderr)
    else:
        print(f"[警告] 写路径不可用：{detail}", file=sys.stderr)
        print("  浏览与搜索正常，但保存会失败。多数情况是用错了 python："
              "本项目写入链路依赖 pydantic（经 memory_tools），"
              "请用装了依赖的解释器启动（例如项目 venv 里的 python）。",
              file=sys.stderr)

    # 2026-10-20：原来是 `build_index()`（= build_index_from_md），启动一次就会
    # DROP notes 再从 md 重建。库是原件之后，这个动作在启动路径上是纯粹的隐患：
    # 每次开个网页都拿快照覆盖一遍原件。改成只重算派生表。
    # 2026-10-23（阶段 6.2）：只读镜像上必须跳过 —— rebuild_derived 会 DELETE links，
    # 撞上 _ro_guard_* 触发器直接 SystemExit，服务变成 3 秒一崩溃的循环
    # （2026-10-23 部署实测踩到：副本上启动即死，restart counter 一路涨）。
    # 2026-10-29（阶段 10）：判据从「是不是副本」扩成「副本 or 只读模式」。
    # 只读模式下 rebuild_derived 会被 web_store._deny_write 当场抛 ReadOnlyReplica，
    # 而这里没有 try —— 启动即崩，表现成 3 秒一重启的循环。
    if _is_replica() or web_store.readonly_mode():
        print("只读副本：跳过派生表重算（正本在本地，跑 sync_push.py 同步后自然一致）")
    else:
        info = rebuild_derived()
        print(f"派生表已重算：notes {info['notes']} 行未动 / links {info['links']} 条，"
              f"耗时 {info['elapsed_ms']} ms -> {INDEX_PATH}")
    if args.rebuild_only:
        return 0

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"L3 Web 已启动：{url}" + ("" if not wv.AUTH_ON else "（需登录）"))
    print(f"绑定：{args.host}:{args.port} ｜ 原件：{INDEX_PATH}")
    print("md 是库的单向快照，不参与读写。")
    # 修了既有 bug：`--no-open` 是 store_true 且默认 False，原来的 `if not args.no_open`
    # 会让「什么都不传」也弹浏览器 —— 与上面注释写的默认行为完全相反。
    if args.open and not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    with ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
