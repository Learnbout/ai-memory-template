"""运行记录存储：预置数据 + 持久化到 backend/data/runs.json。

口径：对外 total 固定报 128 条（与设计稿「共 128 条 · 示例数据仅展示 8 条」一致），
items 只返回真实预置 / 运行期产生的记录。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .paths import data_dir as _data_dir

# 2026-10-27（R1）：data 目录改由 paths.py 统一解析。
#
# 改造前这里自算了一遍 `dirname(dirname(abspath(__file__)))/data`，与 paths.DATA_DIR
# 职责重复。开发态两者解析结果**逐字节相同**（改前实测已记录），所以本补丁对现有
# 行为零影响；但冻结成 exe 或进容器后 `__file__` 的语义会变，两份实现就会分叉 ——
# 一份跟着挂载走、一份指向解包临时目录，症状是「数据看起来存了，重启就没了」。
#
# 2026-10-27（R2）：**去掉导入期求值**。原来这里写 `DATA_DIR = str(_PATHS_DATA_DIR)`
# 是在 import 那一刻就把路径冻成常量，data_dir() 改成函数也没用。现在改成每次用
# 到才问，冻结态才能落到用户目录。
def _runs_file() -> str:
    return os.path.join(str(_data_dir()), "runs.json")

REPORTED_TOTAL = 128

# 调用方缺省值（§5.8）：RunRecord.caller ∈ {"Agent", "手动"}
DEFAULT_CALLER = "手动"

# 由 Agent 主动发起的 MCP 工具（§5.8 派生约定：search_notes / read_note 记为 Agent）
_AGENT_TOOLS = {"search_notes", "read_note"}

# 基准「今天」取机器当前日期
_TODAY = date.today()


def _infer_caller(rec: Dict[str, Any]) -> str:
    """推断记录的调用方：Agent 检索 → Agent；MCP 调用中 search_notes/read_note → Agent，其余 → 手动。"""
    if rec.get("type") == "Agent 检索":
        return "Agent"
    if rec.get("type") == "MCP 调用" and rec.get("target") in _AGENT_TOOLS:
        return "Agent"
    return DEFAULT_CALLER


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _label_for(d: date) -> str:
    if d == _TODAY:
        return "今天"
    if d == _TODAY - timedelta(days=1):
        return "昨天"
    return d.strftime("%m-%d")


def _d(days_ago: int, hms: str) -> str:
    d = _TODAY - timedelta(days=days_ago)
    return f"{d.strftime('%Y-%m-%d')}T{hms}+08:00"


def _preset() -> List[Dict[str, Any]]:
    """预置 >= 12 条记录。含 1 失败 / 1 警告 / 1 运行中。

    每条按 §5.8 约定补齐 caller（search_notes / read_note 为 Agent，其余 MCP 调用为 手动）。
    """
    recs = _preset_raw()
    for r in recs:
        r.setdefault("caller", _infer_caller(r))
    return recs


def _preset_raw() -> List[Dict[str, Any]]:
    """预置 >= 12 条记录。含 1 失败 / 1 警告 / 1 运行中。"""
    return [
        {
            "id": "r_001",
            "at": _d(0, "10:42:18"),
            "type": "MCP 调用",
            "target": "search_notes",
            "status": "成功",
            "elapsedMs": 186,
            "stepLabel": "1",
            "detail": {
                "args": {
                    "query": "MCP 工具调用失败时如何重试",
                    "top_k": 5,
                    "mode": "hybrid",
                    "path_scope": "/知识库",
                },
                "mockId": "req_8a2f91c",
                "resultCount": 4,
            },
        },
        {
            "id": "r_002",
            "at": _d(0, "10:35:02"),
            "type": "MCP 调用",
            "target": "search_tags",
            "status": "失败",
            "elapsedMs": 1200,
            "stepLabel": "1",
            "detail": {
                "args": {"query": "不存在-的-标签", "top_k": 10},
                "error": {"code": "TIMEOUT", "message": "请求超时（timeout 1000ms）"},
            },
        },
        {
            "id": "r_003",
            "at": _d(0, "10:20:11"),
            "type": "MCP 调用",
            "target": "write_note",
            "status": "警告",
            "elapsedMs": 12,
            "stepLabel": "1",
            "detail": {
                "args": {"path": "/知识库/技术/新建笔记.md", "content": "..."},
                "error": {"code": "WRITE_BLOCKED", "message": "写操作已在演示模式被拦截"},
            },
        },
        {
            "id": "r_004",
            "at": _d(0, "10:05:40"),
            "type": "Agent 检索",
            "target": "MCP 工具调用失败时应该如何处理超时与降级？",
            "status": "运行中",
            "elapsedMs": 0,
            "stepLabel": "4 / 7",
            "detail": {
                "mode": "hybrid",
                "topK": 5,
                "scope": "全部笔记",
                "currentStep": {"index": 4, "title": "调用 MCP", "detail": "执行工具请求"},
            },
        },
        {
            "id": "r_005",
            "at": _d(0, "09:58:12"),
            "type": "Agent 检索",
            "target": "MCP 工具调用失败时应该如何重试与降级？",
            "status": "成功",
            "elapsedMs": 2400,
            "stepLabel": "7 / 7",
            "detail": {
                "mode": "hybrid",
                "topK": 5,
                "scope": "全部笔记",
                "citedNotes": 4,
            },
        },
        {
            "id": "r_006",
            "at": _d(0, "09:30:00"),
            "type": "MCP 调用",
            "target": "read_note",
            "status": "成功",
            "elapsedMs": 42,
            "stepLabel": "1",
            "detail": {"args": {"id": "n_7f21ac"}, "resultCount": 1},
        },
        {
            "id": "r_007",
            "at": _d(0, "09:12:44"),
            "type": "MCP 调用",
            "target": "search_notes",
            "status": "成功",
            "elapsedMs": 205,
            "stepLabel": "1",
            "detail": {
                "args": {"query": "检索召回率优化", "top_k": 3, "mode": "keyword"},
                "resultCount": 3,
            },
        },
        {
            "id": "r_008",
            "at": _d(1, "18:12:33"),
            "type": "Agent 检索",
            "target": "记忆上下文缓存策略与活性检测",
            "status": "成功",
            "elapsedMs": 3100,
            "stepLabel": "7 / 7",
            "detail": {"mode": "semantic", "topK": 5, "scope": "全部笔记", "citedNotes": 3},
        },
        {
            "id": "r_009",
            "at": _d(1, "17:40:21"),
            "type": "MCP 调用",
            "target": "list_notes",
            "status": "成功",
            "elapsedMs": 88,
            "stepLabel": "1",
            "detail": {"args": {"type": "技术笔记"}, "resultCount": 6},
        },
        {
            "id": "r_010",
            "at": _d(1, "16:22:09"),
            "type": "MCP 调用",
            "target": "get_memory_context",
            "status": "成功",
            "elapsedMs": 210,
            "stepLabel": "1",
            "detail": {"args": {"query": "工具权限分级", "top_k": 3}, "resultCount": 3},
        },
        {
            "id": "r_011",
            "at": _d(1, "15:03:55"),
            "type": "MCP 调用",
            "target": "list_backlinks",
            "status": "成功",
            "elapsedMs": 33,
            "stepLabel": "1",
            "detail": {"args": {"id": "n_7f21ac"}, "resultCount": 4},
        },
        {
            "id": "r_012",
            "at": _d(1, "11:00:11"),
            "type": "MCP 调用",
            "target": "search_notes",
            "status": "成功",
            "elapsedMs": 174,
            "stepLabel": "1",
            "detail": {"args": {"query": "任务队列并发控制", "top_k": 5, "mode": "hybrid"}, "resultCount": 4},
        },
        {
            "id": "r_013",
            "at": _d(1, "10:15:44"),
            "type": "MCP 调用",
            "target": "search_notes",
            "status": "成功",
            "elapsedMs": 174,
            "stepLabel": "1",
            "detail": {"args": {"query": "MCP 错误处理中间件", "top_k": 5, "mode": "hybrid"}, "resultCount": 3},
        },
        {
            "id": "r_014",
            "at": _d(2, "09:40:12"),
            "type": "MCP 调用",
            "target": "search_tags",
            "status": "成功",
            "elapsedMs": 96,
            "stepLabel": "1",
            "detail": {"args": {"query": "MCP"}, "resultCount": 2},
        },
        # 6 月 10 日批次
        {
            "id": "r_015",
            "at": "2026-06-10T11:20:07+08:00",
            "type": "Agent 检索",
            "target": "MCP 协议握手与能力协商要点",
            "status": "成功",
            "elapsedMs": 2650,
            "stepLabel": "7 / 7",
            "detail": {"mode": "hybrid", "topK": 4, "scope": "全部笔记", "citedNotes": 4},
        },
        {
            "id": "r_016",
            "at": "2026-06-10T10:15:44+08:00",
            "type": "MCP 调用",
            "target": "search_notes",
            "status": "成功",
            "elapsedMs": 168,
            "stepLabel": "1",
            "detail": {"args": {"query": "协议设计要点", "top_k": 5, "mode": "semantic"}, "resultCount": 5},
        },
        {
            "id": "r_017",
            "at": "2026-06-10T09:40:12+08:00",
            "type": "MCP 调用",
            "target": "list_notes",
            "status": "成功",
            "elapsedMs": 74,
            "stepLabel": "1",
            "detail": {"args": {"type": "会议"}, "resultCount": 3},
        },
    ]


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #
_runs: Optional[List[Dict[str, Any]]] = None
_seq = 100


def _migrate_callers(items: List[Dict[str, Any]]) -> bool:
    """给缺 caller 的既有记录补默认值（§5.8 一次性迁移），返回是否有改动。"""
    changed = False
    for r in items:
        if not r.get("caller"):
            r["caller"] = _infer_caller(r)
            changed = True
    return changed


def _dedupe_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 id 去重（保留首次出现，即最新插入的那条），返回新列表。"""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for r in items:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def _resync_seq(items: List[Dict[str, Any]]) -> bool:
    """按既有 id 的最大数字后缀回推 _seq，避免进程重启后 r_101 撞号。

    返回是否有改动（有则需回写）。历史遗留的重复 id 也会在此一并清掉。
    """
    global _seq
    deduped = _dedupe_by_id(items)
    changed = len(deduped) != len(items)
    if changed:
        items[:] = deduped
    max_n = 0
    for r in items:
        rid = r.get("id", "")
        if isinstance(rid, str) and rid.startswith("r_"):
            tail = rid[2:]
            if tail.isdigit():
                max_n = max(max_n, int(tail))
    if max_n >= _seq:
        _seq = max_n
    return changed


