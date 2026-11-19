#!/usr/bin/env python3
"""memory_web 的视图层（2026-10-23 自 memory_web.py 按行切出，函数体未改）。

职责：Markdown 渲染、页面模板、各页面函数、等级徽章。
AUTH_ON 与 _CURRENT_USER 的**单源**在这里：HTTP 层经 `wv.AUTH_ON` /
`wv._CURRENT_USER` 读写，保证 --no-login 与会话身份在两层间一致。
"""

from __future__ import annotations

import html
import json
import re
import threading
import urllib.parse

import web_auth  # noqa: E402 - 状态条要显示登录用户
import note_grade  # noqa: E402 - 等级常量
import acl  # noqa: E402 - 可见等级判定

from web_store import connect, stats, ensure_index, find_path  # noqa: E402
from web_store import strip_md  # noqa: E402,F401 - 再导出：清洗口径单源在存储层

# 阶段 4：是否要求登录。单源在此；memory_web.main 的 --no-login 改这里。
AUTH_ON = True

# 当前请求的登录用户名（thread-local）。ThreadingHTTPServer 一请求一线程，
# Handler 在里面写、_auth_bar()/detail() 在里面读；用模块级全局变量才会串号。
_CURRENT_USER = threading.local()

# ────────────────────────── Markdown 渲染 ──────────────────────────


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def inline(raw: str) -> str:
    """行内标记。先整体转义，再用占位符保护行内代码，避免代码里的标记被二次解析。"""
    s = _esc(raw)
    stash: list[str] = []

    def _keep(m: re.Match) -> str:
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    s = re.sub(r"`([^`]+)`", _keep, s)
    # 双链 → 站内跳转（优先于普通链接，避免 [[x]] 被 [] 规则吃掉）
    s = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda m: '<a class="wl" href="/note/{}">{}</a>'.format(
            urllib.parse.quote(m.group(1).strip()), _esc(m.group(2) or m.group(1))
        ),
        s,
    )
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)

    def _restore(m: re.Match) -> str:
        return f"<code>{_esc(stash[int(m.group(1))])}</code>"

    return re.sub(r"\x00(\d+)\x00", _restore, s)


_BLOCK_STOP = re.compile(r"^(#{1,6}\s|\s*[-*+]\s|\s*\d+\.\s|>|```|\s*\|)")


def render_md(text: str) -> str:
    """极简 Markdown → HTML。只覆盖记忆库实际用到的语法。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + _esc("\n".join(buf)) + "</code></pre>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue

        # 表格：当前行含 | 且下一行是分隔行
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in header)
            tb = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>")
            continue

        if line.strip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue

        if re.match(r"^\s*[-*+]\s+", line):
            buf = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                buf.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ul>")
            continue

        if re.match(r"^\s*\d+\.\s+", line):
            buf = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                buf.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ol>")
            continue

        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _BLOCK_STOP.match(lines[i]):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + inline(" ".join(x.strip() for x in buf)) + "</p>")
    return "\n".join(out)


# strip_md（剥标记取纯文本）已迁到 web_store.py（2026-10-23 分层）：真正调用它的是
# 存储层的 _upsert（写 notes 行），留在视图层会变成「看得到定义、写库调不到」。
# 本模块顶部已 `from web_store import strip_md` 再导出，老引用无需改。
# ────────────────────────── 页面 ──────────────────────────

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#14161a;color:#e6e8eb;font-size:14px;
     font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.7}
a{color:#6f9bff;text-decoration:none} a:hover{text-decoration:underline}
.top{position:sticky;top:0;background:#181b21;border-bottom:1px solid #262b33;
     padding:12px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;z-index:9}
.top .brand{font-weight:600;color:#e6e8eb}
.top form{display:flex;gap:8px;flex:1;min-width:220px}
.top input[type=text]{flex:1;background:#1f232a;border:1px solid #2e343d;color:#e6e8eb;
     padding:7px 11px;border-radius:6px;font-size:13px;font-family:inherit}
.btn{background:#232830;border:1px solid #303643;color:#dfe3e8;padding:7px 13px;border-radius:6px;
     font-size:12.5px;cursor:pointer;text-decoration:none;display:inline-block;font-family:inherit}
.btn:hover{background:#2b313b;text-decoration:none}
.wrap{max-width:1080px;margin:0 auto;padding:22px 20px 70px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px;margin-bottom:22px}
.card{background:#1c1f25;border:1px solid #282c34;border-radius:8px;padding:13px 15px}
.card .k{color:#8b9099;font-size:12px;margin-bottom:5px}
.card .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.card .d{color:#6f747c;font-size:11px;margin-top:3px}
h1{font-size:21px;margin:0 0 6px} h2{font-size:16px;margin:26px 0 10px;color:#c9ccd1}
h3{font-size:14px;margin:20px 0 8px;color:#c9ccd1}
.muted{color:#7c828b;font-size:12.5px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:9px 0 18px}
.chip{background:#1f232a;border:1px solid #2e343d;border-radius:14px;padding:4px 11px;font-size:12px;color:#a8adb5}
.chip.on{background:#2b3a5e;border-color:#3f5794;color:#cfe0ff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid #242830;vertical-align:top}
th{color:#8b9099;font-weight:500}
td.n{text-align:right;font-variant-numeric:tabular-nums;color:#8b9099}
.doc{background:#1c1f25;border:1px solid #282c34;border-radius:9px;padding:22px 26px}
.doc h1{font-size:22px;border-bottom:1px solid #2b3038;padding-bottom:9px}
.doc h2{font-size:17px;border-left:3px solid #4a6cf7;padding-left:10px;margin-top:26px}
.doc h3{font-size:15px}
.doc pre{background:#12141a;border:1px solid #262b33;border-radius:6px;padding:12px 14px;overflow:auto}
.doc code{background:#22262d;padding:1px 5px;border-radius:3px;font-size:12.5px;
     font-family:Consolas,Monaco,monospace}
.doc pre code{background:none;padding:0}
.doc blockquote{border-left:3px solid #3a4050;margin:10px 0;padding:2px 0 2px 14px;color:#a8adb5}
.doc table{margin:12px 0}
.doc hr{border:none;border-top:1px solid #2b3038;margin:20px 0}
.doc ul,.doc ol{padding-left:22px}
a.wl{color:#7fd4a0;border-bottom:1px dashed #3f7a58}
.meta{color:#7c828b;font-size:12px;margin:10px 0 0;display:flex;gap:14px;flex-wrap:wrap}
textarea{width:100%;min-height:60vh;background:#12141a;border:1px solid #2b3038;color:#e6e8eb;
     border-radius:7px;padding:14px;font-family:Consolas,Monaco,"Microsoft YaHei",monospace;
     font-size:13px;line-height:1.7}
.flash{background:#1d2b22;border:1px solid #2f6b45;color:#a7e0bd;padding:9px 14px;
     border-radius:6px;margin-bottom:16px;font-size:13px}
.warn{background:#2b2419;border:1px solid #6b552d;color:#e8cf9a;padding:9px 14px;
     border-radius:6px;margin-bottom:16px;font-size:13px}
.foot{margin-top:34px;padding-top:14px;border-top:1px solid #232830;color:#6f747c;font-size:12px}
.gwrap{position:relative;height:min(74vh,780px);background:#14161b;border:1px solid #282c34;
     border-radius:9px;overflow:hidden;margin-top:6px}
.gwrap canvas{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:2px 0 10px;font-size:12px;color:#9aa0aa}
.legend .lg{display:inline-flex;align-items:center;gap:6px}
.legend .lg i{width:9px;height:9px;border-radius:50%;display:inline-block}
#ginfo{font-family:Consolas,Monaco,monospace;font-size:11.5px;margin-top:8px;min-height:16px}
/* 阶段 4：右下角登录状态条。fixed 定位是为了不打断既有 .top 的 flex 结构 ——
   改 NAV 会牵动所有页面，加一条浮层是成本最低的做法。 */
.who{position:fixed;right:14px;bottom:12px;z-index:9;display:flex;gap:9px;align-items:center;
     background:rgba(24,27,33,.92);border:1px solid #262b33;border-radius:20px;
     padding:5px 10px;font-size:12px;color:#7c848f;backdrop-filter:blur(6px)}
.who .btn{padding:3px 9px;font-size:11.5px}
"""

