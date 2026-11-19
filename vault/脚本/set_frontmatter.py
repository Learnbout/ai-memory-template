#!/usr/bin/env python3
"""按官方序列化出口改笔记的 frontmatter 字段（避免手工 Edit 踩坑）。

为什么有这个脚本（2026-09-17 立）：
    同一天里 AI 两次用「手工 Edit」改聚合页 frontmatter，把新 `updated` 追加在
    `summary` 之后，导致该字段**重复出现两次**；还因手工编辑绕过 `_clamp_summary`
    使三个 summary 涨到 412~419 字符（上限 400）。

    根因是「手工拼 frontmatter」这件事本身。本脚本改为：
      解析（memory_store._parse_frontmatter）→ 改字段 → 序列化（_dump_yaml_frontmatter）→ 写回
    完整走官方出口，字段顺序、引号规则、空值处理、上限都由库自己保证。
    改完自动跑一次自校验（重排结果必须与写盘内容一致），不通过就回滚。

用法：
    <venv>/python.exe 脚本/set_frontmatter.py --title 近期工作动态 --version 75 --touch
    <venv>/python.exe 脚本/set_frontmatter.py --title 项目状态 --summary "新的摘要…"
    <venv>/python.exe 脚本/set_frontmatter.py --title X --summary-file new_summary.txt
    <venv>/python.exe 脚本/set_frontmatter.py --title X --set tier=cold --set access_count=0
    <venv>/python.exe 脚本/set_frontmatter.py --title X --show      # 只看当前 frontmatter
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
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))

SUMMARY_LIMIT = 400   # 与 memory_store._clamp_summary 一致


def find_note(title: str) -> Path | None:
    import memory_store as ms

    for path in ms._active_md_files():
        meta, _ = ms._load_memory(path)
        if ms._entry_title(meta, path) == title or path.stem == title:
            return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="按官方序列化改 frontmatter（安全，带自校验）")
    ap.add_argument("--title", required=True, help="笔记标题（或文件名 stem）")
    ap.add_argument("--summary", default=None)
    ap.add_argument("--summary-file", default=None, help="从文件读 summary（长文本用）")
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--touch", action="store_true", help="把 updated 设为当前时间")
    ap.add_argument("--set", action="append", default=[], metavar="K=V", help="设置任意字段")
    ap.add_argument("--show", action="store_true", help="只显示当前 frontmatter")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)

    import memory_store as ms

    path = find_note(args.title)
    if path is None:
        print(f"找不到笔记：{args.title}")
        return 1

    meta, body = ms._load_memory(path)
    orig_meta = dict(meta)   # 备份用：后面 meta 会被就地改

    if args.show:
        print(f"{path.relative_to(VAULT).as_posix()}")
        print(ms._dump_yaml_frontmatter(dict(meta)))
        return 0

    changes = []

    if args.summary_file:
        text = Path(args.summary_file).read_text(encoding="utf-8").strip()
        if len(text) > SUMMARY_LIMIT:
            print(f"summary 超长：{len(text)} > {SUMMARY_LIMIT}，拒绝写入")
            return 1
        meta["summary"] = text
        changes.append(f"summary（{len(text)} 字）")
    elif args.summary is not None:
        if len(args.summary) > SUMMARY_LIMIT:
            print(f"summary 超长：{len(args.summary)} > {SUMMARY_LIMIT}，拒绝写入")
            return 1
        meta["summary"] = args.summary
        changes.append(f"summary（{len(args.summary)} 字）")

    if args.version is not None:
        meta["version"] = args.version
        changes.append(f"version={args.version}")

    for kv in args.set:
        if "=" not in kv:
            print(f"--set 需要 K=V 形式：{kv}")
            return 1
        k, v = kv.split("=", 1)
        # 数字自动转 int，true/false 转 bool
        if v.isdigit():
            meta[k.strip()] = int(v)
        elif v.lower() in ("true", "false"):
            meta[k.strip()] = v.lower() == "true"
        else:
            meta[k.strip()] = v
        changes.append(f"{k.strip()}={v}")

    if args.touch:
        meta["updated"] = datetime.now(timezone.utc).isoformat()
        changes.append("updated")

    if not changes:
        print("没有要改的字段（用 --summary / --version / --touch / --set）")
        return 0

    new_fm = ms._dump_yaml_frontmatter(dict(meta))
    new_text = f"---\n{new_fm}\n---\n\n{body.rstrip(chr(10))}\n"

    # 自校验：重排后再次解析，必须能读回同样的字段（防序列化缺陷）
    probe = VAULT / ".workbuddy" / "_set_fm_probe.md"
    probe.write_text(new_text, encoding="utf-8", newline="\n")
    try:
        m2, b2 = ms._load_memory(probe)
        if b2.strip() != body.strip():
            print("自校验失败：正文在序列化后发生变化，已中止")
            return 1
        for k in ("title", "summary", "version"):
            if k in meta and m2.get(k) != meta.get(k):
                print(f"自校验失败：字段 {k} 写回后不一致（{meta.get(k)!r} -> {m2.get(k)!r}）")
                return 1
    finally:
        probe.unlink(missing_ok=True)

    bak = VAULT / "备份归档" / f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}-setfm前"
    bak.parent.mkdir(exist_ok=True)
    # 备份的是**改之前**的内容，由内存里那份渲染出来，而不是 `copy2(path)` ——
    # 库为源之后，新写的笔记磁盘上可能压根没有文件，copy2 会直接 FileNotFoundError。
    bak.write_text(
        "---\n" + ms._dump_yaml_frontmatter(orig_meta) + "\n---\n\n" + body.rstrip("\n") + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # 落盘出口必须走 `_save_meta_body`：库模式下写 notes 行，md 模式下写文件。
    # 2026-10-20 之前这里是 `path.write_text(...)` —— 库为源之后那等于「改快照」：
    # 库行不动（搜索看不到新 summary），下一次 snapshot 导出还会把它覆盖回去。
    ms._save_meta_body(path, meta, body)

    print(f"已改 {path.relative_to(VAULT).as_posix()}（{'库' if ms._db_mode() else 'md'}）")
    print(f"  字段：{', '.join(changes)}")
    print(f"  备份：{bak.name}")
    print()
    print("新的 frontmatter：")
    print(new_fm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
