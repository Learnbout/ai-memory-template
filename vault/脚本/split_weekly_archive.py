#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚合页窗口滚动 + 归档结构维护（2026-09-20 建立，2026-09-21 扩展）。

背景
----
`近期工作动态.md` 顶部写着「只保留最近 7 天，历史按月归档」，但**长期没有代码
在执行**。后果不只是文件变大：09-15 聚合页被整篇覆盖时，内容「既不在近期文件、
也没进归档」，等于真丢 —— 那句描述是**放大器**。

本脚本把整套归档结构收敛到一个命令：

    主页  近期工作动态.md                     ← 最近 N 天（默认 7）
    归档  日志/<年-月>/工作动态_<年>-W<周>.md   ← 更早的，按 ISO 周分组
    索引  日志/<年-月>/工作动态_<年-月>.md      ← 月索引，保住旧 wikilink

数据来源（**四路全部吸收后重排**，这是幂等的关键）
--------------------------------------------------
1. 主页的日区块
2. 月索引页里的日区块（2026-09-20 之前是完整月页，存量日区块要收）
3. **已存在的周文件里的日区块** —— 不吸收的话，重跑会把 W36/W37 的条目丢掉
4. 月页里的非日期段（如模板遗留的「## 开始使用」）原样保留进重建的月索引

设计约束
--------
- **搬运而非删除**：日区块整块移动，脚本内做**日区块数 + 条目数双守恒断言**，
  任一不等即中止且不落盘
- **平铺旧位置自动迁移**：`日志/工作动态_YYYY-MM.md` 会被迁入 `日志/YYYY-MM/`，
  原文件 move 到 `备份归档/`（内容已在月索引重建，不丢）
- 跨月的周（如 2026-W36 含 08-31）按「**周内多数天所属月**」归入
- 日期区块**最新在前**（库内约定）
- 系统提示：Obsidian 打开着对应笔记时不要跑（外部写入会被它重写回 padding）

用法
----
    <venv>/python.exe 脚本/split_weekly_archive.py --dry-run
    <venv>/python.exe 脚本/split_weekly_archive.py --apply
    <venv>/python.exe 脚本/split_weekly_archive.py --dry-run --today 2026-09-21
