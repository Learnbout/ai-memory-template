#!/usr/bin/env python3
"""记忆库一键同步（正式运维入口）。

一条命令把「数据库 → md 导出」整条链路跑完，替代手工依次调四个脚本。

它按顺序做六件事：

    ⓪ 表格 padding 归一化（对抗 Obsidian 1.5.8+ 自动补齐列宽造成的空白膨胀）
    ① 重建 SQLite 索引（md → notes/links/meta，全量）
    ② 重建两个 md 索引（记忆索引 / 路由索引）
    ③ 灌全文索引（notes → notes_fts，CJK 2-gram 预分词）
    ④ 导出知识类 md（notes → 导出目录，与主库字节级一致）
    ⑤ 导出记忆层 md（五张新表 → 导出目录，带溯源块）

⓪ 放在最前：它改写 md，必须在建索引与导出**之前**跑，否则索引与导出物拿到的
仍是带 padding 的旧内容。该步失败不阻断后续（只损失一次归一机会，不损失数据）。

每一步失败都出声并继续后续（除了 ①，它失败则整体中止）—— 因为
「部分成功」比「全不干」有用，但要让你知道哪步没成。

用法：
    <venv>/python.exe 脚本/sync_all.py                       # 全套
    <venv>/python.exe 脚本/sync_all.py --check               # 顺带跑体检
    <venv>/python.exe 脚本/sync_all.py --out D:/kb-view      # 指定导出目录
    <venv>/python.exe 脚本/sync_all.py --no-export           # 只重建索引不导出
    <venv>/python.exe 脚本/sync_all.py --verify              # 跑完再逐字节校验
"""

from __future__ import annotations

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

VAULT = Path(os.path.expanduser(os.environ.get("AI_MEMORY_DIR", "~/ai-memory"))).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
sys.path.insert(0, str(VAULT / "脚本"))

DEFAULT_OUT = Path(os.path.expanduser(os.environ.get("AI_MEMORY_EXPORT_DIR", "~/ai-memory-导出")))
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


def run_script(rel: str, extra: list[str], key: str | None = None) -> tuple[bool, str]:
    """跑子脚本并返回 (成功, 摘要)。

    key 给定时优先取含该关键词的输出行作摘要 —— 这些子脚本的「最后一行」
    往往是无信息的收尾语（如「迁移后新增列：（无）」），不如含关键词的那行有用。
    """
    p = VAULT / rel
    r = subprocess.run([PY, "-X", "utf8", str(p), *extra],
                       cwd=str(VAULT), capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "AI_MEMORY_DIR": str(VAULT)})
    out = (r.stdout or "") + (r.stderr or "")
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    detail = ""
    if key:
        hit = [l for l in lines if key in l]
        detail = hit[-1] if hit else ""
    if not detail:
        detail = lines[-1] if lines else ""
    return r.returncode == 0, detail


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库一键同步")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="导出目录")
    ap.add_argument("--no-export", action="store_true", help="只重建索引，不导出")
    ap.add_argument("--check", action="store_true", help="顺带跑三项体检")
    ap.add_argument("--verify", action="store_true", help="导出后逐字节校验")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)

    t0 = time.perf_counter()
    print(f"主库：{VAULT}")
    print(f"导出：{'（跳过）' if args.no_export else args.out}")
    print()

    print("⓪ 表格 padding 归一化")
    ok, detail = run_script("脚本/squeeze_table_padding.py", ["--apply"], key="受影响")
    step("表格 padding", ok, detail)

    print("① 重建 SQLite 索引（md → 库）")
    import memory_web
    try:
        info = memory_web.build_index()
        step("SQLite 索引", True, f"{info['notes']} 条笔记 / {info['links']} 双链 / {info['elapsed_ms']} ms")
        notes_ok = True
    except Exception as e:
        step("SQLite 索引", False, f"{type(e).__name__}: {e}")
        notes_ok = False

    if not notes_ok:
        print()
        print("索引重建失败，后续步骤中止。")
        return 1

    print("② 重建两个 md 索引")
    try:
        import memory_store as ms
        ms._refresh_index()
        ms._refresh_routing_index()
        step("md 索引", True, "记忆索引 + 路由索引已重建")
    except Exception as e:
        step("md 索引", False, f"{type(e).__name__}: {e}")

    print("③ 灌全文索引（CJK 2-gram）")
    ok, detail = run_script("脚本/db_init.py", ["--fill-fts"], key="全文索引")
    step("全文索引", ok, detail)

    if not args.no_export:
        print("④ 导出知识类 md")
        ok, detail = run_script("脚本/md2fanout.py", ["--out", args.out], key="笔记 ")
        step("知识类导出", ok, detail)

        print("⑤ 导出记忆层 md")
        ok, detail = run_script("脚本/db2md.py", ["--out", args.out], key="生成 ")
        step("记忆层导出", ok, detail)

        if args.verify:
            print("⑥ 逐字节校验")
            ok1, d1 = run_script("脚本/md2fanout.py", ["--out", args.out, "--verify"], key="校验")
            ok2, d2 = run_script("脚本/db2md.py", ["--out", args.out, "--verify"], key="校验")
            step("知识类校验", ok1, d1)
            step("记忆层校验", ok2, d2)

    if args.check:
        print("⑦ 库体检")
        for name, rel in [("frontmatter", "脚本/check_frontmatter.py"),
                          ("表格结构", "脚本/fix_table_structure.py"),
                          ("死链", "脚本/check_dead_links.py")]:
            ok, detail = run_script(rel, [], key="")
            step(name, ok, detail)

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("=" * 60)
    print(f"共 {len(results)} 步：成功 {passed}，失败 {len(results) - passed}"
          f"  ｜ 耗时 {round((time.perf_counter() - t0) * 1000)} ms")
    if passed < len(results):
        print()
        print("失败项：")
        for n, ok, d in results:
            if not ok:
                print(f"  - {n}  {d}")
    print("=" * 60)
    return 1 if passed < len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
