"""阶段 6 数据迁移（2026-10-23）：给笔记挂归属（vault_id）与等级（grade）。

## 为什么单独一个脚本而不是塞进 db_init

`db_init` 是「从零建库」，本脚本是「往已有 142 行的库里补两列并算级」——
两件事的失败后果完全不同（后者失败要么丢级、要么把库改半截），
分开写才能单独回滚、单独重跑。

## 幂等

每一步都先查现状再动：列已存在就跳过、grade 已有值就不覆盖。重跑安全。

## 备份

第一步 `VACUUM INTO` 出一个完整快照到 `<库>/备份/stage6_<ts>_index.sqlite3`。
这不是可选项 —— 后面所有步骤出错都靠它回滚。

## 用法

    python 脚本/migrate_stage6.py --dry-run   # 只报告将要做什么
    python 脚本/migrate_stage6.py             # 真正执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VAULT))

import note_grade  # noqa: E402

DB = VAULT / "数据库" / "index.sqlite3"
DEFAULT_VAULT_ID = "v_72e8dda6ac90427b"
DEFAULT_ROLE_GRADE = {"owner": "S", "editor": "A", "viewer": "B"}


def cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}


def backup(dry: bool) -> Path | None:
    if dry:
        print("[备份] dry-run 跳过")
        return None
    out_dir = VAULT / "备份"
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / ("stage6_%s_index.sqlite3" % time.strftime("%Y%m%d_%H%M%S"))
    if dst.exists():
        dst.unlink()
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()
    print("[备份] %s（%.1f MB）" % (dst.name, dst.stat().st_size / 1048576))
    return dst


def add_columns(conn: sqlite3.Connection, dry: bool) -> list[str]:
    done = []
    have = cols(conn, "notes")
    todo = []
    if "vault_id" not in have:
        todo.append(("vault_id", "ALTER TABLE notes ADD COLUMN vault_id TEXT NOT NULL DEFAULT '%s'"
                     % DEFAULT_VAULT_ID))
    if "grade" not in have:
        todo.append(("grade", "ALTER TABLE notes ADD COLUMN grade TEXT NOT NULL DEFAULT '%s'"
                     % note_grade.DEFAULT_GRADE))
    for name, sql in todo:
        if dry:
            print("[列] 将要新增 notes.%s" % name)
            continue
        conn.execute(sql)
        done.append(name)
        print("[列] 已新增 notes.%s" % name)
    if not todo:
        print("[列] notes.vault_id / grade 都已在位，跳过")
    return done


def fill_grade(conn: sqlite3.Connection, dry: bool, force: bool = False) -> dict[str, int]:
    # dry-run 时列还没加，SELECT 里不能引用它 —— 用 '' 占位，让预演能跑到底。
    has_grade = "grade" in cols(conn, "notes")
    rows = conn.execute(
        "SELECT title, COALESCE(body_md,''), COALESCE(type,''), COALESCE(tags,''), "
        "%s FROM notes" % ("COALESCE(grade,'')" if has_grade else "''")
    ).fetchall()
    if force:
        # 2026-10-23 踩过的坑：ALTER TABLE ADD COLUMN 会先给**每一行**填上默认值
        # 'B'，于是「已有等级就不覆盖」的保护逻辑把全部 143 条当成「人工已定级」，
        # 一条都不判 —— 结果全库都是 B，A/C 静悄悄地消失了，还打印「已写入 0 条」。
        # 所以「刚加完列」这一种情况必须强制重判，不能信现有的值。
        rows = [(t, b, n, g, "") for (t, b, n, g, _cur) in rows]
    plan: list[tuple[str, str]] = []
    dist = {g: 0 for g in ("S", "A", "B", "C")}
    for title, body, ntype, tags, cur in rows:
        tag_list = [t for t in str(tags).split(",") if t.strip()]
        want = note_grade.grade_of(title, body, ntype, tag_list)
        if str(cur).upper() == want:
            dist[want] += 1          # 已经是这一级，不重写
            continue
        if str(cur).upper() and str(cur).upper() != want:
            # 已有明确等级但与规则不同 —— 尊重既有值（可能是人工改过的），只计数不改
            dist[str(cur).upper()] = dist.get(str(cur).upper(), 0) + 1
            continue
        plan.append((want, title))
        dist[want] += 1
    if dry:
        print("[分级] 将要写入 %d 条" % len(plan))
    else:
        conn.executemany("UPDATE notes SET grade=? WHERE title=?", plan)
        print("[分级] 已写入 %d 条" % len(plan))
    return dist


def fill_vault(conn: sqlite3.Connection, dry: bool) -> int:
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    if "vault_id" not in cols(conn, "notes"):
        # 预演阶段列还没加，全部行都算「待补」
        print("[归属] 列还没建，%d 条全部将补成 %s" % (total, DEFAULT_VAULT_ID))
        return total
    n = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE COALESCE(vault_id,'')=''"
    ).fetchone()[0]
    if dry:
        print("[归属] 有 %d 条笔记 vault_id 为空，将补成 %s" % (n, DEFAULT_VAULT_ID))
        return n
    if n:
        conn.execute("UPDATE notes SET vault_id=? WHERE COALESCE(vault_id,'')=''",
                     (DEFAULT_VAULT_ID,))
        print("[归属] 已补 %d 条 -> %s" % (n, DEFAULT_VAULT_ID))
    else:
        print("[归属] 全部已有 vault_id，跳过")
    return n


def add_member_grade(conn: sqlite3.Connection, dry: bool) -> None:
    have = [t for t in ("vault_members",)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (t,)).fetchone()]
    if not have:
        print("[成员] vault_members 不存在，跳过（db_init 会先建）")
        return
    if "max_grade" in cols(conn, "vault_members"):
        print("[成员] vault_members.max_grade 已在位，跳过")
        return
    if dry:
        print("[成员] 将新增 vault_members.max_grade")
        return
    conn.execute("ALTER TABLE vault_members ADD COLUMN max_grade TEXT NOT NULL DEFAULT ''")
    for role, grade in DEFAULT_ROLE_GRADE.items():
        conn.execute("UPDATE vault_members SET max_grade=? WHERE role=? AND max_grade=''",
                     (grade, role))
    print("[成员] 已新增 max_grade，并按角色回填 owner=S / editor=A / viewer=B")


def ensure_index(conn: sqlite3.Connection, dry: bool) -> None:
    sql = "CREATE INDEX IF NOT EXISTS idx_notes_vault_grade ON notes(vault_id, grade)"
    if dry:
        print("[索引] 将建 idx_notes_vault_grade")
        return
    conn.execute(sql)
    print("[索引] idx_notes_vault_grade 就位")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reclassify", action="store_true",
                    help="忽略现有 grade，按规则全部重判（人工改过等级时慎用）")
    args = ap.parse_args()
    dry = args.dry_run

    if not DB.is_file():
        print("库不存在：%s" % DB)
        return 2
    backup(dry)
    conn = sqlite3.connect(DB)
    try:
        added = add_columns(conn, dry)
        # 刚加上 grade 列时必须强制重判：ADD COLUMN 的默认值会把每一行先填成 B，
        # 不强制就等于「所有笔记都是 B」，而脚本还会乐观地打印「已写入 0 条」。
        force = args.reclassify or ("grade" in added) or (dry and "grade" not in cols(conn, "notes"))
        fill_vault(conn, dry)
        dist = fill_grade(conn, dry, force=force)
        add_member_grade(conn, dry)
        ensure_index(conn, dry)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("\n[分布] " + "  ".join("%s=%d" % (g, dist.get(g, 0)) for g in ("S", "A", "B", "C")))
    if not dry:
        conn = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
        try:
            print("[复核] grade 分布 " + str(dict(conn.execute(
                "SELECT grade, COUNT(*) FROM notes GROUP BY grade ORDER BY grade").fetchall())))
            print("[复核] vault 分布 " + str(dict(conn.execute(
                "SELECT vault_id, COUNT(*) FROM notes GROUP BY vault_id").fetchall())))
            empt = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE COALESCE(vault_id,'')='' OR COALESCE(grade,'')=''"
            ).fetchone()[0]
            print("[复核] 空值行数 %d（必须为 0）" % empt)
            if empt:
                return 1
            print("\nS 级清单（须人工复核是否真含凭据）：")
            for (t,) in conn.execute("SELECT title FROM notes WHERE grade='S' ORDER BY title"):
                print("  -", t)
        finally:
            conn.close()
    print("\n迁移完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
