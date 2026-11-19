#!/usr/bin/env python3
"""检索日志存量回填（只读改写 .workbuddy/检索日志.jsonl）。

背景（2026-09-20）：
`_log_search` 的 `bench` 字段是 2026-09-14（E2）才加上的，此前的旧记录该字段为
null；`bench_memory.py` 统计时用 `if rec.get("bench"): continue` 排除合成样本，
null 是 falsy —— 旧合成样本被算进「零结果率」这个唯一判据里，把真实值污染高
（实测 270 条里 133 条字段缺失）。

同时发现：`bench=True` 从未有一条落盘。根因是 `mt.LOG_AS_BENCH = True` 只在
bench 脚本自身进程里生效，而检索日志由常驻的 MCP 进程写入 —— 标记跨不过进程
边界。故本脚本改用**查询词结构判定**（不依赖任何进程内状态），并把判定结果写
成 `bench: true` + `bench_src: "backfill:probe-lexicon"`，可审计、可复算。

判定口径（保守，只看「明显不可能有人真去搜」的形态）：
1. 含随机串特征：`zzz` / `xyzzy` / 纯小写字母数字混排且长度 >= 8
2. 含生僻字探针：鼩鼱 / 龘 / 齉 等
3. 命中 < 0.5% 的 query 且是历史探针词表成员（见 PROBE_LEXICON）

用法：
    python 脚本/backfill_search_log.py --dry-run   # 只看会改哪些，不落盘
    python 脚本/backfill_search_log.py             # 实际回填（自动备份）
"""

from __future__ import annotations

try:  # 脚本使用计数
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
LOG_PATH = VAULT / ".workbuddy" / "检索日志.jsonl"

# 历史探针词表：这些 query 是过去 bench / 手工验证时造出来的，不可能是真实需求
PROBE_LEXICON = {
    "不存在的词xyzzy",
    "zzz绝无此词zzz",
    "qxzwvu9173",
    "HYJSXY",
    "鼩鼱龘齉",
    "完全不存在 热重载",
    "REFUSAL",
    "MemPalace",       # 外部产品名，本库无此主题，属验证「查不到」的探针
    "claude-mem",      # 同上
    "UU远程",           # 同上
    "GameViewer",      # 同上
}

# 纯小写字母数字混排、长度 >= 8 且无空格 —— 典型随机串。
# 注意必须**同时含数字**：纯小写单词（supervisor / headroom / mempalace）是真实
# 技术名词，曾被本规则误判 6 条（2026-09-20 试跑暴露），已加数字约束修正。
RANDOM_RE = re.compile(r"^(?=[a-z0-9]*\d)[a-z0-9]{8,}$")


def is_probe(query: str) -> tuple[bool, str]:
    """返回 (是否探针, 判定依据)。"""
    q = query.strip()
    if q in PROBE_LEXICON:
        return True, "lexicon"
    low = q.lower()
    if "xyzzy" in low or "zzz" in low:
        return True, "lexicon:placeholder"
    # 生僻字探针：Unicode 扩展区或罕见字
    if any(ch in q for ch in "鼩鼱龘齉"):
        return True, "lexicon:rare-char"
    if " " not in q and RANDOM_RE.match(q):
        return True, "pattern:random-string"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不落盘")
    args = ap.parse_args()

    if not LOG_PATH.exists():
        print(f"[x] 找不到检索日志：{LOG_PATH}")
        return 1

    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    records = []
    bad = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            records.append(json.loads(s))
        except json.JSONDecodeError:
            bad += 1
            records.append(None)  # 原样保留坏行

    total = len([r for r in records if r is not None])
    missing = [r for r in records if r is not None and "bench" not in r]
    already_true = [r for r in records if r is not None and r.get("bench") is True]

    newly = []
    for r in records:
        if r is None:
            continue
        # 只回填缺失标记的旧记录；已显式标记的不动
        if "bench" in r and r["bench"] is not None and "bench_src" in r:
            continue
        hit, why = is_probe(r.get("query", ""))
        if hit and not r.get("bench"):
            r["bench"] = True
            r["bench_src"] = f"backfill:probe-lexicon:{why}"
            newly.append(r)

    print(f"日志路径   : {LOG_PATH}")
    print(f"总记录     : {total} 条（坏行 {bad}）")
    print(f"字段缺失   : {len(missing)} 条（2026-09-14 之前的旧记录）")
    print(f"已是 bench : {len(already_true)} 条")
    print(f"本次新判为 : {len(newly)} 条")
    if newly:
        print()
        print("新判明细：")
        for k, v in Counter(r["query"] for r in newly).most_common():
            print(f"  {v:3d}  {k}")

    # 重算真实零结果率
    real = [r for r in records if r is not None and not r.get("bench")]
    zeros = [r for r in real if r.get("hits") == 0]
    rate = round(len(zeros) / len(real) * 100, 1) if real else None
    print()
    print(f"[回填后] 真实样本 {len(real)} 条，零结果 {len(zeros)} 条 → {rate}%")
    if rate is not None:
        if rate < 5:
            print("         判据：< 5% → 永久否决 FTS5")
        elif rate > 15:
            print("         判据：> 15% → 查失败样本是词形差异还是语义差异")
        else:
            print("         判据：5%~15% 灰区 → 保持子串+2gram，暂不引入 FTS5")

    if args.dry_run:
        print()
        print("[dry-run] 未落盘。去掉 --dry-run 即写入。")
        return 0

    if not newly:
        print()
        print("无需回填，未改动文件。")
        return 0

    backup = LOG_PATH.with_name(
        f"{LOG_PATH.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(LOG_PATH, backup)
    print()
    print(f"[备份] -> {backup.name}")

    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        for r in records:
            if r is None:
                continue
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[写入] -> {LOG_PATH}")
    print(f"[完成] 回填 {len(newly)} 条，备份在 {backup.name}（确认无误后可删）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
