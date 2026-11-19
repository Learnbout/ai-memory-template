#!/usr/bin/env python3
"""collect_env.py — 本机开发环境自动采集，生成/更新 知识_本机开发环境.md

动机（2026-10-09）：环境信息最大的敌人不是「存」，是「过期」。手写的版本号
三个月后必然失真（Python/Node/PHP 一次升级就废）。故本页由脚本实测生成，
机器写、人只读；要改就改脚本，不要手工改笔记（会被下次采集覆盖）。

设计取舍：
- 声明式采集表（PROBES），加一个运行时只需加一行 —— 不额外建 collectors/ 目录树，
  那对本库规模属过度设计（能更新就不新建机制）。
- 命令一律经 shutil.which 解析；找不到即写「未安装」，**绝不静默回落**（E5b 教训：
  静默回落会让「没装」看起来像「装好了」）。
- frontmatter 走 memory_store 官方出口（自动补 type/scope/schema_version + 原子写），
  不自己手搓 YAML。
- 幂等：正文未变则不重写（updated 不空转）。

用法：
    python 脚本/collect_env.py            # 采集并写入
    python 脚本/collect_env.py --dry-run  # 只打印，不写盘
    python 脚本/collect_env.py --json     # 以 JSON 打印采集结果
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:  # 脚本埋点（计数写 <vault>/.workbuddy/script_usage.json，不进 git）
    from _usage import autotrack  # type: ignore
    autotrack(__file__)
except Exception:  # pragma: no cover - 埋点失败不影响主功能
    pass

VAULT = Path(__file__).resolve().parent.parent
NOTE_TITLE = "知识_本机开发环境"
NOTE_REL = Path("知识") / f"{NOTE_TITLE}.md"

# ── 声明式采集表：加运行时 = 加一行 ────────────────────────────────────────
# (展示名, 可执行文件名, 取版本参数, 版本取值方式)
# kind: "stdout" = 取第一行; "python" = 当前解释器 sys.version
PROBES: list[tuple[str, str, list[str], str]] = [
    ("Python",     "python",   ["--version"], "stdout"),
    ("pip",        "pip",      ["--version"], "stdout"),
    ("Node",       "node",     ["--version"], "stdout"),
    ("npm",        "npm",      ["--version"], "stdout"),
    ("pnpm",       "pnpm",     ["--version"], "stdout"),
    ("PHP",        "php",      ["--version"], "stdout"),
    ("Composer",   "composer", ["--version"], "stdout"),
    ("Git",        "git",      ["--version"], "stdout"),
    ("Docker",     "docker",   ["--version"], "stdout"),
    ("MySQL(cli)", "mysql",    ["--version"], "stdout"),
    ("VS Code",    "code",     ["--version"], "stdout"),
]


def _run(cmd: list[str], timeout: int = 8) -> str:
    """跑命令取输出（stdout 优先，空则取 stderr）；失败返回空串，不抛。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
            errors="replace",
        )
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:
        return ""


def _probe(exe: str, args: list[str]) -> tuple[str, str]:
    """返回 (版本, 路径)；未安装返回 ("未安装", "—")。"""
    path = shutil.which(exe)
    if not path:
        return "未安装", "—"
    ver = _run([path, *args]) or "（无法取版本）"
    return ver, path


