# -*- coding: utf-8 -*-
"""命令行版 exe 的入口（薄壳）。

为什么不能直接把 `setup_cli.py` 当入口：PyInstaller 把入口脚本当**顶层脚本**执行，
`from . import agents` 这种包内相对导入会报
`ImportError: attempted relative import with no known parent package`。
所以入口必须是一个**绝对导入**的薄壳，把真正的逻辑留在包里。
"""
from __future__ import annotations

import sys

from installer.setup_cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
