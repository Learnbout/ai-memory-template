#!/usr/bin/env python3
"""记忆库一键同步（正式运维入口）。

一条命令把「库 → 派生数据 → md 快照」整条链路跑完。

它按顺序做六件事：

    ① 重算派生表（links）。notes 一行不动 —— 库是原件。
    ② 重算两个索引页（记忆索引 / 路由索引），**写回库**（2026-10-20 起不再写文件）。
    ③ 灌全文索引（notes → notes_fts，CJK 2-gram 预分词）
       —— 2026-11-16 起 notes_fts 默认不建，本步自动跳过；
       要恢复全文检索先跑 `db_init.py --create-fts` 再跑本脚本
    ③b 文档向量（语义通道，按 title 编码）
    ④a 投影预检（磁盘 md 与库不一致的先留档到 .workbuddy/projection_conflicts/）
    ④ 导出 md 快照（库 → 库内原路径的 md，一篇一文件，供 git 逐行回溯）
    ⑤ 快照校验（渲染结果与磁盘逐字节对比）
    ⑥ 库体检（--check 时）

2026-10-20 删掉了两步，原因是它们的存在意义已经消失：

    ⓪ 表格 padding 归一化 —— 它改的是**磁盘上的 md**，而 md 现在由 ④ 从库重画，
       归一化应该发生在写库那一刻，不该在同步链路上改投影。
       2026-10-21 Obsidian 1.8.10 已彻底卸载，压 padding 的脚本本体也一并归档到
       `备份归档/已下线脚本/squeeze_table_padding.py`；下线前实测全库 2414 行表格
       里真实 padding 只有 0 行（仅 4 行命中 3+ 连续空格，且全在铁律 11 的代码块示例里）。
    ①b md 外部编辑导回库 —— 配合 `projection_hash` 判据，把「有人手改了 md」
       的单篇灌回库。库成为唯一原件之后，「手改 md 再回灌」就是**用投影覆盖源**，
       而维持这套判定需要 200 行 + 一张表列，且这四天的 bug 全部出在这条缝里。

每一步失败都出声并继续后续（除了 ①，它失败则整体中止）—— 因为
「部分成功」比「全不干」有用，但要让你知道哪步没成。

用法：
    <venv>/python.exe 脚本/sync_all.py                  # 全套
    <venv>/python.exe 脚本/sync_all.py --check          # 顺带跑三项体检
    <venv>/python.exe 脚本/sync_all.py --no-snapshot    # 只重建派生数据，不导出 md
    <venv>/python.exe 脚本/sync_all.py --verify         # 导出后再逐字节校验
"""

from __future__ import annotations