NAV = """<div class="top">
<span class="brand">ai-memory</span>
<form action="/search" method="get"><input type="text" name="q" placeholder="搜索标题 / 正文 / 标签"><button class="btn">搜索</button></form>
<a class="btn" href="/">首页</a>
<a class="btn" href="/list">全部</a>
<a class="btn" href="/graph">图谱</a>
<a class="btn" href="/rebuild">重建索引</a>
<a class="btn" href="http://127.0.0.1:8765/">看板</a>
</div>"""


def _auth_bar() -> str:
    """登录状态条。未开启鉴权时返回空串 —— 本地调试不该留下假的「已登录」。

    2026-10-23 修：原来这里写死「已登录：本机」，因为当时只有一台机器一个用户。
    服务器上线、账号可能不止一个之后，这句话就是错的 —— 状态条必须显示**当前会话
    的用户名**，否则「谁在用」这件事在界面上直接失真。

    用户名怎么传进来：page() 被 8 个路由函数调用，逐个加参数要改一大片；而
    ThreadingHTTPServer 是「一请求一线程」，所以用 thread-local 在 Handler._guard
    里写入、在这里读出，既不用改签名也不会串号（全局变量才会串）。
    """
    if not AUTH_ON:
        return ""
    who = getattr(_CURRENT_USER, "sub", "")
    label = f"已登录：{who}" if who else "已登录"
    return (f'<div class="who"><span>{_esc(label)}</span>'
            '<a class="btn" href="/logout">登出</a></div>')


