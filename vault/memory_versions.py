#!/usr/bin/env python3
"""笔记版本历史：每一次 `memory_write` 落盘后的全文快照（T4，2026-10-19）。

## 为什么单独一个库文件，而不是塞进 index.sqlite3

**2026-10-21 订正**：本节原文写的是「`index.sqlite3` 是可删可重建的投影，可以从 md
全量重建」—— 那句在 **md 为源** 时代成立，2026-10-20 反转事实源之后反了：index 是
**本体**，md 是它的快照（从 md 重建会丢掉「已入库、md 未导出」的行）。原文保留在
git 历史里，这里说明它为什么不再适用。

现在的分工：

| 库 | 能否重建 | 备份方式 |
|---|---|---|
| `index.sqlite3` | 只能从**二进制快照**回滚，不能从 md 重建 | `backup_db.py`（本机，最近 7 天 + 每月首日） |
| `versions.sqlite3` | **完全不能** —— 历史只发生一次 | 本机快照 + `db_dump.py` 文本进 git |

两者仍然必须分开：本体会被整表重建（`build_index()` 就是 DROP + CREATE），版本历史
一旦被这种重建扫到就没了。混在一起，迟早被一次重建清空。

## 为什么不存 diff

存 diff 省空间，但要自己写 diff/patch 引擎，且任何一次算法改动都会让旧版本
读不出来。本库 119 篇、平均 ~7 KB 正文、每标题留 20 版，上限约 17 MB ——
用全量快照换「永远读得出来」，值。

## 为什么版本号从 md 的 frontmatter 取，而不是自己发号

`version` 字段是既有契约（乐观锁 `expected_version` 就靠它）。版本表若自己
发号，会出现「md 里写 version=7、版本表里记 5」的两套编号，迟早对不上。

## 失败策略

`record()` 永远返回 bool，**不抛异常**。版本记录是旁路能力：它坏了，
写入本身必须照常成功 —— 主链路不能被一个统计性质的功能拖垮。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent
# 2026-10-19：库文件收口到「数据库/」，位置解析统一走 db_paths（带旧位置回落）。
import db_paths  # noqa: E402

DB_PATH = db_paths.versions_db()

# 每个标题保留的版本数（2026-11-16 由 20 降到 5；主人当天拍板）
#
# 为什么能降：`get_version` / `list_versions` 在代码里**零调用** —— 版本库不是程序
# 读取路径，是「改坏笔记时人工回退」的安全网。而 20.85MB 里绝大多数是同一篇的
# 连续微调（`近期工作动态` 一篇就 20 版 / 530 万字符）。5 版足够照出「上一个干净
# 版本」，再多只是把同一篇的近似文本存五遍。
KEEP_VERSIONS = 5

# 大文件（正文 > 此字符数）只留 3 版：聚合页这类 20 万字符的笔记，一版就是 200KB，
# 留 5 版纯属把磁盘当草稿纸。3 版仍够「退一步、退两步」用。
BIG_NOTE_CHARS = 50_000
BIG_NOTE_KEEP = 3

# 正文压缩（2026-11-16）：body_md 存空串，真身放 body_z（zlib, level 6）。
# 实测 369 版 / 20.29MB UTF-8 -> 9.73MB，省 52%。读取统一走 `get_version`，
# 它对调用方透明（解压后回填 body_md）。旧行没 body_z 时回落读 body_md。
_ZLIB_LEVEL = 6

# 2026-10-21：测试与探针产生的版本一律不记 —— 版本库是「历史」，不是「测试日志」。
#
# 为什么必须挡在写入侧而不是事后清：10-19 到 10-21 三天攒了 76 条，其中 **52 条**是
# 测试产物（`smoke_test` 一家就 27 条：smoke_0~5、并发基线、限流探针0~7、并列编号
# 探针0~3），真实业务只有 24 条。比例 68%，而每跑一次 `smoke_test.py` 就再加 15 条。
#
# 两道判据缺一不可：
#   · source 黑名单 —— 挡 `smoke_test` 这类**标题看不出来**的（它建的笔记叫
#     「并发基线」「锁残留恢复」，跟真实笔记没区别）
#   · 标题前缀 —— 挡 `source` 不可靠的（探针脚本传的 source 五花八门：
#     probe / probe_del / t9 / selfcheck / manual / unknown 都出现过；而
#     `unknown` 里混着**真实笔记**「项目_Graphite笔记页目录手风琴」，所以
#     不能简单按 source 全删，只能靠标题兜底）
_SKIP_SOURCES = {"smoke_test", "probe", "probe_del", "probe_setfm", "selfcheck", "t9"}
_SKIP_TITLE_PREFIX = ("探针_", "_临时_", "smoke_")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    rel_path    TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    recorded_at TEXT    NOT NULL,
    chars       INTEGER NOT NULL,
    source      TEXT,
    note        TEXT,
    body_md     TEXT    NOT NULL,
    body_z      BLOB,
    UNIQUE(title, version)
);
CREATE INDEX IF NOT EXISTS idx_nv_title ON note_versions(title, version DESC);
CREATE INDEX IF NOT EXISTS idx_nv_time  ON note_versions(recorded_at DESC);
"""


