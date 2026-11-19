#!/usr/bin/env python3
"""memory_web 的存储/写路径层（2026-10-23 自 memory_web.py 按行切出，函数体未改）。

职责：索引库连接、upsert、save_note、派生表重算、FTS 刷新。
视图层（web_views）与 HTTP 层（memory_web）都从这里导入，本层不 import 它们俩。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
VAULT = Path(os.environ.get("AI_MEMORY_DIR") or CODE_ROOT).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)

import db_paths  # noqa: E402
import note_grade  # noqa: E402 - 写入时定级
import acl  # noqa: E402 - 归属库默认值

INDEX_PATH = db_paths.resolve("index.sqlite3")

EXCLUDE_DIRS = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__"}


# ─────────────────── 只读副本模式（阶段 10，2026-10-29） ───────────────────
# 为什么要有它：服务器上 index.sqlite3 是本地库的只读镜像。此前靠 9 个
# `_ro_guard_*` 触发器拦写 —— 但触发器只盖 notes/links/meta 三张表，notes_fts
# 是 fts5 虚拟表，**SQLite 不允许在虚拟表上建触发器**，那条路径永远挡不住。
# 而且 OS 层收权（index 改 440）之后，可写连接会在 connect 就 PermissionError，
# 那个报错对使用者毫无指引。
#
# 所以做成「连接即只读 + 写入口 fail-fast」：服务器侧一律 mode=ro 打开，
# 任何写请求在动手之前就抛出 ReadOnlyReplica，消息直接告诉你该去哪写。
#
# 默认关闭。不设或设 0/false/no/off 时，本地主库行为一个字节都不变。
_READONLY_ENV = "AI_MEMORY_READONLY"
_READONLY_TRUTHY = {"1", "true", "yes", "on"}


def readonly_mode() -> bool:
    """当前进程是否以只读副本模式运行。"""
    return os.environ.get(_READONLY_ENV, "").strip().lower() in _READONLY_TRUTHY


class ReadOnlyReplica(RuntimeError):
    """只读副本上的写请求。

    刻意用 RuntimeError 而不是 sqlite3.Error：`refresh_path` / `remove_path`
    等处有 `except (sqlite3.Error, OSError): pass` 的宽捕获，用 sqlite3 系异常
    会被**静默吞掉**，表现成「保存成功但没写进去」—— 比报错糟得多。
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            "只读副本：%s 被拒绝。服务器库是本地库的镜像，写操作请在本机做，"
            "然后跑 脚本/sync_push.py 推上来。" % what
        )
        self.what = what


def _deny_write(what: str) -> None:
    if readonly_mode():
        raise ReadOnlyReplica(what)


def strip_md(text: str) -> str:
    """剥掉标记得到纯文本，供搜索使用。

    2026-10-23（分层当天踩到，见 Bug 日志第 10 条）：这个函数原来在
    `memory_web.py` 单文件里、被切分脚本分到了**视图层** `web_views.py`，
    但真正调用它的是本层的 `_upsert`（写 notes.body_text 时要用纯文本）。
    结果是「看得到定义、调不到」——写入直接 `NameError: strip_md`，
    而且只有真写一条笔记才会暴露。口径：纯文本口径属于存储层，视图层
    通过 `from web_store import strip_md` 复用同一份实现。
    """
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"[#>*`_~\[\]()|-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

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
  chars INTEGER, body_md TEXT
);
CREATE TABLE links (src TEXT, dst TEXT);
CREATE INDEX idx_links_src ON links(src);
CREATE INDEX idx_links_dst ON links(dst);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

# ────────────────────────── 索引构建 ──────────────────────────


def connect() -> sqlite3.Connection:
    if readonly_mode():
        # mode=ro 而不是「连上再指望触发器」：ro 连接下 SQLite 拒绝一切写语句，
        # 连 fts5 虚拟表那条挡不住的路径也一并封掉。不建目录、不建库 ——
        # 库不存在就直接报「unable to open database file」，好过静默造一个空库
        # 让人以为服务正常（memory_tools 里踩过同款坑）。
        conn = sqlite3.connect(
            "file:%s?mode=ro" % INDEX_PATH.as_posix(), uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:  # 连接级调优（缓存/mmap/临时表），单一实现在 db_paths.tune
            import db_paths as _dbp

            _dbp.tune(conn)
        except Exception:
            pass
        return conn
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_PATH)
    conn.row_factory = sqlite3.Row
    try:  # 连接级调优（缓存/mmap/临时表），单一实现在 db_paths.tune
        import db_paths as _dbp

        _dbp.tune(conn)
    except Exception:
        pass
    return conn


# 显式列名（2026-10-19）：原来是 `INSERT ... VALUES (19 个 ?)`，加一列就会「列数不匹配」
# 直接报错。显式列名之后，新增列不在列表里就是 NULL，老代码不会因为加列而崩。
UPSERT_SQL = (
    "INSERT OR REPLACE INTO notes ("
    "title, stem, rel_path, dir, type, scope, tier, source, verified, tags, summary, "
    "created, updated, version, access_count, schema_version, chars, body_md, "
    # 阶段 6（2026-10-23）：归属与等级。**必须显式写进这条语句** ——
    # INSERT OR REPLACE 是整行替换，列没列出来就等于把它重置成默认值。
    # 第一版直接加列不加这里，症状会是「每次改笔记，S 级悄悄退回 B」，
    # 而且不报任何错。所以下面 _upsert 里还要先读旧值兜底。
    "vault_id, grade"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
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
    # 最后一道闸：所有写路径最终都汇到 _upsert，这里再拦一次，防止将来有人
    # 新加一个调用方却忘了在入口处 _deny_write。
    _deny_write("写 notes 行")
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
    _deny_write("从 md 全量重建索引")
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
    _deny_write("重算派生表")
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
        if readonly_mode():
            # 只读副本上「库不存在」是部署问题，不是「帮我建一个」——
            # 静默建成空库会让所有读接口返回 0 条，看起来像数据丢了。
            raise ReadOnlyReplica("索引库不存在（%s），只读副本不会自动建库" % INDEX_PATH)
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
    _deny_write("保存笔记")
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

    _deny_write("增量刷新索引")
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
    _deny_write("删除索引行")
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
        _deny_write("重灌全文索引行")
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
    _deny_write("写入索引行")
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

    _deny_write("单条增量更新")
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