def page(body: str, title: str = "ai-memory Web", script: str = "") -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(title)}</title><style>{CSS}</style></head>
<body>{NAV}{_auth_bar()}<div class="wrap">{body}</div>{script}</body></html>"""


def footer(s: dict) -> str:
    return (f'<div class="foot">SQLite 索引：{s["notes"]} 条笔记 / {s["links"]} 条双链 ｜ '
            f'构建于 {s["built_at"]} ｜ 索引文件 <code>{_esc(s["index_path"])}</code>（{s["index_size_kb"]} KB）<br>'
            '库是唯一事实源；md 是它的单向快照（脚本/snapshot_md.py 生成），不参与读写。</div>')


def home(flash: str = "") -> str:
    s = stats()
    conn = connect()
    try:
        recent = [dict(r) for r in conn.execute(
            "SELECT title, dir, type, summary, updated FROM notes ORDER BY updated DESC LIMIT 15")]
        hot = [dict(r) for r in conn.execute(
            "SELECT title, dir, access_count FROM notes ORDER BY access_count DESC LIMIT 10")]
    finally:
        conn.close()
    cards = "".join([
        f'<div class="card"><div class="k">笔记</div><div class="v">{s["notes"]}</div></div>',
        f'<div class="card"><div class="k">双链</div><div class="v">{s["links"]}</div></div>',
        f'<div class="card"><div class="k">索引构建</div><div class="v" style="font-size:14px">'
        f'{_esc(s["built_at"][11:] or "-")}</div><div class="d">{_esc(s["built_at"][:10])}</div></div>',
        f'<div class="card"><div class="k">索引体积</div><div class="v">{s["index_size_kb"]} KB</div></div>',
    ])
    chips = "".join(
        f'<a class="chip" href="/list?dir={urllib.parse.quote(d["dir"])}">{_esc(d["dir"])} {d["n"]}</a>'
        for d in s["dirs"])
    chips_t = "".join(
        f'<a class="chip" href="/list?type={urllib.parse.quote(t["type"])}">{_esc(t["type"])} {t["n"]}</a>'
        for t in s["types"])
    rows = "".join(
        f'<tr><td><a href="/note/{urllib.parse.quote(r["title"])}">{_esc(r["title"])}</a></td>'
        f'<td class="muted">{_esc(r["dir"])}</td><td class="muted">{_esc(r["type"])}</td>'
        f'<td class="muted">{_esc((r["summary"] or "")[:60])}</td>'
        f'<td class="n">{_esc(str(r["updated"])[:10])}</td></tr>' for r in recent)
    hot_rows = "".join(
        f'<tr><td><a href="/note/{urllib.parse.quote(r["title"])}">{_esc(r["title"])}</a></td>'
        f'<td class="n">{r["access_count"]}</td></tr>' for r in hot)
    return page(
        (f'<div class="flash">{_esc(flash)}</div>' if flash else "")
        + f'<h1>记忆库</h1><div class="muted">本地优先 · 库为原件（index.sqlite3） · md 为快照 · 全部页面直读库</div>'
        + f'<div class="cards">{cards}</div>'
        + '<h2>按目录</h2><div class="chips">' + chips + '</div>'
        + '<h2>按类型</h2><div class="chips">' + chips_t + '</div>'
        + '<h2>最近更新</h2><table><thead><tr><th>标题</th><th>目录</th><th>类型</th>'
          '<th>摘要</th><th>更新</th></tr></thead><tbody>' + rows + '</tbody></table>'
        + '<h2>访问最多</h2><table><thead><tr><th>标题</th><th>读取</th></tr></thead><tbody>'
        + hot_rows + '</tbody></table>'
        + footer(s))


# ── 阶段 6：等级（重要性）展示与改级 ─────────────────────────────────────────
# 等级真源在 notes.grade 列（库为源）；这里的常量只是展示口径。
GRADE_LABELS = {"S": "机密", "A": "重要", "B": "普通", "C": "草稿"}
GRADE_DESC = {
    "S": "机密/危险（明文凭据、令牌、私钥）",
    "A": "重要（规则、决策、索引、项目状态、画像）",
    "B": "普通（项目进展、知识、日常）",
    "C": "草稿（半成品）",
}
GRADE_RANK_SET = set(note_grade.GRADE_RANK)  # {"S","A","B","C"}
# 可改级的管理员（与阶段 5 管理员账号一致）。auth 关闭（--no-login 本机模式）视为可改。
GRADE_ADMINS = {"鱼片"}


def grade_badge(g: str) -> str:
    """等级徽章：icon + 中文词（机密/重要/普通/草稿），**扁平纯色**版。

    2026-10-23 主人三次收敛后的最终口径：
    1. 不显示 S/A/B/C 字母，直接读中文（几何字形非 emoji：◆危险 / ▲注意 / ●平常 / ◇未定稿）；
    2. 与设置里的「模糊 / 透明度」**1:1** 联动 —— 滑块 100% 就是 1 倍基准，不放大；
    3. **扁平化 + 低对比**：没有渐变、没有内高光、没有外柔影、没有文字阴影，
       就是一块纯色 + 一条同色细边。装饰一律不加回去。

    为什么删渐变（踩过的）：17px 高的小徽章上渐变根本看不出层次，只增加视觉重量；
    而且 background 写 linear-gradient 后，`getComputedStyle` 拿到的是 image 而不是
    颜色，验收时得多绕一层。纯色 rgba 既能一眼读懂，也方便按同一系数缩放。

    透明度实现：本页没有设置面板，`--frost-opacity` 缺失时回落 1（保持基准观感）；
    底色 / 边框 / 文字三处同乘同一系数，避免「底色淡了但亮边还在」的假象。
    """
    k = "calc({0} * var(--frost-opacity,1))"
    kt = "calc(.9 * var(--frost-opacity,1) + .1)"  # 文字保底 10%，避免完全看不见
    _b = ("color:rgba(182,189,198," + kt + ");"
          "border:1px solid rgba(170,178,190," + k.format(".22") + ");"
          "background:rgba(170,178,190," + k.format(".10") + ")")
    style = {
        "S": "color:rgba(233,205,201," + kt + ");"
             "border:1px solid rgba(214,92,100," + k.format(".30") + ");"
             "background:rgba(214,92,100," + k.format(".16") + ")",
        "A": "color:rgba(230,211,180," + kt + ");"
             "border:1px solid rgba(212,165,96," + k.format(".28") + ");"
             "background:rgba(212,165,96," + k.format(".14") + ")",
        "B": _b,
        "C": "color:rgba(198,217,234," + kt + ");"
             "border:1px solid rgba(111,165,214," + k.format(".26") + ");"
             "background:rgba(111,165,214," + k.format(".12") + ")",
    }.get(g, _b)
    word = GRADE_LABELS.get(g, g or "?")
    icon = {"S": "◆", "A": "▲", "B": "●", "C": "◇"}.get(g, "·")
    return (f'<span style="display:inline-block;padding:1px 8px;border-radius:9px;'
            f'font-size:12px;font-weight:600;line-height:18px;letter-spacing:.3px;'
            f'backdrop-filter:var(--panel-blur,blur(10px) saturate(150%));'
            f'-webkit-backdrop-filter:var(--panel-blur,blur(10px) saturate(150%));'
            f'{style}">'
            f'<span style="margin-right:4px;font-size:10px">{icon}</span>{_esc(word)}</span>')


def _is_replica() -> bool:
    """当前库是不是服务器只读镜像。判据是机械的：镜像库带 9 个 _ro_guard_*
    写入触发器（阶段 17 加的闸），本地正本没有。不改配置、不试写、不发网请求。

    为什么不缓存（2026-10-23 部署实测踩到）：sync_push 的时序是「停服务 → 覆盖库
    → 起服务（此刻库是本地正本副本，**还没挂触发器**）→ 挂触发器」。起服务那一瞬
    首查会得到 False 并缓存，之后即使触发器挂上，healthz 也一直谎报 write:true。
    查询本身只是一次 sqlite_master 读，不值得拿正确性换。
    """
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '_ro_guard_%'").fetchone()[0]
    finally:
        conn.close()
    return n > 0


def _can_set_grade() -> bool:
    """当前会话是否允许改级：只读镜像上永远不行；登录模式看账号在不在管理员名单。"""
    if _is_replica():
        return False
    if not AUTH_ON:
        return True
    return getattr(_CURRENT_USER, "sub", "") in GRADE_ADMINS


def listing(dir_: str = "", type_: str = "", grade_: str = "") -> str:
    s = stats()
    conn = connect()
    try:
        sql = "SELECT title, dir, type, summary, updated, verified, grade FROM notes WHERE 1=1"
        args: list = []
        if dir_:
            sql += " AND dir=?"
            args.append(dir_)
        if type_:
            sql += " AND type=?"
            args.append(type_)
        if grade_:
            sql += " AND grade=?"
            args.append(grade_)
        sql += " ORDER BY dir, title"
        rows_data = [dict(r) for r in conn.execute(sql, args)]
        # 等级筛选 chips 的计数：只按目录/类型过滤，数各档全量
        cnt_sql = "SELECT grade, COUNT(*) FROM notes WHERE 1=1"
        cnt_args: list = []
        if dir_:
            cnt_sql += " AND dir=?"
            cnt_args.append(dir_)
        if type_:
            cnt_sql += " AND type=?"
            cnt_args.append(type_)
        cnt_sql += " GROUP BY grade"
        counts = dict(conn.execute(cnt_sql, cnt_args).fetchall())
    finally:
        conn.close()
    rows = "".join(
        f'<tr><td><a href="/note/{urllib.parse.quote(r["title"])}">{_esc(r["title"])}</a>'
        f'{" <span class=\"muted\">已确认</span>" if r["verified"] else ""}</td>'
        f'<td class="muted">{_esc(r["dir"])}</td><td class="muted">{_esc(r["type"])}</td>'
        f'<td>{grade_badge(r["grade"])}</td>'
        f'<td class="muted">{_esc((r["summary"] or "")[:70])}</td>'
        f'<td class="n">{_esc(str(r["updated"])[:10])}</td></tr>' for r in rows_data)
    crit = "（".join(p for p in (
        f'目录 {dir_}' if dir_ else "",
        f'类型 {type_}' if type_ else "",
        f'等级 {GRADE_LABELS.get(grade_, grade_)}' if grade_ else "") if p)
    crit = f'（{_esc(crit)}）' if crit else ""
    # 等级筛选 chips：保留现有目录/类型条件，点同一个 = 取消该档筛选
    keep = "&".join(x for x in (
        f"dir={urllib.parse.quote(dir_)}" if dir_ else "",
        f"type={urllib.parse.quote(type_)}" if type_ else "") if x)
    qs_g = (keep + "&") if keep else ""
    chips_g = (f'<a class="chip" href="/list?{qs_g}">全部 {sum(counts.values())}</a>'
               + "".join(
                   f'<a class="chip" href="/list?{qs_g}grade={g}">{GRADE_LABELS[g]} {g} {counts.get(g, 0)}</a>'
                   for g in ("S", "A", "B", "C")))
    return page(
        f'<h1>全部笔记{crit}</h1><div class="muted">共 {len(rows_data)} 条</div>'
        f'<h2>按等级</h2><div class="chips">{chips_g}</div>'
        f'<table><thead><tr><th>标题</th><th>目录</th><th>类型</th><th>等级</th><th>摘要</th><th>更新</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>' + footer(s))


def detail(title: str, flash: str = "") -> str:
    s = stats()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM notes WHERE title=?", (title,)).fetchone()
        if row is None:
            return page(f'<h1>未找到</h1><p class="muted">索引里没有「{_esc(title)}」。'
                        f'<a href="/rebuild">重建索引</a> 试试。</p>' + footer(s))
        r = dict(row)
        backlinks = [dict(x) for x in conn.execute(
            "SELECT DISTINCT src FROM links WHERE dst=? ORDER BY src", (title,))]
        outlinks = [dict(x) for x in conn.execute(
            "SELECT DISTINCT n.title, n.dir FROM links l LEFT JOIN notes n ON n.title=l.dst "
            "WHERE l.src=? ORDER BY l.dst", (title,))]
        known = {x["title"] for x in conn.execute("SELECT title FROM notes")}
    finally:
        conn.close()
    bl = "".join(f'<li><a href="/note/{urllib.parse.quote(b["src"])}">{_esc(b["src"])}</a></li>'
                 for b in backlinks) or '<li class="muted">无</li>'
    ol = "".join(
        f'<li><a href="/note/{urllib.parse.quote(o["title"])}">{_esc(o["title"])}</a></li>'
        if o["title"] in known else
        f'<li><span class="muted">{_esc(o["title"] or "")}（未建索引）</span></li>'
        for o in outlinks) or '<li class="muted">无</li>'
    meta_bits = [
        f'目录 {_esc(r["dir"])}', f'类型 {_esc(r["type"])}', f'scope {_esc(r["scope"])}',
        f'tier {_esc(r["tier"])}',
        f'等级 {grade_badge(r["grade"])}',
        ('已确认' if r["verified"] else '未验证'),
        f'v{r["version"]}', f'读取 {r["access_count"]}',
        f'更新 {_esc(str(r["updated"])[:10])}',
    ]
    # 管理员改级表单（阶段 6.1 追加需求）：改库内 grade 列，md 是投影不受影响
    if _can_set_grade():
        cur = r["grade"] if r["grade"] in GRADE_RANK_SET else ""
        opts = "".join(
            f'<option value="{g}"{" selected" if g == cur else ""}>'
            f'{GRADE_LABELS[g]} {g} — {GRADE_DESC[g]}</option>'
            for g in ("S", "A", "B", "C"))
        grade_form = (
            f'<form method="post" action="/set_grade" style="display:inline-flex;gap:6px;'
            f'align-items:center;vertical-align:middle">'
            f'<input type="hidden" name="title" value="{_esc(title)}">'
            f'<select name="grade" style="font-size:12px">{opts}</select>'
            f'<button type="submit" style="font-size:12px">改级</button></form>')
    else:
        grade_form = ""
    # 正文自带 H1 时不再重复渲染页面标题
    heading = "" if re.match(r"^\s*#\s", r["body_md"]) else f'<h1>{_esc(r["title"])}</h1>'
    return page(
        (f'<div class="flash">{_esc(flash)}</div>' if flash else "")
        + heading
        + f'<div class="meta">' + " ｜ ".join(meta_bits) + f'</div>'
        + f'<div class="meta">库内路径：<code>{_esc(r["rel_path"])}</code>'
          f' ｜ <a href="/edit/{urllib.parse.quote(title)}">编辑</a>{grade_form}</div>'
        + f'<div class="doc" style="margin-top:16px">{render_md(r["body_md"])}</div>'
        + f'<h2>反向链接（{len(backlinks)}）</h2><ul>{bl}</ul>'
        + f'<h2>本文指向（{len(outlinks)}）</h2><ul>{ol}</ul>'
        + footer(s))


def search(q: str) -> str:
    s = stats()
    conn = connect()
    try:
        like = f"%{q}%"
        rows_data = [dict(r) for r in conn.execute(
            "SELECT title, dir, type, summary, updated, grade, "
            "  (CASE WHEN title LIKE ? THEN 0 ELSE 1 END) rank "
            "FROM notes WHERE title LIKE ? OR body_md LIKE ? OR tags LIKE ? OR summary LIKE ? "
            "ORDER BY rank, updated DESC LIMIT 60",
            (like, like, like, like, like))]
    finally:
        conn.close()
    rows = "".join(
        f'<tr><td><a href="/note/{urllib.parse.quote(r["title"])}">{_esc(r["title"])}</a></td>'
        f'<td class="muted">{_esc(r["dir"])}</td><td class="muted">{_esc(r["type"])}</td>'
        f'<td>{grade_badge(r["grade"])}</td>'
        f'<td class="muted">{_esc((r["summary"] or "")[:70])}</td></tr>' for r in rows_data)
    if not rows:
        rows = '<tr><td colspan="5" class="muted">无结果。可试试更短的关键词，或去 <a href="/rebuild">重建索引</a>。</td></tr>'
    return page(
        f'<h1>搜索：{_esc(q)}</h1><div class="muted">命中 {len(rows_data)} 条（标题命中优先）</div>'
        f'<table><thead><tr><th>标题</th><th>目录</th><th>类型</th><th>等级</th><th>摘要</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>' + footer(s))


def edit_form(title: str) -> str:
    s = stats()
    conn = connect()
    try:
        row = conn.execute("SELECT body_md, rel_path FROM notes WHERE title=?", (title,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return page(f'<h1>未找到</h1>' + footer(s))
    return page(
        f'<h1>编辑：{_esc(title)}</h1>'
        f'<div class="warn">保存只写 <code>notes</code> 表（库内路径 <code>{_esc(row["rel_path"])}</code>），'
        f'并递增 version。frontmatter 由 server 维护，这里只编辑正文。</div>'
        f'<form method="post" action="/save">'
        f'<input type="hidden" name="title" value="{_esc(title)}">'
        f'<textarea name="body">{_esc(row["body_md"])}</textarea>'
        f'<div style="margin-top:12px;display:flex;gap:10px">'
        f'<button class="btn">保存（写库）</button>'
        f'<a class="btn" href="/note/{urllib.parse.quote(title)}">取消</a></div></form>'
        + footer(s))


def json_notes(q: str = "") -> dict:
    conn = connect()
    try:
        if q:
            rows = conn.execute(
                "SELECT title, dir, type, scope, tier, source, verified, tags, summary, updated, rel_path "
                "FROM notes WHERE title LIKE ? OR body_md LIKE ? ORDER BY updated DESC LIMIT 100",
                (f"%{q}%", f"%{q}%"))
        else:
            rows = conn.execute(
                "SELECT title, dir, type, scope, tier, source, verified, tags, summary, updated, rel_path "
                "FROM notes ORDER BY dir, title")
        items = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"count": len(items), "items": items}


# ────────────────────────── 知识图谱 ──────────────────────────
#
# 数据完全来自库：节点 = `notes` 一行，边 = `links` 表一条（即正文里的 [[双链]]）。
# 不读 md 文件、不依赖任何前端库 —— 力导向布局是三十行内联 JS，画在 canvas 上。
#
# 之所以不用现成的图谱库（vis / d3 / echarts）：本服务的设计前提是**零第三方依赖**
# （http.server + sqlite3 + 自写 Markdown 渲染）。引一个 500 KB 的 CDN 脚本进来，
# 断网就白屏，而这是个本地服务，断网是常态而不是异常。

# 类型 → 颜色。深色底上取中高亮度，且刻意避开纯红/纯绿 —— 这两个颜色在本库里
# 已经被「涨/跌」语义占住了，混进来会让人误读。
_TYPE_COLORS = {
    "index": "#6f8cff",
    "rule": "#d9a648",
    "project": "#4fb3a5",
    "knowledge": "#a78bfa",
    "log": "#8b93a1",
    "note": "#63a9f0",
    "persona": "#e07ab0",
    "tool": "#5bc0be",
}
_TYPE_FALLBACK = "#8b93a1"


def graph_data(dir_: str = "", type_: str = "") -> dict:
    """节点 / 边 / 统计。筛选在服务端做 —— 前端只负责画。"""
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT title, dir, type, chars, access_count, updated, summary FROM notes")]
        raw_links = [tuple(r) for r in conn.execute("SELECT src, dst FROM links")]
    finally:
        conn.close()

    keep = [r for r in rows
            if (not dir_ or r["dir"] == dir_)
            and (not type_ or (r["type"] or "") == type_)]
    ids = {r["title"] for r in keep}

    # 度数：入 + 出。用它定节点大小，比「字数」更能反映「这篇在结构上的位置」。
    deg: dict[str, int] = {t: 0 for t in ids}
    edges: list[dict] = []
    dangling = 0
    for src, dst in raw_links:
        if src not in ids:
            continue
        if dst not in ids:
            # 指向尚未创建的笔记（实测 12 条）。画成节点会凭空多出一堆空心球，
            # 让人以为库里有这些内容；只报数，不画。
            dangling += 1
            continue
        edges.append({"s": src, "t": dst})
        deg[src] += 1
        deg[dst] += 1

    nodes = [{
        "id": r["title"],
        "type": r["type"] or "note",
        "dir": r["dir"],
        "deg": deg.get(r["title"], 0),
        "chars": int(r["chars"] or 0),
        "updated": str(r["updated"] or "")[:10],
        "summary": (r["summary"] or "")[:120],
    } for r in keep]

    return {
        "nodes": nodes,
        "links": edges,
        "meta": {
            "nodes": len(nodes),
            "links": len(edges),
            "dangling": dangling,
            "isolated": sum(1 for n in nodes if n["deg"] == 0),
            "dirs": sorted({r["dir"] for r in rows}),
            "types": sorted({(r["type"] or "note") for r in rows}),
            "filter": {"dir": dir_, "type": type_},
        },
    }


GRAPH_JS = r"""
const G = __DATA__;
const cv = document.getElementById('gcv');
const ctx = cv.getContext('2d');
const info = document.getElementById('ginfo');
const N = G.nodes, L = G.links;
const idx = new Map(); N.forEach((n,i)=>{ n.i=i; n.x=0; n.y=0; n.vx=0; n.vy=0; idx.set(n.id,n); });
L.forEach(e=>{ e.a=idx.get(e.s); e.b=idx.get(e.t); });
const degMax = Math.max(1, ...N.map(n=>n.deg));
const COLORS = __COLORS__;
const colorOf = n => COLORS[n.type] || '__FALLBACK__';
const rOf = n => 4 + 7*Math.sqrt(n.deg/degMax) + Math.min(3, n.chars/40000);

