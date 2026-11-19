# -*- coding: utf-8 -*-
"""安装器 exe 的入口。

## 现在是什么形态（2026-11-19 改版）

**原生 WinForms 窗口**，不是网页、也不开浏览器。鱼片的要求是「要软件安装」：
用户双击后看到的应该是一个 Windows 窗口（标题栏 / 上一步 / 下一步 / 进度条）。

之前那版把界面做在 WebView2 里（HTML + GSAP），问题有三个：
1. 没装 WebView2 就跑不起来（还得退回浏览器，等于「安装器弹网页」）；
2. 看起来就是浏览器，不像软件；
3. 为了让界面能跑，还得多起一个本地 HTTP 服务（多一个端口、多一个攻击面）。

现在直接 `Application.Run(Form)`，界面与内核同进程：**没有端口、没有 token、没有浏览器**。

## 三种入口

| 谁 | 怎么进 |
|---|---|
| 普通用户 | 双击 exe → GUI 向导 |
| 脚本 / 自动化 | 另一个产物 `ai-memory-setup-cli.exe`（带控制台） |
| 本文件带参数 | `--cli ...` 时转给 setup_cli（方便开发时不打包直接测） |

退路：万一这台机器没有 .NET（WinForms 起不来），**不静默弹浏览器**，而是明确写日志 +
弹一个系统消息框说清楚，然后以非 0 退出 —— 静默降级会让人以为安装器坏了。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime


def _setup_logging() -> str:
    """无控制台（打包成 --noconsole）时，把 stdout/stderr 落到文件。

    为什么必须有：窗口版双击运行，出错时用户什么也看不到，本鲸鱼也无从复现。
    日志放在「安装目录/installer.log」，出问题让用户发这一个文件就够。
    """
    if not getattr(sys, "frozen", False):
        return ""
    base = os.environ.get("AI_MEMORY_INSTALL_DIR") or os.path.dirname(os.path.abspath(sys.executable))
    try:
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, "installer.log")
        f = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        print("\n==== %s 启动 ====" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return path
    except OSError:
        return ""


def _package_mode() -> str:
    from installer.server import _read_package_mode

    return _read_package_mode()


def main() -> int:
    _setup_logging()

    # 带 --cli 参数：转命令行入口（开发期方便，不必打包两个 exe）
    if len(sys.argv) > 1 and sys.argv[1] in ("--cli", "--dry-run", "--check", "--json"):
        from installer.setup_cli import main as cli_main

        rest = sys.argv[1:]
        if rest and rest[0] == "--cli":
            rest = rest[1:]
        return cli_main(rest)

    mode = _package_mode()
    print("[installer] 形态=%s" % mode, flush=True)

    from installer.wizard import HAVE_WINFORMS, run_wizard

    if not HAVE_WINFORMS:
        msg = ("这台机器上没能加载 .NET 窗口组件（WinForms），安装器界面打不开。\n\n"
               "可以改用命令行版：ai-memory-setup-cli.exe --mode %s --json\n"
               "详细原因见安装目录下的 installer.log。" % mode)
        print("[installer] %s" % msg, flush=True)
        try:
            import clr  # noqa: F401 —— 只为把真实异常写进日志
        except Exception as exc:  # noqa: BLE001
            print("[installer] pythonnet 加载失败：%s: %s" % (type(exc).__name__, exc), flush=True)
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, "ai-memory 安装器", 0x10)
        except Exception:  # noqa: BLE001
            pass
        return 3

    return run_wizard(mode)


if __name__ == "__main__":
    raise SystemExit(main())