def _mem_gb() -> str:
    try:
        import ctypes

        class _MEMSTATUS(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return f"{st.ullTotalPhys / 1024**3:.1f} GB（可用 {st.ullAvailPhys / 1024**3:.1f} GB）"
    except Exception:
        return "未取到"


def _cpu() -> str:
    # wmic 在 Win11 26200 已残废（实测取空），改读注册表里的处理器型号字符串。
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        if name:
            return str(name).strip()
    except Exception:
        pass
    return platform.processor() or platform.machine() or "未取到"


def _gpu() -> str:
    # 显卡名走 PowerShell CIM（wmic 已不可用）；失败即「未取到」，不静默兜底。
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name -join ' / '"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        val = (p.stdout or "").strip()
        return val if val else "未取到"
    except Exception:
        return "未取到"


def collect() -> dict:
    runtimes, tools = [], []
    for label, exe, args, _ in PROBES:
        ver, path = _probe(exe, args)
        row = {"name": label, "version": ver, "path": path}
        if label in {"Docker", "VS Code"}:
            tools.append(row)
        else:
            runtimes.append(row)

    sqlite_ver = "未取到"
    try:
        import sqlite3
        sqlite_ver = sqlite3.sqlite_version
    except Exception:
        pass

    return {
        "system": {
            "os": platform.platform(),
            "host": platform.node(),
            "machine": platform.machine(),
            "cpu": _cpu(),
            "ram": _mem_gb(),
            "gpu": _gpu(),
            "shell": os.environ.get("SHELL")
            or (Path(os.environ.get("COMSPEC", "")).name or "—"),
        },
        "runtimes": runtimes,
        "tools": tools,
        "sqlite_module": sqlite_ver,
        "vault": str(VAULT),
        "interpreter": sys.executable,
    }


def _table(rows: list[dict], cols=("name", "version", "path")) -> str:
    head = {"name": "名称", "version": "版本", "path": "路径"}
    out = [f"| {head[cols[0]]} | {head[cols[1]]} | {head[cols[2]]} |", "| --- | --- | --- |"]
    for r in rows:
        out.append(f"| {r.get(cols[0],'')} | {r.get(cols[1],'')} | {r.get(cols[2],'')} |")
    return "\n".join(out)


def build_body(d: dict) -> str:
    s = d["system"]
    lines = [
        "> 索引：[[记忆索引]] ｜ 状态：[[项目状态]]",
        "> 本页由 `脚本/collect_env.py` **自动采集生成**，机器写、人只读 —— 手工编辑会被下次采集覆盖。",
        "",
        "## 系统",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 操作系统 | {s['os']} |",
        f"| 主机名 | {s['host']} |",
        f"| 架构 | {s['machine']} |",
        f"| CPU | {s['cpu']} |",
        f"| 内存 | {s['ram']} |",
        f"| 显卡 | {s['gpu']} |",
        f"| Shell | {s['shell']} |",
        "",
        "## 语言运行时",
        "",
        _table(d["runtimes"]),
        "",
        "## 工具链",
        "",
        _table(d["tools"]),
        "",
        "## 数据库 / 存储",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| SQLite（Python 模块） | {d['sqlite_module']} |",
        "",
        "## 记忆库",
        "",
        f"- 库路径：`{d['vault']}`",
        f"- 采集解释器：`{d['interpreter']}`",
        "- 备份：`auto_push.sh` 推私有仓库（token 外置读、推后抹除）",
        "",
        "> 「未安装」为实测结果，非漏检。加运行时＝在 `脚本/collect_env.py` 的 `PROBES` 表加一行。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    os.environ.setdefault("AI_MEMORY_DIR", str(VAULT))
    sys.path.insert(0, str(VAULT))
    import memory_store as ms  # noqa: E402

    data = collect()

    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    body = build_body(data)
    target = VAULT / NOTE_REL

    prev_created, prev_version, prev_access, prev_body = None, None, 0, None
    if target.exists():
        try:
            em, prev_body = ms._load_memory(target)
            prev_created = em.get("created")
            prev_version = em.get("version")
            prev_access = em.get("access_count", 0)
        except Exception:
            prev_body = None

    if prev_body is not None and prev_body.strip() == body.strip():
        print(f"无变化，跳过（{NOTE_REL}）")
        return 0

    version = (prev_version or 0) + 1 if prev_version is not None else 1
    summary = (
        "本机开发环境自动采集（collect_env.py）：OS/CPU/内存/显卡、Python/Node/npm/pnpm/"
        "PHP/Composer/Git/Docker/MySQL 版本与路径、SQLite 版本、记忆库路径。机器生成，勿手工编辑。"
    )

    if "--dry-run" in sys.argv:
        print(f"[dry-run] 将写入 {target}（version {prev_version} -> {version}）")
        print(body)
        return 0

    ms._write_memory(
        target, NOTE_TITLE, ["environment", "env", "toolchain", "machine"],
        "collect_env.py", body, created=prev_created, summary=summary,
        version=version, access_count=prev_access, verified=True,
    )
    print(f"已写入 {NOTE_REL}（version {version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
