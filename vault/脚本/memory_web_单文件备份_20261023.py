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

# 阶段 4（2026-10-23）：是否要求登录。**默认开** —— 安全默认值。
# 关闭只用于本地调试，用 `--no-login` 显式打开，不会因为「忘了配账号」而静默放行。
AUTH_ON = True

# 写路径是否可用。启动时由 write_path_status() 探一次，之后 /healthz 直接读这里
# （healthz 要便宜，不能每次请求都 import 一遍）。None = 还没探。
WRITE_OK: bool | None = None

# 当前请求的登录用户名（thread-local）。ThreadingHTTPServer 一请求一线程，
# 所以在里面写、在 _auth_bar() 里读是安全的；用模块级全局变量才会串号。
_CURRENT_USER = threading.local()

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

EXCLUDE_DIRS = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__"}

SCHEMA_SQL = """
DROP TABLE IF EXISTS notes;
DROP TABLE IF EXISTS links;
DROP TABLE IF EXISTS meta;
CREATE TABLE notes (
  title TEXT PRIMARY KEY,
  stem TEXT, rel_path TEXT, dir TEXT,
  type TEXT, scope TEXT, tier TEXT, source TEXT, verified INTEGER,
  tags TEXT, summary TEXT,
  created TEXT, updated TEXT, version INTEGER, access_count INTEGER, schema_version INTEGER,
  chars INTEGER, body_md TEXT, body_text TEXT
);
CREATE TABLE links (src TEXT, dst TEXT);
CREATE INDEX idx_links_src ON links(src);
CREATE INDEX idx_links_dst ON links(dst);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

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


def strip_md(text: str) -> str:
    """剥掉标记得到纯文本，供搜索使用。"""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"[#>*`_~\[\]()|-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ────────────────────────── 索引构建 ──────────────────────────


def connect() -> sqlite3.Connection:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 显式列名（2026-10-19）：原来是 `INSERT ... VALUES (19 个 ?)`，加一列就会「列数不匹配」
# 直接报错。显式列名之后，新增列不在列表里就是 NULL，老代码不会因为加列而崩。
UPSERT_SQL = (
    "INSERT OR REPLACE INTO notes ("
    "title, stem, rel_path, dir, type, scope, tier, source, verified, tags, summary, "
    "created, updated, version, access_count, schema_version, chars, body_md, body_text, "
    # 阶段 6（2026-10-23）：归属与等级。**必须显式写进这条语句** ——
    # INSERT OR REPLACE 是整行替换，列没列出来就等于把它重置成默认值。
    # 第一版直接加列不加这里，症状会是「每次改笔记，S 级悄悄退回 B」，
    # 而且不报任何错。所以下面 _upsert 里还要先读旧值兜底。
    "vault_id, grade"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_EXTRA_COLUMNS_READY = False


def _ensure_extra_columns(conn: sqlite3.Connection) -> None:
    """把「后加的列」补齐（幂等，进程内只做一次）。

    2026-10-20 原为补 `projection_hash`（已废弃）；2026-10-23 阶段 6 起改成真正
    补 `vault_id` / `grade` —— 写在写入路径上的好处是：任何还没跑迁移脚本的库，
    第一次写就会自动补上，不会出现「代码已升级、库没升级」的半死状态。
    """
    global _EXTRA_COLUMNS_READY
    if _EXTRA_COLUMNS_READY:
        return
    have = {str(r[1]) for r in conn.execute("PRAGMA table_info(notes)")}
    # 默认值用字面量而不是参数：SQLite 的 ALTER TABLE ADD COLUMN 不接受参数绑定。
    if "vault_id" not in have:
        conn.execute(
            "ALTER TABLE notes ADD COLUMN vault_id TEXT NOT NULL DEFAULT '%s'"
            % acl.DEFAULT_VAULT_ID
        )
    if "grade" not in have:
        conn.execute(
            "ALTER TABLE notes ADD COLUMN grade TEXT NOT NULL DEFAULT '%s'"
            % note_grade.DEFAULT_GRADE
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_vault_grade ON notes(vault_id, grade)")
    _EXTRA_COLUMNS_READY = True



def _upsert(conn: sqlite3.Connection, path: Path, meta: dict, body: str) -> str:
    """把一条笔记写进索引。build_index 与增量更新共用，避免两处字段逻辑漂移。"""
    import memory_store as ms

    _ensure_extra_columns(conn)
    title = ms._entry_title(meta, path)
    rel = path.relative_to(VAULT)
    folder = "（库根）" if path.parent == VAULT else path.parent.name

    def _int(key: str) -> int:
        v = meta.get(key)
        return v if isinstance(v, int) else 0

    # 阶段 6：归属与等级必须「粘住旧值」。
    # INSERT OR REPLACE 整行替换，元数据里没带这两个字段时若直接给默认值，
    # 一次普通编辑就会把 S 级笔记降级成 B —— 静默、且没有任何报错。
    prev = conn.execute(
        "SELECT vault_id, grade FROM notes WHERE title=?", (title,)
    ).fetchone()
    vault_id = str(meta.get("vault_id") or (prev[0] if prev else "") or acl.DEFAULT_VAULT_ID)
    grade = str(meta.get("grade") or (prev[1] if prev else "") or "").upper()
    if grade not in note_grade.GRADE_RANK:
        grade = note_grade.grade_of(
            title, body,
            str(meta.get("type") or ms._derive_type(title)),
            ms._entry_tags(meta),
        )

    conn.execute(UPSERT_SQL, (
        title,
        path.stem,
        str(rel).replace("\\", "/"),
        folder,
        # type / scope 优先取调用方给的 meta（2026-10-20 修）。
        # 此前无条件 `_derive_type(title)` 覆盖：`协同记录_2026-10-13_*` 三篇
        # 磁盘与库里都写着 `type: log`，重建索引时被推成 `note` —— 按标题前缀
        # 推导永远推不出 log。库成为唯一原件之后，这种「入库时重推」等于
        # **每次重建都静默改写一次身份**，且没有任何地方会报错。
        str(meta.get("type") or ms._derive_type(title)),
        str(meta.get("scope") or ms._derive_scope(title, path)),
        str(meta.get("tier") or "warm"),
        str(meta.get("source") or ""),
        1 if meta.get("verified") is True else 0,
        ",".join(ms._entry_tags(meta)),
        str(meta.get("summary") or ""),
        str(meta.get("created") or ""),
        str(meta.get("updated") or ""),
        _int("version"),
        _int("access_count"),
        _int("schema_version"),
        len(body),
        body,
        strip_md(body),
        vault_id,
        grade,
    ))
    conn.execute("DELETE FROM links WHERE src=?", (title,))
    for dst in ms._extract_wiki_links(body):
        conn.execute("INSERT INTO links VALUES (?,?)", (title, dst))
    return title


def build_index_from_md(force: bool = False) -> dict:
    """从 md 全量重建索引 —— **危险操作，只在「库丢失」时用**。

    2026-10-19 改名（原 `build_index`）：写路径切成「库为源」之后，这个函数做的
    是 `DROP TABLE notes` 再从磁盘 md 重建。一旦库里有「已写入但 md 导出失败」的行
    （`_persist_note` 明确允许这种状态），跑一次就会把它抹掉 —— 而且抹得静默、
    看起来还很成功。名字里必须带 `from_md`，让调用方知道自己在做什么。

    日常同步请用 `rebuild_derived()`（只重算 links，不碰 notes）。
    """
    import memory_store as ms

    if ms._db_mode() and not force:
        # 库模式（2026-10-20）：这个函数的入口是 `ms._iter_entries()`，而库模式下
        # 它读的就是 notes 表本身 —— 于是「DROP TABLE 再从 md 重建」变成了
        # **先删掉源、再从源读**，执行完库是空的。这条路径必须硬拦，不能只靠注释。
        raise RuntimeError(
            "build_index_from_md() 在库模式下禁止调用："
            "它会 DROP TABLE notes 再走 ms._iter_entries()，而在库模式下"
            "后者读的就是 notes 表 —— 等于删了源再从源读。"
            "要从快照恢复请用 脚本/snapshot_md.py --restore --force。"
        )
    if force:
        # 恢复动作：临时掐掉库模式，让 `_iter_entries()` 回到扫盘，
        # 否则下面 DROP 完再读 notes 会读到空表。
        os.environ["AI_MEMORY_DB_ONLY"] = "0"
        import vault_db
        vault_db._ENABLED = False
        vault_db._CACHE.update({"stamp": None, "by_rel": {}, "rows": []})
        ms._invalidate_cache()  # 否则 _iter_entries 仍返回缓存里那份库内快照

    t0 = time.perf_counter()
    entries = ms._iter_entries()
    conn = connect()
    try:
        conn.executescript(SCHEMA_SQL)
        n_links = 0
        for path, meta, body in entries:
            _upsert(conn, path, meta, body)
            n_links += len(ms._extract_wiki_links(body))
        conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("INSERT INTO meta VALUES ('vault', ?)", (str(VAULT),))
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(ms.SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()
    return {
        "notes": len(entries),
        "links": n_links,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def rebuild_derived() -> dict:
    """只重算**派生表**（links），notes 一行都不动、也不读 md（2026-10-19）。

    为什么要有它：库为源之后，「日常同步」和「从 md 重建」是两件事，混用会丢数据。
    `notes` 是源，`links` / `notes_fts` / `note_vecs` 都是从 `notes.body_md` 能算出来的
    派生数据 —— 重算派生数据永远安全；重算 `notes` 等于用投影覆盖源。

    判据：notes 为空却要重算派生表 = 库丢了，**直接抛错**让调用方显式选择
    `build_index_from_md()`，不允许这里静默建成空库。
    """
    import memory_store as ms

    t0 = time.perf_counter()
    conn = connect()
    try:
        rows = conn.execute("SELECT title, body_md FROM notes").fetchall()
        if not rows:
            raise RuntimeError("notes 表为空：库可能已丢失，派生表无从重算。"
                               "确认要从 md 重建请显式调用 build_index_from_md()。")
        conn.execute("DELETE FROM links")
        n_links = 0
        for r in rows:
            for dst in ms._extract_wiki_links(r["body_md"] or ""):
                conn.execute("INSERT INTO links VALUES (?,?)", (r["title"], dst))
                n_links += 1
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                     (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
    finally:
        conn.close()
    return {
        "notes": len(rows),
        "links": n_links,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# 旧名兼容（md2fanout.py --rebuild-index 等仍在用）。语义仍是「从 md 全量重建」，
# 新增代码请直接写 `build_index_from_md`，别用这个名字 —— 它看不出危险在哪。
build_index = build_index_from_md


def ensure_index() -> None:
    if not INDEX_PATH.exists():
        build_index_from_md()


def stats() -> dict:
    ensure_index()
    conn = connect()
    try:
        n_notes = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        n_links = conn.execute("SELECT COUNT(*) c FROM links").fetchone()["c"]
        built = conn.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
        dirs = [dict(r) for r in conn.execute(
            "SELECT dir, COUNT(*) n FROM notes GROUP BY dir ORDER BY n DESC")]
        types = [dict(r) for r in conn.execute(
            "SELECT type, COUNT(*) n FROM notes GROUP BY type ORDER BY n DESC")]
        return {
            "notes": n_notes,
            "links": n_links,
            "built_at": built["v"] if built else "-",
            "index_path": str(INDEX_PATH),
            "index_size_kb": round(INDEX_PATH.stat().st_size / 1024, 1) if INDEX_PATH.exists() else 0,
            "dirs": dirs,
            "types": types,
        }
    finally:
        conn.close()


def find_path(title: str) -> Path | None:
    """标题 → 路径（库模式下是虚拟路径，磁盘上不一定有文件）。"""
    import memory_store as ms

    if ms._db_mode():
        try:
            import vault_db

            hit = vault_db.load_by_title(title)
            if hit is not None:
                return hit[0]
        except Exception:  # noqa: BLE001 —— 适配层异常按「没找到」处理，别再扫盘
            return None
        return None
    for f in ms._active_md_files():
        meta, _ = ms._load_memory(f)
        if ms._entry_title(meta, f) == title or f.stem == title:
            return f
    return None


def save_note(title: str, new_body: str) -> str:
    """保存正文。走 memory_store 的写入函数，与 MCP 通道同一条写路径。

    2026-10-20：库模式下 `_write_memory` 内部已改为只写 `notes` 行，
    这里不再需要「写回 md → 重读 md → 刷索引」那一圈。两个投影页
    （记忆索引 / 路由索引）仍要重算，因为它们的内容取决于全库。
    """
    import memory_store as ms

    path = find_path(title)
    if path is None:
        return f"错误：未找到标题为「{title}」的笔记"
    meta, _old = ms._load_memory(path)
    old_version = meta.get("version", 0)
    ms._write_memory(
        path,
        title=title,
        tags=ms._entry_tags(meta),
        source=meta.get("source"),
        content=new_body,
        created=str(meta.get("created")) if meta.get("created") else None,
        summary=str(meta.get("summary")) if meta.get("summary") else None,
        tier=str(meta.get("tier") or "warm"),
        access_count=meta.get("access_count") if isinstance(meta.get("access_count"), int) else 0,
        version=(old_version + 1) if isinstance(old_version, int) else 1,
        verified=meta.get("verified") if isinstance(meta.get("verified"), bool) else None,
    )
    ms._invalidate_cache()
    if ms._db_mode():
        ms._refresh_index()
        ms._refresh_routing_index()
        where = str(path.relative_to(VAULT))
        return f"已保存 {where}（库）"
    ms._refresh_index()
    ms._refresh_routing_index()
    refresh_one(title)
    return f"已写回 {path.relative_to(VAULT)}"


def refresh_path(path: Path) -> None:
    """按**已知路径**增量更新一行（写入路径专用）。

    与 `refresh_one(title)` 的区别：不需要 `find_path` 的全库扫描 —— 写入方
    本来就知道文件落在哪。MCP 的 `memory_write` 每次写完都要刷一行，
    省掉一次全库遍历（本库 119 篇约几十毫秒，写入路径上每一毫秒都算数）。
    """
    import memory_store as ms

    if ms._db_mode():
        # 库模式下「从磁盘读回来刷一行」这件事已经没有意义：磁盘不再是源。
        # 写入方本来就直接 `upsert_row` 了，这里再读一遍只会把内存里的旧文件
        # 覆盖到库上 —— 那是最坏的一种 bug（用快照覆盖原件）。
        return

    try:
        meta, body = ms._load_memory(path)
        conn = connect()
        try:
            _upsert(conn, path, meta, body)
            # 增量刷新也要更新 built_at：`check_integrity` 的 C2 判据是
            # 「索引最后一次写入时间 vs 最新 md mtime」。只在全量构建时写这一行，
            # 每次增量写入后 C2 都会误报「索引落后」（2026-10-19 实测踩到）。
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def remove_path(path: Path) -> None:
    """按已知路径把一行从索引里删掉（删除路径专用，2026-10-19）。

    为什么需要它：`memory_delete` 过去只重建两个 md 索引，**从没碰过
    `index.sqlite3`**。切到 db 读源之后，被删掉的笔记仍然留在 notes 表里 ——
    检索会返回一条「已经不存在、打开就是 404」的笔记，比搜不到更糟。
    这类洞在写路径上补过一次（refresh_path），删除路径上是对称的另一半。

    同时清掉 `note_vecs`：残留向量虽然已被读取侧的「notes 交叉过滤」挡住，
    但留着只是白占空间，也容易让人误以为该笔记还在。
    """
    rel = str(path.relative_to(VAULT)).replace("\\", "/")
    try:
        conn = connect()
        try:
            conn.execute("DELETE FROM notes WHERE rel_path = ?", (rel,))
            try:
                conn.execute("DELETE FROM note_vecs WHERE rel_path = ?", (rel,))
            except sqlite3.Error:
                pass  # 老库还没有这张表
            # 双链与全文索引按来源/路径清，避免留下指向空笔记的边
            for sql in (
                "DELETE FROM links WHERE src = ?",
                "DELETE FROM links WHERE dst = ?",
                "DELETE FROM notes_fts WHERE rel_path = ?",
            ):
                try:
                    conn.execute(sql, (rel,))
                except sqlite3.Error:
                    pass
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


def _refresh_fts_row(conn: sqlite3.Connection, rel: str, title: str, body: str,
                     summary: str, tags: str) -> None:
    """把一行的全文索引重灌（单条版 `db_init.fill_fts`）。

    为什么要单条版：`fill_fts` 是 `DELETE FROM notes_fts` 全量重灌（实测 289 ms），
    写在每次 `memory_write` 后面太贵。而 db 为主之后，**库就是源**，写完不重灌
    这一行，`notes_fts` 会一直停在旧文本上 —— 与 7.6 里那个「写入成功但搜不到」
    是同一形状。分词口径必须与检索侧一致（复用 `memory_tools._tokenize_query`），
    否则会出现「索引里有、却查不到」。
    """
    try:
        import memory_tools as mt

        text = f"{title} {title} {summary} {tags} {body}"
        toks = " ".join(mt._tokenize_query(text))
        conn.execute("DELETE FROM notes_fts WHERE rel_path = ?", (rel,))
        conn.execute(
            "INSERT INTO notes_fts(rel_path, title, tokens) VALUES (?,?,?)",
            (rel, title, toks),
        )
    except sqlite3.Error:
        pass  # 老库没有这张表，或 FTS5 未启用 —— 全文索引属可选能力，不阻断写入


def upsert_row(path: Path, meta: dict, body: str) -> None:
    """用**内存里的 meta + body** 写一行索引（db 为主写路径的出口）。

    与 `refresh_path` 的关键区别：`refresh_path` 是「先写 md、再从磁盘读回来建索引」，
    本质上仍然是 md 为主；本函数**不读磁盘**，直接吃调用方算好的那一份 meta，
    所以它可以发生在 md 文件还不存在的时候。这是「库为源、md 为投影」唯一站得住
    的写法 —— 反过来就成了「库是 md 的缓存」，md 丢了库也跟着丢。
    """
    rel = str(path.relative_to(VAULT)).replace("\\", "/")
    conn = connect()
    try:
        _upsert(conn, path, meta, body)
        _refresh_fts_row(
            conn, rel, str(meta.get("title") or path.stem), body,
            str(meta.get("summary") or ""), ",".join(str(t) for t in (meta.get("tags") or [])),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
            (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.commit()
    finally:
        conn.close()


def file_hash(path: Path) -> str:
    """整文件字节哈希（投影基线口径：导出写了什么就记什么，不做规范化）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refresh_fts_row(conn: sqlite3.Connection, rel: str, title: str, body: str,
                    summary: str, tags: str) -> None:
    """对外暴露的单条 FTS 重灌（供导入脚本复用，口径与写入路径一致）。"""
    _refresh_fts_row(conn, rel, title, body, summary, tags)


