#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_decisions.py — 复查条件检查器（只读，零第三方依赖）。

为什么需要它
------------
本库的决策记录（`决策/决策_*.md`）每份都带一节「复查条件（任一触发即重新评估本决策）」——
这是决策的「保质期标签」：**写决策时就约定了「什么信号出现，就该把这条决定重想一遍」**。
问题是这节文字此前**没有任何东西去读它**：
  · 写决策时想得很全（几条量化阈值）；
  · 之后搁置，没人回头看 → 条件可能早已越线，决策还是老样子；
  · 主人想复查时得手工翻笔记、再手工算指标，麻烦到不如不查。

本脚本补的就是这段「读标签」的手：把每份决策的复查条件**原文**摆出来，
旁边挂上**当前可测指标的当前值**与**决策自己写的阈值**，越线打标记。

设计原则（重要）：**不判定，只摆数据。**
脚本不去说「这条决策该改了没」——那是主人的判断。它只做一件事：
把「条件原文 / 当前值 / 阈值」并排，让越线一眼可见。不推断、不给建议、不写任何文件（除 `--json`）。

指标来源
--------
| 指标 | 来源 | 只读？ |
| --- | --- | --- |
| 库篇数 | 扫活跃 md（排除系统目录） | 是 |
| 零结果率（全量 / 按 ISO 周） | `.workbuddy/检索日志.jsonl`（排除 bench 合成样本） | 是 |
| recall@1 / recall@5 / regressions | **最近一次** `bench_*.json` 的 `retrieval_qa`（**须为新代次量具**，见下；或 `--run-bench` 现场采集） | 是 |
| memory_write 同步耗时 | **无自动源** —— 标「需人工实测」 | — |

**为什么默认不跑 `bench_memory.py`**：那脚本会往库里写临时探针文件、并把合成查询写进检索日志
（不是纯只读）。本脚本定位是「随时可跑、零副作用」，故默认只读最近一次基准 JSON；
要拿真实 recall 时用 `--run-bench` 显式现场采集（明示会写库）。

**量具代次校验（关键，2026-10-13 踩到）**：bench JSON 里的 recall 只有「新代次量具」算出来才可信。
旧量具（2026-10-09 修正前）会把分流层的 `- [tier] **标题**` 行里的 tier 当标题 → recall 误报
（实测旧档 `bench_after_rankv2.json` 报 65%/75%，新代次真实 80%/95%）。指纹＝ `retrieval_qa`
是否含 `edge_case_missed` 字段（该字段正是修正时新增）。缺字段 → 标 [无数据]、**拒绝采信**，
而不是照读假值。若不校验，本脚本会拿旧档报出一个吓人的「recall 跌破 85%」假越线。

条件 → 指标 的绑定怎么加
------------------------
条件文本是自然语言、无法自动推断该挂哪个指标，故用表 `BINDINGS` 显式绑定：
键＝决策文件名里的关键词，值＝{条件序号: [指标代码]}。新写一份带复查条件的决策后，
**在这里补一条**；没绑定的条件会退化为「只列原文 + 标人工」，不会报错、不会瞎猜。

用法
----
    python 脚本/check_decisions.py                # 扫描库内 决策/
    python 脚本/check_decisions.py --vault <路径>
    python 脚本/check_decisions.py --json out.json
    python 脚本/check_decisions.py --weeks 4      # 零结果率按周分桶只看最近 N 周