def _ensure_body_z(conn: sqlite3.Connection) -> None:
    """老库（2026-11-16 之前建的）没有 body_z 列，就地补上。幂等。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(note_versions)")}
        if "body_z" not in cols:
            conn.execute("ALTER TABLE note_versions ADD COLUMN body_z BLOB")
            conn.commit()
            logger.info("VERSION-MIGRATE 已补 body_z 列")
    except Exception as exc:  # noqa: BLE001 —— 迁移失败不阻断写入，回落 body_md
        logger.warning("VERSION-MIGRATE-FAIL err=%s", exc)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:  # 连接级调优（缓存/mmap/临时表），单一实现在 db_paths.tune
        import db_paths as _dbp
        _dbp.tune(conn)
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    _ensure_body_z(conn)
    return conn


def record(
    title: str,
    rel_path: str,
    body_md: str,
    version: int,
    source: str | None = None,
    note: str | None = None,
) -> bool:
    """记一版。同 (title, version) 已存在时跳过（幂等：重试不会写重复）。

    测试 / 探针产物直接返回 False，不入库（判据见 `_SKIP_SOURCES` 的注释）。
    """
    if not title or not isinstance(version, int) or version <= 0:
        return False
    if (source or "").strip().lower() in _SKIP_SOURCES:
        return False
    if str(title).startswith(_SKIP_TITLE_PREFIX):
        return False
    try:
        conn = _connect()
        try:
            raw = body_md or ""
            packed = zlib.compress(raw.encode("utf-8"), _ZLIB_LEVEL) if raw else b""
            row = (
                title,
                rel_path.replace("\\", "/"),
                version,
                time.strftime("%Y-%m-%dT%H:%M:%S"),
                len(raw),
                source or "",
                note or "",
                "",
                packed,
            )
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO note_versions "
                    "(title, rel_path, version, recorded_at, chars, source, note, body_md, body_z) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    row,
                )
            except sqlite3.OperationalError:
                # 极老库 / 迁移失败：回落存明文，行为与改造前一致
                conn.execute(
                    "INSERT OR IGNORE INTO note_versions "
                    "(title, rel_path, version, recorded_at, chars, source, note, body_md) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    row[:8] + (raw,),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 —— 版本记录失败绝不影响写入
        logger.warning("VERSION-RECORD-FAIL title=%s v=%s err=%s", title, version, exc)
        return False


def list_versions(title: str, limit: int = KEEP_VERSIONS) -> list[dict[str, Any]]:
    """列某标题的版本（新→旧），不含正文。"""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT version, recorded_at, chars, source, note, rel_path "
                "FROM note_versions WHERE title=? ORDER BY version DESC LIMIT ?",
                (title, max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("VERSION-LIST-FAIL title=%s err=%s", title, exc)
        return []


def get_version(title: str, version: int) -> dict[str, Any] | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM note_versions WHERE title=? AND version=?",
                (title, int(version)),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            # 压缩存储（2026-11-16）：body_md 为空串时真身在 body_z，解压回填，
            # 让调用方拿到的形状与改造前完全一致。
            if not d.get("body_md") and d.get("body_z"):
                try:
                    d["body_md"] = zlib.decompress(d["body_z"]).decode("utf-8")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("VERSION-UNZIP-FAIL title=%s v=%s err=%s", title, version, exc)
            return d
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("VERSION-GET-FAIL title=%s v=%s err=%s", title, version, exc)
        return None


def prune(title: str, keep: int | None = None) -> int:
    """只留最近 keep 版，返回删除条数。每个标题一个独立清理域 —— 不会被别的标题挤掉。

    keep 不传时按内容体量自动选：普通笔记 `KEEP_VERSIONS`（5），
    大文件（>`BIG_NOTE_CHARS`）只留 `BIG_NOTE_KEEP`（3）。
    """
    try:
        conn = _connect()
        try:
            if keep is None:
                row = conn.execute(
                    "SELECT MAX(chars) FROM note_versions WHERE title=?", (title,)
                ).fetchone()
                biggest = int(row[0] or 0) if row else 0
                keep = BIG_NOTE_KEEP if biggest > BIG_NOTE_CHARS else KEEP_VERSIONS
            keep = max(1, int(keep))
            cur = conn.execute(
                "DELETE FROM note_versions WHERE title=? AND version NOT IN "
                "(SELECT version FROM note_versions WHERE title=? ORDER BY version DESC LIMIT ?)",
                (title, title, keep),
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("VERSION-PRUNE-FAIL title=%s err=%s", title, exc)
        return 0


def stats() -> dict[str, Any]:
    """给看板/校验用：总版本数、覆盖标题数、库体积。"""
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT title) AS t FROM note_versions").fetchone()
            return {
                "versions": int(rows["n"]),
                "titles": int(rows["t"]),
                "bytes": os.path.getsize(DB_PATH) if DB_PATH.is_file() else 0,
            }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"versions": 0, "titles": 0, "bytes": 0, "error": str(exc)}