def _ensure_loaded() -> List[Dict[str, Any]]:
    global _runs
    if _runs is not None:
        return _runs
    runs_file = _runs_file()
    if os.path.isfile(runs_file):
        try:
            with open(runs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                _runs = data
                # 既有记录不丢：补齐 caller + 去重撞号 id + 回推 _seq
                dirty = _migrate_callers(_runs)
                dirty = _resync_seq(_runs) or dirty
                if dirty:
                    _save()
                return _runs
        except (json.JSONDecodeError, OSError):
            pass
    _runs = _preset()
    _resync_seq(_runs)
    _save()
    return _runs


def _save() -> None:
    runs_file = _runs_file()
    os.makedirs(os.path.dirname(runs_file), exist_ok=True)
    with open(runs_file, "w", encoding="utf-8") as f:
        json.dump(_runs or [], f, ensure_ascii=False, indent=2)


def _enrich(r: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 dateLabel / timeLabel / caller（存储只存 at，展示标签动态计算）。"""
    out = dict(r)
    iso = r.get("at", "")
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        out["dateLabel"] = _label_for(dt.date())
        out["timeLabel"] = dt.strftime("%H:%M:%S")
    except ValueError:
        out["dateLabel"] = out.get("dateLabel", "")
        out["timeLabel"] = out.get("timeLabel", "")
    if not out.get("caller"):
        out["caller"] = _infer_caller(out)
    out.setdefault("detail", {})
    return out


def list_runs(
    rtype: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    page_size: int = 8,
) -> Dict[str, Any]:
    items = _ensure_loaded()
    filtered = list(items)
    has_filter = False
    if rtype and rtype not in ("全部", "all"):
        filtered = [r for r in filtered if r["type"] == rtype]
        has_filter = True
    if status and status not in ("全部", "all"):
        filtered = [r for r in filtered if r["status"] == status]
        has_filter = True
    if date_from:
        filtered = [r for r in filtered if r["at"][:10] >= date_from]
        has_filter = True
    if date_to:
        filtered = [r for r in filtered if r["at"][:10] <= date_to]
        has_filter = True

    filtered.sort(key=lambda r: r["at"], reverse=True)
    total = len(filtered) if has_filter else REPORTED_TOTAL

    page = max(1, page)
    page_size = max(1, page_size)
    start = (page - 1) * page_size
    window = filtered[start : start + page_size]
    return {
        "total": total,
        "page": page,
        "pageSize": page_size,
        "items": [_enrich(r) for r in window],
    }


def add_run(
    rtype: str,
    target: str,
    status: str,
    elapsed_ms: int,
    step_label: str = "1",
    detail: Optional[Dict[str, Any]] = None,
    caller: str = DEFAULT_CALLER,
) -> Dict[str, Any]:
    global _seq
    items = _ensure_loaded()
    _seq += 1
    now = datetime.now()
    rec = {
        "id": f"r_{_seq:03d}",
        "at": _iso(now),
        "type": rtype,
        "target": target,
        "status": status,
        "elapsedMs": elapsed_ms,
        "stepLabel": step_label,
        "caller": caller or DEFAULT_CALLER,
        "detail": detail or {},
    }
    items.insert(0, rec)
    _save()
    return _enrich(rec)


def clear_runs() -> bool:
    global _runs
    _runs = []
    _save()
    return True
