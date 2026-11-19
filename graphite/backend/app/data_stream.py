"""数据变更推送（2026-10-26 00:05）。

── 为什么把轮询换成推送（主人原话）────────────────────────
「不要每 20 秒，这样子如果开的多的话会很卡呀」

原来的做法是每个浏览器标签页各自每 20 秒问一次 `/api/data/rev`。
开 5 个标签页 = 每 20 秒 5 个请求；开 20 个 = 20 个。单个请求很轻（<100 字节），
但连接数、TLS/HTTP 开销、以及「每个客户端各自承担的定时器唤醒」都是浪费。

现在改成：**服务端一个线程每 12 秒查一次**（无论多少客户端），变了就推给所有
SSE 订阅者。客户端零轮询，只保持一条长连接（浏览器原生 EventSource，断线自动重连）。

代价（诚实写）：多一条常驻 SSE 连接/标签页；服务端多一个 daemon 线程。
两者都比 N 个定时轮询便宜，且网关 8766 已经支持 SSE 流式透传（阶段 10 加的）。

── rev 的口径 ────────────────────────────────────────────
笔记数 + 最新 updatedAt。与 `/api/data/rev` 完全一致（那边留着手工排查用）。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Set

from . import vault

_subscribers: Set["queue.Queue[Dict[str, Any]]"] = set()
_lock = threading.Lock()
_last_rev = ""
_started = False


def compute_rev() -> str:
    """库的版本戳。故意做得极轻：不排序、不筛、只扫两列。"""
    notes = vault.list_notes()
    latest = ""
    for n in notes:
        v = n.get("updatedAt") or ""
        if v > latest:
            latest = v
    return f"{len(notes)}:{latest}"


def _broadcast(payload: Dict[str, Any]) -> None:
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:  # noqa: BLE001 —— 队列满/已关，丢掉这一个订阅者的一次推送
            pass


def _tick_loop() -> None:
    global _last_rev
    while True:
        time.sleep(12)
        try:
            rev = compute_rev()
        except Exception:  # noqa: BLE001 —— 库暂时读不出来就跳过这一轮
            continue
        if _last_rev and rev != _last_rev:
            _broadcast({"type": "data-changed", "rev": rev})
        _last_rev = rev


def start() -> None:
    """启动后台巡检线程。幂等；由 main.py 的 startup 调用。"""
    global _started, _last_rev
    if _started:
        return
    _started = True
    try:
        _last_rev = compute_rev()
    except Exception:  # noqa: BLE001
        _last_rev = ""
    threading.Thread(target=_tick_loop, name="gw-data-tick", daemon=True).start()


def subscribe() -> "queue.Queue[Dict[str, Any]]":
    q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=32)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: "queue.Queue[Dict[str, Any]]") -> None:
    with _lock:
        _subscribers.discard(q)
