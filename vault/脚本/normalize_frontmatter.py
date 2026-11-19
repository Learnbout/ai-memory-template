"""主库 frontmatter 与末尾换行的规范化（幂等，带备份，默认 dry-run）。

为什么做：
    导出物与主库的差异只剩两类，且**都是主库自身的不规范** ——
    ① 56 篇缺末尾换行（POSIX 与 git 都建议文本以单个 \\n 结尾）
    ② 8 处 summary 引号与库自己的 _yaml_quote_scalar 规则不符（手工编辑留下）
    统一后，导出物即可与主库 100% 字节一致，`--verify` 也才能真正当验收工具
    （否则永远有已知差异，验收信号失效）。

安全设计：
    - 默认 dry-run，必须显式 --apply
    - 改前整库快照到 备份归档/（带时间戳）
    - 只动 frontmatter 的引号形态与文件末尾换行，**值本身与正文一字不改**
    - 改完逐篇自校验：用 memory_store._dump_yaml_frontmatter 重排结果必须与文件一致（幂等）

用法：
    python 脚本/normalize_frontmatter.py            # dry-run
    python 脚本/normalize_frontmatter.py --apply    # 执行
"""

from __future__ import annotations

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))

SKIP_DIRS = {".archive", ".trash", ".trash_backup", ".workbuddy", ".git", ".obsidian",
             "__pycache__", "备份归档"}

FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.+)$")


def normalize_value_line(line: str, qs) -> str:
    """按 _yaml_quote_scalar 规则统一单行的引号形态；列表与空值行原样返回。"""
    m = FIELD_RE.match(line)
    if not m:
        return line
    key, val = m.group(1), m.group(2)
    if val.startswith("["):          # 列表（tags）不动
        return line
    if val in ('""', "''"):
        return line

    has_quote = val.startswith('"') and val.endswith('"') and len(val) >= 2
    if has_quote:
        inner = val[1:-1]
        # 反转义：先 \" 再 \\
        inner = inner.replace('\\"', '"').replace("\\\\", "\\")
    else:
        inner = val

    return f"{key}: {qs(inner)}"


def normalize_file(path: Path, qs, apply: bool) -> tuple[bool, str]:
    """返回 (是否有改动, 说明)。"""
    raw = path.read_text(encoding="utf-8")
    notes = []

    # ① 末尾换行：统一为单个 \n
    if not raw.endswith("\n") or raw.endswith("\n\n"):
        raw = raw.rstrip("\n") + "\n"
        notes.append("末尾换行")

    # ② frontmatter 引号规范化（只处理首尾 --- 之间）
    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end > 0:
            head = raw[:4]
            fm = raw[4:end]
            tail = raw[end:]
            new_fm_lines = []
            fm_changed = False
            for line in fm.split("\n"):
                nl = normalize_value_line(line, qs)
                if nl != line:
                    fm_changed = True
                new_fm_lines.append(nl)
            if fm_changed:
                raw = head + "\n".join(new_fm_lines) + tail
                notes.append("引号")

    if not notes:
        return False, ""

    if apply:
        bak = path.with_suffix(path.suffix + ".normbak")
        shutil.copy2(path, bak)
        try:
            path.write_text(raw, encoding="utf-8", newline="")
        finally:
            bak.unlink(missing_ok=True)

    return True, "+".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser(description="主库 frontmatter 与末尾换行规范化")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只报告）")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)

    import memory_store as ms

    # 库为源（2026-10-20）之后本脚本的 `--apply` 已经**没有意义且有害**：
    #   · frontmatter 的引号口径由 `_dump_yaml_frontmatter` 官方出口统一产出，
    #     库里的值不经过 YAML 文本，不存在「引号不一致」这种问题；
    #   · 即使真去改 md，那也只是改快照 —— 库行不动，下一次 snapshot 还会覆盖回去。
    # 检测模式（不带 --apply）仍然有用：它能查出**历史遗留**的旧 md 是否需要重导。
    if args.apply and ms._db_mode():
        print(
            "拒绝执行：当前是库为源模式（AI_MEMORY_DB_ONLY 未关闭）。\n"
            "  frontmatter 由 _dump_yaml_frontmatter 统一产出，不存在需要规范化的文本；\n"
            "  手改 md 只会改到快照，下次 sync_all 导出即被覆盖。\n"
            "  若确实要修 md 快照，用：脚本/snapshot_md.py --restore --force（从库重建）。"
        )
        return 2

    files = []
    for f in sorted(VAULT.rglob("*.md")):
        try:
            if any(p in SKIP_DIRS for p in f.relative_to(VAULT).parts):
                continue
        except ValueError:
            continue
        if f.stat().st_size == 0:
            continue
        files.append(f)

    # 整库快照（仅 apply 时）
    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        arch = VAULT / "备份归档" / f"normalize_snapshot_{stamp}"
        arch.mkdir(parents=True, exist_ok=True)
        for f in files:
            rel = f.relative_to(VAULT)
            dst = arch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        print(f"整库快照：{arch}（{len(files)} 篇）")
        print()

    changed = []
    for f in files:
        did, why = normalize_file(f, ms._yaml_quote_scalar, args.apply)
        if did:
            changed.append((f.relative_to(VAULT).as_posix(), why))

    print(f"{'已改' if args.apply else '待改'} {len(changed)} 篇 / 共 {len(files)} 篇")
    for rel, why in changed:
        print(f"  [{why}] {rel}")

    if not args.apply:
        print()
        print("（dry-run，未写盘。加 --apply 执行）")
        return 0

    # 自校验：改完再用官方序列化重排，必须与文件一致（幂等）
    print()
    print("=== 自校验：用 _dump_yaml_frontmatter 重排 frontmatter，比对是否一致 ===")
    bad = []
    for f in files:
        raw = f.read_text(encoding="utf-8")
        end = raw.find("\n---", 4)
        if end < 0:
            continue
        fm_orig = raw[4:end].strip()
        meta, _ = ms._load_memory(f)
        fm_re = ms._dump_yaml_frontmatter(dict(meta)).strip()
        if fm_orig != fm_re:
            bad.append((f.relative_to(VAULT).as_posix(), fm_orig[:60], fm_re[:60]))

    print(f"  不一致 {len(bad)} 篇")
    for rel, a, b in bad[:8]:
        print(f"    {rel}")
        print(f"      原: {a}")
        print(f"      排: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