退出码：0 = 无越线；1 = 有越线条件；2 = 库不存在 / 扫描出错。
"""
from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_VAULT = os.environ.get("AI_MEMORY_DIR") or str(Path(__file__).resolve().parents[1])

# 与 memory_store.SYSTEM_DIR_NAMES / bench_memory.SYSTEM_DIRS 保持一致：这些目录下的 md 不计入「库篇数」
SYSTEM_DIRS = {".git", "__pycache__", ".trash", ".obsidian", ".workbuddy",
               ".archive", ".trash_backup", "备份归档"}

# 量具代次指纹（**关键**）：`edge_case_missed` 是 2026-10-09 bench 量具修正时**新增**的字段。
# 有它 = 新代次量具（修正认 smart 行 `- [tier] **标题**`）；没有它 = 修正前的旧量具，
# 会把 tier 当标题 → recall 误报（实测旧档 65%/75%，新代次真实 80%/95%）。
# 因此**旧代次基准 JSON 里的 recall / regressions 一律不可采信**，必须重跑。
NEW_GAUGE_FIELD = "edge_case_missed"

# 复查条件段落标题（容忍 ## / ### 与多种措辞）
COND_HEAD_RE = re.compile(r"^#{1,6}\s*复查条件")
# 编号列表项：「1. xxx」/「1、xxx」/「1) xxx」
ITEM_RE = re.compile(r"^\s*(\d+)\s*[.、)]\s+(.*)$")

# ---------------------------------------------------------------------------
# 绑定表：决策文件名关键词 → {条件序号: [指标代码]}
# ---------------------------------------------------------------------------
# 指标代码语法：
#   entries:<阈值>          库篇数（> 阈值 即越线）
#   zero_rate:<阈值>:<周数>  零结果率（最近 N 个有样本周都 > 阈值 即越线）
#   recall:<1阈值>:<5阈值>   QA 集 recall@1/@5（任一跌破即越线；读最近一次 bench）
#   regressions             退化清单（非空即越线；读最近一次 bench）
#   write_ms:<阈值>         memory_write 同步耗时（无自动源 → 人工）
#   manual:<判据提示>        无法量化 / 未来才可测 → 人工判断
BINDINGS: dict[str, dict[int, list[str]]] = {
    "不引入事件系统": {
        1: ["entries:500", "write_ms:1000"],
        2: ["manual:是否出现多个互不依赖的子系统（多生产者）"],
        3: ["manual:是否出现「多消费者需动态增删 / 彼此解耦」的真实需求"],
        4: ["zero_rate:15:3", "manual:失败样本是否多为语义差异"],
    },
    "检索主路径引入分词分流": {
        1: ["regressions"],
        2: ["recall:85:90"],
        3: ["entries:300", "zero_rate:15:3"],
        4: ["manual:是否有新同义 / 改写失败样本且经确认属语义差异"],
    },
}


# ── 通用工具 ────────────────────────────────────────────────────────────────
def parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


# ── 指标采集（全部只读） ─────────────────────────────────────────────────────
def active_md(root: Path) -> list[Path]:
    """活跃 md（排除系统目录）—— 与 bench_memory.active_md 同口径。"""
    out: list[Path] = []
    for f in root.rglob("*.md"):
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        if any(p in SYSTEM_DIRS for p in rel.parts):
            continue
        out.append(f)
    return out


def read_search_log(root: Path) -> list[dict]:
    """读检索日志，**只留真实查询**（排除 bench 合成样本，与 bench_memory 同口径）。"""
    p = root / ".workbuddy" / "检索日志.jsonl"
    rows: list[dict] = []
    if not p.exists():
        return rows
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and not d.get("bench"):
            rows.append(d)
    return rows


def zero_rate_all(rows: list[dict]) -> dict:
    tot = len(rows)
    zero = sum(1 for r in rows if r.get("hits") == 0)
    return {"samples": tot, "zero": zero,
            "rate": round(zero / tot * 100, 1) if tot else None}


def zero_rate_weekly(rows: list[dict], weeks: int) -> list[dict]:
    """按 ISO 周分桶，返回**最近 weeks 个有样本的周**（按周升序）。"""
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        ts = parse_ts(r.get("ts"))
        if not ts:
            continue
        b = buckets[iso_week(ts)]
        b[0] += 1
        if r.get("hits") == 0:
            b[1] += 1
    weeks_sorted = sorted(buckets)[-weeks:]
    out = []
    for wk in weeks_sorted:
        t, z = buckets[wk]
        out.append({"week": wk, "samples": t, "zero": z,
                    "rate": round(z / t * 100, 1) if t else None})
    return out


def latest_bench(root: Path) -> dict | None:
    """取最近一次 bench_*.json（含 retrieval_qa 的），附路径 / mtime / **量具代次**。"""
    cand = sorted((root / ".workbuddy").glob("bench_*.json"),
                  key=lambda p: p.stat().st_mtime)
    for p in reversed(cand):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("retrieval_qa") is not None:
            d["_path"] = str(p)
            d["_mtime"] = datetime.fromtimestamp(
                p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            d["_gauge"] = "new" if NEW_GAUGE_FIELD in (d["retrieval_qa"] or {}) else "old"
            return d
    return None


def run_bench_now(root: Path) -> dict | None:
    """现场重跑 bench_memory.py 取新代次基准（**非纯只读**：会写临时探针 + 落检索日志）。

    仅在显式 `--run-bench` 时调用。JSON 落到临时目录、读完即删，不污染库。
    """
    import subprocess
    import tempfile

    script = Path(__file__).resolve().parent / "bench_memory.py"
    if not script.exists():
        print(f"[warn] 找不到 {script}", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bench.json"
        env = dict(os.environ, AI_MEMORY_DIR=str(root))
        try:
            proc = subprocess.run([sys.executable, str(script), "--json", str(out)],
                                  env=env, capture_output=True, text=True, timeout=300)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 重跑 bench 失败：{type(e).__name__}: {e}", file=sys.stderr)
            return None
        if proc.returncode != 0 or not out.exists():
            print(f"[warn] 重跑 bench 未产出结果（rc={proc.returncode}）", file=sys.stderr)
            return None
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 解析 bench JSON 失败：{e}", file=sys.stderr)
            return None
        d["_path"] = "(--run-bench 现场采集)"
        d["_mtime"] = datetime.now().astimezone().isoformat(timespec="seconds")
        d["_gauge"] = "new" if NEW_GAUGE_FIELD in (d.get("retrieval_qa") or {}) else "old"
        return d


# ── 决策文件解析 ─────────────────────────────────────────────────────────────
def parse_conditions(text: str) -> list[tuple[int, str]]:
    """抽出「## 复查条件」段的编号列表项 → [(序号, 原文)]。

    支持续行：非编号、非标题、非空行会接到上一条末尾（本库现网两份都是一行，留作兜底）。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if COND_HEAD_RE.match(ln.strip()):
            start = i + 1
            break
    if start is None:
        return []
    out: list[tuple[int, str]] = []
    for ln in lines[start:]:
        if re.match(r"^#{1,6}\s", ln):  # 下一个标题 → 段结束
            break
        m = ITEM_RE.match(ln)
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
        elif out and ln.strip():
            idx, prev = out[-1]
            out[-1] = (idx, (prev + " " + ln.strip()).strip())
    return out


