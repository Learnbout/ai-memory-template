"""检索质量评估（唯一评估入口，2026-10-19 建）。

替代此前散落在 6 个临时脚本里的重复实现，评估集统一取自 `脚本/retrieval_cases.py`。

## 为什么要分「主指标 / 争议样本」

对某条 query 如果所有方法都一致漏检，有两种可能：检索真差，或 gold 标错了。
把明显标错的样本算进主指标，会让「优化」变成「对着错题目调参」。
所以主指标只统计 `disputed=False` 的样本，争议样本单独列出、由人复核。

## 口径纪律

- 一切对比必须在**同一 top-k** 下做。`关键字 top10` 对 `并集 top10` 才有意义；
  拿「并集 10 个候选」去比「关键字 top5」是换尺子（本项目 2026-10-19 就犯过）。
- 空结果不算命中，也不做任何补齐（补齐等于编造候选）。

跑法：
  <venv>/python.exe 脚本/bench_retrieval.py
  <venv>/python.exe 脚本/bench_retrieval.py --full   # 连争议样本一起看
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）
sys.path.insert(0, str(ROOT / "脚本"))

os.environ.setdefault("AI_MEMORY_DIR", str(ROOT))
os.environ.setdefault("AI_MEMORY_READ_SOURCE", "md")

import embed_onnx  # noqa: E402
import memory_tools as mt  # noqa: E402
import retrieval_cases as rc  # noqa: E402

DB = db_paths.resolve("index.sqlite3")


def parse_titles(text: str) -> list[str]:
    """同时认两种输出格式：`- [标题] (文件.md)` 与 `- [warm] **标题** (score=)`。"""
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^- \[[^\]]+\]\s+\*\*(.+?)\*\*", line)
        if m:
            out.append(m.group(1))
            continue
        m = re.match(r"^- \[([^\]]+)\]", line)
        if m:
            out.append(m.group(1))
    return out


def keyword_titles(query: str, k: int) -> list[str]:
    os.environ["AI_MEMORY_READ_SOURCE"] = "md"
    mt._DB_ROWS_CACHE["key"] = None
    return parse_titles(mt.memory_search(query, limit=k))


def load_vectors():
    import numpy as np

    conn = sqlite3.connect("file:" + DB.as_posix() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT rel_path, dim, vec FROM note_vecs").fetchall()
        titles = {r[0]: r[1] for r in conn.execute("SELECT rel_path, title FROM notes")}
    finally:
        conn.close()
    rows = [r for r in rows if r[0] in titles]  # 与 memory_tools 同口径：以 notes 为准
    items = [(titles[rel], rel) for rel, _d, _b in rows]
    mat = np.asarray([np.frombuffer(b, dtype="<f4", count=d) for _rel, d, b in rows], dtype=np.float32)
    return items, mat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="把争议样本也算进指标")
    ap.add_argument("--top", type=int, default=5, help="主指标用的 top-k（默认 5）")
    args = ap.parse_args()

    import numpy as np

    cases = rc.CASES if args.full else rc.clean()
    n = len(cases)
    print(f"样本 {n} 条" + ("" if args.full else f"（已排除 {len(rc.CASES)-n} 条争议样本）"))
    print(f"主指标口径: recall@{args.top}\n")

    items, mat = load_vectors()
    sem_ok = embed_onnx.available()
    print(f"语义通道: {'可用' if sem_ok else '不可用'} / 向量 {len(items)} 条 / 维度 {mat.shape[1] if len(items) else 0}\n")

    stat = {"kw": 0, "sem": 0, "rrf": 0, "ship": 0, "union": 0, "kw10": 0, "rrf10": 0, "union10": 0}
    rows = []
    K_RRF = 60
    for case in cases:
        kw = keyword_titles(case.query, args.top)
        kw10 = keyword_titles(case.query, 10)
        if sem_ok:
            qv = np.asarray(embed_onnx.encode([case.query])[0], dtype=np.float32)
            order = np.argsort(-(mat @ qv))[:10]
            sem = [items[j][0] for j in order]
        else:
            sem = []

        def rrf(a: list[str], b: list[str], k: int) -> list[str]:
            """Reciprocal Rank Fusion：只用名次、不用分数，避免两套分数量纲归一化。
            k=60 是文献通用默认值，不是对着本测试集调出来的。"""
            score: dict[str, float] = {}
            for lst in (a, b):
                for rank, t in enumerate(lst):
                    score[t] = score.get(t, 0.0) + 1.0 / (K_RRF + rank + 1)
            return [t for t, _ in sorted(score.items(), key=lambda kv: -kv[1])][:k]

        rrf_k = rrf(kw, sem, args.top)
        rrf_10 = rrf(kw10, sem, 10)
        # 线上真实行为：关键字结果原样在前，语义只补「前文没有的」、最多 5 条
        # （对应 memory_tools._semantic_section 的 cap=min(_FALLBACK_LIMIT, limit)）。
        tail = [t for t in sem if t not in kw][: min(5, args.top)]
        ship = kw + tail
        union = list(dict.fromkeys(kw + sem))      # 不控条数：候选上界
        union10 = list(dict.fromkeys(kw10 + sem))

        ok = {
            "kw": rc.hit(kw, case.gold),
            "sem": rc.hit(sem[: args.top], case.gold),
            "rrf": rc.hit(rrf_k, case.gold),
            "ship": rc.hit(ship, case.gold),
            "union": rc.hit(union, case.gold),
            "kw10": rc.hit(kw10, case.gold),
            "rrf10": rc.hit(rrf_10, case.gold),
            "union10": rc.hit(union10, case.gold),
        }
        for k in stat:
            stat[k] += ok[k]
        rows.append((case, ok, kw[:2], sem[:2]))

    print(f"{'query':26s}{'类':4s}{'关键字':>7}{'语义':>5}{'RRF':>5}   gold")
    for case, ok, k2, s2 in rows:
        mark = lambda b: "OK" if b else "--"  # noqa: E731
        flag = "!" if case.disputed else " "
        print(f"{case.query[:24]:26s}{case.kind:4s}{mark(ok['kw']):>7}{mark(ok['sem']):>5}{mark(ok['rrf']):>5} {flag} gold={case.gold_text()}")
        if not ok["kw"] and case.disputed is False:
            print(f"     关键字 top2: {k2}")
        if not ok["union"]:
            print(f"     [全漏] 关键字 top2: {k2} / 语义 top2: {s2}")

    print("\n" + "=" * 62)
    print(f"  关键字 @{args.top}          {stat['kw']}/{n} = {stat['kw']/n:.0%}")
    print(f"  语义   @{args.top}          {stat['sem']}/{n} = {stat['sem']/n:.0%}")
    print(f"  RRF 合并 @{args.top}（条数相同） {stat['rrf']}/{n} = {stat['rrf']/n:.0%}")
    print(f"  **线上实际形态（最多 {args.top + min(5, args.top)} 条）**  {stat['ship']}/{n} = {stat['ship']/n:.0%}"
          f"   （关键字 {args.top} 条 + 语义补 5 条）")
    print(f"  关键字 @10         {stat['kw10']}/{n} = {stat['kw10']/n:.0%}")
    print(f"  RRF 合并 @10       {stat['rrf10']}/{n} = {stat['rrf10']/n:.0%}")
    print(f"  候选并集上界（不控条数，仅作天花板参考）")
    print(f"                    {stat['union']}/{n} = {stat['union']/n:.0%}")
    print("=" * 62)
    print("说明：并集条数多于单一方法，不可与 @5/@10 直接比较。判定要看两行：")
    print("  「线上实际形态」= 用户真正看到的东西；「RRF」= 同条数下的融合上限。")

    by_kind: dict[str, list[int]] = {}
    for case, ok, _k, _s in rows:
        b = by_kind.setdefault(case.kind, [0, 0, 0])
        b[0] += 1
        b[1] += ok["kw"]
        b[2] += ok["rrf"]
    print("\n分类型（关键字 / RRF 合并）：")
    for kind, (tot, a, b) in by_kind.items():
        print(f"  {kind:4s} {a}/{tot}   {b}/{tot}")

    disputed = [c for c in rc.CASES if c.disputed]
    print(f"\n争议样本（不计入主指标，需人复核）：{len(disputed)} 条")
    for c in disputed:
        print(f"  {c.query!r} -> gold={c.gold_text()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
