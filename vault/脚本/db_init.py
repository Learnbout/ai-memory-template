#!/usr/bin/env python3
"""记忆库数据库表结构初始化（幂等，可重复跑）。

设计原则（2026-09-17，主人确认后定稿）：
    数据库是**唯一事实源**；md 是**导出物**（脚本生成，供人阅读与审计）。
    因为主人不自己编辑 md（要写也是让 AI 写），所以不需要双向同步 ——
    「md 可编辑」这个最大的复杂度来源被砍掉了。

本脚本只建/改**结构**，不动任何数据。数据由：
    - `memory_web.build_index()` / `md2fanout.py --rebuild-index` 从 md 灌入（迁移期）
    - 或直接写入（新数据，如对话与抽取的事实）

表结构融合了各家开源方案的优点（每个表都标注来源）：

| 表 | 作用 | 借鉴来源 |
|---|---|---|
| notes / links | 笔记主体与双链 | 本库原有（md 唯一事实源的投影） |
| meta | 构建信息 | 本库原有 |
| notes_fts | 全文索引（CJK 2-gram 预分词） | 本库 memory_tools 的分词逻辑 + SQLite FTS5 |
| conversations | L0 原始对话层 | TencentDB Agent Memory 的四层金字塔 |
| atoms | L1 原子记忆层（事实/偏好/约束/结论） | 同上（L1 层） |
| facts | 会变化的事实（带有效时间） | Zep / Graphiti 的双时间轴（valid time + transaction time） |
| schema_log | 结构校验留痕 | Basic Memory 的 schema_validate 思路 |
| provenance | 操作溯源（我为什么写它、谁触发的） | 主人明确提出（「看明白你干了什么」） |
| users / vaults / vault_members | 多用户与知识库归属骨架 | 中心化协作 M1；本步只建结构，不迁移现有账号/笔记 |
| notes_vec（可选） | 向量检索 | Mem0 / 各向量库（需装 sqlite-vec，默不建） |

用法：
    python 脚本/db_init.py                 # 建表 + 补列（幂等）
    python 脚本/db_init.py --fill-fts      # 顺带把 notes 正文灌进全文索引
    python 脚本/db_init.py --with-vec      # 尝试创建向量表（需已装 sqlite-vec）
    python 脚本/db_init.py --stat          # 只看现状
    python 脚本/db_init.py --drop          # 危险：删除本脚本建的表（不含 notes/links/meta）
"""

from __future__ import annotations

try:  # 脚本使用计数
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）

DB_PATH = db_paths.resolve("index.sqlite3")


# ────────────────────────── 表结构 ──────────────────────────
# 全部 IF NOT EXISTS：幂等，可重复跑。

