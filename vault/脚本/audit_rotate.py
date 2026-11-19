#!/usr/bin/env python3
"""审计库轮转（阶段 11，2026-10-25）—— 把上一年的记录整体搬进归档文件。

## 为什么需要轮转

`数据库/audit.sqlite3` 是**只追加**的（库内两个触发器拦住 UPDATE / DELETE），
所以它只会变大：登录留痕上线之后，每条登录成功/失败/登出都记一行，多用户跑
一年是几万行量级。不轮转的话，「近 7 天」这类查询每次都要扫全表，页面越用越慢。

## 为什么和最初的设计不一样（重要）

`audit.py` 的 `archive_files()` 注释里写的是「跨年时**整文件改名**」。实际落地
改成了「VACUUM INTO 归档 + 主库重建表」，原因是 Windows：

- 改名要求**没有任何进程持有该文件的句柄**。线上 gate(8766) / mcp(8770) /
  graphite(8788) 三个进程都常驻写着这个库，SQLite 的连接在进程活着时不会释放
  —— `os.rename` 在 Windows 上直接 `PermissionError`，在 Linux 上能过。
- 而**删行也不行**：`audit_no_delete` 触发器让 DELETE 直接 ABORT，那是刻意的
  硬边界（能删日志的审计等于没有审计）。

所以走第三条路：**用 SQLite 的在线备份 API 把数据整份拷进归档文件，校验行数
一致后，再 DROP 表重建**。数据一行没少，只是换了个文件住；归档文件自己带着
同样的两个只追加触发器（`VACUUM INTO` 保留 schema）。整个过程不动主库文件的
句柄，三个进程照常跑。

**顺序不能反**：先建归档、校验通过，才允许清主库。反过来的话，一旦归档写入
失败，主机上那份唯一的记录就没了。

## 与跨文件查询的关系

轮转后旧记录住在 `audit_<年>.sqlite3`，`audit.archive_files()` 用 glob
`audit_*.sqlite3` 扫得到，`query()` / `stats()` / `action_counts()` 本来就
跨文件合并 —— 所以页面上「轮转前 100 条、轮转后还是 100 条」。这条是被
`脚本/test_stage11_ops.py` 实测钉住的，不是口头承诺。

## 用法

    <venv>/python 脚本/audit_rotate.py --list           # 看现有审计文件与时间跨度
    <venv>/python 脚本/audit_rotate.py --dry-run        # 演练：只报告会做什么
    <venv>/python 脚本/audit_rotate.py                  # 正式执行（默认归到上一年）
    <venv>/python 脚本/audit_rotate.py --year 2026      # 指定归档到哪一年
    <venv>/python 脚本/audit_rotate.py --force          # 库内跨了多年 / 归档已存在时仍继续

退出码：0 成功（或无需轮转）/ 1 失败 / 2 参数或环境问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# 代码根与数据根必须分开：`AI_MEMORY_DIR` 指「数据放哪」，而这个脚本自己住在
# 代码树里。把数据根当 sys.path 用，在临时目录演练时会 import 不到 audit
# —— 第一次实测就是这么挂的（ModuleNotFoundError: audit）。
CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))

try:  # Windows 控制台默认 GBK，打印中文会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 —— 重定向到文件时没有 reconfigure
    pass

import audit  # noqa: E402
import db_paths  # noqa: E402

DATA_ROOT = db_paths.vault_dir()
LOG_PATH = DATA_ROOT / "日志" / "审计轮转记录.jsonl"


# ── 工具 ───────────────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path) -> int:
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    try:
        return int(con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] or 0)
    finally:
        con.close()


def _span(path: Path) -> tuple[str, str]:
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    try:
        row = con.execute("SELECT MIN(ts), MAX(ts) FROM audit_log").fetchone()
        return (str(row[0] or ""), str(row[1] or ""))
    except sqlite3.Error:
        return ("", "")
    finally:
        con.close()


def _append_log(entry: dict) -> None:
    """轮转记录单独落 jsonl —— 写审计库会在轮转后被自己清掉，等于没记。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[warn] 轮转记录写不进 {LOG_PATH}：{e}", file=sys.stderr)