import _runtime  # noqa: F401  解释器守卫：用错 python 时自动切到 envs/default

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

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
sys.path.insert(0, str(VAULT / "脚本"))

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
    ap.add_argument("--no-snapshot", action="store_true", help="不导出 md 快照")
    ap.add_argument("--check", action="store_true", help="顺带跑三项体检")
    ap.add_argument("--verify", action="store_true", help="导出后逐字节校验快照")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)

    t0 = time.perf_counter()
    print(f"主库：{VAULT}")
    print(f"快照：{'（跳过）' if args.no_snapshot else '库内原路径的 md'}")
    print()

    # 注意：步骤前缀用纯 ASCII（0) / 0b)）。2026-11-18 踩过 —— 用「⓪」(U+24EA)
    # 在 GBK 控制台下 py.cmd 直接 UnicodeEncodeError 崩掉，而 ①(U+2460) 在 CP936 里是有的，
    # 所以 ①~⑥ 一直没出事。差一个码位就炸，这类坑只认实测。
    print("0) 草稿归并（把 hook 自动攒下的 待记草案 变成可检索条目；幂等，有台账）")
    # 2026-11-18：这一步是「治漏记」的自动化落点。为什么排在最前 —— 归并会**新增笔记**，
    # 而 ① 派生表、② 索引页、③ 全文索引、③b 向量都依赖 notes 表的当前内容：
    # 排在后面就会导致新条目这一轮没进索引，要等下一次同步才可检索。
    ok, detail = run_script("脚本/merge_drafts.py", ["--write"], key="已写入")
    step("草稿归并", ok, detail)

    print("0b) 草稿淘汰（只动机器生成的条目；未读 90 天或超上限 → 进 .trash）")
    # 只增不减就是「append-only 坟场」。规则刻意保守：只碰 草稿归并_ / 流水草稿_ 前缀，
    # 一律软删除，人手写的笔记永远不动。
    ok, detail = run_script("脚本/evict_drafts.py", ["--apply"], key="淘汰")
    step("草稿淘汰", ok, detail)

    print("① 重算派生表（links；notes 不动）")
    import memory_web
    try:
        info = memory_web.rebuild_derived()
        step("派生表", True,
             f"notes {info['notes']} 行未动 / links 重算 {info['links']} 条 / {info['elapsed_ms']} ms")
        notes_ok = True
    except Exception as e:
        step("派生表", False, f"{type(e).__name__}: {e}")
        notes_ok = False

    if not notes_ok:
        print()
        print("派生表重算失败，后续步骤中止。")
        return 1

    print("② 重算两个索引页（记忆索引 / 路由索引）")
    try:
        import memory_store as ms

        ms._invalidate_cache()
        ms._refresh_index()
        ms._refresh_routing_index()
        step("索引页", True, "记忆索引 + 路由索引已重算并写回库")
    except Exception as e:
        step("索引页", False, f"{type(e).__name__}: {e}")

    print("③ 全文索引（CJK 2-gram；notes_fts 不存在时自动跳过）")
    ok, detail = run_script("脚本/db_init.py", ["--fill-fts"], key="全文索引")
    step("全文索引", ok, detail)

    # ③b 文档向量（语义通道）。必须排在全文索引之后：它按 notes 表里的 title 编码，
    # notes 没刷新就会拿旧标题算向量 —— 这种不一致不会报错，只会让语义检索悄悄变差。
    print("③b 文档向量（语义通道）")
    ok, detail = run_script("脚本/build_vectors.py", [], key="note_vecs")
    step("文档向量", ok, detail)

    if not args.no_snapshot:
        # ④a 投影预检（2026-10-22 事故后补）：导出会把「磁盘 md 与库不一致」的内容
        # 整段覆盖掉。上一轮手改聚合页 md 就是这么丢的 —— 库里与磁盘两边都归零，
        # 而 check_frontmatter 报「130 篇全正常、退出码 0」（它只查字段格式，不看正文）。
        # 所以留档必须排在导出**之前**：先复制一份，再让导出覆盖。
        # 本步不会失败（预检自身出错也返回 0/2，不阻断导出），它只负责留档与喊话。
        print("④a 投影预检（导出前留档）")
        ok, detail = run_script("脚本/check_projection.py", ["--keep", "3"], key="投影预检")
        step("投影预检", ok, detail)

        print("④ 导出 md 快照")
        ok, detail = run_script("脚本/snapshot_md.py", [], key="快照导出")
        step("md 快照", ok, detail)

        if args.verify:
            print("⑤ 快照校验")
            ok, detail = run_script("脚本/snapshot_md.py", ["--verify"], key="快照校验")
            step("快照校验", ok, detail)

    # ⑤b 增量回收空闲页（2026-11-16）。auto_vacuum=INCREMENTAL 只是「允许」归还，
    # 不会自己动手 —— 不主动调 incremental_vacuum，删除留下的页照样躺在 freelist 里。
    # 之前两个月攒了 2108 页（8.6MB），就是从来没归还过的结果。
    print("⑤b 增量回收空闲页")
    try:
        import sqlite3 as _sq3

        import db_paths as _dbp

        _c = _sq3.connect(str(_dbp.resolve("index.sqlite3")), timeout=30)
        _before = _c.execute("PRAGMA freelist_count").fetchone()[0]
        _c.execute("PRAGMA incremental_vacuum")
        _after = _c.execute("PRAGMA freelist_count").fetchone()[0]
        _c.close()
        step("增量回收", True, f"空闲页 {_before} → {_after}")
    except Exception as e:  # noqa: BLE001 —— 回收失败不该让同步失败
        step("增量回收", False, f"{type(e).__name__}: {e}")

    # ⑤c 刷新查询规划器统计（2026-11-16）。两个库的 sqlite_stat1 此前一直是空的，
    # 规划器只能按硬编码默认值猜索引；199 行时猜错无感，涨到几千行就会变成选错索引
    # 的秒级卡顿。analysis_limit=1000 让它只抽样、不整表扫（大库上也只花几十毫秒）。
    print("⑤c 刷新规划器统计")
    try:
        import sqlite3 as _sq3

        import db_paths as _dbp

        _stats = []
        for _name in ("index.sqlite3", "versions.sqlite3"):
            try:
                _c = _sq3.connect(str(_dbp.resolve(_name)), timeout=30)
                _c.execute("PRAGMA analysis_limit=1000")
                _c.execute("ANALYZE")
                _c.execute("PRAGMA optimize")
                _n = _c.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'"
                ).fetchone()[0]
                _rows = _c.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] if _n else 0
                _c.commit()
                _c.close()
                _stats.append(f"{_name}:{_rows}")
            except Exception as _e:  # noqa: BLE001
                _stats.append(f"{_name}:ERR({type(_e).__name__})")
        step("规划器统计", True, " ".join(_stats))
    except Exception as e:  # noqa: BLE001
        step("规划器统计", False, f"{type(e).__name__}: {e}")

    if args.check:
        print("⑥ 库体检")
        # 「自定义指令」这一项 2026-10-20 才真正有了脚本：规则文档从 09-13 起就写着
        # 「由 check_custom_instruction.py 核对三处逐字一致」，而那个脚本一直不存在 ——
        # 于是同型漂移发生了三次（10-13 缺第 4 条、10-19 镜像缺 1/2/3、10-20 全文过期）。
        # 挂进体检的意义：它每跑一次 sync_all 就被执行，不再依赖「有人记得手动跑」。
        for name, rel in [("frontmatter", "脚本/check_frontmatter.py"),
                          ("自定义指令", "脚本/check_custom_instruction.py"),
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
