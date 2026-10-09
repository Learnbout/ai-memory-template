"""触发并核对 ai-memory MCP server 的原地热重载（supervisor + worker 架构）。

原理：server 以「常驻 supervisor + 可替换 worker」运行。supervisor 持有宿主的
stdin/stdout 管道且永不退出；worker 才真正跑 MCP 服务。本脚本向记忆库
.workbuddy/reload.flag 写哨兵，supervisor 轮询到后杀掉旧 worker、拉起新 worker，
宿主管道全程不动，因此无需重启 WorkBuddy 即可让代码改动生效。

判定依据是 .workbuddy/mcp_boot.log 里的 supervisor / worker 记录：
重载后 supervisor PID 必须不变、worker PID 必须变化。旧版按命令行扫 python.exe，
在双角色架构下会把 supervisor 和 worker 混在一起，无法判断谁被替换，故弃用。

用法：
    python 脚本/reload_mcp.py            # 触发重载并核对
    python 脚本/reload_mcp.py --status   # 只看当前 supervisor/worker 状态

环境变量 AI_MEMORY_DIR 可覆盖记忆库位置（默认取本脚本上一级目录）。
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT = SCRIPT_DIR.parent


def vault() -> Path:
    return Path(os.environ.get("AI_MEMORY_DIR") or DEFAULT_VAULT)


def boot_log_path() -> Path:
    return vault() / ".workbuddy" / "mcp_boot.log"


def flag_path() -> Path:
    return vault() / ".workbuddy" / "reload.flag"


def read_roles() -> tuple[list[int], list[int]]:
    """从启动日志读出全部 supervisor PID 与 worker PID（按出现顺序）。"""
    path = boot_log_path()
    if not path.exists():
        return [], []
    supers: list[int] = []
    workers: list[int] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int):
            continue
        if entry.get("mode") == "supervisor":
            supers.append(pid)
        elif entry.get("mode") == "worker":
            workers.append(pid)
    return supers, workers


def alive(pid: int) -> bool:
    """进程是否存活。

    Windows 用 ctypes 直接查句柄状态，不走 tasklist——tasklist 的输出是 OEM 代码页
    （中文系统为 GBK），按 UTF-8 解码会抛 UnicodeDecodeError，且本地化格式还可能给
    PID 加千位分隔符。其他平台用 kill(pid, 0)。
    """
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def latest_alive(pids: list[int]) -> int | None:
    """从后往前找第一个仍存活的 PID，跳过历史残留记录。"""
    for pid in reversed(pids):
        if alive(pid):
            return pid
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="原地重载 ai-memory MCP server（supervisor + worker）")
    parser.add_argument("--status", action="store_true", help="只查看状态，不触发重载")
    parser.add_argument("--wait", type=float, default=15.0, help="触发后等待 worker 完成替换的秒数")
    args = parser.parse_args()

    print(f"记忆库: {vault()}")
    print(f"启动日志: {boot_log_path()}")

    supers, workers = read_roles()
    sup_pid = latest_alive(supers)
    wk_pid = latest_alive(workers)

    if sup_pid is None and wk_pid is None:
        print("状态: 未发现运行中的 supervisor/worker。")
        print("若 MCP 通道断连，请先重启 WorkBuddy，让宿主重新拉起 server。")
        return 1 if args.status else 2

    if sup_pid is None:
        print("警告: 只找到 worker、没有存活 supervisor——说明当前不是 supervisor 模式。")
        print("      常见原因是 mcp.json 里设了 AI_MEMORY_NO_RELOAD=1；热重载不可用。")
        return 1

    print(f"状态: supervisor PID {sup_pid}" + (f"，worker PID {wk_pid}" if wk_pid else "，worker 未就绪"))

    if args.status:
        return 0

    if wk_pid is None:
        print("worker 未就绪，暂不触发重载。")
        return 3

    flag = flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(str(time.time()), encoding="utf-8")
    print("已写入哨兵文件，等待 supervisor 替换 worker ...")

    deadline = time.time() + args.wait
    new_wk: int | None = None
    while time.time() < deadline:
        time.sleep(0.3)
        _, current_workers = read_roles()
        cand = latest_alive(current_workers)
        if cand is not None and cand != wk_pid:
            new_wk = cand
            break

    new_sup = latest_alive(read_roles()[0])
    if new_wk is None:
        print("超时: worker PID 未更新，重载可能失败。")
        print("排查：查看 .workbuddy/mcp_boot.log 是否新增 worker-spawn 行。")
        return 4

    if new_sup != sup_pid:
        print(f"异常: supervisor PID 由 {sup_pid} 变为 {new_sup}，宿主管道可能已断。")
        return 5

    if flag.exists():
        print("提示: 哨兵文件仍在磁盘（可能被下一轮消费）。")

    print(f"重载成功: supervisor {sup_pid} 保持不变，worker {wk_pid} -> {new_wk}，代码已生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
