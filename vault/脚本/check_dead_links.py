#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_dead_links.py — 记忆库 wikilink 死链体检（2026-09-15 新增）。

为什么需要它
------------
本库同时存在两套「名字」：
  - MCP 的 frontmatter ``title``：人类可读，允许空格
  - 磁盘的 ``filename``：经 ``_safe_filename`` 消毒，空格等字符转下划线

而 Obsidian 的 ``[[wikilink]]`` 是**按文件名解析**的（不认 frontmatter title）。
一旦某处用 title 拼链、而该 title 含空格，链接就变成死链；在 Obsidian 里点它
还会以链接文本为名在**库根**新建一个 0 字节空笔记，该空壳又被扫描成一条「记忆」，
于是看起来像「同一条记忆出现两行」。

2026-09-15 实测踩到（根目录多出一个 0 字节文件 + 两个索引各多一行），修复后
把体检固化为本脚本，避免同类问题再次静默累积。

用法
----
    python 脚本/check_dead_links.py             # 全库体检，列出死链
    python 脚本/check_dead_links.py --quiet     # 只输出汇总行

退出码：0 = 无死链；1 = 存在死链（便于接入自动化）。
"""
from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import os
import re
import sys
import argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 与 memory_store.SYSTEM_DIR_NAMES 保持一致：这些目录下的 md 不参与索引
SKIP_DIRS = {".git", "__pycache__", ".trash", ".obsidian", ".workbuddy",
             ".archive", ".trash_backup", "备份归档"}

# [[target]] / [[target|alias]] / [[target#heading]]
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")

# 文件开头的 YAML frontmatter
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    """剥掉文件开头的 YAML frontmatter。

    Obsidian 只在**正文**里解析 [[wikilink]]，frontmatter 里的方括号不会变成
    可点击链接；因此 summary 等字段里写 [[X]] 属合法写法，体检时必须跳过，
    否则会把它们误报成死链（2026-09-15 实测踩到：summary 里写了「拼 [[双链]]」，
    第 51 行反引号里的同名示例已正确剥离，残留的却是 frontmatter 那一处）。
    """
    return FRONTMATTER_RE.sub("", text, count=1)


def strip_code(text: str) -> str:
    """剥掉围栏代码块与行内代码，避免把语法示例里的 [[…]] 当成真链接。"""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    # 行内代码：反引号成对出现，支持 ``code`` 形式
    text = re.sub(r"``.+?``", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def collect(root: Path) -> tuple[list[Path], set[str]]:
    """返回 (全部笔记路径, 可解析目标集合=文件名 stem)。"""
    files: list[Path] = []
    targets: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            files.append(Path(dirpath) / fn)
            targets.add(fn[:-3])
    return files, targets


def find_dead(root: Path) -> tuple[int, list[tuple[str, str]], list[str]]:
    files, targets = collect(root)
    dead: list[tuple[str, str]] = []
    empties: list[str] = []
    total_links = 0
    for p in files:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.strip():
            empties.append(str(p.relative_to(root)))
            continue
        body = strip_code(strip_frontmatter(raw))
        for m in WIKI_LINK_RE.finditer(body):
            target = m.group(1).split("|")[0].split("#")[0].strip()
            if not target:
                continue
            total_links += 1
            if target not in targets:
                dead.append((str(p.relative_to(root)), m.group(1)))
    return total_links, dead, empties


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库 wikilink 死链体检")
    ap.add_argument("--root", default=os.environ.get(
        "AI_MEMORY_DIR", str(Path.home() / "Desktop" / "ai-memory")))
    ap.add_argument("--quiet", action="store_true", help="只输出汇总行")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print("找不到记忆库目录: %s" % root, file=sys.stderr)
        return 2

    total_links, dead, empties = find_dead(root)
    files, targets = collect(root)

    if not args.quiet:
        if empties:
            print("0 字节空文件（会被索引排除，但仍建议清理）:")
            for e in empties:
                print("   ", e)
            print()
        if dead:
            print("死链明细:")
            seen = set()
            for rel, raw in dead:
                if (rel, raw) in seen:
                    continue
                seen.add((rel, raw))
                print("   %s  ->  [[%s]]" % (rel, raw))
            print()

    print("md 文件=%d  可解析目标=%d  双链=%d  死链=%d  空文件=%d"
          % (len(files), len(targets), total_links, len(dead), len(empties)))
    if dead:
        print("提示：含空格的 frontmatter title 对应下划线文件名，"
              "引用这类笔记时必须用**文件名**而非 title。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