SCHEMA: dict[str, str] = {
    # ── 本库原有三表（保留原样，不重建）──
    "notes": """
        CREATE TABLE IF NOT EXISTS notes (
          title TEXT PRIMARY KEY,
          stem TEXT, rel_path TEXT, dir TEXT,
          type TEXT, scope TEXT, tier TEXT, source TEXT, verified INTEGER,
          tags TEXT, summary TEXT,
          created TEXT, updated TEXT, version INTEGER, access_count INTEGER,
          schema_version INTEGER,
          chars INTEGER, body_md TEXT,
          -- 阶段 6（2026-10-23）：归属库与等级。旧库由 脚本/migrate_stage6.py
          -- 补列，这里只保证「新建库」一次到位。
          vault_id TEXT NOT NULL DEFAULT 'v_72e8dda6ac90427b',
          grade    TEXT NOT NULL DEFAULT 'B'
        )
    """,
    "links": """
        CREATE TABLE IF NOT EXISTS links (src TEXT, dst TEXT)
    """,
    "meta": """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)
    """,

    # ── 全文索引：默认不再建（2026-11-16）─────────────────────────────────
    # 原 notes_fts 在这里无条件建表，于是每次 sync_all 都会把表重建再灌满 ——
    # 而生产检索路径里**零 MATCH 调用**（memory_search 走子串 + 向量补漏），
    # 这张表只被写、从没被读，占 12.23MB（当时整个库的 66%）。
    # 现改为显式开关：`db_init.py --create-fts --fill-fts` 才建。
    # 重建时用下面的 _FTS_SCHEMA，分词口径的原始结论一并保留：
    #   实测（2026-09-17）unicode61 把连续中文当单 token、trigram 查不到 2 字
    #   常用词；先用 _tokenize_query 切 2-gram 再喂 ascii 分词器，中文 2 字词
    #   可精确命中，英文数字也正常。

    # ── L0 原始对话层（TencentDB 四层金字塔的最底层）──
    # 全量保留每一轮交互，作为可回溯的底层证据。
    "conversations": """
        CREATE TABLE IF NOT EXISTS conversations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT,
          turn INTEGER,
          role TEXT,                 -- user / assistant / tool
          content TEXT,
          at TEXT,                   -- 该轮发生时间 ISO(UTC)
          imported_at TEXT
        )
    """,

    # ── L1 原子记忆层（TencentDB 的 L1）──
    # 从对话里抽取的原子事实/偏好/约束/结论，可被检索复用。
    "atoms": """
        CREATE TABLE IF NOT EXISTS atoms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT,                 -- fact / preference / constraint / conclusion
          subject TEXT, predicate TEXT, object TEXT,
          confidence REAL,           -- 0~1
          source_ref TEXT,           -- 来自哪次对话（conversations.id 或 session_id）
          at TEXT,
          created_at TEXT
        )
    """,

    # ── 会变化的事实：双时间轴（Zep / Graphiti）──
    # valid_from / valid_to = 该事实在现实世界里何时成立；
    # tx_at = 我何时把它记下来。旧值不删只标记关闭，
    # 于是「某年某月时它是什么」这类问题可以用纯 SQL 回答（实测可行）。
    "facts": """
        CREATE TABLE IF NOT EXISTS facts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT, predicate TEXT, object TEXT,
          valid_from TEXT,           -- 何时开始成立（NULL = 一直）
          valid_to TEXT,             -- 何时不再成立（NULL = 仍然成立）
          tx_at TEXT,                -- 记录时间（事务时间）
          superseded_by INTEGER,     -- 被哪条取代（保留历史，不删除）
          source_ref TEXT,
          note TEXT
        )
    """,

    # ── 结构校验留痕（Basic Memory 的 schema_validate 思路）──
    # 本库被表格事故折腾过三次，把「体检结论」也持久化，便于回溯与趋势。
    "schema_log": """
        CREATE TABLE IF NOT EXISTS schema_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          at TEXT,
          target TEXT,               -- 被检查的文件/对象
          check_name TEXT,           -- 检查项名（如 table_structure / frontmatter）
          result TEXT,               -- pass / fail
          detail TEXT
        )
    """,

    # ── 操作溯源（主人明确要求：「看明白你干了什么」）──
    # 导出的操作类 md 会在末尾带一个折叠块展示这些信息。
    "provenance": """
        CREATE TABLE IF NOT EXISTS provenance (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          target_title TEXT,         -- 作用于哪条笔记/事实
          action TEXT,               -- create / update / export / delete
          reason TEXT,               -- 为什么做
          trigger TEXT,              -- who: 主人要求 / AI 主动
          at TEXT
        )
    """,

    # ── M1 多用户与知识库归属骨架 ──
    # 本步只建结构：现有 web_auth.json 账号和单库 notes 暂不迁移，避免
    # 「表建好了」被误当成「权限已经生效」。业务接入另走迁移与回归。
    "users": """
        CREATE TABLE IF NOT EXISTS users (
          user_id TEXT PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'disabled')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """,
    "vaults": """
        CREATE TABLE IF NOT EXISTS vaults (
          vault_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          owner_user_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
        )
    """,
    "vault_members": """
        CREATE TABLE IF NOT EXISTS vault_members (
          vault_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
          -- 阶段 6（2026-10-23）：这个成员能看到的最高笔记等级（S/A/B/C）。
          -- 空串表示「按角色默认」：owner→S / editor→A / viewer→B，
          -- 管理员可以在管理页逐人调高调低，这就是「可修改能不能看重要的」。
          max_grade TEXT NOT NULL DEFAULT '',
          invited_by TEXT,
          created_at TEXT NOT NULL,
          revoked_at TEXT,
          PRIMARY KEY (vault_id, user_id),
          FOREIGN KEY (vault_id) REFERENCES vaults(vault_id),
          FOREIGN KEY (user_id) REFERENCES users(user_id),
          FOREIGN KEY (invited_by) REFERENCES users(user_id)
        )
    """,
}

