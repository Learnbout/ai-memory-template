#!/usr/bin/env python3
"""一次性字段回填：把冻结字段集（schema_version/type/scope/verified）补进全库笔记。

背景：字段集于 2026-09-10 冻结（schema_version=1），但只有新写入的笔记才带新字段。
本脚本负责把存量笔记补齐，使全库 frontmatter 同构——这是「知识库数据库化-本地Web」
能直接按字段建表的前提。

verified 回填采用保守规则（宁缺勿滥）：
- 只有正文明确出现「实测/验证/验收 …… 通过/一致/生效」才置 true；
- 正文同时含「待重启/待验证/尚未/预期/预计」时不置 true（留着人工复核）；
- 其余保持 false。false 只表示"未表态"，不代表结论错误。

用法：
    python 脚本/backfill_schema.py --dry-run   # 只报告
    python 脚本/backfill_schema.py             # 实际写入
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))

VERIFIED_OK_RE = re.compile(r"(实测|验证|验收)[^。；\n]{0,15}(通过|一致|生效|成功)")
VERIFIED_NO_GO = ("待重启", "待验证", "待确认", "尚未验证", "未经", "预期", "预计")
# 索引页与时间线页面记录的是"过程与目录"，本身不是待验证的结论，不参与 verified 升级。
VERIFIED_SKIP_TYPES = {"index", "log"}
ZERO_ACCESS_MIN_DAYS = 30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写入")
    args = ap.parse_args()

    import memory_store as ms

    entries = ms._iter_entries()
    to_patch: list[dict] = []
    verified_new: list[str] = []
    zero_access_old: list[str] = []
    now = datetime.now(timezone.utc)

    for path, meta, body in entries:
        title = ms._entry_title(meta, path)
        missing = [
            field
            for field in ("schema_version", "type", "scope", "verified")
            if field not in meta or meta.get(field) in (None, "")
        ]
        # 推导值漂移：字段存在但与当前推导规则不一致（如推导规则更新后需纠正存量）。
        # 只报不静默改——写回会同时刷新 updated 时间戳，故记进 missing 一起提示。
        derived_type = ms._derive_type(title)
        derived_scope = ms._derive_scope(title, path)
        drift: list[str] = []
        if meta.get("type") and meta.get("type") != derived_type:
            drift.append(f"type {meta.get('type')}->{derived_type}")
        if meta.get("scope") and meta.get("scope") != derived_scope:
            drift.append(f"scope {meta.get('scope')}->{derived_scope}")
        # verified 是否值得升级为 true
        target_verified = meta.get("verified") if isinstance(meta.get("verified"), bool) else False
        entry_type = meta.get("type") or ms._derive_type(title)
        if entry_type in VERIFIED_SKIP_TYPES:
            # 索引页/时间线不是待验证的结论，即使正文出现「验收通过」也保持 false
            # （降级分支：首版只写了升级，导致它们被误标为已确认、污染统计口径）
            target_verified = False
        elif not target_verified:
            if VERIFIED_OK_RE.search(body) and not any(h in body for h in VERIFIED_NO_GO):
                target_verified = True

        readers = meta.get("access_count", 0)
        readers = readers if isinstance(readers, int) else 0
        age_days = ms._days_since_utc(meta.get("updated"))
        if readers == 0 and age_days >= ZERO_ACCESS_MIN_DAYS:
            zero_access_old.append(f"{title}（零访问，{age_days} 天未更新）")

        if missing or drift or target_verified is not (meta.get("verified") is True):
            to_patch.append(
                {
                    "path": path,
                    "meta": meta,
                    "body": body,
                    "title": title,
                    "missing": missing + drift,
                    "verified": target_verified,
                }
            )
            if target_verified and meta.get("verified") is not True:
                verified_new.append(title)

    print(f"扫描 {len(entries)} 条，需补齐 {len(to_patch)} 条；其中 verified 升为 true {len(verified_new)} 条")
    if args.dry_run:
        for item in to_patch[:10]:
            print(f"  - {item['title']}：缺 {','.join(item['missing']) or '—'}"
                  f"{'，verified→true' if item['verified'] else ''}")
        if len(to_patch) > 10:
            print(f"  ...（共 {len(to_patch)} 条）")
        print("\n[零访问且超 %d 天未更新] %d 条" % (ZERO_ACCESS_MIN_DAYS, len(zero_access_old)))
        for line in zero_access_old[:20]:
            print(f"  - {line}")
        return 0

    patched = 0
    for item in to_patch:
        meta = item["meta"]
        ms._write_memory(
            item["path"],
            title=item["title"],
            tags=ms._entry_tags(meta),
            source=meta.get("source"),
            content=item["body"],
            created=str(meta.get("created")) if meta.get("created") else None,
            summary=str(meta.get("summary")) if meta.get("summary") else None,
            tier=meta.get("tier", "warm"),
            access_count=meta.get("access_count", 0) if isinstance(meta.get("access_count"), int) else 0,
            version=meta.get("version") if isinstance(meta.get("version"), int) else None,
            verified=item["verified"],
        )
        patched += 1

    ms._invalidate_cache()
    ms._refresh_index()
    ms._refresh_routing_index()

    print(f"已回填 {patched} 条笔记。")
    print(f"verified 升级为 true：{len(verified_new)} 条")
    for title in verified_new:
        print(f"  - {title}")
    print("\n[零访问且超 %d 天未更新] %d 条（归档候选）" % (ZERO_ACCESS_MIN_DAYS, len(zero_access_old)))
    for line in zero_access_old[:20]:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
