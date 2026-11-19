"""热重启桥（§19.17）：等旧进程退出、端口释放后，原样拉起 run_resident.pyw。

为什么需要桥：Windows 的 exec 语义是「先装载新进程、后终止旧进程」，
如果在 uvicorn 还握着 8787 的时候直接 os.execv，新进程会被 run_resident.pyw
的单实例守卫（端口被占 → 安静退出）放走，然后旧进程才死 —— 服务死透。
桥进程反过来：先轮询端口直到连不上（旧进程已死、端口已释放），再 execv。

用法：pythonw _restart_bridge.py <port> <run_resident.pyw 绝对路径>
由 /api/admin/restart 以 detached 方式拉起，自身无窗口、跑完即被 execv 替换。
"""

from __future__ import annotations

import os
import socket
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else ""

deadline = time.time() + 30
while time.time() < deadline:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))  # 连得上 = 旧进程还握着端口
        s.close()
        time.sleep(0.3)
    except OSError:
        s.close()
        break  # 端口已释放

os.execv(sys.executable, [sys.executable, SCRIPT])
