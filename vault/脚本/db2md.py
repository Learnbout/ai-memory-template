#!/usr/bin/env python3
"""记忆层五表 → md 导出（md2fanout.py 的姊妹脚本）。

为什么单独一个脚本：
    `md2fanout.py` 管**知识类**（notes 表）—— 它的 md 是「权威源」，导出要严格保持
    与主库**字节级一致**（实测 75/75）。
    本脚本管**操作类**（对话 / 原子事实 / 双时间轴 / 校验留痕 / 溯源）—— 这些数据
    **只存在于数据库**、磁盘上没有对应文件，所以导出是单向产出，且按此前设计
    末尾带一个折叠的「溯源」块（知识类不加，以保持格式一致）。

出口设计：

| 表 | 输出 | 形态 |
|---|---|---|
| conversations | 对话/会话_<session_id>.md | 一次会话一个文件，每轮一个 `##` 小节 |
| atoms | 事实/原子记忆.md | 按 kind 分组 |
| facts | 事实/事实时间线.md | 含双时间轴，标出「现已不再成立」的旧值 |
| schema_log | 体检/结构校验记录.md | 检查项 + 结果 |
| provenance | 溯源/操作记录.md | 谁触发、为什么做 |

三条硬护栏沿用 md2fanout 的：绝不反向写回 / 绝不输出到主库内 / 只动自己写的文件。

用法：
    <venv>/python.exe 脚本/db2md.py --out D:/kb-view
    <venv>/python.exe 脚本/db2md.py --out D:/kb-view --dry-run
    <venv>/python.exe 脚本/db2md.py --out D:/kb-view --verify
    <venv>/python.exe 脚本/db2md.py --out D:/kb-view --no-provenance   # 不带溯源块
"""

from __future__ import annotations

try:
    from _usage import autotrack

    autotrack(__file__)
except Exception:
    pass

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）

DB_PATH = db_paths.resolve("index.sqlite3")
STATE_NAME = ".db2md.json"
SYSTEM_DIR_NAMES = {".archive", ".trash", ".workbuddy", ".git", ".obsidian",
                    "__pycache__", ".trash_backup", "备份归档"}

