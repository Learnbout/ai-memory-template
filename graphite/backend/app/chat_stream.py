"""库内聊天 SSE 事件流（阶段 10，2026-10-24）。·前缀 `/api/chat`

为什么做这个（2026-10-24 主人拍板）：聊天要开源给别人用，准实时 4s 轮询的
「留言板手感」不够；且 8.3 已实证卡点只在网关不在后端，那就两端一起解决。

## 设计借鉴（2026-10-24 调研 GitHub / 官方文档，各取一块）

1. **sse-starlette / FastAPI ≥0.135 EventSourceResponse**：SSE 帧格式
   （`id:` / `event:` / `data:` / `retry:`）、注释帧心跳（`: ping`）、
   响应头 `Cache-Control: no-cache` + `X-Accel-Buffering: no`。
   本文件**手写**这三个头与心跳而不是引库 —— 服务器 venv 版本未知、
   装包要走国内镜像，帧格式总共 20 行，零新增依赖换版本无关。
2. **Last-Event-ID 补发模式**（cooldev.tools / Laravel 社区 / 掘金 SSE 可靠性
   综述）：事件 id = 消息 id（`messages` 自增主键，天然单调递增）；浏览器
   断线重连时自动带 `Last-Event-ID` 头，服务端补发 `id > Last-Event-ID` 的
   消息再接实时流。语义是「至少一次」投递，重复帧由前端按 id 幂等合并
   （阶段 8.3 的合并逻辑正好是它的消费端）。
3. **AWS SSE 韧性清单 + nginx 实践**：心跳间隔必须小于路径上所有空闲超时
   （20s < nginx 默认 60s）；服务端设连接 TTL 防僵尸连接；`retry:` 告诉
   浏览器重连间隔（内网规模不做随机抖动）。

## 三条实现硬决策

1. **广播线程安全**：写接口（send/revoke/react）是同步函数，跑在 FastAPI
   线程池里；`asyncio.Queue` 不许跨线程摸 —— 统一经主事件循环
   `call_soon_threadsafe` 投递（循环引用在 startup 时由 main.py 注入）。
   没有循环或没人订阅时静默跳过：广播永远「尽力而为」，绝不反向影响落库。
2. **先订阅、再补发**：订阅发生在查库之前，两步之间新落的消息最多重复推
   一次，前端按 id 合并天然消化 —— 不为这点重叠加锁。
3. **退化路径保留**：SSE 只解决「服务端 → 浏览器」单向下行；发送仍是普通
   POST。SSE 连不上时前端自动退回 4s 轮询（阶段 8.3 路径原样保留），
   所以本模块挂掉 = 回到昨天，页面不会坏。

## 已知边界（内网规模，先不做）

- 多实例部署需要共享 pub/sub（Redis 之类）—— 现在单进程单实例，内存队列够。
- 每库订阅数不做上限（内网个位数连接）；连接 TTL 1h 兜底协程寿命。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .paths import ensure_import_path

# 库根进 sys.path（与 chat_api 同一套规矩），下一行才 import 得到库根模块。
ensure_import_path()

from . import chat_acl, chat_db  # noqa: E402

router = APIRouter(tags=["chat-stream"])

# vault_id -> 订阅队列集合。队列元素：{"id": int, "message": dict}
_SUBS: dict[str, set[asyncio.Queue]] = {}

# 主事件循环引用（main.py startup 注入）。写接口在线程池，广播要经它转投。
_LOOP: asyncio.AbstractEventLoop | None = None

# 单条连接最长寿命。到点服务端主动收线，浏览器带 Last-Event-ID 无感重连补发
# （AWS 清单「连接 TTL」：防僵尸连接无限累积）。1 小时对协作页面足够慷慨。
CONNECTION_TTL_SECONDS = 3600.0

# 心跳间隔。硬约束：必须小于路径上所有空闲超时（nginx 类代理默认 60s）。
HEARTBEAT_SECONDS = 20.0

# 补发上限：断线太久不无限灌历史；超量的部分前端靠「打开时整页拉取」兜底。
REPLAY_LIMIT = 200


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """main.py startup 时注入主事件循环（广播投递要用）。"""
    global _LOOP
    _LOOP = loop


def subscriber_count(vault_id: str) -> int:
    """当前该库的 SSE 订阅数（测试与排障用）。"""
    return len(_SUBS.get(vault_id, ()))


def _fmt(event_id: int, message: dict[str, Any]) -> str:
    """一条 SSE 帧。event 名固定 `msg`，payload 是消息的对外完整形态。"""
    return f"id: {event_id}\nevent: msg\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"


def publish(vault_id: str, message: dict[str, Any]) -> None:
    """向该库所有 SSE 订阅者广播一条消息变更（线程安全）。

    三个出口都调它：send（新消息）/ revoke（撤回）/ react（表情变更）。
    payload 一律是**完整消息**而不是增量 —— 与前端「按 id 合并」的幂等
    消费对称：重复收到、乱序收到都无害，这是「至少一次」语义的另一半。
    """
    subs = _SUBS.get(vault_id)
    if not subs or _LOOP is None or _LOOP.is_closed():
        return
    try:
        payload = {"id": int(message["id"]), "message": message}
    except (KeyError, TypeError, ValueError):
        return
    for q in tuple(subs):
        try:
            _LOOP.call_soon_threadsafe(q.put_nowait, payload)
        except RuntimeError:
            # 循环正在关闭：连接马上会被断连/TTL 收走，无需处理
            pass


@router.get("/chat/stream")
async def chat_stream(request: Request, vault_id: str) -> StreamingResponse:
    """SSE 下行流：`msg` 事件推消息变更，注释帧当心跳。

    鉴权与聊天其他接口同一把闸（`require_chat_member`：非成员 403、
    只读 403、无身份 401）。EventSource 不能带自定义头 —— 经网关时身份
    由网关会话注入（cookie → X-AI-Memory-User-B64），直连单机模式本来
    就不需要身份，两种访问形态都天然成立。
    """
    try:
        user = chat_acl.require_user(request.headers.get("X-AI-Memory-User-B64"))
        chat_acl.require_chat_member(user, vault_id)
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    # Last-Event-ID：浏览器重连时自动带；`last_id` 查询参数是给 curl/测试用的等价口。
    raw = request.headers.get("last-event-id") or request.query_params.get("last_id") or ""
    try:
        last_id = max(0, int(raw))
    except ValueError:
        last_id = 0

    q: asyncio.Queue = asyncio.Queue()
    _SUBS.setdefault(vault_id, set()).add(q)

    async def gen() -> AsyncIterator[str]:
        started = time.monotonic()
        try:
            # retry：断线后浏览器重连间隔。hello：注释帧，客户端用它确认
            # 「网关没有缓冲」（有缓冲时这帧会迟迟到不了）。
            yield "retry: 3000\n\n: hello\n\n"
            if last_id > 0:
                for it in chat_db.since(vault_id, last_id, REPLAY_LIMIT):
                    yield _fmt(int(it["id"]), it)
            while True:
                if await request.is_disconnected():
                    return
                if time.monotonic() - started > CONNECTION_TTL_SECONDS:
                    # 主动收线：浏览器会带 Last-Event-ID 重连，补发让序列无缝
                    yield ": ttl\n\n"
                    return
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield f": ping {HEARTBEAT_SECONDS}s\n\n"
                    continue
                yield _fmt(int(payload["id"]), payload["message"])
        finally:
            _SUBS.get(vault_id, set()).discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx 类代理看到这个头就不缓冲（本项目的网关在阶段 10 也已支持流式）
            "X-Accel-Buffering": "no",
        },
    )