def decision_title(path: Path, text: str) -> str:
    """优先用 frontmatter 的 title，否则用文件名 stem。"""
    head = text[:600]
    m = re.search(r'^title:\s*"?([^\n"]+)"?', head, re.M)
    return m.group(1).strip() if m else path.stem


# ── 指标求值 → 一行「当前值 + 阈值 + 标记」 ─────────────────────────────────
def eval_probe(code: str, ctx: dict) -> dict:
    """把一个指标代码求值为 {label, text, status}；status ∈ ok/trip/manual/na。"""
    parts = code.split(":")
    kind = parts[0]

    if kind == "entries":
        th = int(parts[1])
        n = ctx["entries"]
        return {"label": "库篇数", "status": "trip" if n > th else "ok",
                "text": f"当前 {n} 篇 ｜ 阈值 > {th} 篇"}

    if kind == "write_ms":
        th = int(parts[1])
        return {"label": "memory_write 同步耗时", "status": "manual",
                "text": f"无自动源 —— 需人工实测一次（阈值 > {th} ms）"}

    if kind == "zero_rate":
        th = float(parts[1])
        weeks = int(parts[2])
        allst = ctx["zero_all"]
        wk = ctx["zero_weeks"]
        wks_txt = "、".join(f"{w['week']}={w['rate']}%" for w in wk) or "无分周样本"
        trip = bool(wk) and all(w["rate"] is not None and w["rate"] > th for w in wk) \
            and len(wk) >= weeks
        all_txt = f"全量 {allst['rate']}%（{allst['samples']} 条）" if allst["samples"] else "全量无样本"
        return {"label": "零结果率",
                "status": "trip" if trip else "ok",
                "text": f"{all_txt} ｜ 最近 {weeks} 周：{wks_txt} ｜ 阈值 连续 {weeks} 周 > {th}%"}

    if kind == "recall":
        t1, t5 = float(parts[1]), float(parts[2])
        b = ctx.get("bench")
        if not b:
            return {"label": "QA recall", "status": "na",
                    "text": "无基准数据 —— 跑 脚本/bench_memory.py --json，或本脚本加 --run-bench"}
        if b.get("_gauge") == "old":
            return {"label": "QA recall", "status": "na",
                    "text": f"最近基准为**旧量具**（缺 {NEW_GAUGE_FIELD} 字段，10-09 修正前），"
                            "recall 不可信 —— 需重跑 bench"}
        qa = b["retrieval_qa"]
        r1, r5 = qa.get("recall_at_1"), qa.get("recall_at_5")
        trip = (r1 is not None and r1 < t1) or (r5 is not None and r5 < t5)
        n = qa.get("pairs") or 0
        unit = f"（QA 集 {n} 组，1 组 = {round(100 / n, 1)}%）" if n else ""
        return {"label": "QA recall", "status": "trip" if trip else "ok",
                "text": f"recall@1={r1}%（阈值 ≥ {t1}）｜ recall@5={r5}%（阈值 ≥ {t5}）{unit}"}

    if kind == "regressions":
        b = ctx.get("bench")
        if not b:
            return {"label": "退化清单 regressions", "status": "na",
                    "text": "无基准数据 —— 跑 脚本/bench_memory.py --json，或本脚本加 --run-bench"}
        if b.get("_gauge") == "old":
            return {"label": "退化清单 regressions", "status": "na",
                    "text": f"最近基准为**旧量具**（缺 {NEW_GAUGE_FIELD} 字段），"
                            "regressions 不可信 —— 需重跑 bench"}
        qa = b["retrieval_qa"]
        regs = qa.get("regressions") or []
        if regs:
            names = "、".join(m.get("query", "?") for m in regs[:6])
            return {"label": "退化清单 regressions", "status": "trip",
                    "text": f"非空（{len(regs)} 条：{names}）｜ 阈值 应为空"}
        return {"label": "退化清单 regressions", "status": "ok",
                "text": f"为空（0 条；已知边界未命中 {qa.get('edge_case_missed', 0)} 条属预期）"
                        "｜ 阈值 应为空"}

    if kind == "manual":
        hint = ":".join(parts[1:]) or "需人工判断"
        return {"label": "人工判断", "status": "manual", "text": f"{hint} —— 自动指标不可测"}

    return {"label": code, "status": "na", "text": "未知指标代码"}


