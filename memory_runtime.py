"""MCP runtime, stdio supervisor, and tool registration helpers for ai-memory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import mcp_compat

# G10 后续（2026-10-09）：SDK 依赖全部收口进 mcp_compat.py —— 本文件与
# memory_tools.py 不再直接 import mcp.*。SDK 升级 / 回滚只动兼容层或 pip，
# 上层零改动（设计动机与分支说明见 mcp_compat.py 模块头）。


# 工具选择指南（2026-09-19）：MCP 协议的工具 schema 里没有 category 字段，所以
# 「分类」只能物化在两个模型真能看到的地方 —— 每个工具的 description，以及本
# instructions。源码里给函数分组的注释等于零信息（模型看不到），故不采用。
# 这里写「场景 → 工具」映射，常驻成本约 300 token，换的是选路稳定：不再出现
# 「只要一段 summary 却把整篇笔记拉进上下文」这类 99% 的浪费。
_TOOL_GUIDE = (
    "本地共享 Markdown 记忆库。md 是唯一事实源，索引库（SQLite）只是它的只读"
    "快照，正文一律以 md 为准。\n"
    "选工具按场景走，别默认抓最重的那条：\n"
    "- 想知道库里有没有、笔记叫什么 → memory_search（关键词，扫标题/标签/正文；可带 since 时间窗）\n"
    "- 只要 summary / 计数 / 某一列 / 紧凑列表 → memory_sql（只读 SQL，约 200 tok）\n"
    "- 标题已确定、确实需要整篇正文 → memory_read（约 18,900 tok，最后才用）\n"
    "- 要「一篇 + 它的关联笔记」→ memory_context（1-hop 骨架包，一次调用）\n"
    "- 写入或更新笔记 → memory_write（唯一写入口；先查重，能更新就不新建）\n"
    "- 库整体体检、找孤儿页与零访问条目 → memory_stats\n"
    "聚合页（近期工作动态 / 项目状态）只用 Read + Edit 追加，严禁整篇覆盖。"
)
mcp = mcp_compat.make_server("ai-memory", instructions=_TOOL_GUIDE)

# 工具面优化（2026-09-10）：默认暴露全部 20 个工具。设置 AI_MEMORY_CORE_TOOLS=1
# 时只注册高频核心工具（search/read/write/stats/sql/context），把每轮占用的工具
# schema 压到最小；需要管理类工具（archive/delete/batch 等）时，去掉该环境变量
# 并重启 MCP 即可恢复全部工具。
# 2026-09-17 加入 memory_sql（只读查库）：实测它能让「只要摘要/只要计数」类取数
# 从 18,985 tok 降到 78 tok（-99.6%），收益远大于约 0.3KB 的常驻 schema 开销。
# 2026-10-09 加入 memory_context（G4，1-hop 骨架包）：对标 Basic Memory 的
# build_context，替代「search + N 次 read」的关联阅读路径。
_CORE_TOOL_NAMES = {
    "memory_search",
    "memory_read",
    "memory_write",
    "memory_stats",
    "memory_sql",
    "memory_context",
}
_LIMIT_TO_CORE = os.environ.get("AI_MEMORY_CORE_TOOLS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _tool(fn=None, *, annotations=None):
    """Register tools with optional MCP annotations passthrough.

    G2（2026-10-09）：annotations 是 MCP 协议的 ToolAnnotations（readOnlyHint /
    destructiveHint 等），宿主据此可对只读工具做缓存/并行优化，对写工具弹确认。

    G10 后续（2026-10-09）：注册动作走 mcp_compat.register_tool —— 线程池策略
    （v1 需包 async_tool / v2 原生）由兼容层按 SDK 大版本决定，本文件不感知。
    这里只保留 core 过滤。
    """
    def register(f):
        if _LIMIT_TO_CORE and f.__name__ not in _CORE_TOOL_NAMES:
            return f
        mcp_compat.register_tool(mcp, f, annotations=annotations)
        return f

    if fn is None:
        return register
    return register(fn)


# ── 热重载：常驻 supervisor + 可替换 worker（2026-09-10 23:10 重构）────────────
#
# 旧实现（已废弃）用哨兵文件 + os.execv 让进程替换自己。2026-09-10 生产实测证明
# 该路子不可用：execv 之后宿主的 stdio 管道随旧进程失效，而宿主连接缓存仍报
# `ai-memory:connected`，于是 tools/call 只派发不返回、无超时无报错，会话直接卡死
# （22:26~22:53 共 4 次，最长挂起 11 分 52 秒，只能手动取消）。失败模式是「记忆
# 系统静默失联」，代价不可控。完整证据见知识库
# [[知识_MCP原地热重载与宿主重启脚本（2026-09-10）]]。
#
# 新实现把「宿主面对的进程」与「跑业务的进程」拆开：
#
#   WorkBuddy(宿主) ──同一条 pipe──> supervisor（永不退出，只等哨兵）
#                                        └── spawn ──> worker（真正跑 MCP 服务）
#
# worker 通过 subprocess 显式继承 supervisor 手里的 stdin/stdout 句柄，因此宿主的
# 管道始终挂在一个不会退出的进程上；重载时只终止并重建 worker，宿主管道全程不动，
# 通道不会断。
#
# 免握手（G10 迁移后，2026-10-09）：热重载后新 worker 没握过手，宿主续发的裸
# tools/call 需要免握手才能被接受。SDK 差异（v1 的 stateless=True / v2 的
# born-ready 补丁，v2 legacy 连接的 pre-init 门宿主实测会 -32602）全部收口在
# mcp_compat.run_stdio，本文件不感知。已用「模拟宿主的裸 JSON-RPC + 热重载 +
# 续发裸 tools/call」实测验证，证据见 [[知识_MCPv2迁移落地（2026-10-09）]]。
#
# 触发方式不变：`脚本/reload_mcp.py` 写 .workbuddy/reload.flag，本进程 1 秒轮询。
# 设 AI_MEMORY_NO_RELOAD=1 退回「不套 supervisor、直接跑服务」的最小模式。
RELOAD_FLAG_NAME = "reload.flag"
# 探针隔离（2026-10-10）：脚本/probe_hot_reload.py 自起 supervisor 做热重载测试时
# 设 AI_MEMORY_RELOAD_FLAG_NAME 用独立哨兵，避免与宿主常驻 supervisor 抢同一个
# reload.flag（_consume_flag 是原子 unlink，谁抢到谁重载——测试误触发生产重载）。
_reflag_env = os.environ.get("AI_MEMORY_RELOAD_FLAG_NAME", "").strip()
RELOAD_FLAG_OVERRIDE = _reflag_env if _reflag_env else None
BOOT_LOG_NAME = "mcp_boot.log"
WORKER_FLAG = "--worker"
_NO_RELOAD = os.environ.get("AI_MEMORY_NO_RELOAD", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def vault_dir() -> Path:
    """记忆库根目录（AI_MEMORY_DIR 优先，退回本文件所在目录）。"""
    return Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parent)


def reload_flag_path() -> Path:
    """哨兵文件路径。放 .workbuddy/，不参与记忆库缓存判据、不产生 token 成本。

    AI_MEMORY_RELOAD_FLAG_NAME 可换哨兵文件名（探针隔离用，见 RELOAD_FLAG_OVERRIDE）。
    """
    name = RELOAD_FLAG_OVERRIDE or RELOAD_FLAG_NAME
    return vault_dir() / ".workbuddy" / name


def server_entry() -> Path:
    """server.py 的绝对路径，supervisor 用它拉起 worker。"""
    return Path(__file__).resolve().parent / "server.py"


def _boot_log(mode: str) -> None:
    """把启动方式与父子 PID 追加进 .workbuddy/mcp_boot.log。

    只为跨会话诊断服务：一眼看清当前实例是 supervisor 还是 worker、由谁拉起。
    任何 IO 异常都吞掉——绝不让日志问题影响 MCP 通道。
    """
    try:
        path = vault_dir() / ".workbuddy" / BOOT_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": mode,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "argv": sys.argv[1:],
                "sdk": mcp_compat.SDK_VERSION,
            },
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 - 诊断日志永不阻塞启动
        pass


def _consume_flag(flag: Path) -> bool:
    """哨兵文件存在则删除并返回 True；异常一律按“未命中”处理。

    直接用 unlink 而不是「exists 再 unlink」：unlink 是 OS 级原子操作，同一时刻
    只有一个调用者能成功，避免两个 supervisor 读到同一哨兵后各自触发重载。任何
    异常（含文件不存在）都收敛为 False。
    """
    try:
        flag.unlink()
        return True
    except OSError:
        return False


def _terminate_worker(proc: subprocess.Popen) -> None:
    """终止 worker 及其子进程树。

    Windows 上 venv 的 python.exe 是个 launcher，会再起一个真实解释器，所以必须
    连树一起杀（taskkill /T），否则会留下不读 stdin 的孤儿进程。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return
    proc.terminate()


