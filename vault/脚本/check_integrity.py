#!/usr/bin/env python3
"""索引与源数据的完整性校验（T3，2026-10-19）。

为什么单独一个脚本、而不是塞进 check_frontmatter：
    `check_frontmatter` 管**格式**（13 字段冻结集、顺序、摘要长度）；
    本脚本管**一致性** —— 「磁盘上的 md」与「index.sqlite3 里的行」是不是同一份数据。
    db 成为数据源之后，最危险的故障不是文件损坏，而是**两边悄悄分叉**
    （写了一半、索引没重建、导出的库是旧的）。这一类故障不看校验看不出来。

六个检查项（C1~C6）—— **判据方向已于 2026-10-19 反转**，因为写路径切成了
「库为源、md 为投影」。反转前是「md 数 == notes 数，不等就重建索引」，而那正是
丢数据的动作：重建会 DROP notes，把「已入库、md 未导出」的行一起抹掉。

    C1 全集守恒      **库是全集**：库有而磁盘无 = 导出滞后（WARN）；
                     磁盘有而库无 = 未导入的投影（WARN，需人工确认，**不许自动重建**）
    C2 索引新鲜度    meta.built_at >= 库内最大 updated（容差）   WARN
    C3 字段非空      title / updated / type 空值数为 0            FAIL
    C4 正文完整      notes.body_md 为空的行数为 0                 FAIL
    C5 id 唯一       md5(relPath)[:7] 前缀冲突为 0                FAIL
    C6 双链可解析    links 表里有源无目标的悬空数（只报不判）      WARN
    C7 投影抽样      最近 3 篇的 title 与库一致；不一致=投影漂移   WARN

设计约束：
    - **不 import memory_store**。那个模块在导入期会惰性建库（09-12 踩过
      「导入即建库」的 footgun），校验脚本必须是纯只读的旁观者。
    - 目录白名单与 `memory_store.SYSTEM_DIR_NAMES` 保持同一份口径（这里复制
      常量而不是 import，理由同上）；两边不一致时以 memory_store 为准。
    - 退出码：0 = 全过（可带 WARN）；1 = 有 FAIL；2 = 脚本自身异常。

用法：
    python 脚本/check_integrity.py            # 人读
    python 脚本/check_integrity.py --json     # 机器读（给 CI / auto_push 用）
    python 脚本/check_integrity.py --quiet    # 只在有问题时输出
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:  # 脚本使用计数；失败绝不能影响脚本本身
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # noqa: BLE001
    pass

# `MEMORY_VAULT` 只给反向验证用（造一个隔离库，故意弄坏，断言脚本必须报 FAIL）。
# 不传就是本库 —— 日常使用不需要知道这个变量的存在。
VAULT = Path(os.environ.get("MEMORY_VAULT") or Path(__file__).resolve().parent.parent)
# 2026-10-19：库文件收口到「数据库/」。位置解析走 db_paths —— 它是**纯计算模块**
# （不建目录、不写盘），因此不破坏本脚本「不 import memory_store」的只读约束。
# 先把 MEMORY_VAULT 同步成 AI_MEMORY_DIR：db_paths 只认后者，不同步的话
# 反向验证会「用隔离库的 VAULT 去查主库的库文件」，测出来的全是对的，毫无意义。
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
import db_paths  # noqa: E402

INDEX_PATH = db_paths.resolve("index.sqlite3")

# 与 memory_store.SYSTEM_DIR_NAMES 同口径（刻意复制而非 import，见模块 docstring）
SKIP_DIRS = {
    ".archive", ".trash", ".workbuddy", ".git", ".obsidian",
    "__pycache__", ".trash_backup", "备份归档", "协同",
}

# 索引新鲜度容差：索引构建与文件写入在同一秒内是常事，60 秒足够宽松
STALE_TOLERANCE_SEC = 60.0


def _note_id(rel_path: str) -> str:
    """与 vault.py 完全相同的算法：n_ + md5('/' + relPath)[:7]。"""
    return "n_" + hashlib.md5(("/" + rel_path).encode("utf-8")).hexdigest()[:7]


def active_md_files() -> list[Path]:
    """磁盘上的活跃 md（跳过系统目录与 0 字节空壳）。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(VAULT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size == 0:
                    continue  # 空壳：Obsidian 点死链留下的，不进索引
            except OSError:
                continue
            out.append(p)
    return out


def _connect_ro() -> sqlite3.Connection:
    uri = "file:" + INDEX_PATH.as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def run_checks() -> dict:
    result: dict = {
        "vault": str(VAULT),
        "index": str(INDEX_PATH),
        "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
        "fails": 0,
        "warns": 0,
    }

    def add(code: str, name: str, ok: bool, detail: str, level: str = "FAIL") -> None:
        status = "PASS" if ok else level
        if not ok:
            if level == "FAIL":
                result["fails"] += 1
            else:
                result["warns"] += 1
        result["items"].append({"code": code, "name": name, "status": status, "detail": detail})

    files = active_md_files()
    disk_count = len(files)
    result["diskNotes"] = disk_count

    if not INDEX_PATH.is_file():
        add("C0", "索引库存在", False, f"缺少 {INDEX_PATH}（先跑 脚本/memory_web.py --rebuild-only）")
        return result

    conn = _connect_ro()
    try:
        db_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        result["dbNotes"] = db_count

        # C1 全集守恒（2026-10-19 反转）：库为源 —— notes 是全集，md 是投影。
        # 旧判据「磁盘数 == 库行数」在两边不一致时只说「需重建索引」，而重建就是
        # DROP notes 从 md 重灌 —— 恰好会抹掉「已入库、md 未导出」的笔记。
        # 现在改成分别报告两个方向的差集，且都只判 WARN（都要人来决定）。
        disk_rels = {p.relative_to(VAULT).as_posix() for p in files}
        db_rels = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM notes")}
        db_only = sorted(db_rels - disk_rels)
        md_only = sorted(disk_rels - db_rels)
        result["drift"] = {"db_only": db_only, "md_only": md_only}
        if not db_only and not md_only:
            add("C1", "全集守恒（库为源）", True,
                f"磁盘 {disk_count} 篇 / 库 {db_count} 行，一一对应")
        else:
            parts = []
            if db_only:
                parts.append(f"库有而磁盘无 {len(db_only)} 条（导出滞后，跑 sync_all 第④步即可；"
                             f"例：{', '.join(db_only[:3])}）")
            if md_only:
                parts.append(f"磁盘有而库无 {len(md_only)} 条（未导入的投影，需人工确认后在 Obsidian"
                             f"删掉或显式 --from-md 导入；例：{', '.join(md_only[:3])}）")
            add("C1", "全集守恒（库为源）", False, "；".join(parts), level="WARN")

        # C2 索引新鲜度（2026-10-19 反转）：比 **库内最大 updated**，不再比 md mtime。
        # md mtime 反映的是「投影被写的时间」，不是「库被写的时间」；拿它当判据，
        # 导出之后必然「md 更新」，永远误报落后。
        try:
            built_at = conn.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()[0]
            built_ts = time.mktime(time.strptime(built_at, "%Y-%m-%d %H:%M:%S"))
            newest_db = conn.execute("SELECT MAX(updated) FROM notes").fetchone()[0] or ""
            db_ts = datetime.fromisoformat(newest_db).timestamp() if newest_db else 0.0
            lag = db_ts - built_ts
            add(
                "C2", "索引新鲜度", lag <= STALE_TOLERANCE_SEC,
                f"built_at={built_at}，库内最新 updated 比它{'晚' if lag > 0 else '早'} {abs(lag):.0f}s"
                + ("" if lag <= STALE_TOLERANCE_SEC else "（有写入没刷 built_at）"),
                level="WARN",
            )
        except Exception as exc:  # noqa: BLE001
            add("C2", "索引新鲜度", False, f"读不到 built_at 或 updated：{exc}", level="WARN")

        # C3 字段非空
        empty_title = conn.execute("SELECT COUNT(*) FROM notes WHERE TRIM(COALESCE(title,''))=''").fetchone()[0]
        empty_updated = conn.execute("SELECT COUNT(*) FROM notes WHERE TRIM(COALESCE(updated,''))=''").fetchone()[0]
        empty_type = conn.execute("SELECT COUNT(*) FROM notes WHERE TRIM(COALESCE(type,''))=''").fetchone()[0]
        total_empty = empty_title + empty_updated + empty_type
        add(
            "C3", "字段非空", total_empty == 0,
            f"title 空 {empty_title} / updated 空 {empty_updated} / type 空 {empty_type}",
        )

        # C4 正文完整（body_md 为空 = 有行没正文，读出来就是空笔记）
        empty_body = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE LENGTH(COALESCE(body_md,''))=0"
        ).fetchone()[0]
        add("C4", "正文完整", empty_body == 0, f"body_md 为空的行：{empty_body}")

        # C5 id 唯一（7 位 md5 前缀碰撞虽罕见，但碰撞会让详情页取错笔记）
        ids = [_note_id(r["rel_path"]) for r in conn.execute("SELECT rel_path FROM notes")]
        dup = len(ids) - len(set(ids))
        add("C5", "id 唯一", dup == 0, f"{len(ids)} 个 id，重复 {dup}")

        # C6 双链可解析（只报数，不判失败 —— 悬空链接是正常的「还没写的笔记」）
        titles = {r["title"] for r in conn.execute("SELECT title FROM notes")}
        links = conn.execute("SELECT src, dst FROM links").fetchall()
        dangling = [r for r in links if r["dst"] not in titles]
        result["links"] = {"total": len(links), "dangling": len(dangling)}
        add(
            "C6", "双链可解析", True,
            f"共 {len(links)} 条链接，悬空 {len(dangling)} 条（悬空属正常：指向尚未创建的笔记）",
            level="WARN",
        )
    finally:
        conn.close()

    # C7 抽样比对：最近 3 篇的 title 与库是否一致。库为源之后，不一致的含义变了 ——
    # 不是「索引旧了」，而是**投影漂移**（md 被人手改过，或导出写了一半）。
    # 处理方向也随之反转：以库为准重新导出，**不要**反过来拿 md 改库。
    sample = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    mismatches = []
    conn = _connect_ro()
    try:
        for p in sample:
            rel = p.relative_to(VAULT).as_posix()
            row = conn.execute("SELECT title FROM notes WHERE rel_path=?", (rel,)).fetchone()
            if row is None:
                mismatches.append(f"{rel} 不在库里（未导入的投影）")
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            fm_title = ""
            if text.startswith("---"):
                for line in text.split("\n")[1:]:
                    if line.strip() == "---":
                        break
                    if line.startswith("title:"):
                        fm_title = line.split(":", 1)[1].strip().strip("\"'")
                        break
            expect = fm_title or p.stem
            if row["title"] != expect:
                mismatches.append(f"{rel}：库 title={row['title']!r}，投影={expect!r}")
    finally:
        conn.close()
    add(
        "C7", "投影抽样（最近 3 篇）", not mismatches,
        "最近 3 篇 title 与库一致"
        if not mismatches
        else "；".join(mismatches) + "（投影漂移：以库为准重导出，勿反向改库）",
        level="WARN",
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="索引与源数据完整性校验（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = ap.parse_args()

    try:
        result = run_checks()
    except Exception as exc:  # noqa: BLE001
        print(f"校验脚本自身异常：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        clean = result["fails"] == 0 and result["warns"] == 0
        if not (args.quiet and clean):
            print(f"完整性校验  {result['checkedAt']}")
            print(f"  磁盘 {result.get('diskNotes', 0)} 篇 / 索引 {result.get('dbNotes', 0)} 行")
            for it in result["items"]:
                mark = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL"}[it["status"]]
                print(f"  [{mark}] {it['code']} {it['name']:14s} {it['detail']}")
            print(f"\n结果：FAIL {result['fails']} / WARN {result['warns']}")

    return 1 if result["fails"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
