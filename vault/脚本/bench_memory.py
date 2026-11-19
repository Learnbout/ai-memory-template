#!/usr/bin/env python3
"""记忆库基准采集（只读 + 一次临时写入探针）。

用途：在实施优化前后各跑一次，输出可直接 diff 的 JSON，用于量化「改了什么、好了多少」。

采集指标：
- 规模：条目数 / 总字符 / 估算 token / 目录分布
- 字段：schema_version / verified / type / scope 覆盖率
- 索引：路由索引字符数、记忆索引字符数、库根字符占比
- 缓存：外部改动（只动子目录）后 _iter_entries 是否看得到（跨进程缓存失效回归）
- 性能：memory_search / memory_stats / _iter_entries 墙钟耗时
- 检索：零结果率（读 .workbuddy/检索日志.jsonl，无日志则记 null）

用法：
    python 脚本/bench_memory.py                 # 输出人类可读
    python 脚本/bench_memory.py --json out.json # 同时写 JSON
    python 脚本/bench_memory.py --diff a.json b.json   # 对比两份基准，量化改动前后差异
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
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
# 必须在 import memory_store 之前落定，否则 memory_store 会 fallback 到 ~/ai-memory
# 并在那里 mkdir 出一个空库，导致所有指标静默失真（本脚本曾踩过）。
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))

SYSTEM_DIRS = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__", ".trash_backup", "备份归档"}
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# ── G6（2026-10-09）：检索质量自测集 ──────────────────────────────────────────
# 20 组「query → 期望命中」。期望给标题片段（不是完整标题），且一条 query 可给
# 多个可接受片段、任一出现在窗口内即算命中 —— 片段比整标题抗演化（笔记改名 /
# 加日期后缀不会把自测集打穿）。产出 recall@1 / recall@5 写进基准 JSON，
# 与零结果率互补：零结果率只看「有没有」，recall 才看「排得靠不靠前」。
# 命中口径：期望片段出现在该 query 结果的组标题（含折叠列出的标题）中。
QA_SET: list[tuple[str, list[str]]] = [
    ("热重载", ["热重载"]),
    ("supervisor", ["热重载", "supervisor"]),
    ("frontmatter", ["frontmatter"]),
    ("死链", ["死链"]),
    ("双链", ["双链", "wikilink"]),
    ("自动备份", ["auto_push", "备份"]),
    ("聚合页", ["近期工作动态", "项目状态", "聚合页"]),
    ("token 优化", ["token"]),
    ("记忆库对标", ["对标"]),
    ("写入格式", ["记忆写入格式规范"]),
    ("路由索引", ["路由索引"]),
    ("乐观锁", ["expected_version", "乐观锁", "并发"]),
    ("缓存失效", ["缓存"]),
    ("MySQL", ["MySQL", "mysql"]),
    ("人设", ["人设", "AI人格"]),
    ("stop hook", ["hook", "钩子", "记忆写入"]),
    ("检索日志", ["检索日志", "零结果"]),
    ("骨架压缩", ["骨架", "skeleton"]),
    ("同主题折叠", ["折叠"]),
    ("工具选择", ["memory_search", "选路", "工具"]),
]

# 已知边界（negative_cases，2026-10-09 立）：这两条**不算退化**，任何搜索改动重跑后仍不
# 命中属预期，不许据此判定「改坏了」；反过来，某次改动后它们**命中了**，说明该路径被修好，
# 应更新本表并记入项目笔记。处置口径：把这两条当作「检索改动的固定回归项」——
# **以后任何搜索改动（排序 / 分流 / 分词 / 别名）都必须重跑 QA 集并逐一核对这两条**。
EDGE_CASES: dict[str, str] = {
    "frontmatter": "设题无效：全库无标题含该词（仅正文/目录名出现），属题面缺陷而非检索缺陷",
    "乐观锁": "甲案已知退化：期望片段 expected_version/乐观锁/并发 分散在各页正文、无标题强命中",
}


def est_tokens(text: str) -> int:
    cjk = len(CJK.findall(text))
    other = len(text) - cjk
    return int(cjk * 0.7 + other / 4)


def active_md(root: Path) -> list[Path]:
    files = []
    for f in root.rglob("*.md"):
        try:
            if any(p in SYSTEM_DIRS for p in f.relative_to(root).parts):
                continue
        except ValueError:
            continue
        files.append(f)
    return sorted(files)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def fm_of(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def collect() -> dict:
    root = VAULT
    files = active_md(root)
    total_chars = 0
    by_dir: dict[str, int] = {}
    field_count = {"schema_version": 0, "verified": 0, "type": 0, "scope": 0, "summary": 0}
    verified_true = 0
    total_tokens = 0

    for f in files:
        text = read(f)
        n = len(text)
        total_chars += n
        total_tokens += est_tokens(text)
        rel_dir = "库根" if f.parent == root else f.parent.name
        by_dir[rel_dir] = by_dir.get(rel_dir, 0) + n
        fm = fm_of(text)
        for k in field_count:
            if k in fm:
                field_count[k] += 1
        if fm.get("verified", "").lower() == "true":
            verified_true += 1

    routing = root / "路由索引.md"
    memory_index = root / "记忆索引.md"
    root_chars = sum(by_dir.get("库根", 0) for _ in [0])

    # 简单关键词，覆盖高频主题，用真实检索结果统计零结果率
    queries = ["优化", "MCP", "记忆库", "缓存", "字段", "Headroom", "双链", "不存在的词xyzzy"]

    import memory_store as ms
    import memory_tools as mt

    # E2（2026-09-14）：本脚本的查询是合成样本，必须打标，否则会混进「零结果率」
    # 这个唯一判据里（2026-09-11 实测：87 条样本中 73 条来自本脚本）。
    #
    # 2026-09-20 订正：这个标记**只在本脚本进程内有效**。检索日志的实际写入方是
    # 常驻的 MCP 进程（supervisor/worker），进程内全局变量跨不过进程边界 ——
    # 实测 270 条日志中 bench=true 落盘 0 条，即本标记从未真正生效过。
    # 因此本脚本的采样样本仍会以 bench=false 落盘，靠「查询词形」识别；
    # 存量与新增的探针样本由 脚本/backfill_search_log.py 事后归集。
    # 保留此行无副作用（本进程内的 memory_search 调用仍会被标记），但不可再
    # 依赖它作为唯一防线。彻底修法需给日志写入加查询白名单/来源参数。
    mt.LOG_AS_BENCH = True

    def timeit(fn, *args, repeat=3):
        best = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn(*args)
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
        return round(best * 1000, 1)

    t_iter = timeit(ms._iter_entries)
    t_stats = timeit(mt.memory_stats, repeat=2)
    t_search = {}
    zero = 0
    hit_total = 0
    for q in queries:
        dt = timeit(mt.memory_search, q, repeat=2)
        t_search[q] = dt
        res = mt.memory_search(q)
        m = re.search(r"找到 (\d+) 条", res)
        n = int(m.group(1)) if m else (0 if "未找到" in res else None)
        if n == 0:
            zero += 1
        if n is not None:
            hit_total += 1

    # 跨进程缓存失效回归探针：只改子目录，看缓存是否失效
    cache_probe = probe_cache(ms)

    # G6（2026-10-09）：检索质量自测（recall@1 / recall@5）
    qa = retrieval_quality(mt)

    # 检索日志统计（第 12 项产出物）
    search_log = None
    log_path = root / ".workbuddy" / "检索日志.jsonl"
    if log_path.exists():
        total = 0
        zeros = 0
        bench = 0
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # E2（2026-09-14）：基准合成样本单独计数、不进零结果率分母。
            # 此前 87 条样本里 84% 是合成查询，使「零结果率」这个唯一判据失真。
            if rec.get("bench"):
                bench += 1
                continue
            total += 1
            if rec.get("hits") == 0:
                zeros += 1
        search_log = {
            "samples": total,
            "zero_result": zeros,
            "zero_rate_pct": round(zeros / total * 100, 1) if total else None,
            "bench_samples": bench,
        }

    # G11（2026-10-10）：语义检索触发线仪表（要吃 search_log 的零结果率，放其之后）
    zero_rate = None
    if search_log and search_log.get("zero_rate_pct") is not None:
        zero_rate = search_log["zero_rate_pct"]
    trigger = semantic_trigger(len(files), zero_rate)

    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "scale": {
            "entries": len(files),
            "total_chars": total_chars,
            "est_tokens": total_tokens,
            "by_dir_chars": dict(sorted(by_dir.items(), key=lambda kv: -kv[1])),
            "by_dir_pct": {k: round(v / total_chars * 100, 1) for k, v in by_dir.items()},
        },
        "index": {
            "routing_index_chars": len(read(routing)) if routing.exists() else 0,
            "routing_index_share_pct": round(len(read(routing)) / total_chars * 100, 1) if routing.exists() else 0,
            "memory_index_chars": len(read(memory_index)) if memory_index.exists() else 0,
        },
        "fields": {
            "coverage": field_count,
            "coverage_pct": {k: round(v / len(files) * 100, 1) for k, v in field_count.items()},
            "verified_true": verified_true,
        },
        "cache_probe": cache_probe,
        "retrieval_qa": qa,
        "semantic_trigger": trigger,
        "perf_ms": {"iter_entries": t_iter, "memory_stats": t_stats, "memory_search": t_search},
        "search_sampling": {
            "queries": len(queries),
            "zero_result": zero,
            "zero_rate_pct": round(zero / len(queries) * 100, 1) if queries else None,
        },
        "search_log": search_log,
    }


def retrieval_quality(mt) -> dict:
    """G6：跑 QA_SET，产出 recall@1 / recall@5 与逐条未命中清单。

    解析 memory_search 的分组输出，兼容两种行格式：
      · 精确 / 分组路径 —— `- [标题] (文件名) | ...`，同主题折叠的标题以反引号
        列在「↳ 另 N 条同主题」行里；
      · 分词分流（甲案）—— `- [tier] **标题** (score=..., source)`，每个结果各成
        一组（smart 路径不做折叠）。

    甲案（2026-10-09）把 smart 从「零结果兜底」提为「无标题命中时的正常分流层」，
    故本解析器必须认 smart 行 —— 否则量具会把分流后的命中也算成未命中（口径失真，
    实测会把 recall@1 从 85% 误报成 60%）。仍按未命中计的只剩真·零结果。
    """
    hits1 = 0
    hits5 = 0
    misses = []
    for query, expect in QA_SET:
        res = mt.memory_search(query)
        groups: list[list[str]] = []
        cur: list[str] = []
        for line in res.splitlines():
            # 先认分词分流格式（- [tier] **标题** (score=...)，标题在 ** 里），
            # 再认精确分组格式（- [标题] (文件名) ...）—— 顺序不可反，否则
            # smart 行会被精确正则抓走 tier 当标题。
            msmart = re.match(r"\s*- \[[^\]]+\] \*\*(.+?)\*\*", line)
            if msmart:
                if cur:
                    groups.append(cur)
                cur = [msmart.group(1)]
                continue
            m = re.match(r"- \[(.+?)\]", line)
            if m:
                if cur:
                    groups.append(cur)
                cur = [m.group(1)]
            else:
                cur.extend(re.findall(r"`([^`]+)`", line))
        if cur:
            groups.append(cur)
        window1 = " ".join(groups[0]) if groups else ""
        window5 = " ".join(" ".join(g) for g in groups[:5])
        ok1 = any(e.lower() in window1.lower() for e in expect)
        ok5 = any(e.lower() in window5.lower() for e in expect)
        if ok1:
            hits1 += 1
        if ok5:
            hits5 += 1
        else:
            misses.append({
                "query": query,
                "expect": expect,
                "groups_found": len(groups),
                "edge_case": EDGE_CASES.get(query),  # 非 None 即已知边界，不算退化
            })
    n = len(QA_SET)
    return {
        "pairs": n,
        "recall_at_1": round(hits1 / n * 100, 1),
        "recall_at_5": round(hits5 / n * 100, 1),
        "hits_at_1": hits1,
        "hits_at_5": hits5,
        "misses": misses,
        # 退化清单 = 未命中里**排除已知边界**后的部分；这才是「改动是否改坏」的判据
        "edge_case_missed": sum(1 for m in misses if m.get("edge_case")),
        "regressions": [m for m in misses if not m.get("edge_case")],
    }


# ── G11 触发线仪表（2026-10-10）：语义检索立项判据自动监测 ────────────────────
# 触发线 = 库 >300 篇（2026-09-11 立据：子串+CJK 2-gram 在这个规模前零结果率
# 稳定 <5%，为语义向量预留的观察线）。此前只靠人翻 bench 数字，现在 bench 自己
# 判定并给结论；零结果率是第二判据（>15% 时无论库多大都要先分析失败样本）。
SEMANTIC_TRIGGER_ENTRIES = 300


def semantic_trigger(entries: int, zero_rate_pct: float | None = None) -> dict:
    """产出语义检索触发线判定。纯函数，零副作用。"""
    progress = round(entries / SEMANTIC_TRIGGER_ENTRIES * 100, 1)
    triggered = entries >= SEMANTIC_TRIGGER_ENTRIES
    if triggered:
        verdict = "已到触发线（>=300 篇）：可立项语义检索/混合排序评估"
    else:
        verdict = f"未触发（{entries}/{SEMANTIC_TRIGGER_ENTRIES} 篇，{progress}%），维持观察项"
    if zero_rate_pct is not None:
        if zero_rate_pct > 15:
            verdict += f"；且零结果率 {zero_rate_pct}% > 15%，应先分析失败样本（词形 vs 语义）"
        elif not triggered and zero_rate_pct < 5:
            verdict += f"；零结果率 {zero_rate_pct}% < 5%，现状检索够用"
    return {
        "threshold": SEMANTIC_TRIGGER_ENTRIES,
        "entries": entries,
        "progress_pct": progress,
        "triggered": triggered,
        "verdict": verdict,
    }


def probe_cache(ms) -> dict:
    """只改子目录新增文件，验证 _iter_entries 能否看到（不加库根改动）。"""
    sub = ms.MEMORY_DIR / "项目"
    sub.mkdir(parents=True, exist_ok=True)
    probe = sub / f"_bench_probe_{int(time.time())}.md"
    before = len(ms._iter_entries())
    try:
        ms._atomic_write_text(
            probe,
            "---\ntitle: _bench_probe\ntags: [bench]\ntier: warm\n---\n\nprobe\n",
        )
        after = len(ms._iter_entries())
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    ms._invalidate_cache()
    return {
        "entries_before": before,
        "entries_after_subdir_add": after,
        "detected": after > before,
        "verdict": "缓存正确失效" if after > before else "缓存未失效（子目录改动不可见）",
    }


def human(data: dict) -> str:
    s = data["scale"]
    lines = [
        "# 记忆库基准",
        "",
        f"采集时间：{data['collected_at'][:19]}",
        "",
        "## 规模",
        f"- 条目数：{s['entries']}",
        f"- 总字符：{s['total_chars']:,}",
        f"- 估算 token：{s['est_tokens']:,}",
        "- 目录占比：" + " / ".join(f"{k} {v}%" for k, v in s["by_dir_pct"].items()),
        "",
        "## 索引",
        f"- 路由索引：{data['index']['routing_index_chars']:,} 字符（全库 {data['index']['routing_index_share_pct']}%）",
        f"- 记忆索引：{data['index']['memory_index_chars']:,} 字符",
        "",
        "## 字段覆盖",
        "- " + " / ".join(f"{k} {v}%" for k, v in data["fields"]["coverage_pct"].items()),
        f"- verified=true：{data['fields']['verified_true']} 条",
        "",
        "## 缓存失效探针",
        f"- 探针前 {data['cache_probe']['entries_before']} 条 → 子目录新增后 "
        f"{data['cache_probe']['entries_after_subdir_add']} 条：**{data['cache_probe']['verdict']}**",
        "",
        "## 检索质量自测（G6）",
        f"- QA 集 {data['retrieval_qa']['pairs']} 组：recall@1 = {data['retrieval_qa']['recall_at_1']}%"
        f"（{data['retrieval_qa']['hits_at_1']}/{data['retrieval_qa']['pairs']}），"
        f"recall@5 = {data['retrieval_qa']['recall_at_5']}%"
        f"（{data['retrieval_qa']['hits_at_5']}/{data['retrieval_qa']['pairs']}）",
    ]
    for miss in data["retrieval_qa"]["misses"]:
        tag = "（已知边界，不算退化）" if miss.get("edge_case") else "（**回归**：非已知边界）"
        lines.append(
            f"- 未命中：{miss['query']}（期望 {('/'.join(miss['expect']))}，"
            f"结果 {miss['groups_found']} 组）{tag}"
        )
    if "regressions" in data["retrieval_qa"]:
        lines.append(
            f"- **回归数（排除已知边界后）**：{len(data['retrieval_qa']['regressions'])}"
            f"（已知边界未命中 {data['retrieval_qa'].get('edge_case_missed', 0)} 条，属预期）"
        )
    lines += [
        "",
        "## 性能（最快一次，毫秒）",
        f"- _iter_entries：{data['perf_ms']['iter_entries']}",
        f"- memory_stats：{data['perf_ms']['memory_stats']}",
        f"- memory_search：" + ", ".join(f"{k}={v}" for k, v in data["perf_ms"]["memory_search"].items()),
        "",
        "## 检索采样",
        f"- 采样 {data['search_sampling']['queries']} 个 query，零结果 "
        f"{data['search_sampling']['zero_result']} 个（{data['search_sampling']['zero_rate_pct']}%）",
    ]
    if data.get("search_log"):
        sl = data["search_log"]
        lines.append(
            f"- 检索日志：{sl['samples']} 条真实查询，零结果率 {sl['zero_rate_pct']}%"
            f"（另有 {sl.get('bench_samples', 0)} 条基准合成样本已排除）"
        )
    if data.get("semantic_trigger"):
        st = data["semantic_trigger"]
        lines += [
            "",
            "## 语义检索触发线（G11 仪表）",
            f"- 进度：{st['entries']}/{st['threshold']} 篇（{st['progress_pct']}%）—— {st['verdict']}",
        ]
    return "\n".join(lines)


def diff_baselines(before: dict, after: dict) -> str:
    """对比两份基准 JSON，输出「改了什么、好了多少」的结构化差异。

    设计原则：每个指标给出 前值 → 后值（delta，符号带方向），并标出「好/坏」。
    不追求全字段覆盖 —— 只比那些**有改善方向**的指标（token、索引占比、
    覆盖率、缓存探针、耗时），其余（如条目数）只报变化量不做好坏判断。
    """

    def num(d, *path, default=None):
        cur = d
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def cell(b, a, unit="", better="lower", pct=True):
        """生成一格对比文本。"""
        if b is None or a is None:
            return f"{b if b is not None else '—'} → {a if a is not None else '—'}"
        delta = a - b
        # 统一舍入到 1 位小数，避免浮点残差（-3.6999999999999993%）泄漏到报告里
        d = round(delta, 1)
        b_r, a_r = round(b, 1), round(a, 1)
        sign = "+" if d > 0 else ""
        rel = ""
        if pct and b:
            rel = f"，{sign}{round(delta / b * 100, 1)}%"
        mark = ""
        if better in ("lower", "higher") and d != 0:
            good = (d < 0) if better == "lower" else (d > 0)
            mark = " [改善]" if good else " [恶化]"
        elif d != 0:
            mark = " [变化]"
        return f"{b_r:,}{unit} → {a_r:,}{unit}（{sign}{d:,}{unit}{rel}）{mark}"

    lines = [
        "# 基准对比",
        "",
        f"改前：{before.get('collected_at', '?')[:19]}",
        f"改后：{after.get('collected_at', '?')[:19]}",
        "",
        "## 规模（条目数只报变化，不判好坏）",
        f"- 条目数：{num(before,'scale','entries')} → {num(after,'scale','entries')}",
        f"- 总字符：{cell(num(before,'scale','total_chars'), num(after,'scale','total_chars'))}",
        f"- 估算 token：{cell(num(before,'scale','est_tokens'), num(after,'scale','est_tokens'))}",
        "",
        "## 索引（越小越好）",
        f"- 路由索引字符：{cell(num(before,'index','routing_index_chars'), num(after,'index','routing_index_chars'))}",
        f"- 路由索引占全库：{cell(num(before,'index','routing_index_share_pct'), num(after,'index','routing_index_share_pct'), unit='%')}",
        f"- 记忆索引字符：{cell(num(before,'index','memory_index_chars'), num(after,'index','memory_index_chars'))}",
        "",
        "## 字段覆盖（越大越好）",
    ]

    keys = sorted(
        set((num(before, "fields", "coverage_pct") or {}).keys())
        | set((num(after, "fields", "coverage_pct") or {}).keys())
    )
    for k in keys:
        b = num(before, "fields", "coverage_pct", k)
        a = num(after, "fields", "coverage_pct", k)
        lines.append(f"- {k}：{cell(b, a, unit='%', better='higher', pct=False)}")
    lines.append(
        f"- verified=true 条数："
        f"{cell(num(before,'fields','verified_true'), num(after,'fields','verified_true'), pct=False, better='higher')}"
    )

    lines += ["", "## 缓存失效探针（必须为 True）"]
    b_det = num(before, "cache_probe", "detected")
    a_det = num(after, "cache_probe", "detected")
    verdict = "通过" if a_det else "**未通过（子目录改动不可见）**"
    lines.append(f"- {b_det} → {a_det}：{verdict}")
    b_v = (before.get("cache_probe") or {}).get("verdict") or "—"
    a_v = (after.get("cache_probe") or {}).get("verdict") or "—"
    lines.append(f"  - 改前口径：{b_v}")
    lines.append(f"  - 改后口径：{a_v}")

    lines += ["", "## 性能（毫秒，越小越好）"]
    for mk in ("iter_entries", "memory_stats"):
        lines.append(f"- {mk}：{cell(num(before,'perf_ms',mk), num(after,'perf_ms',mk), unit='ms', pct=False)}")
    bq = num(before, "perf_ms", "memory_search") or {}
    aq = num(after, "perf_ms", "memory_search") or {}
    for q in sorted(set(bq) | set(aq)):
        lines.append(f"- memory_search({q})：{cell(bq.get(q), aq.get(q), unit='ms', pct=False)}")

    lines += ["", "## 检索质量"]
    bs = before.get("search_log") or {}
    as_ = after.get("search_log") or {}
    bqa = before.get("retrieval_qa") or {}
    aqa = after.get("retrieval_qa") or {}
    if bqa or aqa:
        lines.append(f"- QA recall@1：{cell(bqa.get('recall_at_1'), aqa.get('recall_at_1'), unit='%', better='higher', pct=False)}")
        lines.append(f"- QA recall@5：{cell(bqa.get('recall_at_5'), aqa.get('recall_at_5'), unit='%', better='higher', pct=False)}")
    if bs or as_:
        lines.append(
            f"- 真实样本数：{bs.get('samples','—')} → {as_.get('samples','—')}"
        )
        lines.append(
            f"- 零结果率：{cell(bs.get('zero_rate_pct'), as_.get('zero_rate_pct'), unit='%')}"
        )
        lines.append(
            f"- 已排除的基准样本：{bs.get('bench_samples','—')} → {as_.get('bench_samples','—')}"
        )
        rate = as_.get("zero_rate_pct")
        if rate is not None:
            if rate < 5:
                lines.append("  - 判据结论：< 5% → 永久否决 FTS5")
            elif rate > 15:
                lines.append("  - 判据结论：> 15% → 需分析失败样本属词形还是语义差异")
            else:
                lines.append("  - 判据结论：5%~15% 灰区 → 维持子串+2gram，暂不引入 FTS5")
    else:
        lines.append("- 两份基准均无检索日志统计（search_log 为空）")

    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="同时写出 JSON 结果")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="对比两份基准 JSON，输出差异报告（不采集新数据）")
    args = ap.parse_args()

    if args.diff:
        b_path, a_path = (Path(p) for p in args.diff)
        for p in (b_path, a_path):
            if not p.exists():
                print(f"[x] 找不到基准文件：{p}")
                sys.exit(1)
        before = json.loads(b_path.read_text(encoding="utf-8"))
        after = json.loads(a_path.read_text(encoding="utf-8"))
        print(diff_baselines(before, after))
        sys.exit(0)

    data = collect()
    print(human(data))
    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[json] -> {args.json}")
