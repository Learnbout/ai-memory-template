"""Agent Search 路由（§5.7）。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from .. import agent as agent_mod
from .. import caps
from ..schemas import AgentRun, AgentSearchRequest

router = APIRouter(tags=["agent"])


@router.post("/agent-search", response_model=AgentRun)
def agent_search(
    body: AgentSearchRequest,
    who: Dict[str, Any] = Depends(caps.require_cap("agent.search")),
) -> AgentRun:
    result = agent_mod.run_agent_search(
        query=body.query, mode=body.mode, top_k=body.topK, scope=body.scope
    )
    return AgentRun(**result)


@router.get("/agent-search/{run_id}", response_model=AgentRun)
def agent_run(run_id: str) -> AgentRun:
    result = agent_mod.get_agent_run(run_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"未找到 Agent 运行记录: {run_id}")
    return AgentRun(**result)