// 初始摆位：按目录分扇区、半径随度数收缩 —— 让「核心节点在里圈」这个直觉
// 一开场就成立，而不是先撒一圈随机点再看它乱飞二十秒。
const dirs = [...new Set(N.map(n=>n.dir))];
N.forEach(n=>{
  const a = (dirs.indexOf(n.dir)/Math.max(1,dirs.length))*Math.PI*2;
  const rr = 300 - 14*Math.sqrt(n.deg/degMax)*100;
  n.x = Math.cos(a)*rr + (Math.random()-0.5)*60;
  n.y = Math.sin(a)*rr + (Math.random()-0.5)*60;
});

let view = {x:0, y:0, k:1};
let hover=null, dragNode=null, panning=false, last=null, moved=false, settled=false;

function resize(){
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth*dpr; cv.height = cv.clientHeight*dpr;
  ctx.setTransform(dpr,0,0,dpr,0,0);
}
function tick(alpha){
  // 斥力：所有节点两两相斥。119 个点 = 1.4 万次，单帧不到 1 ms，不必上四叉树。
  for (let i=0;i<N.length;i++){
    const a=N[i];
    for (let j=i+1;j<N.length;j++){
      const b=N[j];
      let dx=b.x-a.x, dy=b.y-a.y, d2=dx*dx+dy*dy;
      if (d2<1){ dx=Math.random()-0.5; dy=Math.random()-0.5; d2=1; }
      if (d2>160000) continue;
      // 2600 而不是 1400：中心那几个枢纽（项目状态 deg=168）会把整张图吸成一个球。
      // 斥力加倍后外围节点才散得开，结构（几个星团 + 长尾）才看得出来。
      const f = 2600/d2, d = Math.sqrt(d2);
      a.vx-=dx/d*f; a.vy-=dy/d*f; b.vx+=dx/d*f; b.vy+=dy/d*f;
    }
  }
  // 弹簧：边长随两端度数放大，否则大节点会被一堆小节点拽成一团
  for (const e of L){
    const dx=e.b.x-e.a.x, dy=e.b.y-e.a.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1;
    const goal=55+45*Math.sqrt((e.a.deg+e.b.deg)/degMax);
    const f=(d-goal)*0.006;
    e.a.vx+=dx/d*f; e.a.vy+=dy/d*f; e.b.vx-=dx/d*f; e.b.vy-=dy/d*f;
  }
  for (const n of N){
    n.vx += -n.x*0.0014; n.vy += -n.y*0.0014;   // 向心：防止整图漂走
    if (n===dragNode){ n.vx=0; n.vy=0; continue; }
    n.x += n.vx*alpha; n.y += n.vy*alpha;
    n.vx*=0.82; n.vy*=0.82;
    // 硬夹紧：斥力只跟距离平方成反比，弱连接的节点一旦被弹远就再也拉不回来，
    // 会在画布角落留下一颗孤零零的点，而 fit() 为了让那颗点可见会把整张图缩到
    // 看不清（实测过一次：119 个点挤在右下角 1/6 的区域里）。
    const rr = Math.hypot(n.x, n.y);
    if (rr > 900){ n.x = n.x/rr*900; n.y = n.y/rr*900; n.vx*=0.3; n.vy*=0.3; }
  }
}
// 自适应视野：模拟结束后把整张图缩放到画布里，省得用户先滚两下滚轮找内容。
function fit(){
  if (!N.length) return;
  // 用 92 分位半径而不是 bbox：一两个离群点不该决定全局缩放比。
  const cx = N.reduce((s,n)=>s+n.x,0)/N.length, cy = N.reduce((s,n)=>s+n.y,0)/N.length;
  const ds = N.map(n=>Math.hypot(n.x-cx, n.y-cy)).sort((a,b)=>a-b);
  const R = Math.max(80, ds[Math.min(ds.length-1, Math.floor(ds.length*0.92))] * 1.18);
  const w = Math.max(1, cv.clientWidth-40), h = Math.max(1, cv.clientHeight-40);
  view.k = Math.min(2.2, Math.max(0.25, Math.min(w, h) / (2*R)));
  view.x = -cx; view.y = -cy;
}
function toScreen(n){ return [ (n.x+view.x)*view.k + cv.clientWidth/2, (n.y+view.y)*view.k + cv.clientHeight/2 ]; }
const drawn=[];
function draw(){
  ctx.clearRect(0,0,cv.clientWidth,cv.clientHeight);
  const hi = hover ? new Set([hover.id, ...L.filter(e=>e.a===hover||e.b===hover).flatMap(e=>[e.a.id,e.b.id])]) : null;

  for (const e of L){
    const [x1,y1]=toScreen(e.a), [x2,y2]=toScreen(e.b);
    const on = !hi || (hi.has(e.a.id) && hi.has(e.b.id));
    ctx.strokeStyle = on ? 'rgba(140,160,200,0.30)' : 'rgba(120,130,150,0.06)';
    ctx.lineWidth = on ? 1 : 0.6;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
  }
  for (const n of N){
    const [x,y]=toScreen(n), r=rOf(n)*Math.sqrt(view.k);
    const on = !hi || hi.has(n.id);
    ctx.globalAlpha = on ? 1 : 0.16;
    ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2);
    if (n.type === 'index'){
      // 索引页画成空心环：它们是「全库的目录」，deg 动辄上百，实心画出来会变成
      // 一个吞掉整张图的球，让人误以为那是一篇内容最多的笔记。
      ctx.strokeStyle = colorOf(n); ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = 'rgba(111,140,255,0.14)'; ctx.fill();
    } else {
      ctx.fillStyle = colorOf(n); ctx.fill();
    }
    if (n === hover){ ctx.lineWidth=2; ctx.strokeStyle='#fff'; ctx.stroke(); }
    ctx.globalAlpha = 1;
  }
  // 标签：按度数从大到小排，画之前先查一次碰撞 —— 不查的话中心那几个枢纽的
  // 标签会叠在一起糊成一坨（实测「项目状态 / 记忆索引 / 路由索引」三行重叠）。
  ctx.font = '11px "Microsoft YaHei",system-ui,sans-serif';
  ctx.textAlign='center'; ctx.textBaseline='top';
  drawn.length = 0;
  const order = [...N].sort((a,b)=>b.deg-a.deg);
  // 默认只标度数前 22 的节点：119 个全标必然糊成一片（实测中心区三行文字重叠，
  // 谁也读不出来）。放大到 1.3 倍以上才放开到 deg>=4，交给用户自己决定看细不细。
  const topN = Math.max(8, Math.min(22, Math.round(N.length*0.2)));
  const minDeg = view.k >= 1.3 ? 4 : 1e9;
  for (const n of order){
    const forced = hi && hi.has(n.id);
    const rank = order.indexOf(n);
    if (!forced && rank >= topN && n.deg < minDeg) continue;
    const [x,y]=toScreen(n), r=rOf(n)*Math.sqrt(view.k);
    let t=n.id; if (t.length>14) t=t.slice(0,13)+'…';
    const w = ctx.measureText(t).width, ty = y+r+3;
    const box = [x-w/2, ty, x+w/2, ty+13];
    // 碰撞检查对**所有**标签生效，包括悬停高亮的那批。之前给高亮开了后门，
    // 悬停「记忆索引」（197 条边）时 197 个标签全部强行画出来，整屏糊成一片。
    if (n !== hover && drawn.some(b=>box[0]<b[2]+4 && box[2]>b[0]-4 && box[1]<b[3]+3 && box[3]>b[1]-3)) continue;
    drawn.push(box);
    ctx.fillStyle = (hi && !hi.has(n.id)) ? 'rgba(200,205,215,0.18)' : 'rgba(228,232,240,0.94)';
    ctx.fillText(t, x, ty);
  }
}
function hitTest(px,py){
  for (let i=N.length-1;i>=0;i--){
    const n=N[i], [x,y]=toScreen(n), r=rOf(n)*Math.sqrt(view.k)+4;
    if ((px-x)**2 + (py-y)**2 <= r*r) return n;
  }
  return null;
}
function local(e){ const b=cv.getBoundingClientRect(); return [e.clientX-b.left, e.clientY-b.top]; }

