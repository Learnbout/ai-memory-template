"""mcp SDK 兼容层（2026-10-09，G10 迁移后续）：全部 SDK 依赖收口于此。

背景：v1→v2 迁移暴露的问题不是「API 改名」，而是我们三处直连 SDK（server 构造、
工具注册、热重载内部结构）散在两个文件里，其中两处摸了 SDK 内部（_mcp_server /
Connection.for_loop），大版本升级即碎。本层把它们收口成一个文件：

  - 上层（memory_runtime / memory_tools / smoke_test）只 import 本模块；
  - SDK 升级或回滚 = 换 pip 版本即用（v1.30.x 与 v2.x 双分支共存），上层零改动；
  - 新的大版本出来时，只需在本文件加一个分支，并照例用「裸 JSON-RPC 模拟真实
    宿主」打热重载生死点（经验见 知识_MCPv2迁移落地 2026-10-09）。

每个分支都写明依赖哪个内部结构、失效时的退路。
"""

from __future__ import annotations

import functools
import importlib.metadata
import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger("ai-memory.compat")

SDK_VERSION = importlib.metadata.version("mcp")
SDK_MAJOR = int(SDK_VERSION.split(".")[0])

# ToolAnnotations：v1/v2 均在 mcp.types，字段名 v2 起改 snake_case（readOnlyHint →
# read_only_hint），但构造关键字两种写法 pydantic 都接受 v2；v1 只收 camelCase。
# 诊断探针读字段时注意口径差异。
from mcp.types import ToolAnnotations  # noqa: E402,F401

__all__ = [
    "SDK_VERSION",
    "SDK_MAJOR",
    "ToolAnnotations",
    "make_server",
    "register_tool",
    "run_stdio",
    "extract_text",
]


def make_server(name: str, instructions: str | None = None):
    """构造 server 实例。v2 改名 MCPServer（FastMCP 硬删），v1 为 FastMCP。"""
    if SDK_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name, instructions=instructions)
    from mcp.server.fastmcp import FastMCP

    return FastMCP(name, instructions=instructions)


def register_tool(server: Any, fn: Callable, annotations: Any = None) -> Callable:
    """注册一个工具，返回原函数。

    v2：func_metadata.call_fn 原生把同步工具丢 anyio.to_thread（func_metadata.py:164），
    同步函数直接注册即可。
    v1：同步函数会被直接调在事件循环上（阻塞），必须包 async_tool 丢线程池 ——
    这是 2026-09-10 MCP异步改造 的原始动机，回滚 v1 时该行为必须保留。
    """
    if SDK_MAJOR >= 2:
        server.tool(annotations=annotations)(fn)
        return fn

    if inspect.iscoroutinefunction(fn):
        server.tool(annotations=annotations)(fn)
        return fn

    @functools.wraps(fn)
    async def _async_tool(*args, **kwargs):
        import anyio

        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    server.tool(annotations=annotations)(_async_tool)
    return fn


def _apply_v2_born_ready_patch() -> bool:
    """v2：让 legacy 连接 born-ready，语义等价 v1 的 stateless=True。

    依赖内部结构：mcp.server.connection.Connection.for_loop（classmethod）与
    连接对象的 initialized 事件（connection.py:212）。v2 的 legacy 时代连接带
    pre-init 门（runner.py:211）：未握手连接的裸 tools/call 一律 -32602 ——
    热重载后宿主续发的请求正是这种，不打补丁热重载必死（宿主实测）。

    失效退路：SDK 内部结构变了（找不到 for_loop / initialized）就跳过并告警，
    退化为「热重载后需重启宿主」——通道本身不炸，与 v1 时代 stateless 缺失同险级。
    """
    try:
        from mcp.server import connection as _connection_mod

        orig_for_loop = _connection_mod.Connection.for_loop

        def _born_ready_for_loop(cls, *args, **kwargs):
            conn = orig_for_loop(*args, **kwargs)
            evt = getattr(conn, "initialized", None)
            if evt is not None and hasattr(evt, "set"):
                evt.set()
            return conn

        _connection_mod.Connection.for_loop = classmethod(_born_ready_for_loop)
        return True
    except Exception as e:  # noqa: BLE001 - 补丁失败绝不阻塞启动
        logger.warning("born-ready 补丁未生效（SDK 内部结构可能已变）：%s；热重载后将需重启宿主", e)
        return False


def run_stdio(server: Any, *, hot_reload_safe: bool = True) -> None:
    """以 stdio 方式阻塞运行 server。

    v2：born-ready 补丁（可关）+ server.run("stdio")。
    v1：hot_reload_safe=True 时走 lowlevel.run(stateless=True)（依赖 _mcp_server
    内部结构，缺失则退回 server.run()）；False 直接 server.run()。
    """
    if SDK_MAJOR >= 2:
        if hot_reload_safe:
            _apply_v2_born_ready_patch()
        server.run(transport="stdio")
        return

    import anyio
    from mcp.server.stdio import stdio_server

    lowlevel = getattr(server, "_mcp_server", None)
    if not hot_reload_safe or lowlevel is None or not hasattr(lowlevel, "create_initialization_options"):
        server.run()
        return

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await lowlevel.run(
                read_stream,
                write_stream,
                lowlevel.create_initialization_options(),
                stateless=True,
            )

    anyio.run(_main)


def extract_text(res: Any) -> str:
    """call_tool 结果 → 真实文本（测试与探针用）。

    v2：CallToolResult（content[0].text）。
    v1.30：FastMCP.call_tool 直接返回内容块列表（res[0].text）。
    兜底 str() —— 但 repr 会把换行渲染成字面 \\n（09-16 老坑），只作最后退路。
    """
    content = getattr(res, "content", None)
    if content and getattr(content[0], "text", None) is not None:
        return content[0].text
    if isinstance(res, list) and res and getattr(res[0], "text", None) is not None:
        return res[0].text
    if isinstance(res, tuple) and len(res) == 2:
        first = res[0]
        if isinstance(first, list) and first:
            block = first[0]
            if getattr(block, "text", None) is not None:
                return block.text
    return str(res)