def refresh_one(title: str) -> None:
    """单条增量更新：从 md 重读整条（含 version/updated），省去全量重建。

    库模式下是空操作：磁盘不再是源，重读文件只会把快照覆盖到库上。
    """
    import memory_store as ms

    if ms._db_mode():
        return

    path = find_path(title)
    if path is None:
        return
    try:
        meta, body = ms._load_memory(path)
        conn = connect()
        try:
            _upsert(conn, path, meta, body)
            # 增量刷新也要更新 built_at：`check_integrity` 的 C2 判据是
            # 「索引最后一次写入时间 vs 最新 md mtime」。只在全量构建时写这一行，
            # 每次增量写入后 C2 都会误报「索引落后」（2026-10-19 实测踩到）。
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                (datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


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
    """等级徽章：S 红 / A 橙 / B 灰 / C 蓝描边。颜色只标重要性，不依赖外链 CSS。"""
    style = {
        "S": "background:#8b1a1a;color:#fff",
        "A": "background:#b45309;color:#fff",
        "B": "background:#e5e7eb;color:#374151",
        "C": "background:transparent;color:#2563eb;border:1px solid #2563eb",
    }.get(g, "background:#e5e7eb;color:#374151")
    label = GRADE_LABELS.get(g, g or "?")
    return f'<span style="display:inline-block;padding:0 8px;border-radius:9px;font-size:12px;line-height:18px;{style}">{_esc(label)} {g}</span>'


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
            "FROM notes WHERE title LIKE ? OR body_text LIKE ? OR tags LIKE ? OR summary LIKE ? "
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
                "FROM notes WHERE title LIKE ? OR body_text LIKE ? ORDER BY updated DESC LIMIT 100",
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
        if sub and web_auth.user_exists(sub):
            return sub
        return ""

    def _guard(self, path: str) -> bool:
        """返回 True 表示已放行；False 表示已经发出 302 跳登录页，调用方应直接 return。"""
        if not AUTH_ON or path in PUBLIC_PATHS or path.startswith("/assets/"):
            return True
        sub = self._session_user()
        if sub:
            # 给 _auth_bar() 用：状态条要显示真实用户名，不能写死。
            _CURRENT_USER.sub = sub
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
                self._json({"ok": True, "auth": AUTH_ON,
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
                if AUTH_ON and not self._session_user():
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
    """
    if _is_replica():
        return False, "只读镜像库（正本在本地，写请走本地 Web/MCP 后 sync_push 同步）"
    try:
        import memory_tools  # noqa: F401
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - 缺包/路径错都要报出来，不许静默
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    global AUTH_ON, WRITE_OK
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
        AUTH_ON = False

    # 安全默认：要开门就得先有锁。没有账号时拒绝启动，并给出可直接复制的创建命令。
    # 与阶段 2「--require-token 装不上锁就不许跑」是同一个原则：不要静默降级成无认证。
    if AUTH_ON and not web_auth.has_users():
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
    if _is_replica():
        print("只读镜像库：跳过派生表重算（正本在本地，跑 sync_push.py 同步后自然一致）")
    else:
        info = rebuild_derived()
        print(f"派生表已重算：notes {info['notes']} 行未动 / links {info['links']} 条，"
              f"耗时 {info['elapsed_ms']} ms -> {INDEX_PATH}")
    if args.rebuild_only:
        return 0

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"L3 Web 已启动：{url}" + ("" if not AUTH_ON else "（需登录）"))
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