# ── 子命令 ─────────────────────────────────────────────────────────────────
def cmd_list() -> int:
    files = audit.archive_files()
    if not files:
        print("没有审计库文件。")
        return 0
    print(f"{'文件':<28}{'行数':>8}{'大小':>12}  时间跨度")
    for p in files:
        try:
            n = _rows(p)
            lo, hi = _span(p)
            size = f"{p.stat().st_size / 1024:.0f} KB"
        except (sqlite3.Error, OSError) as e:
            n, lo, hi, size = -1, "", "", f"读不出：{e}"
        mark = "（当前）" if p.name == db_paths.audit_db().name else ""
        print(f"{p.name:<28}{n:>8}{size:>12}  {lo or '—'} ~ {hi or '—'}{mark}")
    total = 0
    for p in files:
        try:
            total += _rows(p)
        except sqlite3.Error:
            pass
    print(f"\n合计 {total} 条（跨文件合并后的页面口径）")
    return 0


def cmd_rotate(year: int, dry_run: bool, force: bool) -> int:
    base = db_paths.audit_db()
    if not base.exists():
        print(f"审计库还不存在：{base}（没有可轮转的记录）")
        return 0

    folder = base.parent
    target = folder / f"audit_{year}.sqlite3"

    # ① 先摸清现状：行数与时间跨度
    rows = _rows(base)
    lo, hi = _span(base)
    years = sorted({s[:4] for s in (lo, hi) if s})

    print(f"当前库：{base.name}  行数 {rows}  跨度 {lo or '—'} ~ {hi or '—'}")
    if rows == 0:
        print("当前库是空的，无需轮转。")
        return 0

    # ② 三条前置校验，任何一条不过都不动手
    if target.exists():
        if not force:
            print(
                f"[拒绝] 归档文件已存在：{target.name}\n"
                f"       同一年轮转两次会把两份记录混在一个文件里。\n"
                f"       确认要覆盖请手工改名旧的归档，或加 --force（会覆盖该归档，风险自负）。",
                file=sys.stderr,
            )
            return 2
        print(f"[warn] --force：将覆盖已存在的归档 {target.name}")

    if len(years) > 1 and not force:
        print(
            f"[拒绝] 库内跨了多年：{lo} ~ {hi}（{','.join(years)}）。\n"
            f"       整库搬进 audit_{year}.sqlite3 会让年份归属说不清。\n"
            f"       要么先把记录按年分好（人工），要么加 --force 接受「全归 {year}」。",
            file=sys.stderr,
        )
        return 2

    this_year = datetime.now().year
    if year >= this_year and not force:
        print(
            f"[拒绝] {year} 年还没过完，当年记录仍在写。\n"
            f"       把当年整库搬走会让「今天」的历史和「昨天」割开，且下一条登录\n"
            f"       审计会立刻再建一个新库 —— 每天切一次没有意义。\n"
            f"       确认要切当年，加 --force。",
            file=sys.stderr,
        )
        return 2

    if dry_run:
        print(
            f"\n[dry-run] 会做这些事：\n"
            f"  1. checkpoint 主库 WAL（把未落盘的页先写进库）\n"
            f"  2. VACUUM INTO → {target.name}（{rows} 行）\n"
            f"  3. 校验：归档行数 == {rows}、触发器仍在\n"
            f"  4. DROP TABLE audit_log（主库）→ 按 audit._SCHEMA 重建空表\n"
            f"  5. 复验：跨文件查询总数仍为 {rows}\n"
            f"盘上没有任何改动。"
        )
        return 0

    # ③ 真正开始。tmp 后缀刻意不用 .sqlite3 —— 半成品不能被 archive_files() 的 glob 读到
    tmp = folder / f"audit_{year}.sqlite3.tmp"
    for stale in (tmp, folder / (tmp.name + "-journal"), folder / (tmp.name + "-wal")):
        if stale.exists():
            stale.unlink()

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src_sha = ""
    try:
        src_sha = _sha256(base)
        con = sqlite3.connect(base.as_posix(), timeout=15.0)
        try:
            # WAL 里可能有已提交未 checkpoint 的页；不 checkpoint 就 VACUUM INTO，
            # 拷出来的会缺最后几条 —— 而那恰好是「出事前最后那几秒」的记录。
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()

            if target.exists():
                target.unlink()
            con.execute("VACUUM INTO ?", (tmp.as_posix(),))
        finally:
            con.close()

        # ④ 归档文件必须自证是好的：行数一致 + 只追加触发器还在
        if not tmp.exists():
            raise RuntimeError("VACUUM INTO 没有产出文件")
        got = _rows(tmp)
        if got != rows:
            raise RuntimeError(f"归档行数不一致：期望 {rows}，实际 {got}")
        vcon = sqlite3.connect(tmp.as_posix(), timeout=5.0)
        try:
            trig = {r[0] for r in vcon.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            check = vcon.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            vcon.close()
        missing = {"audit_no_update", "audit_no_delete"} - trig
        if missing:
            raise RuntimeError(f"归档库缺触发器：{sorted(missing)}（归档必须同样是只追加）")
        if str(check).lower() != "ok":
            raise RuntimeError(f"归档库 quick_check 未通过：{check}")

        tmp.replace(target)  # 同一目录内原子改名
        arc_sha = _sha256(target)

        # ⑤ 到这里归档已落地且校验通过，才允许清主库
        con = sqlite3.connect(base.as_posix(), timeout=15.0)
        try:
            con.execute("DROP TABLE IF EXISTS audit_log")
            con.commit()
            con.executescript(audit._SCHEMA)  # 表 + 索引 + 两个只追加触发器
            con.commit()
        finally:
            con.close()

        # ⑥ 复验：跨文件口径必须和轮转前一模一样
        after = audit.query(page=1, page_size=1)
        if int(after.get("total") or 0) != rows:
            raise RuntimeError(
                f"轮转后跨文件总数不对：期望 {rows}，实际 {after.get('total')}。"
                f"归档文件 {target.name} 已保留，请人工核对后再清主库。"
            )

        entry = {
            "ts": stamp,
            "action": "rotate",
            "year": year,
            "rows": rows,
            "span": [lo, hi],
            "archive": target.name,
            "archive_sha256": arc_sha,
            "source_sha256": src_sha,
            "files_after": after.get("files"),
        }
        _append_log(entry)
        print(
            f"\n[OK] 轮转完成：{rows} 条 → {target.name}（{target.stat().st_size / 1024:.0f} KB）\n"
            f"     主库已重建为空表，跨文件查询复验 total={after.get('total')}（与轮转前一致）\n"
            f"     sha256={arc_sha[:16]}…  记录写入 {LOG_PATH.name}"
        )
        return 0
    except Exception as e:  # noqa: BLE001 —— 任何一步失败都要说清停在哪
        # 归档已改名成功、但清主库/复验失败：主库与归档都在，数据没丢，修完再跑一次
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"[失败] 轮转中止：{type(e).__name__}: {e}", file=sys.stderr)
        if target.exists():
            print(f"       归档文件 {target.name} 已生成且校验通过，主库未清空。", file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="审计库轮转（按年归档，不删行）")
    ap.add_argument("--list", action="store_true", help="列出审计库文件与时间跨度")
    ap.add_argument("--dry-run", action="store_true", help="演练，不动盘")
    ap.add_argument("--year", type=int, default=None, help="归档到哪一年（默认上一年）")
    ap.add_argument("--force", action="store_true", help="跳过三条前置校验（覆盖归档 / 跨年 / 切当年）")
    args = ap.parse_args()

    if args.list:
        return cmd_list()

    year = args.year if args.year else datetime.now().year - 1
    if year < 2000 or year > 2100:
        print(f"[拒绝] --year 不合理：{year}", file=sys.stderr)
        return 2
    return cmd_rotate(year, args.dry_run, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