def _serve_worker() -> None:
    """worker：真正跑 MCP 服务。

    G10（2026-10-09）：热重载免握手的 SDK 差异全部在 mcp_compat.run_stdio 内处理
    —— v1 走 lowlevel.run(stateless=True)，v2 给 Connection.for_loop 打
    born-ready 补丁（v2 legacy 连接的 pre-init 门会拒裸 tools/call，宿主实测
    -32602；补丁语义等价 stateless）。本文件只声明「要热重载安全」。
    """
    mcp_compat.run_stdio(mcp, hot_reload_safe=True)


def run_supervisor() -> None:
    """supervisor：持有宿主管道，只负责看哨兵与替换 worker。

    自身从不读写 stdin/stdout，只把这两个句柄交给 worker，因此它可以无限期存活而
    不影响宿主连接；worker 自行退出（例如宿主关闭了 stdin）时 supervisor 也随之
    退出，便于宿主回收，不留孤儿。
    """
    flag = reload_flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    _consume_flag(flag)  # 启动时清掉历史哨兵，避免一启动就自转
    _boot_log("supervisor")

    while True:
        proc = subprocess.Popen(
            [sys.executable, str(server_entry()), WORKER_FLAG],
            stdin=sys.stdin,
            stdout=sys.stdout,
        )
        _boot_log(f"worker-spawn pid={proc.pid}")
        reloaded = False
        while proc.poll() is None:
            if _consume_flag(flag):
                reloaded = True
                _terminate_worker(proc)
                break
            time.sleep(0.2)
        code = proc.wait()
        if not reloaded:
            # worker 自己退了（通常是宿主关掉 stdin），supervisor 随之退出。
            sys.exit(code if isinstance(code, int) else 0)


def main() -> None:
    """stdio 入口：worker 直接服务，其余情况按配置决定是否套 supervisor。"""
    if WORKER_FLAG in sys.argv[1:]:
        _boot_log("worker")
        _serve_worker()
        return
    if _NO_RELOAD:
        _boot_log("direct")
        mcp_compat.run_stdio(mcp, hot_reload_safe=False)
        return
    run_supervisor()
