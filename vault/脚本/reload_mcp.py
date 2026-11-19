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
from datetime import datetime
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


def read_clusters() -> dict[int, list[int]]:
    """读出「supervisor -> 它拉起过的 worker 列表」的归属关系（2026-10-19 补）。

    ## 为什么必须分归属

    旧版把日志里所有 worker 当成一个池子，取「最新存活的那个」。单实例时没问题，
    但本机实测**同时有两个 supervisor 各自服务一个宿主**（一个桌面端、一个 CLI），
    于是：

    - 哨兵是原子 unlink，谁先轮询谁吃掉 -> 只重载其中一个；
    - 另一个 supervisor 拉起的新 worker 会被误认成「刚才那个的替换」，
      于是脚本打印「重载成功」，而真正服务你那个会话的 worker 仍是旧代码。

    这不是理论风险：2026-10-19 实测中它连续误报两次（还把 PID 报成"回退"）。

    归属依据是日志里的 `worker-spawn pid=<supervisor>` 行 —— 它出现之后的第一条
    `worker` 行就属于该 supervisor。所以必须**按顺序**扫，不能先分类再配对。
    """
    path = boot_log_path()
    if not path.exists():
        return {}
    clusters: dict[int, list[int]] = {}
    pending_sup: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int):
            continue
        mode = entry.get("mode") or ""
        if mode == "supervisor":
            clusters.setdefault(pid, [])
        elif mode.startswith("worker-spawn"):
            # 形如 "worker-spawn pid=46540"，pid 字段即其 supervisor
            pending_sup = pid
            clusters.setdefault(pid, [])
        elif mode == "worker":
            if pending_sup is not None:
                clusters.setdefault(pending_sup, []).append(pid)
            pending_sup = None
    return clusters


def read_times() -> dict[int, float]:
    """{PID: 日志里该 PID **首次**出现的时间戳(epoch)}。

    必须是首次而不是最后一次：supervisor 的 PID 会反复出现在它自己拉起的
    `worker-spawn pid=<supervisor>` 行里，用「最后一次」会把时间推后好几个小时
    （实测 28896 被推后 7162 秒），于是与进程真实启动时间对不上，
    所有存活判定都会误判为否 —— 第一版就踩了这个坑。
    """
    path = boot_log_path()
    out: dict[int, float] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            pid = entry.get("pid")
            ts = entry.get("time")
        except json.JSONDecodeError:
            continue
        if not isinstance(pid, int) or not isinstance(ts, str) or pid in out:
            continue
        try:
            out[pid] = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
    return out


def live_worker_of(clusters: dict[int, list[int]], sup: int,
                   times: dict[int, float] | None = None) -> int | None:
    """某个 supervisor 名下最新**确认存活**的 worker。"""
    times = times or {}
    for pid in reversed(clusters.get(sup, [])):
        if alive_since(pid, times.get(pid)):
            return pid
    return None


def start_time(pid: int) -> float | None:
    """进程启动时间（epoch 秒）；取不到返回 None。

    ## 为什么光有 alive() 不够

    日志是**只追加**的，里面躺着大量历史 PID。Windows 会复用 PID，于是
    `OpenProcess` 对一个早就退出的 supervisor 的旧 PID 也可能返回可用句柄 ——
    实测 `--status` 因此报出 7 个"存活 supervisor"，其中 5 个是假阳性
    （PID 被后续的 python 进程，包括测试脚本自己占用了）。

    判据必须是「这个 PID 对应的进程，是不是日志里那一次启动」。用启动时间对齐：
    进程真实启动时间与日志记录时间之差应在分钟级以内，否则就是复用。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            )
            if not ok:
                return None
            # FILETIME：1601-01-01 起的 100ns 计数
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return ticks / 1e7 - 11644473600.0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def alive_since(pid: int, log_epoch: float | None, tolerance: float = 300.0) -> bool:
    """这是不是日志里那一次启动的进程（而不是复用同一 PID 的别的进程）。"""
    if not alive(pid):
        return False
    if log_epoch is None:
        return True  # 拿不到日志时间就只能退回旧判据
    st = start_time(pid)
    if st is None:
        return False
    return abs(st - log_epoch) <= tolerance


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

    clusters = read_clusters()
    times = read_times()
    live = {s: live_worker_of(clusters, s, times) for s in clusters if alive_since(s, times.get(s))}

    if not live:
        print("状态: 未发现运行中的 supervisor。")
        print("若 MCP 通道断连，请先重启 WorkBuddy，让宿主重新拉起 server。")
        return 1 if args.status else 2

    print(f"存活 supervisor {len(live)} 个：")
    for s in sorted(live):
        w = live[s]
        print(f"  supervisor {s} -> worker {w if w is not None else '未就绪'}")

    if args.status:
        return 0

    targets = {s: w for s, w in live.items() if w is not None}
    if not targets:
        print("所有 worker 均未就绪，暂不触发重载。")
        return 3

    flag = flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)

    # 哨兵是原子 unlink，一轮只能喂饱一个 supervisor。存在多个宿主时，写一次
    # 只会有其中一个集群重载 —— 所以必须循环写到「每个集群的 worker 都换过」为止。
    done: dict[int, int] = {}
    attempts = max(3, len(targets) * 3)
    for attempt in range(1, attempts + 1):
        flag.write_text(str(time.time()), encoding="utf-8")
        step_deadline = time.time() + max(2.0, args.wait / 3)
        while time.time() < step_deadline:
            time.sleep(0.3)
            cur = read_clusters()
            cur_times = read_times()
            for s, old in targets.items():
                if s in done:
                    continue
                nw = live_worker_of(cur, s, cur_times)
                if nw is not None and nw != old and alive_since(s, cur_times.get(s)):
                    done[s] = nw
            if len(done) == len(targets):
                break
        if len(done) == len(targets):
            print(f"第 {attempt} 轮后全部集群已完成替换。")
            break

    if flag.exists():
        print("提示: 哨兵文件仍在磁盘（可能被下一轮消费）。")

    dead = [s for s in targets if not alive_since(s, read_times().get(s))]
    if dead:
        print(f"异常: supervisor {dead} 已消失，宿主管道可能已断。")
        return 5

    for s, old in targets.items():
        nw = done.get(s)
        if nw is None:
            print(f"超时: supervisor {s} 的 worker {old} 未被替换（它可能没在轮询哨兵）。")
        else:
            print(f"重载成功: supervisor {s} 保持不变，worker {old} -> {nw}")

    missing = [s for s in targets if s not in done]
    if missing:
        print(f"共 {len(targets)} 个集群，成功 {len(done)} 个；未完成: {missing}")
        return 4
    print(f"全部 {len(done)} 个集群重载完成，代码已生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