TABLE_ORDER = ("conversations", "atoms", "facts", "schema_log", "provenance")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在：{DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _resolve_out(raw: str) -> Path:
    out = Path(raw).expanduser().resolve()
    if out == VAULT:
        raise SystemExit(f"输出目录不能是主库本身：{out}")
    if VAULT in out.parents:
        parts = out.relative_to(VAULT).parts
        if not parts or parts[0] not in SYSTEM_DIR_NAMES:
            raise SystemExit(
                f"输出目录落在主库内：{out}\n"
                f"主库递归扫描 *.md，落在里面的导出会被索引两次。换主库之外的路径。")
    if out in VAULT.parents:
        raise SystemExit(f"输出目录不能包含主库：{out}")
    return out


# ────────────────────────── 渲染 ──────────────────────────


def _esc_cell(s: str) -> str:
    """表格单元格转义：竖线与换行会让表格结构坏掉。"""
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def _fm(title: str, extra: dict | None = None) -> str:
    """操作类导出物的 frontmatter（不与知识类的 13 字段规范混用）。

    **这里刻意不放「生成时间」**（2026-09-17 踩到）：
    首版写了 `generated: <now>`，导致每次运行内容都不同 —— 既不幂等
    （二次运行仍「已写 5」），`--verify` 也必然报全部不一致，验收信号直接失效。
    生成时间放在同目录的 `.db2md.json` 状态文件里（那里本就该记运行元信息）。
    """
    lines = ["---", f"title: {title}", "source: db2md.py", "readonly: true"]
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _tail_provenance(rows: list[dict]) -> str:
    """折叠的溯源块 —— 主人要求「看明白你干了什么、读了什么数据」。"""
    if not rows:
        return ""
    lines = ["", "---", "", "> [!note]- 操作溯源", "> 本文件由脚本从数据库导出，以下为对应的操作记录。", ">"]
    for r in rows[:30]:
        lines.append(f"> - **{r.get('action', '')}** ｜ {r.get('reason', '')} ｜ 触发：{r.get('trigger', '')} ｜ {r.get('at', '')}")
    if len(rows) > 30:
        lines.append(f"> - …另有 {len(rows) - 30} 条")
    lines.append("")
    return "\n".join(lines)


def render_conversations(conn) -> dict[str, str]:
    """一次会话一个文件，每轮一个 ## 小节。"""
    out: dict[str, str] = {}
    if not _has_table(conn, "conversations"):
        return out
    rows = list(conn.execute("SELECT * FROM conversations ORDER BY session_id, turn, id"))
    if not rows:
        return out
    by_sess: dict[str, list] = {}
    for r in rows:
        by_sess.setdefault(str(r["session_id"] or "未命名会话"), []).append(r)

    for sid, items in by_sess.items():
        first = items[0]["at"] or ""
        body = [f"# 会话 {sid}", "",
                f"> 共 {len(items)} 轮 ｜ 起始 {first} ｜ 由脚本从 L0 对话层导出", ""]
        for it in items:
            role = it["role"] or "?"
            head = {"user": "你", "assistant": "助手", "tool": "工具"}.get(role, role)
            body.append(f"## {head} · 第 {it['turn']} 轮")
            body.append("")
            body.append(f"> {it['at'] or ''}")
            body.append("")
            body.append(str(it["content"] or "").rstrip())
            body.append("")
        out[f"对话/会话_{sid}.md"] = _fm(f"会话 {sid}", {"session_id": sid, "turns": len(items)}) + "\n" + "\n".join(body)
    return out


def render_atoms(conn) -> dict[str, str]:
    if not _has_table(conn, "atoms"):
        return {}
    rows = list(conn.execute("SELECT * FROM atoms ORDER BY kind, id"))
    if not rows:
        return {}
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(str(r["kind"] or "未分类"), []).append(r)

    label = {"fact": "事实", "preference": "偏好", "constraint": "约束", "conclusion": "结论"}
    body = ["# 原子记忆", "",
            f"> 共 {len(rows)} 条，按类型分组 ｜ 由脚本从 L1 原子记忆层导出", ""]
    for kind, items in by_kind.items():
        body.append(f"## {label.get(kind, kind)}（{len(items)}）")
        body.append("")
        for it in items:
            conf = it["confidence"]
            conf_s = f"（置信 {conf:.2f}）" if isinstance(conf, (int, float)) else ""
            body.append(f"- **{it['subject']} {it['predicate']}**：{it['object']}{conf_s}")
            body.append(f"  来源：{it['source_ref'] or '—'} ｜ 记录于 {it['at'] or '—'}")
        body.append("")
    return {"事实/原子记忆.md": _fm("原子记忆", {"count": len(rows)}) + "\n" + "\n".join(body)}


def render_facts(conn) -> dict[str, str]:
    """双时间轴：标出「已不再成立」的旧值。"""
    if not _has_table(conn, "facts"):
        return {}
    rows = list(conn.execute("SELECT * FROM facts ORDER BY subject, valid_from"))
    if not rows:
        return {}

    now = _now()
    body = ["# 事实时间线", "",
            f"> 共 {len(rows)} 条 ｜ 双时间轴（何时成立 / 何时记录），旧值不删除只标记 ｜ 由脚本导出", "",
            "| 主体 | 属性 | 取值 | 生效期 | 状态 |", "|---|---|---|---|---|"]
    for it in rows:
        vf, vt = it["valid_from"] or "—", it["valid_to"]
        period = f"{vf[:10]} ~ {'至今' if not vt else vt[:10]}"
        state = "**当前**" if not vt else "已失效"
        body.append(f"| {_esc_cell(it['subject'])} | {_esc_cell(it['predicate'])} | "
                    f"{_esc_cell(it['object'])} | {_esc_cell(period)} | {state} |")
    body.append("")
    body.append("## 明细")
    body.append("")
    for it in rows:
        body.append(f"### {it['subject']} · {it['predicate']}")
        body.append("")
        body.append(f"- 取值：**{it['object']}**")
        body.append(f"- 生效：{it['valid_from'] or '—'} → {it['valid_to'] or '（仍然成立）'}")
        body.append(f"- 记录于：{it['tx_at'] or '—'}")
        if it["note"]:
            body.append(f"- 备注：{it['note']}")
        body.append("")
    return {"事实/事实时间线.md": _fm("事实时间线", {"count": len(rows)}) + "\n" + "\n".join(body)}


def render_schema_log(conn) -> dict[str, str]:
    if not _has_table(conn, "schema_log"):
        return {}
    rows = list(conn.execute("SELECT * FROM schema_log ORDER BY id DESC"))
    if not rows:
        return {}
    body = ["# 结构校验记录", "",
            f"> 共 {len(rows)} 条 ｜ 本库三次表格事故后新增的留痕机制 ｜ 由脚本导出", "",
            "| 时间 | 对象 | 检查项 | 结果 | 详情 |", "|---|---|---|---|---|"]
    for it in rows:
        mark = "通过" if (it["result"] or "").lower() == "pass" else "**不通过**"
        body.append(f"| {_esc_cell((it['at'] or '')[:19])} | {_esc_cell(it['target'])} | "
                    f"{_esc_cell(it['check_name'])} | {mark} | {_esc_cell(it['detail'])} |")
    body.append("")
    return {"体检/结构校验记录.md": _fm("结构校验记录", {"count": len(rows)}) + "\n" + "\n".join(body)}


def render_provenance(conn, with_tail: bool) -> dict[str, str]:
    if not _has_table(conn, "provenance"):
        return {}
    rows = list(conn.execute("SELECT * FROM provenance ORDER BY id DESC"))
    if not rows:
        return {}
    body = ["# 操作记录", "",
            f"> 共 {len(rows)} 条 ｜ 记「谁触发、为什么做」｜ 由脚本导出", "",
            "| 时间 | 对象 | 动作 | 原因 | 触发者 |", "|---|---|---|---|---|"]
    for it in rows:
        body.append(f"| {_esc_cell((it['at'] or '')[:19])} | {_esc_cell(it['target_title'])} | "
                    f"{_esc_cell(it['action'])} | {_esc_cell(it['reason'])} | {_esc_cell(it['trigger'])} |")
    body.append("")
    return {"溯源/操作记录.md": _fm("操作记录", {"count": len(rows)}) + "\n" + "\n".join(body)}


RENDERERS = (
    ("conversations", render_conversations),
    ("atoms", render_atoms),
    ("facts", render_facts),
    ("schema_log", render_schema_log),
    ("provenance", render_provenance),
)


def build_all(conn, with_tail: bool) -> dict[str, str]:
    """渲染全部文件；with_tail 时给每个操作类文件末尾追加溯源块。

    注意：溯源文件自身不加（它本身就是溯源，加了自己的溯源没意义）。
    """
    out: dict[str, str] = {}

    tail_rows: list[dict] = []
    if with_tail and _has_table(conn, "provenance"):
        tail_rows = [dict(r) for r in
                     conn.execute("SELECT * FROM provenance ORDER BY id DESC LIMIT 30")]
    tail = _tail_provenance(tail_rows)

    for name, fn in RENDERERS:
        try:
            got = fn(conn, with_tail) if name == "provenance" else fn(conn)
        except TypeError:
            got = fn(conn)
        if not got:
            continue
        if tail and name != "provenance":
            got = {k: (v if v.endswith("\n") else v + "\n") + tail for k, v in got.items()}
        out.update(got)
    return out


# ────────────────────────── 导出 ──────────────────────────


def _read_state(out: Path) -> dict:
    f = out / STATE_NAME
    if not f.exists():
        return {"files": {}}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d.get("files"), dict) else {"files": {}}
    except Exception:
        return {"files": {}}


