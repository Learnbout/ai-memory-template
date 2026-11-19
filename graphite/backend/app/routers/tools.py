"""MCP 工具路由（§5.4 / §5.5 + 全量工具目录新增契约）。"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends

from .. import caps
from .. import tool_catalog as catalog_mod
from .. import tools as tools_mod
from ..schemas import InvokeResult, ToolDef, ToolSource

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=List[ToolDef])
def list_tools() -> List[ToolDef]:
    return [ToolDef(**t) for t in catalog_mod.get_full_catalog()]


@router.get("/tools/sources", response_model=List[ToolSource])
def list_tool_sources() -> List[ToolSource]:
    return [ToolSource(**s) for s in catalog_mod.get_sources()]


# 这里是阶段 9 之前最危险的一个口子：调用**以服务端身份**发出去
# （mcp_client 用配置里的管理员令牌），调用者是谁对 MCP 一侧不可见。
# 不设闸时，一个 editor 就能借管理员令牌调 memory_sql 把 S 级凭据读走。
# 要按人降权得让每次调用带调用者自己的令牌 —— 不塞进阶段 9，先整块只给 admin。
@router.post("/tools/{name}/invoke", response_model=InvokeResult)
def invoke(
    name: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    who: Dict[str, Any] = Depends(caps.require_cap("tool.invoke")),
) -> InvokeResult:
    return InvokeResult(**tools_mod.invoke_tool(name, payload))


@router.post("/tools/mcp/reconnect")
def reconnect_mcp(
    payload: Dict[str, Any] = Body(default_factory=dict),
    who: Dict[str, Any] = Depends(caps.require_cap("tool.reconnect")),
) -> Dict[str, Any]:
    """（重）连接 mcp.json 里的 MCP 服务；body 可带 {"server": "名字"} 只连一个。"""
    from .. import mcp_client

    name = payload.get("server")
    if name:
        return {"results": [mcp_client.connect_server(str(name))]}
    return {"results": list(mcp_client.reconnect().values())}


@router.get("/tools/mcp/status")
def mcp_status() -> Dict[str, Any]:
    """各 MCP 服务的连接状态摘要（不触发连接，只看当前态）。"""
    from .. import mcp_client

    out = {}
    for name, st in mcp_client.status_all().items():
        out[name] = {
            "status": st["status"],
            "error": st["error"],
            "toolCount": len(st["tools"]),
        }
    return out
