"""启动入口：python run.py  →  http://127.0.0.1:8787

2026-10-23 中心化改造：host/port 改为环境变量可覆盖（默认值与改造前一致）。
服务器上是「回环 8788 + 前面套登录网关」，用法：

    GRAPHITE_HOST=127.0.0.1 GRAPHITE_PORT=8788 python run.py

为什么 host 默认仍然是 127.0.0.1：工作台本身零鉴权，绑 0.0.0.0 等于把库
裸奔到局域网。要对外必须经过 `web_gate.py`。
"""

from __future__ import annotations

import os
import sys

import uvicorn

# 保证 `app` 包可被导入（无论从哪个目录调用）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("GRAPHITE_HOST") or "127.0.0.1",
        port=int(os.environ.get("GRAPHITE_PORT") or 8787),
        reload=False,
    )
