"""运行记录路由（§5.6）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from .. import caps
from .. import runs as runs_store
from ..schemas import OkResponse, RunPage

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=RunPage)
def list_runs(
    type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    page: int = Query(default=1),
    pageSize: int = Query(default=8, alias="pageSize"),
) -> RunPage:
    return RunPage(**runs_store.list_runs(type, status, from_, to, page, pageSize))


@router.delete("/runs", response_model=OkResponse)
def clear_runs(who: Dict[str, Any] = Depends(caps.require_cap("runs.clear"))) -> OkResponse:
    """清空调用历史。记录是排障证据，删它等于销毁日志 → 只给 admin（阶段 9）。"""
    runs_store.clear_runs()
    return OkResponse(ok=True)
