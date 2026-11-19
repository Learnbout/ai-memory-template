"""管理路由（§19.17 热重启）。

为什么要有进程级自重启：常驻进程（pythonw，无控制台）改了 Python 代码后，
唯一生效方式原本是「任务管理器杀进程 → 双击 vbs」。这条端点让服务自己
完成重启 —— 数秒内恢复，之后所有 Python 侧变更都不再需要手动杀进程。

鸡生蛋说明：本端点随新代码上线，需要**最后一次**手动重启常驻激活；
之后「改后端 → 点设置页的热重启按钮」即可。

实现不走 os.execv 原地替换 —— Windows 的 exec 是「先装载新进程、后终止
旧进程」，新进程会撞上旧进程还没释放的 8787，被 run_resident.pyw 的
单实例守卫放走，服务直接死透。正解是三段式：

    本进程 → detached 拉起 _restart_bridge.py（等端口释放的桥）
          → os._exit(0) 释放端口
          → 桥检测到端口空了，execv 原样拉起 run_resident.pyw
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import caps
from ..paths import data_dir

router = APIRouter(tags=["admin"])

# 端口与 run_resident.pyw 位置：本文件在 `backend/app/routers/admin.py`，
# 向上三级才是 `backend/`（parents[0]=routers → [1]=app → [2]=backend）。
# （这两条是**代码**路径，不是数据路径；冻结态的"重启链路"是另一件事，见 D-001）
#
# 2026-11-18 修：原来是 `parents[1]`，指向 `backend/app/`，两个文件都找不到 →
# `_do()` 只写一行「缺文件」就 return，**但后面那句 os._exit(0) 照样执行**，
# 于是「重启失败」变成了「服务自杀且没人接管」：8787 端口空着、页面直接连不上。
# 教训：拉桥是前置条件，它没成时必须让整个流程失败，不能继续往下走到退出那句。
_PORT = int(os.environ.get("GRAPHITE_PORT", "8787"))
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_RUN_RESIDENT = _BACKEND_DIR / "run_resident.pyw"
_BRIDGE = _BACKEND_DIR / "_restart_bridge.py"


def _log_exc(msg: str) -> None:
    """把异常写进 data/resident.log。

    为什么要有它：本进程是 pythonw 起的，**没有 stdout/stderr**。`_do` 跑在
    daemon 线程里，抛了异常既不打印也不落盘 —— 表现就是「接口回了 ok，进程
    一动不动」，比直接报错难查一百倍（2026-10-23 实测：调用后 pid 没变、
    resident.log 今天一行都没有，只能靠 netstat 才发现）。

    2026-10-27（R2）：日志位置改由 `paths.data_dir()` 计算。原来是
    `_RUN_RESIDENT.parent / "data"` —— 与 `run_resident.pyw` 里的写法各自独立推导
    同一条路径，冻结态下两处会指向不同目录（一处解包临时目录、一处用户目录），
    症状是「日志页永远是空的」且不报错。现在两边都走同一个函数。
    """
    try:
        log = data_dir() / "resident.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR 热重启失败：{msg}\n")
    except OSError:
        pass


@router.post("/admin/restart")
def admin_restart(who: Dict[str, Any] = Depends(caps.require_cap("system.restart"))) -> dict:
    """进程级热重启：先让本响应送达，再走「桥等端口 → 新进程」三段式。

    只给 admin（阶段 9）：这接口一被调用进程就退出，非管理员拿到就是一颗
    「隔几秒把服务打死一次」的按钮 —— 而且它不需要任何参数，最好写。
    """

    def _do() -> None:
        time.sleep(0.8)  # 让响应先离开服务器
        if not _BRIDGE.is_file() or not _RUN_RESIDENT.is_file():
            _log_exc(f"缺文件 bridge={_BRIDGE.is_file()} resident={_RUN_RESIDENT.is_file()}")
            return  # 文件不在（异常部署）就什么都不做，服务继续活着
        flags = 0
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                [sys.executable, str(_BRIDGE), str(_PORT), str(_RUN_RESIDENT)],
                creationflags=flags,
                close_fds=True,
                cwd=str(_BRIDGE.parent),
                # pythonw 下 sys.stdin/stdout/stderr 都是 None。不显式重定向时，
                # Popen 在部分环境下会因句柄无效抛 OSError/ValueError ——
                # 而异常发生在 daemon 线程里，进程就「假装重启成功」继续跑。
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as e:
            _log_exc(f"拉桥失败 {type(e).__name__}: {e}")
            return
        time.sleep(0.5)  # 给桥进程拉起来的时间
        os._exit(0)  # 硬退出：socket 立即释放，桥接管后续

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "msg": "重启中：服务将中断数秒后自动恢复"}

