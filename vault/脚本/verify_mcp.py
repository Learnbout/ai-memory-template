"""自检 ai-memory MCP server：起一份 supervisor 实例，走完整握手并调一次 memory_stats。

相比旧版有三点修正：
1. initialize 与 tools/call 走**同一条** stdin/stdout 管道。旧版两次调用各起一个
   进程，第二次没有握手却直接 tools/call，语义上不成立，结论也不可靠。
2. 以 supervisor 模式启动（不设 AI_MEMORY_NO_RELOAD），顺带验证 supervisor 是否
   正确拉起 worker；并核对 .workbuddy/mcp_boot.log 是否记录了 supervisor/worker。
3. 结束时用 taskkill /T 清理整棵进程树，不留孤儿 supervisor。

退出码：0 = 一切正常；1 = 握手/调用/角色判定失败。
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = SCRIPT_DIR.parent
SERVER = SCRIPT_DIR.parent / "server.py"

# 新版特征：memory_stats 在 2026-09-10 优化后输出这两个字段
NEW_MARKERS = ("正文双链数", "schema_version")
OLD_MARKERS = ("总双向链接数",)


def vault() -> Path:
    return Path(os.environ.get("AI_MEMORY_DIR") or DEFAULT_VAULT)


def boot_log_path() -> Path:
    return vault() / ".workbuddy" / "mcp_boot.log"


class StdioClient:
    """单进程 stdio 客户端：一次启动，整条管道复用。"""

    def __init__(self) -> None:
        env = {**os.environ, "AI_MEMORY_DIR": str(vault())}
        env.pop("AI_MEMORY_NO_RELOAD", None)  # 强制 supervisor 模式
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(SERVER.parent),
            env=env,
        )
        self._buf: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._buf.append(line)

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def await_id(self, rid: int, timeout: float = 25.0) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                for i, line in enumerate(self._buf):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("id") == rid:
                        del self._buf[i]
                        return obj
            time.sleep(0.05)
        return None

    def kill_tree(self) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
            )
        else:
            self.proc.kill()


def boot_roles() -> tuple[int | None, int | None]:
    path = boot_log_path()
    if not path.exists():
        return None, None
    sup: int | None = None
    worker: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("mode") == "supervisor":
            sup = entry.get("pid")
        elif entry.get("mode") == "worker":
            worker = entry.get("pid")
    return sup, worker


def main() -> int:
    if not SERVER.exists():
        print(f"找不到 server.py：{SERVER}")
        return 1

    client = StdioClient()
    try:
        time.sleep(1.2)  # 等 supervisor 拉起 worker

        client.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "verify_mcp", "version": "2"},
            },
        })
        init = client.await_id(1)
        if init is None or "serverInfo" not in init.get("result", {}):
            print("握手失败，未取得 serverInfo。")
            print(f"原始返回：{json.dumps(init, ensure_ascii=False)[:300]}")
            return 1
        print(f"握手成功：serverInfo.name={init['result']['serverInfo'].get('name')}")

        client.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        client.send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "memory_stats", "arguments": {}},
        })
        stats = client.await_id(2)
        try:
            text = stats["result"]["content"][0]["text"]
        except (TypeError, KeyError, IndexError):
            print("memory_stats 调用失败（同管道第二跳未返回）。")
            print(f"原始返回：{json.dumps(stats, ensure_ascii=False)[:300]}")
            return 1

        head = " | ".join(line for line in text.splitlines() if line.strip())[:160]
        print(f"memory_stats 正常：{head}")

        sup, worker = boot_roles()
        if sup is None or worker is None:
            print("启动日志缺少 supervisor/worker 记录——supervisor 模式可能未生效。")
            return 1
        print(f"角色判定：supervisor PID {sup}，worker PID {worker}")

        if any(m in text for m in NEW_MARKERS):
            print("判定：已加载最新代码。")
            return 0
        if any(m in text for m in OLD_MARKERS):
            print("判定：仍是旧代码，请重载或重启 WorkBuddy。")
            return 1
        print("判定：无法识别版本特征，请人工确认。")
        return 1
    finally:
        client.kill_tree()


if __name__ == "__main__":
    sys.exit(main())
