# -*- coding: utf-8 -*-
"""打包安装器：**两个 GUI 包 + 一个命令行包**。

## 为什么要三个产物（不是两个）

鱼片 2026-11-18 定稿：**模式由包决定** —— 装本地包就是本地模式，不让用户理解「模式」这个词。
所以 GUI 版有两个（local / central），区别只有塞进去的 `package_mode.txt`。

第三个是**命令行版**：参考文档里那条决策是对的 —— 一个 exe 硬凑两种形态会翻车
（`--noconsole` 想弹控制台在受限环境直接挂死）。命令行版给脚本、CI、沙盒验收用。

## 产物

    dist/ai-memory-installer-local.exe     本地版 GUI 向导（原生窗口，无控制台）
    dist/ai-memory-installer-central.exe   中心化版 GUI 向导
    dist/ai-memory-setup-cli.exe           命令行安装（有控制台，给脚本和自动化）

## 界面依赖

界面是 **.NET WinForms**（通过 pythonnet），不是 WebView2 —— 所以不再收 webview，
省体积也少一个「用户机器没装 WebView2 就白屏」的失败面。pythonnet 与 clr_loader
是动态加载的，PyInstaller 静态分析看不到，必须显式 collect。

## 用法

    installer\\.venv\\Scripts\\python.exe installer\\build_exe.py             # 三个都打
    installer\\.venv\\Scripts\\python.exe installer\\build_exe.py --mode local
    installer\\.venv\\Scripts\\python.exe installer\\build_exe.py --only cli
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # graphite-workspace/
INST = ROOT / "installer"
DIST = INST / "dist"
BUILD = INST / "build"


def _move_into_dist(src: Path, name: str) -> Path:
    """把打好的 exe 搬进 dist。

    为什么不直接让 PyInstaller 输出到 dist：它最后要改写 exe 的资源段
    （`EndUpdateResourceW`），而 Windows 搜索索引器 / 杀软常在扫描刚出现的 exe，
    文件被占用时这一步会连试 20 次失败（实测 `[WinError 32] 另一个程序正在使用此文件`）。
    所以先输出到临时目录，再带重试地搬过来。
    """
    dst = DIST / name
    last = ""
    for i in range(12):
        try:
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
            return dst
        except OSError as exc:
            last = str(exc)
            time.sleep(0.8)
    raise SystemExit("搬到 dist 失败（%s）：%s" % (last, name))


def _common_payload_args() -> list[str]:
    return [
        # 安装内容（后端 + 预构建前端 + 库代码 + 自带运行时）：装的时候从 _MEIPASS/payload 拷
        "--add-data", "%s;payload" % (INST / "payload"),
        # .NET WinForms：pythonnet / clr_loader 都是动态加载，静态分析看不到
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        # 版本号：安装清单要记它
        "--add-data", "%s;." % (ROOT / "VERSION"),
    ]


def build_gui(mode: str) -> Path:
    # 标记文件**必须叫 package_mode.txt**：PyInstaller 保留源文件 basename。
    # 每个形态放各自目录、文件名固定，否则读不到 → 回落 local（第一版就这么错过）。
    stage = BUILD / mode
    stage.mkdir(parents=True, exist_ok=True)
    marker = stage / "package_mode.txt"
    marker.write_text(mode + "\n", encoding="utf-8")

    name = "ai-memory-installer-%s" % mode
    tmpdist = Path(tempfile.mkdtemp(prefix="amg_"))
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", name,
        "--distpath", str(tmpdist),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--add-data", "%s;." % marker,
        *_common_payload_args(),
        # 安装器是窗口程序：不要弹黑色控制台；日志写到 <安装目录>/installer.log
        "--noconsole",
        str(INST / "entry.py"),
    ]
    print("building %s ..." % name)
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    built = tmpdist / (name + ".exe")
    if not built.is_file():
        raise SystemExit("打包失败：%s 不存在" % built)
    exe = _move_into_dist(built, name + ".exe")
    print("OK %s  (%.1f MB)" % (exe.name, exe.stat().st_size / 1048576))
    return exe


def build_cli() -> Path:
    name = "ai-memory-setup-cli"
    tmpdist = Path(tempfile.mkdtemp(prefix="amc_"))
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", name,
        "--distpath", str(tmpdist),
        "--workpath", str(BUILD / "work-cli"),
        "--specpath", str(BUILD),
        *_common_payload_args(),
        # 命令行版保留控制台：脚本要读 stdout / 退出码
        # 入口必须是绝对导入的薄壳，否则包内相对导入会在冻结后炸
        str(INST / "cli_entry.py"),
    ]
    print("building %s ..." % name)
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    built = tmpdist / (name + ".exe")
    if not built.is_file():
        raise SystemExit("打包失败：%s 不存在" % built)
    exe = _move_into_dist(built, name + ".exe")
    print("OK %s  (%.1f MB)" % (exe.name, exe.stat().st_size / 1048576))
    return exe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["local", "central", "both"], default="both")
    ap.add_argument("--only", choices=["gui", "cli", "all"], default="all")
    args = ap.parse_args()

    if args.only in ("gui", "all"):
        modes = ["local", "central"] if args.mode == "both" else [args.mode]
        for m in modes:
            build_gui(m)
        for m in modes:
            (DIST / ("package_mode.%s.txt" % m)).write_text(m + "\n", encoding="utf-8")
    if args.only in ("cli", "all"):
        build_cli()

    print("\n产物目录：%s" % DIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