# 全文索引的建表语句（2026-11-16 起不再自动执行，只在 --create-fts 时用）。
# 用 contentless_delete 而不是默认模式：默认模式会为每行再存一份原文
# （notes_fts_content 那 3.41MB 就是这么来的），而这里只做 MATCH + rowid 关联，
# 不读它的列值。官方文档：contentless-delete 是新代码的推荐姿势（3.43+）。
_FTS_SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
      rel_path UNINDEXED,
      title,
      tokens,
      tokenize='ascii',
      content='',
      contentless_delete=1
    )
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_links_src ON links(src)",
    "CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst)",
    "CREATE INDEX IF NOT EXISTS idx_notes_dir ON notes(dir)",
    "CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated)",
    "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_atoms_kind ON atoms(kind)",
    "CREATE INDEX IF NOT EXISTS idx_facts_subj ON facts(subject)",
    "CREATE INDEX IF NOT EXISTS idx_prov_target ON provenance(target_title)",
    "CREATE INDEX IF NOT EXISTS idx_vault_members_user ON vault_members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_vault_members_active ON vault_members(vault_id, revoked_at)",
]

# 迁移期给已有表补的列（列不存在时才加）。
#
# 2026-09-17 决定留空：`memory_web.build_index()` 是**全量重建**（DROP + CREATE notes），
# 任何加在 notes 上的新列都会在下次重建时被清掉 —— 加了也白加，反而制造
# 「明明加过却没了」的困惑。增量状态改由**导出侧**的状态文件管理
# （`md2fanout.py` 的 `.md2fanout.json` 记 sha256），db 侧不需要冗余列。
# 机制保留，将来若改为增量重建（UPSERT 而非 DROP）再启用。
ADD_COLUMNS: dict[str, list] = {}

# 本脚本"拥有"的表（--drop 只删这些，绝不碰 notes/links/meta）
OWNED_TABLES = [
    "conversations", "atoms", "facts", "schema_log", "provenance", "notes_fts",
    "users", "vaults", "vault_members",
]


# ────────────────────────── 工具 ──────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:  # 连接级调优（缓存/mmap/临时表），单一实现在 db_paths.tune
        import db_paths as _dbp
        _dbp.tune(conn)
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE (type='table' OR type='view') AND name=?",
        (name,),
    ).fetchone()
    return r is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


# ────────────────────────── 主流程 ──────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """建表 + 建索引 + 补列。返回动作统计。"""
    created, existed = [], []

    for name, ddl in SCHEMA.items():
        if _table_exists(conn, name):
            existed.append(name)
        else:
            conn.execute(ddl)
            created.append(name)

    for ddl in INDEXES:
        conn.execute(ddl)

    added_cols = []
    for table, cols in ADD_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        have = _columns(conn, table)
        for col, typ in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                added_cols.append(f"{table}.{col}")

    conn.commit()
    return {"created": created, "existed": existed, "added_columns": added_cols}


def try_vector_table(conn: sqlite3.Connection, dim: int = 384) -> tuple[bool, str]:
    """尝试创建 sqlite-vec 虚拟表。未安装扩展时返回 (False, 原因)，不抛错。"""
    try:
        conn.enable_load_extension(True)
        conn.load_extension("vec0")
    except Exception as e:
        return False, f"sqlite-vec 不可用（{type(e).__name__}）：{e}"
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec USING vec0("
            f"rel_path TEXT PRIMARY KEY, embedding float[{dim}])"
        )
        conn.commit()
        return True, f"已创建 notes_vec（{dim} 维）"
    except sqlite3.Error as e:
        return False, f"建表失败：{e}"


