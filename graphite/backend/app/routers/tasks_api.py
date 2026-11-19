"""任务队列 HTTP 层（阶段 23，2026-10-25）。

三个端点：提交、查单个、查最近 + 统计。
全部走 `vault.reindex` 这个能力 —— 任务面板能看到「谁在跑重活」，只有管理员需要看；
普通成员要用重活时仍走各自的业务接口（plan 导出等），不需要这个面板。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import caps, task_queue, vault

router = APIRouter(tags=["tasks"])


@router.get("/tasks")
def tasks_list(
    limit: int = Query(default=20, ge=1, le=50),
    who: Dict[str, Any] = Depends(caps.require_cap("vault.reindex")),
) -> Dict[str, Any]:
    return {"ok": True, "stats": task_queue.stats(), "items": task_queue.recent(limit)}


@router.get("/tasks/{task_id}")
def tasks_get(
    task_id: str,
    who: Dict[str, Any] = Depends(caps.require_cap("vault.reindex")),
) -> Dict[str, Any]:
    rec = task_queue.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    return {"ok": True, "task": rec}


@router.post("/tasks/reindex")
def tasks_reindex(
    who: Dict[str, Any] = Depends(caps.require_cap("vault.reindex")),
) -> Dict[str, Any]:
    """异步重建索引：立刻返回 task_id。

    为什么保留同步的 `/vault/reindex`：它是既有测试与脚本的契约（阶段 10/11 的探针
    都在用），改签名会让那批验收脚本集体失效。异步版走新路径，两者并存。
    key 固定 → 两个管理员同时点也只会跑一次。
    """
    rec = task_queue.submit("vault.reindex", vault.reindex, key="vault.reindex", label="重建索引")
    return {"ok": True, "task": rec}
