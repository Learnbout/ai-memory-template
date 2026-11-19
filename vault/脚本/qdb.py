#!/usr/bin/env python3
"""只读查库 CLI —— memory_sql 工具的脚本形态（给运维、排错与命令行使用）。

与 MCP 工具 `memory_sql` **共用同一套护栏实现**（`memory_tools.run_readonly_sql`）：
两处各写一遍必然漂移，而护栏漂移一次就是安全洞。本文件只负责 CLI 层
（参数解析、--list / --schema 这类便利命令），核心查询一律转发给库根的实现。

## 三道护栏（实现见 memory_tools.py）

1. **只读连接** —— `file:...?mode=ro`，SQLite 在**引擎层面物理拒绝**写操作。
   实测：绕过上层 SQL 检查直接写，仍得到 `attempt to write a readonly database`。
2. **只允许单条 SELECT / WITH** —— 双保险，且防一次跑多条语句。
3. **超时 + 行数上限** —— 防全表扫卡死、防一次拉回十万行。

## 用法

    python 脚本/qdb.py "SELECT title, updated FROM notes ORDER BY updated DESC LIMIT 5"
    python 脚本/qdb.py "SELECT summary FROM notes WHERE title='项目状态'"      # 只要摘要
    python 脚本/qdb.py "SELECT COUNT(*) FROM notes WHERE body_md LIKE '%热重载%'"  # 只要计数
    python 脚本/qdb.py --list                      # 有哪些表，各多少行
    python 脚本/qdb.py --schema notes              # 某张表的列定义
    python 脚本/qdb.py --max-rows 100 "SELECT ..."

## 什么时候用脚本、什么时候用 MCP 工具

    只读查库   → 两边都行，能力完全一致（同一套实现）
    跨宿主复用 → MCP 工具（Codex / Claude Code 也接同一个服务）
    排错与运维 → 脚本（能单独跑、看得到退出码）
"""

from __future__ import annotations

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import os
import sqlite3
import sys
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))          # 让本脚本能 import 库根的 memory_tools
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）
sys.path.insert(0, str(VAULT / "脚本"))


def _mt():
    """延迟导入 memory_tools（它会 import memory_store，需要上面的 sys.path 生效）。"""
    import memory_tools
    return memory_tools


def cmd_list(db: Path) -> str:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        lines = [f"{'表名':30s} {'行数':>8s}", "-" * 40]
        for r in rows:
            name = r[0]
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                n = "-"
            lines.append(f"{name:30s} {str(n):>8s}")
        return "\n".join(lines)
    finally:
        conn.close()


def cmd_schema(db: Path, table: str) -> str:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(f'PRAGMA table_info("{table}")'))
        if not rows:
            return f"表不存在或无列：{table}"
        lines = [f"{'列名':24s} {'类型':12s} {'非空':4s} {'主键':4s}", "-" * 50]
        for r in rows:
            lines.append(f"{r[1]:24s} {str(r[2] or ''):12s} "
                         f"{'是' if r[3] else '':4s} {'是' if r[5] else '':4s}")
        return "\n".join(lines)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="只读查库（与 memory_sql 共用护栏实现）")
    ap.add_argument("sql", nargs="?", help="单条 SELECT / WITH 查询")
    ap.add_argument("--list", action="store_true", help="列出所有表与行数")
    ap.add_argument("--schema", metavar="TABLE", help="查看某张表的列定义")
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--format", choices=["table", "tsv"], default="table")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
        os.environ["AI_MEMORY_DIR"] = str(VAULT)

    db = db_paths.resolve("index.sqlite3")
    if not db.exists():
        print(f"索引库不存在：{db}\n先跑 `脚本/sync_all.py` 重建索引。", file=sys.stderr)
        return 1

    if args.list:
        print(cmd_list(db))
        return 0
    if args.schema:
        print(cmd_schema(db, args.schema))
        return 0
    if not args.sql:
        ap.print_help()
        return 0

    # 核心查询转发给库根实现 —— 护栏单一来源
    try:
        out = _mt().run_readonly_sql(args.sql, max_rows=args.max_rows,
                                     max_chars=args.max_chars, fmt=args.format)
    except ValueError as e:
        print(f"拒绝执行：{e}", file=sys.stderr)
        return 2

    print(out)
    # 护栏拒绝/查询失败都以「拒绝执行」「查询失败」开头
    head = out.lstrip()[:6]
    return 1 if out.startswith(("查询失败", "索引库不存在")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
