#!/usr/bin/env python3
"""导出前投影预检：把「会被覆盖的 md 手改」先留档。

事故来源（2026-10-22）
----------------------
手改 `近期工作动态.md` 与 `项目状态.md`（聚合页快照）后跑 sync_all，
第 ④ 步 snapshot_md 从库导出，把改动整段覆盖 —— 两条记录**两边归零**
（库里 `body_md LIKE` 与磁盘 grep 均为 0）。而 `check_frontmatter` 当时
仍报「130 篇全正常、退出码 0」。**假绿**：那个护栏只校验 frontmatter
字段格式，从不看正文有没有少东西。

为什么本脚本不做「不一致就报错」
--------------------------------
库刚写完、md 还没导出时，两者必然不一致 —— 这是**正常状态**，不是异常。
若据此报错，每次收尾都会红，护栏立刻被无视（狼来了）。
更麻烦的是「md 比库旧」与「md 被手改」在字节层面无法区分：
两边都是「内容不同」，没有可靠的方向判据。mtime 不可信（任何工具都改它）。

所以本脚本不判定、不计分、不影响退出码，只做一件事：
**在覆盖发生之前，把每一个会被覆盖的差异文件原样留档。**

- 留档位置：`<vault>/.workbuddy/projection_conflicts/<时间戳>/<原相对路径>`
- 自动清理：只保留最近 5 次，其余删除（占不了地方）
- 聚合页命中时额外醒目提示 —— 这两页是 09-15（丢 59 条）与 10-22（丢 2 条）
  两次事故的共同现场

想找回手改内容：去那个目录翻，不依赖 git、不依赖记忆。

用法：
    <venv>/python.exe 脚本/check_projection.py              # 预检 + 留档
    <venv>/python.exe 脚本/check_projection.py --dry-run    # 只看差异，不留档
    <venv>/python.exe 脚本/check_projection.py --keep 10    # 改保留份数

退出码：0 = 正常（含「有差异已留档」）；2 = 自身异常（不阻断导出）。
"""

from __future__ import annotations

try:  # 脚本使用计数
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

VAULT = Path(__import__("os").environ.get("AI_MEMORY_DIR")
             or Path(__file__).resolve().parents[1]).resolve()
__import__("os").environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
sys.path.insert(0, str(VAULT / "脚本"))

# 重灾区：这两页历史上出过两次覆盖事故，命中时单独喊一嗓子
AGGREGATE_PAGES = {"近期工作动态.md", "项目状态.md"}

CONFLICT_DIR = VAULT / ".workbuddy" / "projection_conflicts"


def find_conflicts() -> list[tuple[str, Path, str]]:
    """返回 [(相对路径, 绝对路径, 首处差异说明)]。

    差异 = 磁盘上的 md 与「库渲染出来的文本」不一致。
    这既包含「库刚写完还没导出」，也包含「有人手改了 md」—— 本脚本不区分，
    一律留档，因为在覆盖前留一份永远不亏。
    """
    import snapshot_md as sm

    out: list[tuple[str, Path, str]] = []
    for row in sm._rows():
        rel = str(row["rel_path"]).replace("\\", "/")
        path = VAULT / rel
        if not path.is_file():
            out.append((rel, path, "文件不存在（库里还有这条）"))
            continue
        expect = sm.render(sm._meta(row), str(row["body_md"] or ""))
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError as e:
            out.append((rel, path, f"读取失败 {e}"))
            continue
        if actual != expect:
            why = sm._first_diff(expect, actual) if hasattr(sm, "_first_diff") else "内容不一致"
            out.append((rel, path, why))
    return out


def _prune(keep: int) -> int:
    """只保留最近 keep 次留档目录。返回删除的目录数。"""
    if not CONFLICT_DIR.is_dir():
        return 0
    dirs = sorted((d for d in CONFLICT_DIR.iterdir() if d.is_dir()),
                  key=lambda d: d.name, reverse=True)
    removed = 0
    for d in dirs[keep:]:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="导出前投影预检（只留档，不阻断）")
    ap.add_argument("--dry-run", action="store_true", help="只看差异，不留档")
    ap.add_argument("--keep", type=int, default=5, help="保留最近几次留档目录，默认 5")
    ap.add_argument("--quiet", action="store_true", help="无差异时不打印")
    args = ap.parse_args()

    try:
        conflicts = find_conflicts()
    except Exception as e:  # 预检自身坏了，绝不能拖累导出
        print(f"投影预检异常（不影响导出）：{type(e).__name__}: {e}")
        return 2

    if not conflicts:
        if not args.quiet:
            print("投影预检：磁盘 md 与库渲染逐字节一致，无覆盖风险")
        return 0

    aggregates = [c for c in conflicts if Path(c[0]).name in AGGREGATE_PAGES]

    if args.dry_run:
        print(f"投影预检（dry-run）：{len(conflicts)} 篇 md 与库不一致，未留档")
        for rel, _p, why in conflicts[:10]:
            print(f"  - {rel}  {why}")
        if len(conflicts) > 10:
            print(f"  （另有 {len(conflicts) - 10} 篇）")
        if aggregates:
            print(f"  ⚠ 其中含聚合页 {len(aggregates)} 篇：{[c[0] for c in aggregates]}")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_root = CONFLICT_DIR / ts
    archived = 0
    for rel, path, _why in conflicts:
        if not path.is_file():
            continue  # 文件不存在，没什么可留的
        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            archived += 1
        except OSError:
            pass

    pruned = _prune(max(1, args.keep))
    print(f"投影预检：{len(conflicts)} 篇 md 与库不一致，导出会覆盖；已留档 {archived} 篇 -> "
          f"{dest_root.relative_to(VAULT).as_posix()}"
          + (f"（清理旧档 {pruned} 份）" if pruned else ""))
    for rel, _p, why in conflicts[:8]:
        print(f"  - {rel}  {why}")
    if len(conflicts) > 8:
        print(f"  （另有 {len(conflicts) - 8} 篇）")
    if aggregates:
        print(f"  ⚠ 聚合页被手改：{[c[0] for c in aggregates]}")
        print("    聚合页只能走 memory_write 的 append / edits，直接 Edit 磁盘 md 必被导出覆盖。")
        print("    上一版已留档，需要可去上面目录取回并重新写库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
