"""事件推送端点（2026-10-26 00:05）。

`GET /api/events` —— 一条 SSE 长连接，服务端发现库变了就推 `data-changed`。

为什么用 SSE 而不是 WebSocket：这里只有「服务端 → 客户端」单向通知，
SSE 是原生 EventSource 一行代码、断线自动重连、走普通 HTTP（网关不用改协议），
比 WebSocket 少一整套握手与心跳维护。

心跳：每 25 秒没有事件就发一行注释（`: ping`），防止中间设备把空闲连接掐掉。
"""

from __future__ import annotations

import asyncio
import json
import queue
from typing import AsyncIterator, Dict, Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import data_stream

router = APIRouter(tags=["events"])


@router.get("/events")
def events() -> StreamingResponse:
    q = data_stream.subscribe()

    async def gen() -> AsyncIterator[str]:
        try:
            # 立刻给一个注释：让客户端知道连接已建立（EventSource 的 onopen 也会触发）
            yield ": connected\n\n"
            loop = asyncio.get_running_loop()
            while True:
                try:
                    item: Dict[str, Any] = await loop.run_in_executor(None, q.get, True, 25)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            data_stream.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # 有些反代会缓冲响应体，加了这条才真的流式（网关自己已经是流式透传）
            "X-Accel-Buffering": "no",
        },
    )
