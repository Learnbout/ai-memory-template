#!/usr/bin/env python3
"""草稿队列预检（只读探针）。

用途：两阶段写入（规范「规则_记忆写入格式规范」）的阶段二需要一个
「先看看有没有活」的廉价探针。宿主定时任务可先跑本脚本：

    输出 EMPTY   → 直接结束，不必唤醒 Agent 去做任何读写。
    输出 PENDING → 确实有待定稿草稿，再交给有 LLM 的一方去定稿。

队列就是草稿本身 —— 带 `draft` 标签的笔记即队列项，不存在额外的队列文件。

用法：
    <venv>/python.exe 脚本/check_draft_queue.py              # 人类可读
    <venv>/python.exe 脚本/check_draft_queue.py --json       # 机器可读
    <venv>/python.exe 脚本/check_draft_queue.py --stale-days 7

退出码：0 = 无待定稿草稿；1 = 有待定稿草稿；2 = 出错。

设计约束：只读。不写任何文件、不建索引、不调 LLM。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_CODE_ROOT = Path(__file__).resolve().parent.parent  # 代码根：脚本/ 的上一级，与数据位置无关
VAULT = Path(os.environ.get("AI_MEMORY_DIR", str(_CODE_ROOT))).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(_CODE_ROOT))
sys.path.insert(0, str(_CODE_ROOT / "脚本"))

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

DRAFT_TAG = "draft"
DEFAULT_STALE_DAYS = 14


def _age_days(stamp: object, now: datetime) -> int | None:
    """把 frontmatter 的时间戳换算成「已落 N 天」，失败返回 None。"""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def collect() -> list[dict]:
    """扫描全库，返回带 draft 标签的条目（按落草稿时间从早到晚）。"""
    import memory_store as ms

    ms._invalidate_cache()
    entries = ms._iter_entries()
    drafts: list[dict] = []
    for path, meta, _body in entries:
        tags = [str(t).lower() for t in ms._entry_tags(meta)]
        if DRAFT_TAG not in tags:
            continue
        drafts.append({
            "title": ms._entry_title(meta, path),
            "rel_path": str(path.relative_to(VAULT)).replace("\\", "/"),
            "created": str(meta.get("created") or ""),
            "updated": str(meta.get("updated") or ""),
        })
    return entries, drafts


def main() -> int:
    ap = argparse.ArgumentParser(description="草稿队列预检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help="超期提醒天数（默认 14）")
    args = ap.parse_args()

    t0 = time.perf_counter()
    try:
        entries, drafts = collect()
    except Exception as e:
        print(f"ERROR 扫描失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    now = datetime.now(timezone.utc)
    for d in drafts:
        d["age_days"] = _age_days(d.get("created") or d.get("updated"), now)
        d["stale"] = bool(d["age_days"] is not None and d["age_days"] >= args.stale_days)
    drafts.sort(key=lambda d: -(d["age_days"] if d["age_days"] is not None else 0))

    if args.json:
        print(json.dumps({
            "count": len(drafts),
            "scanned": len(entries),
            "elapsed_ms": elapsed_ms,
            "stale_days": args.stale_days,
            "drafts": drafts,
        }, ensure_ascii=False, indent=2))
    elif not drafts:
        print(f"EMPTY  全库 {len(entries)} 篇，无待定稿草稿。（扫描 {elapsed_ms} ms）")
    else:
        print(f"PENDING {len(drafts)} 篇待定稿（全库 {len(entries)} 篇，扫描 {elapsed_ms} ms）：")
        for d in drafts:
            age = f"已落 {d['age_days']} 天" if d["age_days"] is not None else "落草时间未知"
            mark = "  [超期]" if d["stale"] else ""
            print(f"  - {d['title']}  |  {d['rel_path']}  |  {age}{mark}")

    return 1 if drafts else 0


if __name__ == "__main__":
    raise SystemExit(main())
