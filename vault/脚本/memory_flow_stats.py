"""memory_flow_stats.py — 读「会话流水」算漏记率（只读，零依赖）

数据来源：<vault>/.workbuddy/会话流水.jsonl
    由 Stop hook `~/.workbuddy/hooks/ai_memory_stop.sh` 追加，每回合一行。
    字段：ts / session_id / cwd / tools / substantive_tools /
          memory_written / user_said_remember / transcript

判据（与 hook 内一致）：
    substantive_tools 非空 = 本回合有实质写类工具调用，才算「该写记忆」的分母
    漏记率 = 分母中 memory_written=false 的占比

用法：
    python memory_flow_stats.py                 # 全量
    python memory_flow_stats.py --days 7        # 只看最近 7 天
    python memory_flow_stats.py --vault <路径>   # 指定库
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import collections
import datetime
import json
import os
import sys
from pathlib import Path

DEFAULT_VAULT = os.environ.get("AI_MEMORY_DIR") or str(Path(__file__).resolve().parents[1])


def load(vault: Path, days: int | None) -> tuple[list[dict], int]:
    path = vault / ".workbuddy" / "会话流水.jsonl"
    if not path.is_file():
        return [], 0
    cutoff = None
    if days:
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)
    rows, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                skipped += 1
                continue
            if cutoff:
                try:
                    ts = datetime.datetime.fromisoformat(obj.get("ts", ""))
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
            rows.append(obj)
    return rows, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="会话流水统计（只读）")
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天，0=全量")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"库不存在：{vault}")
        return 1

    rows, skipped = load(vault, args.days or None)
    scope = f"最近 {args.days} 天" if args.days else "全量"
    print(f"库：{vault}")
    print(f"口径：{scope}")

    if not rows:
        print("样本：0 条 —— 流水还没有数据。")
        print("提示：确认 Stop hook 已生效（改完 ~/.workbuddy/hooks.json 需去 /hooks 面板审核），")
        print("      且 vault 存在、本回合确有工具调用。")
        return 0

    turn_total = len(rows)
    substantive = [r for r in rows if r.get("substantive_tools")]
    written = [r for r in substantive if r.get("memory_written")]
    missed = [r for r in substantive if not r.get("memory_written")]
    rate = (len(missed) / len(substantive) * 100) if substantive else 0.0

    print(f"样本：{turn_total} 回合"
          + (f"（另有 {skipped} 行无法解析，已跳过）" if skipped else ""))
    print("")
    print(f"  有实质工具调用（分母）: {len(substantive)}")
    print(f"    其中 已写记忆        : {len(written)}")
    print(f"    其中 漏记            : {len(missed)}")
    print(f"  漏记率                 : {rate:.1f}%")
    print(f"  分母占比               : {len(substantive) / turn_total * 100:.1f}%"
          "（其余为纯只读/闲聊回合，不计）")

    weak = [r for r in missed if r.get("user_said_remember")]
    print(f"  漏记中「用户明说记住」 : {len(weak)}"
          + ("  <-- 这类是硬伤，值得单独看" if weak else ""))

    tool_counter = collections.Counter(
        t for r in substantive for t in (r.get("substantive_tools") or [])
    )
    if tool_counter:
        print("")
        print("  实质工具频次 TOP：")
        for name, n in tool_counter.most_common(8):
            print(f"    {name:24} {n}")

    if rows:
        stamps = [r.get("ts", "") for r in rows if r.get("ts")]
        if stamps:
            print("")
            print(f"  时间范围：{min(stamps)} ~ {max(stamps)}")

    print("")
    print("提示：样本少于约 30 个「分母」时，漏记率还不稳，别据此下结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
