"""解释器守卫：用错解释器时自动切到装齐依赖的那一个（2026-11-05 立）。

为什么有这个文件（事故记录，2026-11-05 09:20）
    bash 里的 `python` 指向 WorkBuddy 托管的裸解释器：
        C:\\Users\\user\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe
    这套只有标准库 + numpy，没有 pydantic / mcp / onnxruntime。
    记忆库的依赖装在：
        C:\\Users\\user\\.workbuddy\\binaries\\python\\envs\\default
    也就是 mcp.json 里 ai-memory 服务写死的那个解释器。
    于是 `python 脚本/set_frontmatter.py --touch` 当场
        ModuleNotFoundError: No module named 'pydantic'

为什么不在 PATH 层一次解决（试过，都不行）
    1. WorkBuddy 给每个 bash 子进程注入 PATH，versions/3.13.12 排在很前面；
    2. 非交互式 bash 不读 ~/.bashrc，BASH_ENV 指向平台自带的
       shell-runtime-bash-env.sh，里面只 source 同目录脚本，不读用户配置；
    3. 平台已经用 PYTHONPATH 注入了一个 sitecustomize.py（safe-delete 回收站拦截），
       sitecustomize 这个钩子被占了 —— 抢它会让删除操作绕过回收站，不能碰。
    结论：只能在脚本入口自愈。

用法
    在脚本顶部、任何业务 import 之前写一行：
        import _runtime  # noqa: F401
    本模块 import 时就会检查；解释器不对就原地 execv 到正确的那个，
    进程号不变、参数/环境变量/cwd 原样带过去，调用方无感。
"""

from __future__ import annotations

import importlib.util
import os
import sys

# 公开版：解释器路径由环境变量给（作者机器上原本是写死的绝对路径）
RUNTIME_PY = os.environ.get("AI_MEMORY_PYTHON", "")

# 哨兵：只查一个最便宜的包。缺它基本可以断定当前不是 envs/default。
SENTINEL = "pydantic"


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _guard() -> None:
    # 正确的解释器不存在（换机器 / 目录改名）→ 什么都不做，让原本的报错照常出现
    if not os.path.exists(RUNTIME_PY):
        return
    if _same_file(sys.executable, RUNTIME_PY):
        return
    if importlib.util.find_spec(SENTINEL) is not None:
        return
    # `python -c ...` 这类没有真实脚本路径的调用，切过去也没法还原，直接放行
    if not sys.argv or sys.argv[0].startswith("-"):
        return

    sys.stderr.write(
        f"[runtime] 当前解释器 {sys.executable} 缺 {SENTINEL}，"
        f"自动切到 envs/default 重跑\n"
    )
    sys.stderr.flush()
    # execv 会替换当前进程镜像，不会返回；参数用 envs/default 的 python 重新起同一个脚本
    os.execv(RUNTIME_PY, [RUNTIME_PY, *sys.argv])


_guard()
