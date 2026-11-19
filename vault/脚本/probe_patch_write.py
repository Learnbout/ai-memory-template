#!/usr/bin/env python3
"""局部编辑写入（edits / append）的探针：分支覆盖 + 真库端到端。

背景（2026-10-21）：memory_write 原来只收整篇 content，改一句要重发一万三千字。
本次加了 edits（精确替换，必须唯一命中）与 append（追加，自动去重）。

用法：
    <venv>/python.exe 脚本/probe_patch_write.py            # 临时库，覆盖 12 个分支，不碰真库
    <venv>/python.exe 脚本/probe_patch_write.py --live     # 额外在真库做一次 E2E（建→改→查→删）

判定：每条 PASS/FAIL，任一 FAIL 退出码 1。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
VAULT_ROOT = HERE.parents[1]          # 记忆库/
REPO_ROOT = VAULT_ROOT.parent         # ai-memory/

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


def _ver(mt, title: str) -> int:
    """取当前 version（走读入口，拿 frontmatter 的 version）。"""
    txt = mt.memory_read(title, mode="skeleton")
    for line in str(txt).splitlines():
        if "version=" in line:
            try:
                return int(line.split("version=")[1].split()[0].rstrip("|").strip())
            except Exception:
                pass
    return -1


def run_temp() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-memory-patch-") as td:
        os.environ["AI_MEMORY_DIR"] = td
        os.environ["AI_MEMORY_NO_RELOAD"] = "1"
        sys.path.insert(0, str(VAULT_ROOT))
        import memory_tools as mt

        T = "探针_局部编辑"
        body = "第一行：原始内容。\n\n第二行：重复片段 X。\n\n第三行：重复片段 X。\n"
        mt.memory_write(title=T, content=body, tags=["probe"], summary="探针")
        check("1 建条目", mt.memory_read(T).count("原始内容") == 1)

        # 2 edits 正常替换（唯一命中）
        msg = mt.memory_write(title=T, edits=[["第一行：原始内容。", "第一行：改过的内容。"]])
        txt = mt.memory_read(T)
        check("2 edits 替换成功", "改过的内容" in txt and "原始内容" not in txt, msg.splitlines()[0])

        # 3 edits 0 命中 → 拒绝且不写
        msg = mt.memory_write(title=T, edits=[["这段根本不存在", "x"]])
        check("3 edits 0 命中被拒", "0 处命中" in msg and "改过的内容" in mt.memory_read(T), msg.splitlines()[0])

        # 4 edits 多命中 → 拒绝
        msg = mt.memory_write(title=T, edits=[["重复片段 X", "重复片段 Y"]])
        check("4 edits 多命中被拒", "命中 2 处" in msg, msg.splitlines()[0])

        # 5 old == new → 拒绝
        msg = mt.memory_write(title=T, edits=[["改过的内容", "改过的内容"]])
        check("5 新旧相同被拒", "等于没改" in msg, msg.splitlines()[0])

        # 6 空旧片段 → 拒绝
        msg = mt.memory_write(title=T, edits=[["", "x"]])
        check("6 空旧片段被拒", "旧片段为空" in msg, msg.splitlines()[0])

        # 7 批量原子性：第一段合法、第二段非法 → 整批不写（用 version 判定，不用正文全文，
        #   因为 memory_read 每次会动 access_count，两次读取的文本本来就不逐字相同）
        v_before = _ver(mt, T)
        body_before = mt.memory_read(T)
        msg = mt.memory_write(title=T, edits=[["改过的内容", "不该落地的内容"], ["不存在", "x"]])
        body_after = mt.memory_read(T)
        check("7 批量原子性",
              "不该落地" not in body_after and _ver(mt, T) == v_before
              and "改过的内容" in body_after and "改过的内容" in body_before,
              msg.splitlines()[0])

        # 8 append 正常
        msg = mt.memory_write(title=T, append="\n第四行：追加的内容。")
        check("8 append 成功", "第四行：追加的内容" in mt.memory_read(T), msg.splitlines()[0])

        # 9 append 重复 → 跳过
        msg = mt.memory_write(title=T, append="\n第四行：追加的内容。")
        check("9 append 重复被跳过", "已存在" in msg, msg.splitlines()[0])

        # 10 append_after 锚点不唯一 → 拒绝
        msg = mt.memory_write(title=T, append="尾巴", append_after="重复片段 X")
        check("10 锚点不唯一被拒", "命中 2 处" in msg, msg.splitlines()[0])

        # 11 append_after 正常
        msg = mt.memory_write(title=T, append="（锚点后插入）", append_after="第二行：重复片段 X。")
        check("11 锚点后插入", "重复片段 X。（锚点后插入）" in mt.memory_read(T), msg.splitlines()[0])

        # 12 三者都不给 → 拒绝
        msg = mt.memory_write(title=T)
        check("12 三模式都空被拒", "三者都没给" in msg, msg.splitlines()[0])

        # 13 edits 改不存在的条目 → 拒绝
        msg = mt.memory_write(title="根本不存在_探针", edits=[["a", "b"]])
        check("13 条目不存在被拒", "找不到标题" in msg, msg.splitlines()[0])

        # 14 version 递增：建(1) + edits(2) + append(3) + 锚点插入(4) = 4；失败的与去重的都不涨
        v = _ver(mt, T)
        check("14 version 递增", v == 4, f"version={v}（期望 4：建 1 + 替换 + 追加 + 锚点插入）")
    return 0


def run_live() -> int:
    """真库 E2E：建 → edits → append → 检索命中 → 彻底删除。"""
    os.environ["AI_MEMORY_DIR"] = str(VAULT_ROOT)
    os.environ["AI_MEMORY_NO_RELOAD"] = "1"
    sys.path.insert(0, str(VAULT_ROOT))
    import memory_tools as mt

    T = "探针_局部编辑_真库（2026-10-21）"
    try:
        mt.memory_write(title=T, content="甲：原始值 AAAA。\n\n乙：不动。", tags=["probe"],
                        summary="真库探针，跑完自动删除", source="probe")
        msg = mt.memory_write(title=T, edits=[["原始值 AAAA", "改后的值 BBBB"]])
        txt = str(mt.memory_read(T))
        check("L1 真库 edits", "改后的值 BBBB" in txt and "原始值 AAAA" not in txt, msg.splitlines()[0])

        msg = mt.memory_write(title=T, append="丙：追加段落 CCC。")
        txt = str(mt.memory_read(T))
        check("L2 真库 append", "追加段落 CCC" in txt, msg.splitlines()[0])

        hit = str(mt.memory_search("BBBB"))
        check("L3 全文索引已同步", T in hit, hit.splitlines()[0] if hit else "无结果")
    finally:
        try:
            mt.memory_delete(title=T, purge=True)
        except Exception as exc:  # noqa: BLE001
            check("L4 清理", False, f"{type(exc).__name__}: {exc}")
        else:
            gone = str(mt.memory_search(T))
            check("L4 清理", T not in gone, "已彻底删除")
    return 0


def main() -> int:
    # 真库段必须**独立进程**跑：memory_store 在 import 期就把 MEMORY_DIR 绑定成
    # 当时的 AI_MEMORY_DIR，同一进程里改环境变量不会让模块常量跟着变（2026-10-21 实测
    # 直接 ValueError: ... is not in the subpath of ...）。所以父子进程分开。
    if "--live-only" in sys.argv:
        print("真库端到端：")
        run_live()
        passed = sum(1 for _, ok, _ in results if ok)
        print()
        print("=" * 56)
        print(f"共 {len(results)} 条：PASS {passed}，FAIL {len(results) - passed}")
        print("=" * 56)
        return 0 if passed == len(results) else 1

    live = "--live" in sys.argv
    print("临时库分支覆盖：")
    run_temp()

    if live:
        import subprocess

        env = {**os.environ, "AI_MEMORY_DIR": str(VAULT_ROOT)}
        r = subprocess.run([sys.executable, "-X", "utf8", str(HERE), "--live-only"],
                           cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8")
        out = (r.stdout or "") + (r.stderr or "")
        print(out.rstrip())
        ok = r.returncode == 0
        # 把子进程的结论并进父进程的汇总（只取结论行，避免重复计数）
        passed = sum(1 for _, o, _ in results if o)
        if not ok:
            results.append(("真库 E2E", False, "见上方输出"))
        else:
            results.append(("真库 E2E", True, "L1~L4 全过"))

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("=" * 56)
    print(f"共 {len(results)} 条：PASS {passed}，FAIL {len(results) - passed}")
    print("=" * 56)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
