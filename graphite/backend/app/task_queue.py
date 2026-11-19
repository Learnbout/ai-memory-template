"""进程内任务队列（阶段 23，2026-10-25）

── 解决的问题（主人原话）────────────────────────────────────
「服务器用队列做异步线程，要不然多打开很卡。」

根因不是「接口慢」，是**重活跑在请求线程里**：重建索引、备份、轮转审计库这些
动辄几秒到几十秒的操作，一旦由某个客户端触发，那一个请求线程就被占住；
FastAPI 的默认线程池被几个这样的活占满，其他客户端的普通读接口就开始排队。

── 为什么不用 Celery / Redis / RQ ─────────────────────────
单人维护、单机部署。多养一个常驻中间件（外加 broker 与服务端两套配置、两套
部署脚本、两套故障模式）换来的只是「多机分发」，而这里只有一台机器。
进程内「1 个 worker 线程 + 内存队列」就把问题解决干净了 —— 与《零新增优先》一致。

**worker 数固定为 1，是刻意的**：这些活全是重 CPU/重 IO，并发 2 只会让两边都变慢、
还会让「同一份库被两个活同时动」变成可能。串行 + 可见的排队状态，比并发更快也更好懂。

── 状态与持久化 ────────────────────────────────────────────
queued → running → done / failed；进程重启时残留的 running 一律标 interrupted
（不假装还在跑，否则前端会永远转圈）。
状态落 `paths.data_dir()/tasks.json`（源码态就是 `backend/data/tasks.json`，
冻结态是用户数据目录），原子替换写（os.replace），只保留最近 50 条。

── 边界（刻意不做）────────────────────────────────────────
· 不支持取消（重活中途打断会留下半更新状态，比不做更糟）
· 不支持跨进程（这是单实例方案；真要集群再说，那时该换的是整个部署形态）
· 不接受任意 callable 的远程提交 —— 只有代码里显式注册的任务才能进队列
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import paths

# 只保留最近这么多个任务记录（状态文件会一直长，超出就丢最旧的）
_MAX_KEEP = 50

_LOCK = threading.RLock()
_TASKS: Dict[str, Dict[str, Any]] = {}
_ORDER: List[str] = []
_DEDUP: Dict[str, str] = {}
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_LOADED = False

# 2026-10-27（R2）：去掉导入期求值。原来这里是模块级 `_STATE_FILE = paths.DATA_DIR / ...`，
# 导致 import 那一刻就把路径冻死，冻结态会写进解包临时目录（退出即蒸发）。
def _state_file() -> Path:
    return paths.data_dir() / "tasks.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load_once() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        raw = _state_file().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    for t in tasks:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        # 重启时还在 running 的：进程已经没了，不可能再跑完
        if t.get("status") == "running":
            t["status"] = "interrupted"
            t["error"] = "服务重启，任务中断"
            t["finished_at"] = _now()
        _TASKS[t["id"]] = t
        _ORDER.append(t["id"])


def _save() -> None:
    """原子写：先写 .tmp 再 replace —— 直接写会被服务重启/断电截断成半个 JSON"""
    payload = {"tasks": [_TASKS[i] for i in _ORDER if i in _TASKS]}
    state_file = _state_file()
    tmp = state_file.with_suffix(".json.tmp")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, state_file)
    except OSError:
        # 状态文件写不进去不该让业务失败 —— 内存里的状态仍然是准的
        pass


def _trim_locked() -> None:
    while len(_ORDER) > _MAX_KEEP:
        old = _ORDER.pop(0)
        _TASKS.pop(old, None)


def _executor_get() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gw-task")
    return _EXECUTOR


def submit(kind: str, fn: Callable[[], Any], *, key: str = "", label: str = "") -> Dict[str, Any]:
    """把 fn 丢进队列，立刻返回任务记录。

    key 非空时做去重：同一个 key 已经在排队/运行，就返回那一条，不再重复提交
    （典型场景：两个管理员同时点「重建索引」）。
    """
    _load_once()
    with _LOCK:
        if key:
            exist = _DEDUP.get(key)
            if exist and exist in _TASKS and _TASKS[exist]["status"] in ("queued", "running"):
                return dict(_TASKS[exist])

        tid = uuid.uuid4().hex[:12]
        rec: Dict[str, Any] = {
            "id": tid,
            "kind": kind,
            "label": label or kind,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
        }
        _TASKS[tid] = rec
        _ORDER.append(tid)
        if key:
            _DEDUP[key] = tid
        _trim_locked()
        _save()

    def runner() -> None:
        with _LOCK:
            rec["status"] = "running"
            rec["started_at"] = _now()
            _save()
        try:
            out = fn()
            with _LOCK:
                rec["status"] = "done"
                rec["result"] = out
                rec["finished_at"] = _now()
                _save()
        except Exception as e:  # noqa: BLE001 —— 任何异常都要落成可读状态，不能吞
            with _LOCK:
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["finished_at"] = _now()
                _save()
        finally:
            with _LOCK:
                if key and _DEDUP.get(key) == tid:
                    _DEDUP.pop(key, None)

    _executor_get().submit(runner)
    return dict(rec)


def get(task_id: str) -> Optional[Dict[str, Any]]:
    _load_once()
    with _LOCK:
        rec = _TASKS.get(task_id)
        return dict(rec) if rec else None


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    _load_once()
    with _LOCK:
        ids = list(reversed(_ORDER))[: max(1, min(limit, _MAX_KEEP))]
        return [dict(_TASKS[i]) for i in ids if i in _TASKS]


def stats() -> Dict[str, Any]:
    _load_once()
    with _LOCK:
        counts: Dict[str, int] = {}
        for rec in _TASKS.values():
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        return {"total": len(_TASKS), "by_status": counts, "workers": 1}
