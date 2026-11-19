# -*- coding: utf-8 -*-
"""脚本使用计数管理：查看排行 / 手工登记 / 刷回 脚本索引.md。

用法：
    python 脚本/script_usage.py                 打印使用排行
    python 脚本/script_usage.py touch <脚本名>   手工 +1（埋点失效时的兜底）
    python 脚本/script_usage.py --apply         把计数刷回 脚本索引.md（缺列则补列）
    python 脚本/script_usage.py reset           清零（谨慎）

计数由各脚本顶部的 `from _usage import autotrack; autotrack(__file__)` 自动累加，
落在 `<vault>/.workbuddy/script_usage.json`。本脚本（script_usage.py）刻意不埋点：
它要处理「手工 touch 别人」的场景，自我计数会让统计失真。
"""

from __future__ import annotations

import io
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _usage import load, record, save, usage_path  # noqa: E402

VAULT = Path(__file__).resolve().parent.parent
INDEX = VAULT / "脚本索引.md"
SECTION = "## 可复用脚本"
COL_USAGE = "使用次数"
COL_LAST = "最近使用"


def read_text(path: Path) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def append_cell(line: str, value: str) -> str:
    """给表格行追加一个单元格（行首尾的 `|` 保持不变）。"""
    parts = line.split("|")
    if parts and parts[-1].strip() == "":
        parts.insert(len(parts) - 1, value)
    else:
        parts.append(value)
    return "|".join(parts)


def cmd_report() -> int:
    data = load()
    if not data:
        print("暂无计数。计数文件：%s" % usage_path())
        print("提示：脚本顶部会自动埋点，也可用 touch <脚本名> 手工登记。")
        return 0
    rows = sorted(data.items(), key=lambda kv: (-int(kv[1].get("count", 0) or 0), kv[0]))
    total = sum(int(r.get("count", 0) or 0) for _, r in rows)
    print("脚本使用排行（共 %d 次，%d 个脚本）：" % (total, len(rows)))
    for name, row in rows:
        print("  %-24s %4d 次   最近使用 %s" % (
            name, int(row.get("count", 0) or 0), str(row.get("last_used") or "-")[:10]))
    return 0


def cmd_touch(name: str) -> int:
    if not record(name):
        print("登记失败：%s" % name)
        return 1
    data = load()
    print("已登记 %s，累计 %d 次" % (name, int((data.get(name) or {}).get("count", 0) or 0)))
    return 0


def cmd_reset() -> int:
    save({})
    print("计数已清零。")
    return 0


def cmd_apply() -> int:
    """把计数刷回 脚本索引.md：缺「使用次数」列则补列，然后填值 + 刷新最近使用。"""
    if not INDEX.exists():
        print("找不到 %s" % INDEX)
        return 1
    text = read_text(INDEX)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)

    header_i = sep_i = None
    data_is: list[int] = []
    in_section = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("## "):
            in_section = s.startswith(SECTION)
            continue
        if not in_section or not s.startswith("|"):
            continue
        if header_i is None:
            header_i = i
        elif sep_i is None:
            sep_i = i
        else:
            data_is.append(i)

    if header_i is None or sep_i is None:
        print("未在「%s」节找到表格" % SECTION)
        return 1

    data = load()
    added = COL_USAGE not in lines[header_i]
    if added:
        lines[header_i] = append_cell(lines[header_i].rstrip(), " %s " % COL_USAGE)
        lines[sep_i] = append_cell(lines[sep_i].rstrip(), " --- ")
        for i in data_is:
            lines[i] = append_cell(lines[i], " 0 ")

    header_parts = lines[header_i].split("|")
    usage_idx = next((j for j, p in enumerate(header_parts) if p.strip() == COL_USAGE), None)
    last_idx = next((j for j, p in enumerate(header_parts) if p.strip() == COL_LAST), None)
    if usage_idx is None:
        print("补列失败")
        return 1

    # 备份进 `备份归档/` 并带时间戳（2026-09-16 规范：`.bak` 快照不铺在库根 / 笔记同目录）
    bakdir = VAULT / "备份归档"
    bakdir.mkdir(exist_ok=True)
    shutil.copy2(INDEX, str(bakdir / ("脚本索引.md.bak-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))))

    filled = 0
    for i in data_is:
        parts = lines[i].split("|")
        if len(parts) <= usage_idx:
            continue
        name = parts[1].strip().strip("`")
        row = data.get(name)
        parts[usage_idx] = " %d " % int((row or {}).get("count", 0) or 0)
        # 计数文件里**没有**该脚本的记录时，保留原有的「最近使用」单元格不动。
        # 否则会把 2026-09-10~09-15 的手工登记值刷成 `-`，属于静默的信息退化
        # （2026-09-16 首版实测踩到，已从 `备份归档/脚本索引.md.bak-script-usage` 还原）。
        if row and last_idx is not None and last_idx < len(parts):
            last = str(row.get("last_used") or "")
            parts[last_idx] = " %s " % (last[:10] if last else "-")
        lines[i] = "|".join(parts)
        filled += 1

    # frontmatter 同步（updated / version），与库内其它笔记口径一致
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    for i, ln in enumerate(lines):
        if ln.startswith("updated: "):
            lines[i] = 'updated: "%s"' % now
            break
    for i, ln in enumerate(lines):
        if ln.startswith("version: "):
            lines[i] = "version: %d" % (int(ln[len("version: "):].strip()) + 1)
            break

    write_text(INDEX, nl.join(lines))
    print("「使用次数」列：%s，已刷 %d 行（旧版备份进 备份归档/）"
          % ("本次新增" if added else "已存在", filled))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return cmd_report()
    cmd = argv[0]
    if cmd == "--apply":
        return cmd_apply()
    if cmd == "touch":
        if len(argv) < 2:
            print("用法：script_usage.py touch <脚本名>")
            return 2
        return cmd_touch(argv[1])
    if cmd == "reset":
        return cmd_reset()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print("未知参数：%s（用 --help 看用法）" % cmd)
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