cv.addEventListener('mousemove', e=>{
  const [px,py]=local(e);
  if (dragNode){ dragNode.x=(px-cv.clientWidth/2)/view.k-view.x; dragNode.y=(py-cv.clientHeight/2)/view.k-view.y; moved=true; draw(); return; }
  if (panning && last){ view.x += (px-last[0])/view.k; view.y += (py-last[1])/view.k; last=[px,py]; draw(); return; }
  const n=hitTest(px,py);
  if (n!==hover){
    hover=n; cv.style.cursor = n?'pointer':'grab'; draw();
    info.textContent = n ? `${n.id} ｜ ${n.type} ｜ ${n.dir} ｜ 连边 ${n.deg} ｜ ${n.chars} 字 ｜ 更新 ${n.updated}` : '悬停看节点明细；拖动空白处平移，滚轮缩放，双击复位，点节点进详情。';
  }
});
cv.addEventListener('mousedown', e=>{
  const [px,py]=local(e); const n=hitTest(px,py); moved=false;
  if (n){ dragNode=n; } else { panning=true; last=[px,py]; cv.style.cursor='grabbing'; }
});
window.addEventListener('mouseup', e=>{
  if (dragNode && !moved){ const [px,py]=local(e); const n=hitTest(px,py); if (n) location.href='/note/'+encodeURIComponent(n.id); }
  dragNode=null; panning=false; cv.style.cursor='grab';
});
cv.addEventListener('wheel', e=>{
  e.preventDefault();
  const [px,py]=local(e), k0=view.k, k1=Math.min(4, Math.max(0.25, k0*(e.deltaY<0?1.12:0.89)));
  // 以光标为锚点缩放：先算光标处的世界坐标，缩放后再把它挪回光标下
  const wx=(px-cv.clientWidth/2)/k0-view.x, wy=(py-cv.clientHeight/2)/k0-view.y;
  view.k=k1; view.x=(px-cv.clientWidth/2)/k1-wx; view.y=(py-cv.clientHeight/2)/k1-wy;
  draw();
}, {passive:false});
cv.addEventListener('dblclick', ()=>{ fit(); draw(); });

