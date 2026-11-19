"""Agent Search：7 步流水线（CONTRACT §5.7 / §9.7 真数据化）。

- 真证据：`search_notes`（真检索） + `get_memory_context`（真上下文）。
- `answer.text` 抽取式生成：从证据各篇正文取与 query 最相关的句子，按序串成结论并带 [n] 角标，
  措辞用固定连接句，不编造库中不存在的内容。
- `confidence` 按 top score 分档（≥0.75 较高 / ≥0.5 中等 / 其余 一般）。
- 每次调用写一条 "Agent 检索" 运行记录，stepLabel = "7 / 7"。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, List, Optional

from . import runs as runs_store
from . import vault

# 固定 7 步（§5.7）
STEPS: List[Dict[str, Any]] = [
    {"index": 1, "title": "理解问题", "detail": "解析用户意图"},
    {"index": 2, "title": "选择工具", "detail": "匹配 MCP 工具"},
    {"index": 3, "title": "构造参数", "detail": "生成调用参数"},
    {"index": 4, "title": "调用 MCP", "detail": "执行工具请求"},
    {"index": 5, "title": "筛选候选", "detail": "过滤低相关结果"},
    {"index": 6, "title": "重排证据", "detail": "核验与排序"},
    {"index": 7, "title": "组织回答", "detail": "生成最终结论"},
]

_DEFAULT_QUERY = "MCP 工具调用失败时应该如何重试与降级？"
_SENT_SPLIT = re.compile(r"[。！？!?\n]+")


def _mock_id() -> str:
    return "ar_" + "".join(random.choice("0123456789abcdef") for _ in range(7))


def _sentences(text: str) -> List[str]:
    out: List[str] = []
    for raw in _SENT_SPLIT.split(text or ""):
        s = raw.strip().lstrip("#>-* \t").strip()
        s = re.sub(r"^\|.*\|$", "", s).strip()  # 跳过表格行
        if len(s) >= 8 and not s.startswith("http"):
            out.append(s)
    return out


def _best_sentence(content: str, terms: List[str], used: set) -> Optional[str]:
    best = None
    best_score = -1.0
    for s in _sentences(content):
        if s in used:
            continue
        hits = sum(1 for t in terms if len(t) >= 2 and t.lower() in s.lower())
        if hits == 0:
            continue
        score = hits * 10.0 - len(s) * 0.01
        if score > best_score:
            best_score = score
            best = s
    return best


def _shorten(s: str, limit: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _extractive_answer(evidence: List[Dict[str, Any]], terms: List[str]) -> str:
    segments: List[str] = []
    used: set = set()
    for ev in evidence:
        rec = vault.get_record(ev["id"])
        content = rec["content"] if rec else ""
        sent = _best_sentence(content, terms, used)
        if not sent:
            sent = (rec["summary"] if rec else ev["quote"]) or ev["quote"]
        sent = _shorten(sent).rstrip("。！？；;，, ")
        used.add(sent)
        segments.append(f"{sent}[{ev['rank']}]")
    if not segments:
        return "未在本地 Vault 中检索到与该查询相关的笔记内容。"
    return "；".join(segments) + "。"


def _confidence(top_score: float) -> str:
    if top_score >= 0.75:
        return "较高"
    if top_score >= 0.5:
        return "中等"
    return "一般"


def run_agent_search(
    query: str,
    mode: Optional[str] = None,
    top_k: Optional[int] = None,
    scope: str = "全部笔记",
) -> Dict[str, Any]:
    # 参数缺省时取设置里的检索默认值（§9.9）
    try:
        from . import settings_store

        defaults = settings_store.get_search_defaults()
    except Exception:
        defaults = {"topK": 5, "mode": "hybrid"}

    if mode not in ("keyword", "semantic", "hybrid"):
        mode = defaults["mode"]
    query = (query or "").strip() or _DEFAULT_QUERY
    try:
        top_k = int(top_k) if top_k is not None else defaults["topK"]
    except (TypeError, ValueError):
        top_k = defaults["topK"]
    top_k = max(1, min(5, top_k))

    t0 = time.perf_counter()
    trace: List[Dict[str, str]] = []

    def mark(step: str, note: str) -> None:
        trace.append({"t": f"+{int((time.perf_counter() - t0) * 1000)}ms", "step": step, "note": note})

    terms = vault._terms(query)
    kw = vault.query_chunks(query, limit=4)
    mark("理解问题", f"识别查询「{_shorten(query, 24)}」，提取关键词 " + "、".join(f"「{k}」" for k in kw))

    scope_filter = None
    if scope and scope not in ("全部笔记", "全部"):
        scope_filter = scope
    mark("选择工具", "命中 search_notes、get_memory_context")

    params_note = f"mode={mode}, topK={top_k}, scope={scope}"
    mark("构造参数", params_note)

    # 真调用 1：search_notes（多召回一些候选）
    candidates = vault.search(query, top_k=max(top_k * 2, top_k), mode=mode, path_scope=scope_filter)
    tool_names = ["search_notes"]
    mark("调用 MCP", f"search_notes 命中 {len(candidates)} 篇候选")

    # 真调用 2：get_memory_context
    vault.get_memory_context(query, top_k=min(3, max(1, top_k)))
    tool_names.append("get_memory_context")

    # 筛选 + 重排，取 top_k 作为证据
    evidence_src = sorted(candidates, key=lambda x: (-x["score"], x["id"]))[:top_k]
    mark("筛选候选", f"保留 {len(evidence_src)} 篇高相关笔记")
    mark("重排证据", "按匹配度降序重排")

    evidence: List[Dict[str, Any]] = []
    for i, h in enumerate(evidence_src, start=1):
        evidence.append(
            {
                "rank": i,
                "id": h["id"],
                "title": h["title"],
                "score": h["score"],
                "relPath": h["relPath"],
                "quote": h["quote"],
            }
        )

    answer_text = _extractive_answer(evidence, terms)
    top_score = evidence_src[0]["score"] if evidence_src else 0.0
    confidence = _confidence(top_score)
    used_ranks = [e["rank"] for e in evidence if f"[{e['rank']}]" in answer_text]
    if not used_ranks and evidence:
        used_ranks = [1]

    narration = (
        f"正在解析用户意图：提取关键词 " + "、".join(f"「{k}」" for k in kw) + "。"
        f"调用 search_notes 命中 {len(candidates)} 篇候选，"
        f"重排后保留 {len(evidence_src)} 篇作为证据。"
    )

    elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
    if elapsed_ms < 1:
        elapsed_ms = 1
    mark("组织回答", f"引用 {len(evidence)} 篇证据生成结论")

    stats = {
        "elapsedMs": elapsed_ms,
        "toolCalls": len(tool_names),
        "toolNames": tool_names,
        "citedNotes": len(evidence),
    }

    run_id = _mock_id()
    result: Dict[str, Any] = {
        "id": run_id,
        "query": query,
        "status": "成功",
        "steps": [dict(s, status="完成") for s in STEPS],
        "narration": narration,
        "evidence": evidence,
        "answer": {"text": answer_text, "citations": used_ranks, "confidence": confidence},
        "stats": stats,
        "trace": trace,
    }

    _store[run_id] = result

    runs_store.add_run(
        rtype="Agent 检索",
        target=query,
        status="成功",
        elapsed_ms=elapsed_ms,
        step_label="7 / 7",
        detail={
            "mode": mode,
            "topK": top_k,
            "scope": scope,
            "citedNotes": len(evidence),
            "resultIds": [e["id"] for e in evidence],
        },
        caller="Agent",
    )
    return result


_store: Dict[str, Dict[str, Any]] = {}


def get_agent_run(run_id: str) -> Optional[Dict[str, Any]]:
    return _store.get(run_id)