def _write(path: Path, text: str, dry: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    if dry:
        return True
    tmp = path.with_name(path.name + ".tmp-db2md")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


def export(out: Path, dry: bool, with_tail: bool, no_clean: bool) -> dict:
    t0 = time.perf_counter()
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
    state = _read_state(out)
    prev = state.get("files", {})

    conn = _connect()
    try:
        files = build_all(conn, with_tail)
        counts = {}
        for t in TABLE_ORDER:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if _has_table(conn, t) else 0
    finally:
        conn.close()

    written = unchanged = 0
    planned, state_files = set(), {}
    for rel, text in files.items():
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        planned.add(rel)
        state_files[rel] = digest
        if prev.get(rel) == digest and (out / rel).exists():
            unchanged += 1
            continue
        if _write(out / rel, text, dry):
            written += 1

    removed = 0
    if not no_clean:
        for rel in sorted(prev):
            if rel in planned:
                continue
            p = out / rel
            if p.exists():
                if not dry:
                    p.unlink()
                removed += 1
                print(f"  [{'将删除' if dry else '已删除'}] {rel}")

    if not dry:
        (out / STATE_NAME).write_text(json.dumps({
            "generated_at": _now(), "source_db": str(DB_PATH),
            "tables": counts, "files": state_files,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    return {"tables": counts, "files": len(files), "written": written,
            "unchanged": unchanged, "removed": removed,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


def verify(out: Path, with_tail: bool) -> int:
    conn = _connect()
    try:
        files = build_all(conn, with_tail)
    finally:
        conn.close()
    bad = 0
    for rel, text in files.items():
        p = out / rel
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            print(f"  [不一致] {rel}")
            bad += 1
    print(f"  校验 {len(files)} 个文件，不一致 {bad} 个")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆层五表 → md 导出")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-clean", action="store_true")
    ap.add_argument("--no-provenance", action="store_true", help="不在操作类文件末尾加溯源块")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stat", action="store_true", help="只看各表行数")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()

    global VAULT, DB_PATH
    if args.vault:
        VAULT = Path(args.vault).expanduser().resolve()
    os.environ["AI_MEMORY_DIR"] = str(VAULT)
    DB_PATH = db_paths.resolve("index.sqlite3")

    if args.stat:
        conn = _connect()
        try:
            for t in TABLE_ORDER:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if _has_table(conn, t) else "（表不存在）"
                print(f"  {t:16s} {n}")
        finally:
            conn.close()
        return 0

    out = _resolve_out(args.out)
    print(f"主库：{VAULT}")
    print(f"输出：{out}")
    print(f"溯源块：{'不加' if args.no_provenance else '加'}{'（dry-run）' if args.dry_run else ''}")

    with_tail = not args.no_provenance
    if args.verify:
        print()
        return 1 if verify(out, with_tail) else 0

    info = export(out, args.dry_run, with_tail, args.no_clean)
    print()
    print("各表行数：" + " ｜ ".join(f"{k} {v}" for k, v in info["tables"].items()))
    print(f"生成 {info['files']} 个文件 ｜ {'将写' if args.dry_run else '已写'} {info['written']} ｜ "
          f"未变 {info['unchanged']} ｜ 清理 {info['removed']} ｜ {info['elapsed_ms']} ms")
    if not args.dry_run:
        print(f"状态文件：{out / STATE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