# ── 扫描 + 渲染 ──────────────────────────────────────────────────────────────
def scan(root: Path, weeks: int, bench_override: dict | None = None) -> dict:
    ddir = root / "决策"
    files = sorted(ddir.glob("决策_*.md")) if ddir.is_dir() else []

    log_rows = read_search_log(root)
    ctx = {
        "entries": len(active_md(root)),
        "zero_all": zero_rate_all(log_rows),
        "zero_weeks": zero_rate_weekly(log_rows, weeks),
        "bench": bench_override if bench_override is not None else latest_bench(root),
    }

    decisions = []
    for f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        conds = parse_conditions(raw)
        title = decision_title(f, raw)
        key = next((k for k in BINDINGS if k in f.name), None)
        binding = BINDINGS.get(key, {}) if key else {}

        items = []
        for idx, text in conds:
            codes = binding.get(idx, [])
            probes = [eval_probe(c, ctx) for c in codes] if codes \
                else [{"label": "未绑定指标", "status": "manual",
                       "text": "本条未在脚本 BINDINGS 里绑定指标 —— 只列原文"}]
            items.append({"index": idx, "text": text, "probes": probes,
                          "bound": bool(codes)})

        trip = sum(1 for it in items for p in it["probes"] if p["status"] == "trip")
        decisions.append({
            "file": str(f.relative_to(root)), "title": title,
            "bound": bool(key), "conditions": items, "trip_count": trip,
        })

    return {"vault": str(root), "ctx": ctx, "decisions": decisions,
            "trip_total": sum(d["trip_count"] for d in decisions)}