resize(); fit();
window.addEventListener('resize', ()=>{ resize(); if (settled) fit(); draw(); });
let frame=0;
(function animate(){
  if (frame < 320){ tick(1); frame++; if (frame===320){ settled=true; fit(); } draw(); requestAnimationFrame(animate); }
  else if (dragNode){ tick(0.5); draw(); requestAnimationFrame(animate); }   // 拖节点时才继续算，其余时间静止
})();
"""


def graph(dir_: str = "", type_: str = "") -> str:
    """知识图谱页面。"""
    data = graph_data(dir_, type_)
    m = data["meta"]
    js = (GRAPH_JS
          .replace("__DATA__", json.dumps(data, ensure_ascii=False))
          .replace("__COLORS__", json.dumps(_TYPE_COLORS, ensure_ascii=False))
          .replace("__FALLBACK__", _TYPE_FALLBACK))

    legend = "".join(
        f'<span class="lg"><i style="background:{_TYPE_COLORS.get(t, _TYPE_FALLBACK)}"></i>{_esc(t)}</span>'
        for t in m["types"])
    chips_d = "".join(
        f'<a class="chip{" on" if d == dir_ else ""}" href="/graph?dir={urllib.parse.quote(d)}'
        f'{"&type=" + urllib.parse.quote(type_) if type_ else ""}">{_esc(d)}</a>'
        for d in m["dirs"])
    chips_t = "".join(
        f'<a class="chip{" on" if t == type_ else ""}" href="/graph?type={urllib.parse.quote(t)}'
        f'{"&dir=" + urllib.parse.quote(dir_) if dir_ else ""}">{_esc(t)}</a>'
        for t in m["types"])

    scope = "全库" if not dir_ and not type_ else f"筛选：{dir_ or '全部目录'} / {type_ or '全部类型'}"
    return page(
        f'<h1>知识图谱</h1>'
        f'<div class="muted">{_esc(scope)} ｜ {m["nodes"]} 节点 / {m["links"]} 边 ｜ '
        f'孤立 {m["isolated"]} ｜ 悬空链接 {m["dangling"]}（指向尚未创建的笔记，不画）</div>'
        f'<div class="chips" style="margin-top:10px">{chips_d}'
        f'<a class="chip" href="/graph">全部</a></div>'
        f'<div class="chips">{chips_t}</div>'
        f'<div class="legend">{legend}</div>'
        f'<div class="gwrap"><canvas id="gcv"></canvas></div>'
        f'<div class="muted" id="ginfo">悬停看节点明细；拖动空白处平移，滚轮缩放，点节点进详情。</div>'
        + footer(stats()),
        title="知识图谱",
        script=f"<script>{js}</script>",
    )