def fill_fts(conn: sqlite3.Connection) -> dict:
    """把 notes 的标题与正文按 CJK 2-gram 切词灌进 notes_fts。

    复用 memory_tools._tokenize_query，保证与检索侧**同一套分词口径** ——
    这是实测结论：口径不一致会导致「索引里有、却查不到」。
    """
    import memory_tools as mt

    t0 = time.perf_counter()
    if not _table_exists(conn, "notes_fts"):
        # 2026-11-16 起默认不建：这张表只被写、从没被读（生产代码零 MATCH），
        # 占过 12.23MB / 66% 的库体积。要重建显式加 `--create-fts`。
        return {
            "rows": 0,
            "skipped": True,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "note": "notes_fts 未建（默认跳过；要重建加 --create-fts）",
        }
    conn.execute("DELETE FROM notes_fts")
    n = 0
    for r in conn.execute("SELECT rel_path, title, body_md, summary, tags FROM notes"):
        title = r["title"] or ""
        body = r["body_md"] or ""
        # 标题与摘要权重更高，重复灌入让它们在 BM25 里更靠前
        text = f"{title} {title} {r['summary'] or ''} {r['tags'] or ''} {body}"
        toks = " ".join(mt._tokenize_query(text))
        conn.execute(
            "INSERT INTO notes_fts(rel_path, title, tokens) VALUES (?,?,?)",
            (r["rel_path"], title, toks),
        )
        n += 1
    conn.commit()
    return {"rows": n, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


def drop_owned(conn: sqlite3.Connection) -> list[str]:
    """只删本脚本拥有的表，绝不碰 notes / links / meta。"""
    dropped = []
    for t in OWNED_TABLES:
        if _table_exists(conn, t):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            dropped.append(t)
    conn.commit()
    return dropped


def show_stat(conn: sqlite3.Connection) -> None:
    print(f"数据库：{DB_PATH}")
    print(f"大小：{DB_PATH.stat().st_size / 1024:.1f} KB" if DB_PATH.exists() else "")
    print()
    print("表 / 行数：")
    for name in list(SCHEMA) + ["notes_vec"]:
        if not _table_exists(conn, name):
            continue
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.Error:
            n = "?"
        print(f"  {name:16s} {n}")
    print()
    cols = _columns(conn, "notes")
    print(f"notes 列数：{len(cols)}")
    extra = [c for c in cols if c in ("content_hash", "exported_at")]
    print(f"  迁移后新增列：{extra or '（无）'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库数据库表结构初始化（幂等）")
    # 注意：default 里不能引用 VAULT —— 模块级常量在函数内被 global 重新赋值，
    # 先读后 global 会触发 SyntaxError: name 'VAULT' is used prior to global
    # declaration（本库已在 md2fanout.py 与 db_init.py 各踩一次）。
    ap.add_argument("--vault", default=None, help="主库路径（默认取 AI_MEMORY_DIR）")
    ap.add_argument("--fill-fts", action="store_true", help="顺带把 notes 正文灌进全文索引（表不存在则跳过）")
    ap.add_argument("--create-fts", action="store_true", help="重建 notes_fts（2026-11-16 起默认不建；只在真要全文检索时用）")
    ap.add_argument("--with-vec", action="store_true", help="尝试创建向量表（需 sqlite-vec）")
    ap.add_argument("--stat", action="store_true", help="只看现状")
    ap.add_argument("--drop", action="store_true", help="危险：删除本脚本建的表")
    args = ap.parse_args()

    global VAULT, DB_PATH
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)
    DB_PATH = db_paths.resolve("index.sqlite3")

    conn = connect()
    try:
        if args.stat:
            show_stat(conn)
            return 0

        if args.drop:
            dropped = drop_owned(conn)
            print(f"已删除本脚本拥有的表：{dropped or '（无）'}")
            print("notes / links / meta 未受影响。")
            return 0

        info = ensure_schema(conn)
        print(f"数据库：{DB_PATH}")
        print(f"  新建表 {len(info['created'])}：{info['created'] or '（无）'}")
        print(f"  已存在 {len(info['existed'])}：{info['existed']}")
        print(f"  新增列：{info['added_columns'] or '（无）'}")

        if args.with_vec:
            ok, msg = try_vector_table(conn)
            print(f"  向量表：{msg}")

        if args.create_fts:
            conn.executescript(_FTS_SCHEMA)
            conn.commit()
            print("  notes_fts：已重建（contentless_delete 模式，不存原文副本）")

        if args.fill_fts:
            r = fill_fts(conn)
            if r.get("skipped"):
                print(f"  全文索引：跳过（{r.get('note', '')}）")
            else:
                print(f"  全文索引：{r['rows']} 条，耗时 {r['elapsed_ms']} ms")

        print()
        show_stat(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