MARK = {"trip": "[越线]", "ok": "[未越线]", "manual": "[人工]", "na": "[无数据]"}


def render(res: dict, weeks: int) -> int:
    ctx = res["ctx"]
    print("复查条件仪表（只读，仅摆数据、不判定）")
    print(f"库：{res['vault']}")
    print(f"扫描决策 {len(res['decisions'])} 份 ｜ 库篇数 {ctx['entries']} ｜ "
          f"零结果率全量 "
          + (f"{ctx['zero_all']['rate']}%" if ctx["zero_all"]["samples"] else "无样本"))
    b = ctx.get("bench")
    if b:
        rq = b["retrieval_qa"]
        gauge = "新量具" if b.get("_gauge") == "new" else "旧量具·recall 不可信"
        print(f"基准：{Path(b['_path']).name}（{b['_mtime'][:16]}，{gauge}）｜ "
              f"recall@1={rq.get('recall_at_1')}% ｜ recall@5={rq.get('recall_at_5')}% ｜ "
              f"regressions={len(rq.get('regressions') or [])}")
    else:
        print("基准：无（recall / regressions 类条件将标 [无数据]；可用 --run-bench 现场采集）")
    print()

    for d in res["decisions"]:
        head = "═" * 4
        print(f"{head} {d['title']} {head}")
        if not d["conditions"]:
            print("  （未找到「复查条件」段）")
            print()
            continue
        if not d["bound"]:
            print("  提示：本决策未在脚本 BINDINGS 中登记，条件只列原文")
        for it in d["conditions"]:
            print(f"  {it['index']}. {it['text']}")
            for p in it["probes"]:
                print(f"     {MARK.get(p['status'], p['status'])} {p['label']}：{p['text']}")
        print(f"  → 越线条件：{d['trip_count']} 条")
        print()

    print(f"合计越线：{res['trip_total']} 条"
          + ("　（请对着上面原文判断是否重新评估对应决策）" if res["trip_total"] else ""))
    return 1 if res["trip_total"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="复查条件检查器（只读）")
    ap.add_argument("--vault", default=DEFAULT_VAULT, help="记忆库路径")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("--weeks", type=int, default=4,
                    help="零结果率按周分桶看最近 N 周（默认 4）")
    ap.add_argument("--run-bench", action="store_true",
                    help="现场重跑 bench_memory.py 取真实 recall（**会写临时探针 + 落检索日志**，非纯只读）")
    args = ap.parse_args()

    root = Path(args.vault)
    if not root.is_dir():
        print(f"找不到记忆库目录: {root}", file=sys.stderr)
        return 2

    bench_override = None
    if args.run_bench:
        print("[--run-bench] 现场重跑 bench_memory.py（会写临时探针 + 落检索日志）…", file=sys.stderr)
        bench_override = run_bench_now(root)

    try:
        res = scan(root, max(1, args.weeks), bench_override)
    except Exception as e:  # noqa: BLE001
        print(f"扫描出错：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.json:
        Path(args.json).write_text(
            json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"已写出 JSON: {args.json}")

    return render(res, max(1, args.weeks))


if __name__ == "__main__":
    sys.exit(main())