"""

from __future__ import annotations

try:  # 脚本使用计数
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import datetime as dt
import os
import re
import shutil
import sys
from collections import OrderedDict
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
HOME_PAGE = VAULT / "近期工作动态.md"
LOG_DIR = VAULT / "日志"
BACKUP_DIR = VAULT / "备份归档"
KEEP_DAYS = 7
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PAGE_RE = re.compile(r"^工作动态_(\d{4}-\d{2})\.md$")
WEEK_FILE_RE = re.compile(r"^工作动态_(\d{4}-W\d{2})\.md$")

# 本脚本自己生成的段落标题。它们会随重建重新生成，**不能**被当成"遗留内容"
# 再追加一遍（否则月索引会自己重复自己的「## 周归档」）。
GENERATED_SECTIONS = {"周归档", "跨月周指路", "月归档"}


def extract_extras(suffix: list[str]) -> list[str]:
    """从尾段里挑出**非生成段** —— 也就是真正要原样保留的外来内容。

    例：2026-08 月页末尾模板遗留的「## 开始使用」要留；而月索引自己的
    「## 周归档」、主页的「## 月归档」是本脚本生成的，不能重复追加。
    """
    out: list[str] = []
    keep = False
    for line in suffix:
        if line.startswith("## "):
            keep = line[3:].strip() not in GENERATED_SECTIONS
        if keep:
            out.append(line)
    return [l for l in out if l.strip()]


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def split_fm(text: str) -> tuple[str | None, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return None, text


def parse_blocks(body: str) -> tuple[list[str], OrderedDict, list[str]]:
    """把正文切成 (前言, {日期: 行列表}, 尾段)。

    「尾段」= 第一个非日期的 `## 标题` 及其后全部内容。本库里可能是「## 月归档」
    （主页）、「## 周归档」（月索引）、或模板遗留的「## 开始使用」（2026-08 月页）。
    """
    prefix: list[str] = []
    days: OrderedDict[str, list[str]] = OrderedDict()
    suffix: list[str] = []
    cur: str | None = None
    in_suffix = False
    for line in body.splitlines():
        if line.startswith("## "):
            head = line[3:].strip()
            if DATE_RE.match(head):
                cur = head
                days[cur] = []
                in_suffix = False
                continue
            in_suffix = True
            cur = None
            suffix.append(line)
            continue
        if in_suffix:
            suffix.append(line)
        elif cur:
            days[cur].append(line)
        else:
            prefix.append(line)
    return prefix, days, suffix


def count_entries(day_lines: list[str]) -> int:
    return sum(1 for line in day_lines if line.startswith("- ["))


def iso_of(date_str: str) -> tuple[int, int]:
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return y, w


def week_key(date_str: str) -> str:
    y, w = iso_of(date_str)
    return f"{y}-W{w:02d}"


def fm_field(fm_text: str | None, key: str) -> str | None:
    """从 frontmatter 文本里取一个字段的裸值（去引号）。取不到返回 None。"""
    if not fm_text:
        return None
    for line in fm_text.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip().strip('"')
    return None


def discover_month_pages() -> dict[str, Path]:
    """找出每个月的索引页路径。同月两处都有时，**优先已迁入文件夹的那个**。

    平铺位置 `日志/工作动态_YYYY-MM.md` 是 2026-09-20 之前的落点；
    迁入 `日志/YYYY-MM/工作动态_YYYY-MM.md` 后仍要能被发现（幂等要求）。
    """
    found: dict[str, Path] = {}
    if not LOG_DIR.is_dir():
        return found
    for p in sorted(LOG_DIR.glob("工作动态_*.md")):          # 平铺（旧位置）
        m = MONTH_PAGE_RE.match(p.name)
        if m:
            found[m.group(1)] = p
    for d in sorted(x for x in LOG_DIR.iterdir() if x.is_dir()):
        for p in sorted(d.glob("工作动态_*.md")):            # 月文件夹（新位置）
            m = MONTH_PAGE_RE.match(p.name)
            if m:
                found[m.group(1)] = p                        # 新位置覆盖旧位置
    return found


def discover_week_files() -> dict[str, Path]:
    """找出所有已存在的周文件，key 为 ISO 周号（如 2026-W36）。"""
    found: dict[str, Path] = {}
    if not LOG_DIR.is_dir():
        return found
    for d in sorted(x for x in LOG_DIR.iterdir() if x.is_dir()):
        for p in sorted(d.glob("工作动态_*.md")):
            m = WEEK_FILE_RE.match(p.name)
            if m:
                found[m.group(1)] = p
    for p in sorted(LOG_DIR.glob("工作动态_*.md")):          # 兼容平铺周文件
        m = WEEK_FILE_RE.match(p.name)
        if m:
            found.setdefault(m.group(1), p)
    return found


def render_week_file(wkey: str, dates: list[str], blocks: dict[str, list[str]],
                     folder_month: str, now_iso: str) -> str:
    """生成周文件的完整 md 文本。"""
    total = sum(count_entries(blocks[d]) for d in dates)
    span = f"{dates[0]} ~ {dates[-1]}" if len(dates) > 1 else dates[0]
    title = f"工作动态_{wkey}"
    summary = (
        f"{wkey} 周归档（{span}），共 {len(dates)} 个有记录的工作日 / {total} 条动态。"
        f"由聚合页窗口滚动自动生成，主页只保留最近 7 天。"
    )
    lines = [
        "---",
        f"title: {title}",
        "tags: [log, worklog, archive]",
        f'created: "{now_iso}"',
        f'updated: "{now_iso}"',
        "tier: warm",
        "access_count: 0",
        "source: workbuddy",
        f'summary: "{summary}"',
        "version: 1",
        "type: log",
        "scope: project",
        "verified: false",
        "schema_version: 1",
        "---",
        "",
        f"# {title}",
        "",
        f"> 索引：[[记忆索引]] ｜ 月归档：[[工作动态_{folder_month}]] ｜ 返回：[[近期工作动态]]",
        "",
        f"> 本周区间：{span}（ISO {wkey}）",
        "",
    ]
    for d in reversed(dates):
        lines.append(f"## {d}")
        lines.extend(blocks[d])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_month_index(month: str, weeks: OrderedDict, now_iso: str,
                       extra: list[str] | None = None,
                       all_weeks: OrderedDict | None = None,
                       meta: dict | None = None) -> str:
    """生成月索引页：列出该月各周文件。

    `extra` 是从原月页保留下来的非日期段（如模板遗留的「## 开始使用」），
    原样附在末尾 —— 内容只能搬家，不能因为"是模板样板"就悄悄丢掉。
    `all_weeks` 是全库周文件，用来给「跨月周」加指路行（防走失）。
    `meta` 是原页的 created / source / version，**必须继承**。重建时若把
    `created` 刷成"现在"就是静默丢历史（本脚本首版踩过，靠比对备份才发现）。
    """
    meta = meta or {}
    created = meta.get("created") or now_iso
    source = meta.get("source") or "workbuddy"
    try:
        version = int(meta.get("version") or 0) + 1
    except (TypeError, ValueError):
        version = 1
    rows = []
    for wkey, info in weeks.items():
        dates = info["dates"]
        span = f"{dates[0]} ~ {dates[-1]}" if len(dates) > 1 else dates[0]
        rows.append(f"- [[工作动态_{wkey}]]（{span}，{info['entries']} 条）")
    lines = [
        "---",
        f"title: 工作动态_{month}",
        "tags: [log, worklog, archive, index]",
        f'created: "{created}"',
        f'updated: "{now_iso}"',
        "tier: warm",
        "access_count: 0",
        f"source: {source}",
        f'summary: "{month} 工作动态月索引。按 ISO 周分档，指向该月各周归档文件；'
        f'本页由聚合页窗口滚动维护。"',
        f"version: {version}",
        "type: log",
        "scope: project",
        "verified: false",
        "schema_version: 1",
        "---",
        "",
        f"# 工作动态 {month}",
        "",
        "> 索引：[[记忆索引]] ｜ 返回：[[近期工作动态]]",
        "",
        "本页是该月的**周归档索引**。每日动态以「ISO 周」为单位存放于同月文件夹下，",
        "主页 [[近期工作动态]] 只保留最近 7 天。",
        "",
        "## 周归档",
        "",
        *rows,
        "",
    ]
    # 跨月周指路：某些周横跨两个月（如 2026-W36 = 08-31 ~ 09-06），按「多数天所属月」
    # 只落在一边。另一边必须显式指路，否则按月翻找会觉得「那天不见了」。
    if all_weeks:
        strays = []
        for wkey, info in all_weeks.items():
            if wkey in weeks:
                continue
            dates = info["dates"]
            if any(d[:7] == month for d in dates):
                span = f"{dates[0]} ~ {dates[-1]}"
                strays.append(f"- [[工作动态_{wkey}]]（{span}）")
        if strays:
            lines += [
                "## 跨月周指路",
                "",
                "下列周横跨本月与相邻月，按「周内多数天所属月」归档在**另一侧**，此处仅指路：",
                "",
                *strays,
                "",
            ]
    if extra:
        lines += [l.rstrip() for l in extra]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    mode.add_argument("--apply", action="store_true", help="执行（自动备份）")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument("--today", help="覆盖今天（YYYY-MM-DD），默认系统日期")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    cutoff = today - dt.timedelta(days=args.keep_days - 1)

    if not HOME_PAGE.exists():
        print(f"[x] 找不到主页：{HOME_PAGE}")
        return 1

    fm_home, body_home = split_fm(read_text(HOME_PAGE))
    prefix, days_home, suffix = parse_blocks(body_home)

    # ── 四路吸收 ────────────────────────────────────────────────────────
    month_pages = discover_month_pages()
    week_files = discover_week_files()

    days_month: OrderedDict[str, list[str]] = OrderedDict()
    month_extras: dict[str, list[str]] = {}
    month_meta: dict[str, dict] = {}
    for m, mp in month_pages.items():
        fm_m, body_m = split_fm(read_text(mp))
        month_meta[m] = {
            "created": fm_field(fm_m, "created"),
            "source": fm_field(fm_m, "source"),
            "version": fm_field(fm_m, "version"),
        }
        _, mp_days, mp_suffix = parse_blocks(body_m)
        for k, v in mp_days.items():
            days_month.setdefault(k, v)
        extra = extract_extras(mp_suffix)
        if extra:
            month_extras[m] = extra

    days_week: OrderedDict[str, list[str]] = OrderedDict()
    for wkey, wp in week_files.items():
        _, body_w = split_fm(read_text(wp))
        _, w_days, _ = parse_blocks(body_w)
        for k, v in w_days.items():
            days_week.setdefault(k, v)

    # 合并优先级：主页 > 周文件 > 月页（同日多处时取更"新鲜"的位置）
    all_days: OrderedDict[str, list[str]] = OrderedDict()
    for k in sorted(set(days_home) | set(days_week) | set(days_month)):
        all_days[k] = days_home.get(k) or days_week.get(k) or days_month.get(k) or []

    keep: OrderedDict[str, list[str]] = OrderedDict()
    arch: OrderedDict[str, list[str]] = OrderedDict()
    for k, v in all_days.items():
        (keep if dt.date.fromisoformat(k) >= cutoff else arch)[k] = v

    # 按 ISO 周分组 + 归月（周内多数天所属月）
    weeks: OrderedDict[str, dict] = OrderedDict()
    for k, v in arch.items():
        wkey = week_key(k)
        w = weeks.setdefault(wkey, {"dates": [], "blocks": {}, "entries": 0, "months": {}})
        w["dates"].append(k)
        w["blocks"][k] = v
        w["entries"] += count_entries(v)
        m = k[:7]
        w["months"][m] = w["months"].get(m, 0) + 1

    # ── 守恒断言 ──
    n_before = len(all_days)
    n_after = len(keep) + sum(len(w["dates"]) for w in weeks.values())
    e_before = sum(count_entries(v) for v in all_days.values())
    e_after = sum(count_entries(v) for v in keep.values()) + \
        sum(w["entries"] for w in weeks.values())
    print("=" * 64)
    print(f"今天 {today} · 窗口 {args.keep_days} 天 · 截止 {cutoff}")
    print(f"吸收来源：主页 {len(days_home)} 天 ｜ 周文件 {len(week_files)} 个"
          f"（{len(days_week)} 天）｜ 月页 {len(month_pages)} 个（{len(days_month)} 天）")
    print(f"主页保留 {len(keep)} 天 / 归档 {len(arch)} 天")
    print(f"守恒检查：日区块 {n_before} → {n_after} ｜ 条目 {e_before} → {e_after}")
    if n_before != n_after or e_before != e_after:
        print("[x] 守恒断言失败，中止（不落盘）")
        return 2
    print("[OK] 守恒通过")
    print("=" * 64)

    print("\n主页保留：")
    for d in reversed(list(keep)):
        print(f"  {d}  {count_entries(keep[d])} 条")

    print("\n归档计划：")
    plan = []
    for wkey, w in weeks.items():
        m = max(w["months"], key=lambda x: w["months"][x])
        path = LOG_DIR / m / f"工作动态_{wkey}.md"
        plan.append((path, wkey, w, m))
        print(f"  {path.relative_to(VAULT)}")
        print(f"      {w['dates'][0]} ~ {w['dates'][-1]}  {len(w['dates'])} 天  "
              f"{w['entries']} 条  [归月 {m}]")

    # 周 → 归属月（周内多数天所属月）。**必须按这个映射筛月索引的周档**，
    # 不能按 `wkey[:4]`（那是年份，会把所有周都算进每个月 —— 首版踩过）。
    week_month = {wkey: m for _, wkey, _, m in plan}
    weeks_for_index: OrderedDict = OrderedDict(
        (k, {"dates": v["dates"], "entries": v["entries"], "month": week_month[k]})
        for k, v in weeks.items()
    )
    months_all = sorted({m for _, _, _, m in plan} | set(month_pages) | set(month_extras))

    print("\n月索引（重建，迁入月文件夹）：")
    for m in months_all:
        idx = LOG_DIR / m / f"工作动态_{m}.md"
        sub = OrderedDict((k, v) for k, v in weeks_for_index.items() if v["month"] == m)
        extra_note = f" + 保留遗留段 {len(month_extras.get(m, []))} 行" if m in month_extras else ""
        print(f"  {idx.relative_to(VAULT)}   （本页周档 {len(sub)} 个{extra_note}）")

    # 需要迁移的平铺旧页
    to_migrate = [(m, p) for m, p in month_pages.items()
                  if p.parent == LOG_DIR]
    if to_migrate:
        print("\n平铺旧页迁移（原文件 move 到 备份归档/）：")
        for m, p in to_migrate:
            print(f"  {p.name}  ->  备份归档/")

    if args.dry_run:
        print("\n[dry-run] 未落盘。加 --apply 执行。")
        return 0

    # ── 落盘 ──
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(exist_ok=True)
    shutil.copy2(HOME_PAGE, BACKUP_DIR / f"近期工作动态.md.bak-{stamp}-周拆分前")
    for m, p in month_pages.items():
        shutil.copy2(p, BACKUP_DIR / f"{p.name}.bak-{stamp}-周拆分前")
    print(f"\n[备份] 已写入 备份归档/*.bak-{stamp}-周拆分前")

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    # 1) 写周文件
    for path, wkey, w, m in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_week_file(wkey, w["dates"], w["blocks"], m, now_iso),
                        encoding="utf-8")
        print(f"[写入] {path.relative_to(VAULT)}")

    # 2) 重建月索引（写入月文件夹）
    for m in months_all:
        folder = LOG_DIR / m
        folder.mkdir(parents=True, exist_ok=True)
        idx = folder / f"工作动态_{m}.md"
        sub = OrderedDict((k, v) for k, v in weeks_for_index.items() if v["month"] == m)
        idx.write_text(
            render_month_index(m, sub, now_iso, month_extras.get(m), weeks_for_index,
                               month_meta.get(m)),
            encoding="utf-8")
        print(f"[写入] {idx.relative_to(VAULT)}")

    # 3) 迁移平铺旧页（move 到备份归档，内容已在上面重建）
    for m, p in to_migrate:
        target = BACKUP_DIR / f"{p.name}.moved-{stamp}"
        shutil.move(str(p), str(target))
        print(f"[迁移] {p.name} -> {target.relative_to(VAULT)}")

    # 4) 重写主页（保留 prefix + keep + 新尾段）
    new_suffix = ["## 月归档", ""]
    for m in sorted(months_all, reverse=True):
        new_suffix.append(f"- [[工作动态_{m}]]（按周分档，见 `日志/{m}/`）")
    new_suffix.append("")

    body_new = "\n".join(prefix).rstrip() + "\n\n"
    for d in reversed(list(keep)):
        body_new += f"## {d}\n" + "\n".join(keep[d]).rstrip() + "\n\n"
    body_new += "\n".join(new_suffix)

    HOME_PAGE.write_text(f"---{fm_home}---{body_new}", encoding="utf-8")
    print(f"[写入] {HOME_PAGE.relative_to(VAULT)}")

    print(f"\n[完成] 主页 {len(keep)} 天 / 周文件 {len(plan)} 个 / 月索引 {len(months_all)} 个")
    print("[下一步] 跑 脚本/set_frontmatter.py 刷 frontmatter，再跑 sync_all.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
