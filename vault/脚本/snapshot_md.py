#!/usr/bin/env python3
"""库 → md 快照（单向投影）。库是原件，本脚本只负责把它渲染成人可读的文本。

为什么要这个脚本
----------------
2026-10-20 起写入只写 `数据库/index.sqlite3`，md 文件仍然是全库唯一「可 diff、
可回溯」的载体：一篇一文件，改一行 → `git diff` 只显示那一行。库是二进制，
`git log -p` 对它没有任何意义。

所以 md 保留，但**只有一个来源**：本脚本。写入路径不再顺手渲染它 ——
两份数据各有写入方，是这四天所有 bug 的根源（投影漂移、排序不确定、
type 被重建推平、md 有库无）。

三种模式
--------
    snapshot_md.py                     导出：库 → 库内原路径的 md（覆盖）
    snapshot_md.py --verify            校验：渲染结果与磁盘逐字节对比，不写盘
    snapshot_md.py --restore           危险：md → 库（库丢了才用，会清空 notes）

`--restore` 的前提是**库已经不可用**。库还在的时候跑它等于用快照覆盖原件，
而快照可能比库旧 —— 所以它要求显式确认，且拒绝在库正常时静默执行。

逐字节口径
----------
文件形态固定为 `---\\n<frontmatter>\\n---\\n\\n<body>\\n`，正文末尾恰好一个换行。
不这样定死，快照会随渲染器版本漂移，`--verify` 就永远在报差异（实测 119 篇里
有 87 篇只差正文末尾这一个换行，看起来像 87 个 bug，其实是同一件事）。
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
sys.path.insert(0, str(VAULT))
sys.path.insert(0, str(VAULT / "脚本"))

import db_paths  # noqa: E402
import memory_store as ms  # noqa: E402


def render(meta: dict, body: str) -> str:
    """一条记录 → md 文本。**全库唯一的渲染口径**。"""
    fm = ms._dump_yaml_frontmatter(dict(meta))
    return f"---\n{fm}\n---\n\n{body.rstrip(chr(10))}\n"


def _rows() -> list[sqlite3.Row]:
    db = db_paths.index_db()
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT title, rel_path, type, scope, tier, source, verified, tags, summary, "
            "created, updated, version, access_count, body_md FROM notes ORDER BY rel_path"
        ).fetchall()
    finally:
        conn.close()


def _meta(row: sqlite3.Row) -> dict:
    tags = [t for t in str(row["tags"] or "").split(",") if t.strip()]
    meta = {
        "title": str(row["title"] or ""),
        "tags": tags,
        "type": str(row["type"] or ""),
        "scope": str(row["scope"] or ""),
        "tier": str(row["tier"] or "warm"),
        "verified": bool(row["verified"]),
        "created": str(row["created"] or ""),
        "updated": str(row["updated"] or ""),
        "version": int(row["version"] or 0),
        "access_count": int(row["access_count"] or 0),
    }
    if row["source"]:
        meta["source"] = str(row["source"])
    if row["summary"]:
        meta["summary"] = str(row["summary"])
    return meta


def export() -> int:
    rows = _rows()
    written = same = 0
    wanted: set[Path] = set()
    for row in rows:
        path = VAULT / str(row["rel_path"]).replace("\\", "/")
        wanted.add(path.resolve())
        text = render(_meta(row), str(row["body_md"] or ""))
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8") == text:
                    same += 1
                    continue
            except OSError:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        ms._atomic_write_text(path, text)
        written += 1

    stale = _stale_files(wanted)
    for p in stale:
        try:
            p.unlink()
        except OSError:
            pass
    print(f"快照导出：{len(rows)} 条笔记 / 重写 {written} / 未变 {same} / 清理陈旧 {len(stale)}")
    return 0


def _stale_files(wanted: set[Path]) -> list[Path]:
    """库里的笔记已被删除，但磁盘上还留着的 md（只清库管辖区内的）。"""
    out: list[Path] = []
    skip = set(ms.SYSTEM_DIR_NAMES) | {"数据库", "脚本", "规则", "备份归档"}
    for f in VAULT.rglob("*.md"):
        try:
            rel = f.relative_to(VAULT)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in skip:
            continue
        try:
            if f.resolve() not in wanted:
                out.append(f)
        except OSError:
            continue
    return out


def verify(verbose: bool = False) -> int:
    rows = _rows()
    bad: list[tuple[str, str]] = []
    for row in rows:
        rel = str(row["rel_path"]).replace("\\", "/")
        path = VAULT / rel
        if not path.is_file():
            bad.append((rel, "文件不存在"))
            continue
        expect = render(_meta(row), str(row["body_md"] or ""))
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError as e:
            bad.append((rel, f"读取失败 {e}"))
            continue
        if actual != expect:
            bad.append((rel, _first_diff(expect, actual)))
    stale = _stale_files({(VAULT / str(r["rel_path"]).replace("\\", "/")).resolve() for r in rows})

    print(f"快照校验：{len(rows)} 条 / 一致 {len(rows) - len(bad)} / 不一致 {len(bad)} / 陈旧文件 {len(stale)}")
    for rel, why in bad[:20 if verbose else 5]:
        print(f"  - {rel}  {why}")
    if len(bad) > 5 and not verbose:
        print(f"  （另有 {len(bad) - 5} 条，加 --verbose 看全）")
    for p in stale[:5]:
        print(f"  [陈旧] {p.relative_to(VAULT)}")
    return 0 if not bad and not stale else 1


def _first_diff(a: str, b: str) -> str:
    la, lb = a.splitlines(), b.splitlines()
    for i in range(min(len(la), len(lb))):
        if la[i] != lb[i]:
            return f"第 {i + 1} 行：快照={la[i][:60]!r} 磁盘={lb[i][:60]!r}"
    if len(la) == len(lb):
        # 行内容全同、字符串却不等 → 只可能是末尾换行的差别（splitlines 会吃掉它）。
        # 这一条必须单独说清楚：不然它会伪装成「行数相同但内容不同」的诡异 bug。
        return f"仅末尾换行不同（快照 {len(a)} 字节 / 磁盘 {len(b)} 字节）"
    return f"行数不同：快照 {len(la)} 行 / 磁盘 {len(lb)} 行"


def restore(force: bool) -> int:
    """md → 库。**库丢了才用**：会 DROP notes 再从磁盘 md 重建。"""
    if not force:
        print("拒绝执行：--restore 会用磁盘 md 覆盖整个 notes 表。")
        print("确认库已不可用、且 md 是最新快照后，加 --force 重跑。")
        return 2
    # 2026-10-23 分层后 build_index_from_md 在 web_store.py（memory_web 只剩 HTTP 入口）
    import web_store as _ws

    info = _ws.build_index_from_md(force=True)
    print(f"已从 md 重建：{info['notes']} 条 / {info['links']} 双链 / {info['elapsed_ms']} ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="库 → md 快照（单向投影）")
    ap.add_argument("--verify", action="store_true", help="只校验，不写盘")
    ap.add_argument("--restore", action="store_true", help="危险：md → 库")
    ap.add_argument("--force", action="store_true", help="配合 --restore 的二次确认")
    ap.add_argument("--verbose", action="store_true", help="打印全部差异")
    args = ap.parse_args()

    if args.restore:
        return restore(args.force)
    if args.verify:
        return verify(args.verbose)
    return export()


if __name__ == "__main__":
    raise SystemExit(main())
