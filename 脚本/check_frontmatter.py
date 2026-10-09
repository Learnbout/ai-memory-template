#!/usr/bin/env python3
"""全库 frontmatter 字段体检：重复 / 非冻结 / 缺失 / 顺序错位。

为什么有这个脚本（2026-09-17 立）：
    同一天里，AI 连续两次用「手工 Edit」改聚合页 frontmatter，把新的 `updated`
    追加在 `summary` 之后，导致该字段**重复出现两次**（原字段还在文件顶部）。
    YAML 解析通常取最后一个值，所以表面看不出问题，但已违反「13 字段冻结集」
    的设计，且 `_normalize_meta` 之外的手工写入本身就是隐患。

    `check_dead_links.py` 管双链、`fix_table_structure.py` 管表格结构，
    本脚本补上第三块：**字段层**的结构校验。

本脚本只读，不改任何文件。退出码 0 = 全部正常，1 = 有问题。

用法：
    python 脚本/check_frontmatter.py
    python 脚本/check_frontmatter.py --quiet     # 只报结论
    python 脚本/check_frontmatter.py --root D:/other-vault
"""

from __future__ import annotations

try:  # 脚本使用计数（写 <vault>/.workbuddy/script_usage.json）
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.path.expanduser(os.environ.get("AI_MEMORY_DIR", "~/ai-memory"))).resolve()
os.environ.setdefault("AI_MEMORY_DIR", str(VAULT))

SKIP_DIRS = {".archive", ".trash", ".trash_backup", ".workbuddy", ".git", ".obsidian",
             "__pycache__", "备份归档"}

# 与 memory_store.FROZEN_FIELD_ORDER 保持一致；改那边必须同步这里
FROZEN_FIELD_ORDER = [
    "title", "tags", "created", "updated", "tier", "access_count",
    "source", "summary", "version", "type", "scope", "verified", "schema_version",
]

# 自动生成的索引页：字段集由生成器代码决定（记忆索引/路由索引/脚本索引各自不同），
# 本脚本不替它判断「缺失」与「顺序」，但**重复字段与非冻结字段仍要检查** ——
# 那两类是手工误写才会出现的，自动生成不可能产生。
AUTO_PAGES = {"记忆索引", "路由索引", "脚本索引"}

# 与 memory_store 的 summary 硬上限保持一致（改那边必须同步这里）
SUMMARY_LIMIT = 400

FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def split_frontmatter(text: str) -> str | None:
    """返回 frontmatter 正文（不含首尾 --- 行），没有则 None。"""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    return text[4:end]


def check_file(path: Path, vault: Path) -> list[str]:
    """返回该文件的问题列表（空列表 = 无问题）。"""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [f"读取失败: {type(e).__name__}"]

    fm = split_frontmatter(text)
    if fm is None:
        return ["缺 frontmatter（文件不是以 --- 开头或缺结束标记）"]

    keys: list[str] = []
    for line in fm.split("\n"):
        m = FIELD_RE.match(line)
        if m:
            keys.append(m.group(1))

    # 1. 重复字段（最高危：手工追加造成的静默覆盖）
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        for d in dupes:
            problems.append(f"重复字段 {d}（出现 {keys.count(d)} 次）")

    # 2. 非冻结字段
    unknown = [k for k in keys if k not in FROZEN_FIELD_ORDER]
    if unknown:
        problems.append("非冻结字段: " + ", ".join(unknown))

    # 3. summary 长度（手工 Edit 会绕过 memory_write 的 _clamp_summary 护栏，
    #    2026-09-17 实测三个聚合页 summary 因此涨到 412~419 字符）
    for line in fm.split("\n"):
        if line.startswith("summary:"):
            val = line[len("summary:"):].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            if len(val) > SUMMARY_LIMIT:
                problems.append(f"summary 超长: {len(val)} 字符（上限 {SUMMARY_LIMIT}）")
            break

    # 自动生成页到此为止（字段集由生成器决定，不判缺失与顺序）
    if path.stem in AUTO_PAGES:
        return problems

    # 4. 缺失字段
    missing = [k for k in FROZEN_FIELD_ORDER if k not in keys]
    if missing:
        problems.append("缺字段: " + ", ".join(missing))

    # 5. 顺序错位（只比对都存在且非重复的字段的相对顺序）
    present = [k for k in FROZEN_FIELD_ORDER if k in keys and keys.count(k) == 1]
    actual = [k for k in keys if k in present]
    if actual != present:
        problems.append(f"字段顺序不对（应为 {'/'.join(present[:4])}…）")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="全库 frontmatter 字段体检（只读）")
    ap.add_argument("--root", default=str(VAULT))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"库路径不存在：{root}")
        return 1

    files: list[Path] = []
    for f in sorted(root.rglob("*.md")):
        try:
            if any(part in SKIP_DIRS for part in f.relative_to(root).parts):
                continue
        except ValueError:
            continue
        try:
            if f.stat().st_size == 0:
                continue
        except OSError:
            continue
        files.append(f)

    bad: list[tuple[str, list[str]]] = []
    for f in files:
        probs = check_file(f, root)
        if probs:
            bad.append((f.relative_to(root).as_posix(), probs))

    print(f"扫描 {len(files)} 个 md 文件（跳过空文件与系统目录）")
    if not bad:
        print(f"\n全部正常：{len(FROZEN_FIELD_ORDER)} 字段无重复、无非冻结、无缺失、顺序正确")
        return 0

    print(f"\n发现 {len(bad)} 个文件有问题：\n")
    for rel, probs in bad:
        for p in probs:
            print(f"  [{p}]")
        print(f"      {rel}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
