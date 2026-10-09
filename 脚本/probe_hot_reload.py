#!/usr/bin/env python3
"""宿主模拟生死测试（热重载存活性探针，常驻版 2026-10-10）。

用途：SDK 大版本升级（v2→v3…）后验证「热重载后宿主续发裸请求」是否存活。
这是 SDK 升级三步规程的第 ② 步，规程全文见
知识_MCPv2迁移落地（2026-10-09） 第五节。

为什么必须存在（2026-10-09 教训）：v2 迁移时用 v2 SDK 客户端做探针，热重载测试
「通过」是假阴性——v2 客户端自带 modern envelope 走 born-ready 路径；而真实宿主
（WorkBuddy）是 legacy 客户端，续发裸 tools/call 被宿主实测 -32602 拒绝。
验证客户端必须与生产客户端同代：本脚本用裸 JSON-RPC 精确复刻宿主行为
（协议 2025-11-25、无 envelope、热重载后不重新握手）。

时序：
  1) 起独立 supervisor + worker（哨兵文件用独立名字，不干扰宿主常驻实例）
  2) initialize → notifications/initialized → tools/call（基线）
  3) 写哨兵 → supervisor 杀 worker 换新（管道不动）
  4) 生死点：裸 tools/call（无 _meta、无重新握手）
  5) tools/list 计数 + memory_context / memory_search since 抽查

退出码 0 = 全过；非 0 = 有失败项（每项独立打印 PASS/FAIL）。
"""

from __future__ import annotations

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不影响脚本本身
    pass

import asyncio
import json
import os
import sys
import time

VAULT = os.environ.get("AI_MEMORY_DIR", os.path.expanduser("~/ai-memory"))
os.environ["AI_MEMORY_DIR"] = VAULT
os.environ["AI_MEMORY_CORE_TOOLS"] = "1"
# 独立哨兵：绝不触碰宿主常驻 supervisor 的 reload.flag
os.environ["AI_MEMORY_RELOAD_FLAG_NAME"] = "probe_reload.flag"

sys.path.insert(0, VAULT)  # 脚本在 脚本/ 子目录，库根（mcp_compat/server.py）需显式入径

SERVER = os.path.join(VAULT, "server.py")
FLAG = os.path.join(VAULT, ".workbuddy", "probe_reload.flag")
PROTOCOL = "2025-11-25"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f" :: {detail}"))


async def main() -> int:
    import mcp_compat  # 顺带打印兼容层认到的 SDK 版本

    print(f"mcp SDK: {mcp_compat.SDK_VERSION}（兼容层判定 major={mcp_compat.SDK_MAJOR}）")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, SERVER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=2 ** 22,
        env=dict(os.environ),
    )
    pending: dict[int, asyncio.Future] = {}

    async def reader() -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in pending:
                pending.pop(rid).set_result(msg)

    reader_task = asyncio.create_task(reader())

    async def request(rid: int, method: str, params: dict) -> dict:
        fut = pending.setdefault(rid, asyncio.get_running_loop().create_future())
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        return await asyncio.wait_for(fut, 30)

    async def notify(method: str, params: dict) -> None:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()

    # 1) 握手（legacy 协议，与 WorkBuddy 宿主同代）
    r1 = await request(1, "initialize", {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "hot-reload-probe", "version": "1.0"},
    })
    sinfo = r1.get("result", {}).get("serverInfo") or r1.get("result", {}).get("server_info") or {}
    print("server:", sinfo.get("name"), "| protocol:", r1.get("result", {}).get("protocolVersion"))
    check("initialize 握手", sinfo.get("name") == "ai-memory", str(r1)[:150])
    await notify("notifications/initialized", {})

    # 2) 基线调用
    r2 = await request(2, "tools/call", {"name": "memory_stats", "arguments": {}})
    check("重载前 tools/call", "result" in r2 and not r2["result"].get("isError"), str(r2)[:150])

    # 3) 热重载（独立哨兵，不影响宿主常驻实例）
    with open(FLAG, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    await asyncio.sleep(3.0)

    # 4) 生死点：裸 tools/call（无 _meta、无重新握手 —— 与宿主续发一致）
    r3 = await request(3, "tools/call", {"name": "memory_stats", "arguments": {}})
    err = r3.get("error")
    check(
        "热重载后裸 tools/call（生死点）",
        "result" in r3 and not r3["result"].get("isError"),
        str(err or r3)[:200],
    )

    # 5) 抽查：tools/list / memory_context / since
    r4 = await request(4, "tools/list", {})
    names = sorted(t["name"] for t in r4.get("result", {}).get("tools", []))
    check("tools/list 6 工具", len(names) == 6, str(names))

    r5 = await request(5, "tools/call", {"name": "memory_context", "arguments": {"title": "项目状态", "max_neighbors": 1}})
    t5 = (r5.get("result", {}).get("content") or [{}])[0].get("text", "") if "result" in r5 else ""
    check("memory_context", "context-pack" in t5, t5[:120])

    r6 = await request(6, "tools/call", {"name": "memory_search", "arguments": {"keyword": "热重载", "since": "30d"}})
    t6 = (r6.get("result", {}).get("content") or [{}])[0].get("text", "") if "result" in r6 else ""
    check("memory_search since", "找到" in t6, t6[:120])

    proc.terminate()
    reader_task.cancel()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n== 宿主模拟生死测试: {len(results) - len(failed)} pass / {len(failed)} fail ==")
    if failed:
        print("FAILED:", failed)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
