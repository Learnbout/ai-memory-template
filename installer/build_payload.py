# -*- coding: utf-8 -*-
"""准备安装器要装进用户机器的那份「货」：installer/payload/。

装什么：
  payload/backend/            ← 后端代码 + 依赖清单 + 启动器
  payload/graphite/frontend/dist/  ← 预构建前端
  payload/vault/              ← 记忆库代码（MCP 服务端与脚本）

不装什么：笔记（*.md）、数据库、模型、备份、node_modules、.venv。
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

# 所有路径都从「本文件在哪」推，不写死任何人的机器路径（开源版必须能跑）
ROOT = Path(__file__).resolve().parent.parent          # graphite-workspace/
PAYLOAD = ROOT / "installer" / "payload"


def find_vault() -> Path:
    """找记忆库代码目录：环境变量 > 几个常见布局 > 报错说清楚怎么指定。

    为什么给多个候选：安装包构建者可能把库放仓库旁边、放上级、或干脆放桌面上的
    ai-memory 目录（作者本机就是最后这种）。硬编码一个必然在别人机器上崩，
    所以按「离仓库由近到远」试，全不中就把选择权交回给 `--vault` / `AI_MEMORY_VAULT`。
    """
    env = os.environ.get("AI_MEMORY_VAULT", "").strip()
    if env:
        return Path(env)
    home = Path.home()
    for cand in (
        ROOT.parent / "记忆库",            # 仓库同级
        ROOT.parent / "vault",
        ROOT / "vault",                    # 仓库内
        home / "Desktop" / "ai-memory" / "记忆库",
        home / "ai-memory" / "记忆库",
    ):
        if (cand / "db_paths.py").is_file() or (cand / "脚本").is_dir():
            return cand
    return ROOT.parent / "记忆库"          # 交给调用方报错，错误信息里带候选列表

SKIP_DIR = {"__pycache__", ".venv", "node_modules", ".pytest_cache",
            # 2026-11-19：这几项是**用户私有数据**，绝不能进安装包。
            # 实测发现 .workbuddy 里带着 1 MB 会话流水、1.5 MB 检索日志、含内网 IP 的
            # server_host —— 开源发出去就是把用户的隐私一起发了。
            ".workbuddy", "数据库", "备份", "备份归档", "草稿", "队列", "附件", "协同",
            ".trash", ".git", ".obsidian"}
SKIP_NAME = ("_probe", "probe_", "_test_", "test_", "备份", ".bak", ".normbak",
             "hook_test", "selftest", "retrieval_cases", "queue_watch", "skill_sync", "sync_push")
SKIP_SUFFIX = {".sqlite3", ".pyc", ".pyo", ".log", ".md", ".jsonl", ".html", ".db", ".json"}

# 库代码里这些后缀必须继续跳过（里面有用户数据/报告），但**前端目录不能套这套规则**
# —— dist 里全是 .html/.json，一刀切会把页面删光，装出来就是「/ 打不开」。
VAULT_ONLY_SKIP = {".sqlite3", ".pyc", ".pyo", ".log", ".md", ".jsonl", ".html", ".db", ".json"}
GENERIC_SKIP = {".pyc", ".pyo", ".log"}


def _ok(p: Path, *, vault: bool = False) -> bool:
    """`vault=True` 时按库代码的严格规则过滤（用户数据一律不打包）。"""
    if p.suffix in (VAULT_ONLY_SKIP if vault else GENERIC_SKIP):
        return False
    if vault and any(t in p.name for t in SKIP_NAME) and p.name != "smoke_test.py":
        return False
    return True


def copy_tree(src: Path, dst: Path, *, vault: bool = False) -> int:
    n = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if any(part in SKIP_DIR for part in item.parts):
            continue
        if not _ok(item, vault=vault):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="把要安装的内容收进 installer/payload/")
    ap.add_argument("--vault", default=None,
                    help="记忆库代码目录（默认取环境变量 AI_MEMORY_VAULT，或仓库同级的 vault/）")
    args = ap.parse_args()
    vault_src = Path(args.vault) if args.vault else find_vault()
    if not vault_src.is_dir():
        print("找不到记忆库代码目录：%s\n用 --vault 指定，或设 AI_MEMORY_VAULT" % vault_src)
        return 2

    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    PAYLOAD.mkdir(parents=True)

    n = 0
    n += copy_tree(ROOT / "backend" / "app", PAYLOAD / "backend" / "app")
    for f in ("requirements.txt", "run.py", "run_resident.pyw", "_restart_bridge.py"):
        src = ROOT / "backend" / f
        if src.is_file():
            shutil.copy2(src, PAYLOAD / "backend" / f)
            n += 1
    n += copy_tree(ROOT / "frontend" / "dist", PAYLOAD / "graphite" / "frontend" / "dist")
    n += copy_tree(vault_src, PAYLOAD / "vault", vault=True)

    # 自带运行环境（便携安装的关键）：把装好依赖的 embeddable Python 一起打进去。
    # 有它用户就不用先装 Python，安装过程**完全离线**，删掉安装文件夹就等于卸载干净。
    # MCP 服务端依赖清单：装的时候要照着它补装（否则 8770 那扇门没人守）
    req = ROOT / "installer" / "assets" / "vault-requirements.txt"
    if req.is_file():
        shutil.copy2(req, PAYLOAD / "vault-requirements.txt")
        n += 1

    runtime = ROOT / "installer" / "assets" / "runtime"
    if runtime.is_dir():
        n += copy_tree(runtime, PAYLOAD / "runtime")
        print("runtime 已打包（%.1f MB）" % (sum(f.stat().st_size for f in runtime.rglob('*') if f.is_file()) / 1048576))
    else:
        print("警告：没有 installer/assets/runtime（便携模式会退化成用系统 Python）")

    total = sum(1 for _ in PAYLOAD.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in PAYLOAD.rglob("*") if f.is_file())
    print("payload: %d 文件, %.2f MB（本次复制 %d）" % (total, size / 1048576, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
