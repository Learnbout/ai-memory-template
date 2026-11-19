"""库作为唯一存储的**适配层**（2026-10-20）。

为什么要有这一层
----------------
改成「库是原件、md 只是快照」之后，全库有 **35 处** 代码在调
`memory_store._active_md_files()` / `_load_memory()` 拿笔记。逐处改成 SQL 是
一场必出漏的扫荡：漏一处就变成「有的功能看库、有的看盘」，两边又会漂移。

所以这里只做一件事：**把「扫盘 + 读文件」换成「查 notes 表」的同名能力**，
让 `memory_store` 内部把 `_active_md_files` / `_load_memory` 接到本模块上，
上层 35 个调用点一行都不用改。这是兼容层（<sdk>_compat 那一套）的同款思路：
把差异收进一个文件，业务代码不认识数据库。

三条边界（改之前先想清楚）
--------------------------
1. **只读快照、不碰文件系统**。`files()` 返回的是**虚拟路径**
   （`MEMORY_DIR/rel_path`），磁盘上有没有这个文件不影响结果 —— 这正是
   「库是原件」的定义。任何在本模块里 `open()` 文件的代码都是 bug。
2. **写入必须一次落全**：notes 行 + links + FTS + 向量 + built_at。漏掉任何
   一项都会表现为「写成功了但搜不到」，而这类症状最容易被误判成数据丢失。
3. **库不可用时安静地返回 None**，由调用方回落到读盘。宁可慢，不能返回空
   —— 「检索不到」和「库里没有」在用户眼里是同一件事，前者会直接导致 AI 编造。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import db_paths

# notes 表里的字段 → meta 字典。顺序无关，缺一个就会在导出/渲染时丢字段，
# 所以这里刻意逐列写全，不做 `SELECT *`（加列时能立刻看见这里没跟上）。
_META_COLS = (
    "title", "tags", "type", "scope", "tier", "source", "verified",
    "summary", "created", "updated", "version", "access_count",
)


_VAULT: Path | None = None


def vault() -> Path:
    """库根目录（进程内缓存一次）。

    `db_paths.vault_dir()` 每次都会 `resolve()` 一遍，而 Windows 上 `resolve()`
    是要摸文件系统的（实测 0.3~0.6 ms）。`rel_of()` 在 `_iter_entries` 里被调
    119 次，不收口就是 **70 ms** 的纯开销，而它算出来的永远是同一个值。
    """
    global _VAULT
    if _VAULT is None:
        _VAULT = db_paths.vault_dir()
    return _VAULT


def _db_path() -> Path:
    return db_paths.index_db()


_ENABLED: bool | None = None


def enabled() -> bool:
    """是否走库。`AI_MEMORY_DB_ONLY=0` 可一键退回扫盘（应急用）。

    只缓存**肯定答案**：一次判定「真」之后就固定为真（库文件不会中途消失，
    真消失了下一次读写会报错，而不是悄悄换成扫盘）。判「假」不缓存 ——
    那对应「刚建库、库还没写入」的过渡期，缓存住就再也不会恢复。
    不缓存的话代价很实在：`_db_mode()` 被 `_iter_entries` 循环里调用 238 次，
    每次开一次 SQLite 连接，实测首轮要多花 **1.5 秒**。
    """
    global _ENABLED
    if _ENABLED:
        return True
    if (os.environ.get("AI_MEMORY_DB_ONLY") or "1").strip() == "0":
        return False
    db = _db_path()
    if not db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            n = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'"
            ).fetchone()
            ok = n is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if ok:
        _ENABLED = True
    return ok


def rel_of(path: Path) -> str:
    """Path → 库内相对路径（统一正斜杠、无前导斜杠）。

    先走**纯字符串**的 `relative_to`，只有不是绝对前缀时才 `resolve()`。
    从 `path_of()` 造出来的路径本来就规整，99% 的调用落在第一条分支上，
    避免为了算一个字符串去摸磁盘。
    """
    p = Path(path)
    v = vault()
    try:
        return str(p.relative_to(v)).replace("\\", "/").lstrip("/")
    except ValueError:
        pass
    try:
        return str(p.resolve().relative_to(v)).replace("\\", "/").lstrip("/")
    except (ValueError, OSError):
        return str(p).replace("\\", "/").lstrip("/")


def path_of(rel: str) -> Path:
    """库内相对路径 → 虚拟 Path（不保证磁盘上存在）。"""
    return vault() / str(rel).replace("\\", "/").lstrip("/")


def _connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path().as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:  # 连接级调优（缓存/mmap/临时表），单一实现在 db_paths.tune
        import db_paths as _dbp
        _dbp.tune(conn)
    except Exception:
        pass
    return conn


# 全表缓存：`_iter_entries` 会对 119 条路径逐个调 `load()`，每次都开一个连接
# 实测 **3.5 秒**（119 × 29 ms，Windows 上 SQLite 开库很贵）。整表只有 119 行、
# 十几 MB，一次读全再按路径查字典是明确更优的交换：**一次查询 ≈ 30 ms**。
# 失效判据用 `stamp()`（库文件 mtime_ns + size）—— 与 memory_store 的跨进程
# 缓存判据同源，避免「这个缓存觉得新、那个觉得旧」。
_CACHE: dict[str, Any] = {"stamp": None, "by_rel": {}, "rows": []}
_STAMP_TTL = 0.25  # 秒；见 _snapshot() 里为什么需要这道闸
_LAST_CHECK = [0.0]


def _ident_key() -> tuple:
    """快照缓存的身份维度（阶段 6，2026-10-23 修）。

    为什么必须算进来：`_CACHE` 存的是 `rows()` 的**过滤后**结果。MCP 服务是
    单进程多请求，管理员先查一次 → 缓存里是全量；下一个只读用户进来，只要库文件
    没变就命中缓存，直接拿到 S 级笔记 —— 越权发生在缓存层，上层所有过滤全部失效，
    而且不报任何错。这跟 `_db_search_entries` 的缓存是同一个坑，两边都按身份分键。
    """
    try:
        import acl

        who = acl.identity()
    except Exception:  # noqa: BLE001
        return ("", "")
    if who is None:
        return ("*", "*")
    return (str(who.get("user_id") or ""), str(who.get("max_grade") or ""))


def _snapshot() -> dict[str, Any]:
    """整表快照（按需重建）。

    为什么 stamp 要加 TTL：`stamp()` 要 stat 主库和 -wal 两个文件，Windows 上
    一次 stat ≈ 0.4 ms。`_iter_entries` 会对 119 条路径逐个 `load()`，每次都
    stat 四个 → 实测 **多花 195 ms**，而这 195 ms 里库根本没变。
    加 250 ms 闸之后，同一次调用的 119 次查询只 stat 一次。
    代价是「别的进程刚写完，本进程最多晚 250 ms 看见」—— 对 MCP 这种一问一答
    的通道没有可感影响，比每篇笔记多付 1.6 ms 划算得多。
    """
    now = time.monotonic()
    ident = _ident_key()
    if (_CACHE["by_rel"] and (now - _LAST_CHECK[0]) < _STAMP_TTL
            and _CACHE.get("ident") == ident):
        return _CACHE
    st = stamp()
    _LAST_CHECK[0] = now
    if _CACHE["stamp"] == st and _CACHE["by_rel"] and _CACHE.get("ident") == ident:
        return _CACHE
    all_rows = rows()
    by_rel: dict[str, tuple[dict[str, Any], str]] = {}
    for r in all_rows:
        by_rel[str(r["rel_path"])] = (_row_to_meta(r), str(r["body_md"] or ""))
    _CACHE.update({"stamp": st, "by_rel": by_rel, "rows": all_rows, "ident": ident})
    return _CACHE


def _row_to_meta(row: sqlite3.Row) -> dict[str, Any]:
    tags = [t for t in str(row["tags"] or "").split(",") if t.strip()]
    return {
        "title": str(row["title"] or ""),
        "tags": tags,
        "type": str(row["type"] or ""),
        "scope": str(row["scope"] or ""),
        "tier": str(row["tier"] or "warm"),
        "source": str(row["source"] or ""),
        "verified": bool(row["verified"]),
        "summary": str(row["summary"] or ""),
        "created": str(row["created"] or ""),
        "updated": str(row["updated"] or ""),
        "version": int(row["version"] or 0),
        "access_count": int(row["access_count"] or 0),
    }


def rows() -> list[sqlite3.Row]:
    """全表读取 —— **阶段 6 起这里是权限过滤的唯一咽喉**。

    为什么在这层过滤而不是给 35 个上层调用点逐个加条件：`files()` / `load()` /
    `_snapshot()` 全部由 `rows()` 出发，在这拦一次，上层「扫全库」类功能
    （索引页、图、孤儿检测、列表）就整体被收窄了，不需要逐处改。

    代价是「多取几十行再丢掉」。142 行的库可以忽略不计；换成拼 WHERE 反而要
    处理「老库还没有 vault_id / grade 两列」的分支，复杂度不划算。
    """
    conn = _connect_ro()
    try:
        try:
            raw = conn.execute(
                "SELECT title, stem, rel_path, dir, type, scope, tier, source, verified, "
                "tags, summary, created, updated, version, access_count, body_md, "
                "vault_id, grade "
                # 顺序必须是确定的：调用方（检索候选流）会依赖它，
                # 而 md 时代这里是「路径降序」，保持一致才不会让同一查询
                # 在两条读源上排出两种顺序（2026-10-19 实测踩过）。
                "FROM notes ORDER BY rel_path DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            # 阶段 6 迁移脚本还没在本地跑过时的兜底：缺两列就按「主库 + 默认等级」读。
            raw = conn.execute(
                "SELECT title, stem, rel_path, dir, type, scope, tier, source, verified, "
                "tags, summary, created, updated, version, access_count, body_md "
                "FROM notes ORDER BY rel_path DESC"
            ).fetchall()
    finally:
        conn.close()
    import acl

    return acl.filter_rows(raw)


def files() -> list[Path]:
    """全部笔记的**虚拟路径**，顺序与旧 `_active_md_files()` 一致（路径降序）。"""
    snap = _snapshot()
    return [path_of(str(r["rel_path"])) for r in snap["rows"]]


def load(path: Path) -> "tuple[dict[str, Any], str] | None":
    """按路径取 (meta, body)。库里没有这一行 → None（调用方决定是否回落读盘）。"""
    hit = _snapshot()["by_rel"].get(rel_of(path))
    if hit is None:
        return None
    meta, body = hit
    # 返回副本：调用方会就地改 meta（加 access_count / 改 updated / 补 summary），
    # 直接把缓存里那份递出去，等于让一次读顺手污染了整个进程的视图。
    return dict(meta), body


def load_by_title(title: str) -> "tuple[Path, dict[str, Any], str] | None":
    conn = _connect_ro()
    try:
        row = conn.execute(
            "SELECT title, rel_path, tags, type, scope, tier, source, verified, summary, "
            "created, updated, version, access_count, body_md FROM notes WHERE title=?",
            (title,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return path_of(str(row["rel_path"])), _row_to_meta(row), str(row["body_md"] or "")


def exists(path: Path) -> bool:
    conn = _connect_ro()
    try:
        return conn.execute(
            "SELECT 1 FROM notes WHERE rel_path=?", (rel_of(path),)
        ).fetchone() is not None
    finally:
        conn.close()


def title_of(path: Path) -> str:
    conn = _connect_ro()
    try:
        row = conn.execute(
            "SELECT title FROM notes WHERE rel_path=?", (rel_of(path),)
        ).fetchone()
    finally:
        conn.close()
    return str(row["title"]) if row else ""


def _ensure_scripts_path() -> None:
    """把 `<vault>/脚本` 塞进 sys.path（`web_store` / `memory_web` 住在那儿）。

    为什么必须在这里做：本模块会被 `memory_store` 在**没有** `脚本/` 路径的进程
    里导入（例如直接 `python -c "import memory_store"`），而写入链路的
    单行 upsert 实现放在 `脚本/web_store.py`（2026-10-23 分层前在 `memory_web.py`）。
    少了这一步，写入就报 `ModuleNotFoundError` —— 不是「写入失败」，是**根本进不去**。
    """
    scripts = str(vault() / "脚本")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def save(path: Path, meta: dict[str, Any], body: str) -> None:
    """写一行（含 links / FTS / 向量 / built_at）。

    向量是可选能力：模型不在、onnxruntime 缺失都只记一条 WARNING，
    **不能让「没有向量」把一次成功的写入变成失败**。
    """
    _ensure_scripts_path()
    # 2026-10-23 分层后 upsert_row / remove_path 搬到了 web_store.py，
    # 这里曾仍写 `import memory_web` → 索引页重建报 AttributeError（Bug 日志第 10 条）。
    import web_store as _ws

    _ws.upsert_row(path, meta, body)
    invalidate()  # 同进程写完立刻作废，不指望 mtime 追得上（含 TTL 闸，见 invalidate 文档）
    _refresh_vectors(path, meta)


def invalidate() -> None:
    """作废本进程的快照缓存 —— 给**绕过本模块写出口**的调用方用。

    为什么需要这个函数（2026-10-21，实测出的真 bug）：
    `memory_tools._remove_from_sqlite_index()` 直接调 `memory_web.remove_path()`
    删库，不走 `remove()`，于是这里的 `_CACHE` 没人清。删完立刻刷索引页时，
    `_snapshot()` 的第一道闸是 **TTL**（`_STAMP_TTL = 0.25` 秒），它排在 stamp
    判断**之前** —— 250 ms 内直接返回旧快照，连 stamp 都不查。
    结果：已删除的笔记还留在 [[记忆索引]] 里，`updated` 也不动，看起来像
    「索引不刷新」，实际是读到了删除前的快照。

    所以作废必须**两件事一起做**：置 `stamp=None`（跨进程判据失效），
    并且把 `_LAST_CHECK` 推回原点（解除 TTL 闸）。只做前半件等于没做。
    """
    _CACHE["stamp"] = None
    _LAST_CHECK[0] = 0.0


def _refresh_vectors(path: Path, meta: dict[str, Any]) -> None:
    try:
        import build_vectors as _bv
    except Exception:  # noqa: BLE001 —— 脚本目录不在 sys.path 时静默跳过
        return
    try:
        _bv.refresh_one(
            rel_of(path),
            str(meta.get("title") or path.stem),
            str(meta.get("updated") or ""),
            db=_db_path(),
        )
    except Exception:  # noqa: BLE001
        pass


def remove(path: Path) -> None:
    """删一行（连带 links / FTS / 向量）。软删除到 .trash 的搬运是调用方的事。"""
    _ensure_scripts_path()
    import web_store as _ws

    _ws.remove_path(path)
    invalidate()


def stamp() -> tuple:
    """跨进程缓存失效判据：(主库 mtime_ns, size, WAL mtime_ns, WAL size)。

    为什么必须带上 `-wal`：本库是 WAL 模式，写入先落 `index.sqlite3-wal`，
    主库文件的 mtime **可能一直不变**（要等 checkpoint）。只盯主库的结果就是
    「另一个进程写了，这个进程的缓存永远不失效」—— 表现和 md 时代那个
    「目录 mtime 读取滞后」一模一样，最难查。
    """
    out = []
    for p in (_db_path(), _db_path().with_name(_db_path().name + "-wal")):
        try:
            st = p.stat()
            out.extend((st.st_mtime_ns, st.st_size))
        except OSError:
            out.extend((0, 0))
    return tuple(out)
