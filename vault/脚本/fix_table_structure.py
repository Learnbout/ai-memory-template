#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表格结构体检与修复（2026-09-17）。

背景
----
`项目状态.md` 的表头行与分隔线之间被插入了一行数据，导致 CommonMark
判定「表头 + 分隔线」结构不成立，整张表退化为纯文本。同类事故已是第三次
（09-15 空行 / 09-17 行列），故本次同时提供「体检」与「修复」。

判据
----
合法表格 = 表头行 `| ... |` 的下一行必须是分隔线 `|---|`。
本脚本扫描全库 md，报出三类问题：
  A. 表头后紧跟非分隔线行（错位，本次事故形态）
  B. 分隔线之前有数据行被夹住（同 A 的另一视角，用于定位需移动的行）
  C. 引用块后紧贴非空行（lazy continuation 风险，09-15 形态）

用法
----
  python fix_table_structure.py            # 只体检，打印报告
  python fix_table_structure.py --fix      # 体检并就地修复（自动备份）
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".obsidian", ".trash", "__pycache__", ".workbuddy", "备份归档"}

# 表头/数据行：以 | 开头且以 | 结尾（允许行尾空格）
ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# 分隔线：单元格内只含 - : 空格
SEP_CELL_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def iter_md_files() -> list[Path]:
    out: list[Path] = []
    for p in sorted(VAULT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def is_separator(line: str) -> bool:
    return bool(SEP_CELL_RE.match(line)) and "-" in line


def is_row(line: str) -> bool:
    return bool(ROW_RE.match(line))


def scan(text: str) -> list[dict]:
    """返回问题列表。每个问题含 kind / line_no(1-based) / detail。"""
    lines = text.split("\n")
    issues: list[dict] = []

    for i, ln in enumerate(lines):
        if not is_row(ln):
            continue
        # 只看「疑似表头」：有下一行且下一行也是行
        if i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        if not is_row(nxt):
            continue
        # 表头候选：本身是行，下一行是行，但下一行不是分隔线
        # 且第三行是分隔线 —— 这就是错位形态
        if not is_separator(nxt):
            if i + 2 < len(lines) and is_separator(lines[i + 2]):
                issues.append({
                    "kind": "A",
                    "line_no": i + 2,
                    "detail": f"表头在第 {i+1} 行，分隔线却跑到第 {i+3} 行，"
                              f"中间第 {i+2} 行是数据行（被夹住）",
                })
        # C：引用块后紧贴非空行
    prev_quote = False
    for i, ln in enumerate(lines):
        is_quote = ln.lstrip().startswith(">")
        if prev_quote and not is_quote and ln.strip():
            issues.append({
                "kind": "C",
                "line_no": i + 1,
                "detail": f"第 {i+1} 行紧贴引用块、缺空行（lazy continuation 风险）",
            })
        prev_quote = is_quote
    return issues


def fix_misplaced(text: str) -> tuple[str, int]:
    """把夹在表头与分隔线之间的数据行移到分隔线之后。返回 (新文本, 移动行数)。"""
    lines = text.split("\n")
    moved = 0
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if (is_row(ln) and i + 2 < len(lines) and is_row(lines[i + 1])
                and not is_separator(lines[i + 1]) and is_separator(lines[i + 2])):
            # 形态：header, data, sep
            header, data, sep = ln, lines[i + 1], lines[i + 2]
            out.append(header)
            out.append(sep)
            out.append(data)
            moved += 1
            i += 3
            continue
        out.append(ln)
        i += 1
    return "\n".join(out), moved


def fix_quote_spacing(text: str) -> tuple[str, int]:
    """引用块后补空行。"""
    lines = text.split("\n")
    out: list[str] = []
    prev_quote = False
    fixed = 0
    for ln in lines:
        is_quote = ln.lstrip().startswith(">")
        if prev_quote and not is_quote and ln.strip():
            out.append("")
            fixed += 1
        out.append(ln)
        prev_quote = is_quote
    return "\n".join(out), fixed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="就地修复并备份")
    args = ap.parse_args()

    # 库为源（2026-10-20）之后 `--fix` 会把修复结果写进**快照**：
    # 库行不动（搜索/读库看不到修复），下一次 sync_all 导出还会原样覆盖回去。
    # 检测模式照旧可用（它只读文件、只报告），修复请走 memory_write 重写正文，
    # 或改库内行后跑 snapshot_md 重导。
    if args.fix:
        import os
        import sys

        sys.path.insert(0, str(VAULT))
        os.environ.setdefault("AI_MEMORY_DIR", str(VAULT))
        try:
            import memory_store as _ms

            if _ms._db_mode():
                print(
                    "拒绝执行 --fix：当前是库为源模式，改 md 只是改快照，导出时会被覆盖。\n"
                    "  要修表格请用 memory_write 重写该篇正文（写入走库），"
                    "或改库内行后跑 脚本/snapshot_md.py 重导快照。"
                )
                return 2
        except Exception as exc:  # noqa: BLE001 —— 判不出来就别拦，交给人工判断
            print(f"（库模式判定失败，继续按文件模式执行：{exc}）")

    files = iter_md_files()
    print(f"扫描 {len(files)} 个 md 文件\n")

    total_a = total_c = 0
    changed: list[Path] = []

    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  [读取失败] {p.name}: {e}")
            continue
        issues = scan(text)
        a_list = [x for x in issues if x["kind"] == "A"]
        c_list = [x for x in issues if x["kind"] == "C"]
        if a_list:
            total_a += len(a_list)
            print(f"[A 错位] {p.relative_to(VAULT)}")
            for x in a_list:
                print(f"    第 {x['line_no']} 行: {x['detail']}")
        if c_list:
            total_c += len(c_list)
            print(f"[C 空行] {p.relative_to(VAULT)} × {len(c_list)} 处")

        if args.fix and (a_list or c_list):
            new_text, moved = fix_misplaced(text)
            new_text, padded = fix_quote_spacing(new_text)
            if new_text != text:
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                bak_dir = VAULT / "备份归档"
                bak_dir.mkdir(exist_ok=True)
                shutil.copy2(p, bak_dir / f"{p.name}.bak-{ts}-表格结构修复前")
                # newline="\n" 必给：默认 None 在 Windows 会把 LF 静默翻成 CRLF
                p.write_text(new_text, encoding="utf-8", newline="\n")
                changed.append(p)
                print(f"    → 已修复（移行 {moved}、补空行 {padded}），备份至 备份归档/")

    print(f"\n汇总：A 类错位 {total_a} 处、C 类空行 {total_c} 处")
    if args.fix:
        print(f"已修复文件 {len(changed)} 个")
    else:
        print("（未改动任何文件；加 --fix 执行修复）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
