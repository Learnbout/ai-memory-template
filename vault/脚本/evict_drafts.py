# -*- coding: utf-8 -*-
"""归并草稿的淘汰（append-only 变坟场的唯一解药）。

## 为什么必须有这一步

2026-11-18 的调研里有直接反例：某个个人 vault 攒了 933 行「教训」+ 396 份会话日志，
agent 照样重复犯同样的错。作者的结论是「记忆系统需要的不是更多记忆，而是淘汰策略」。
`merge_drafts.py` 只负责「把草稿变成可检索条目」，如果只增不减，半年后库里全是没人读的碎屑。

## 两条规则（只动机器生成的条目）

1. **无用且过期**：`access_count == 0` 且距 `updated` 超过 `KEEP_UNUSED_DAYS` 天 → 软删除
2. **总量上限**：归并条目超过 `MAX_KEEP` 篇时，从「零访问且最旧」开始删到上限为止

安全边界：
- **只处理 `草稿归并_` / `流水草稿_` 前缀的条目**，人手写的笔记永远不碰
- 一律走 `memory_delete(trash=True)`（进 `.trash`，可恢复），不做物理删除
- 默认 dry-run，`--apply` 才真删

用法：
  python evict_drafts.py            # 只报告
  python evict_drafts.py --apply    # 执行
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sys
from datetime import datetime, timezone

VAULT = os.environ.get('AI_MEMORY_DIR') or str(Path(__file__).resolve().parents[1])
if VAULT not in sys.path:
    sys.path.insert(0, VAULT)
os.environ.setdefault('AI_MEMORY_DIR', VAULT)
os.environ.setdefault('AI_MEMORY_READ_SOURCE', 'db')

PREFIXES = ('草稿归并_', '流水草稿_')

# 2026-11-18 鱼片裁决：「能提炼提炼，不能提炼就删除」「当场提炼，不要草稿」。
# 所以这两条阈值从「宽」收紧到「窄」——草稿类条目现在的定位是**临时检索索引**，
# 不是长期记忆：21 天没人读就该清，总量压到 40 篇以内。
# 为什么不是 0 天/0 篇：留一点缓冲，让「当天没来得及提炼」的草稿还有一次被翻到的机会。
# 真正的出口是 `merge_drafts.py` 之后的**当场提炼**（进 规则_/决策_/项目_ 正式笔记），
# 不是让它在库里养老。
KEEP_UNUSED_DAYS = 21
MAX_KEEP = 40


def parse_dt(s: str):
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--max-keep', type=int, default=MAX_KEEP)
    ap.add_argument('--keep-days', type=int, default=KEEP_UNUSED_DAYS)
    args = ap.parse_args()

    import memory_tools as mt

    where = " OR ".join("title LIKE '%s%%'" % p for p in PREFIXES)
    rows = mt.memory_sql(
        "SELECT title, IFNULL(access_count,0) AS reads, IFNULL(updated,'') AS upd "
        "FROM notes WHERE " + where, format='tsv')
    lines = [ln for ln in rows.strip().splitlines()[1:] if ln.strip()]
    items = []
    for ln in lines:
        parts = ln.split('\t')
        if len(parts) < 3:
            continue
        title, reads, upd = parts[0], parts[1], parts[2]
        if not title.startswith(PREFIXES):
            continue
        try:
            reads = int(reads)
        except ValueError:
            reads = 0
        items.append({'title': title, 'reads': reads, 'upd': upd, 'dt': parse_dt(upd)})

    now = datetime.now(timezone.utc)
    print('归并类条目 %d 篇（上限 %d，未读保留 %d 天）' % (len(items), args.max_keep, args.keep_days))
    if not items:
        print('没有可淘汰的条目')
        return 0

    def age_days(it):
        return (now - it['dt']).days if it['dt'] else 9999

    to_drop: list[dict] = []
    by_age = sorted(items, key=lambda it: age_days(it), reverse=True)

    # 规则 1
    for it in by_age:
        if it['reads'] == 0 and age_days(it) > args.keep_days:
            it['why'] = '未读 %d 天' % age_days(it)
            to_drop.append(it)

    # 规则 2（在规则 1 之后按剩余数量算）
    remaining = [it for it in items if it not in to_drop]
    if len(remaining) > args.max_keep:
        overflow = len(remaining) - args.max_keep
        zero = sorted([it for it in remaining if it['reads'] == 0], key=lambda it: age_days(it), reverse=True)
        for it in zero[:overflow]:
            it['why'] = '超出上限（%d > %d）' % (len(remaining), args.max_keep)
            to_drop.append(it)

    if not to_drop:
        print('无需淘汰：没有过期未读项，且数量未超上限')
        return 0

    print('拟淘汰 %d 篇：' % len(to_drop))
    for it in to_drop[:10]:
        print('  - %s（%s）' % (it['title'], it['why']))
    if len(to_drop) > 10:
        print('  …另 %d 篇' % (len(to_drop) - 10))

    if not args.apply:
        print('')
        print('[dry-run] 未删除。加 --apply 执行（一律进 .trash，可恢复）')
        return 0

    ok = 0
    for it in to_drop:
        try:
            r = mt.memory_delete(it['title'], trash=True)
            if '已软删除' in str(r) or '已删除' in str(r):
                ok += 1
        except Exception as exc:  # noqa: BLE001
            print('  异常 %s: %s' % (it['title'], exc))
    print('已淘汰 %d/%d 篇（进 .trash）' % (ok, len(to_drop)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
