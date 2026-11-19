#!/usr/bin/env python3
"""SQLite 索引 -> 独立 md 文件夹（单向只读导出，供 Obsidian 直接打开）。

背景（2026-09-17 主人需求）：
    主人希望「AI 读数据库（快、省 token），人读 md（Obsidian 一目了然）」。
    本脚本做的是**单向投影**：

        md 文件夹（唯一事实源）
              |
              |  memory_web.build_index()  只读，从不动 md
              v
        数据库/index.sqlite3（查询索引；2026-10-19 起为记忆库本体）
              |
              |  本脚本：notes.body_md 原样导出 + frontmatter 重建
              v
        任意输出文件夹（第二份 md，供 Obsidian 当 vault 打开）

三条铁律（写死在代码里，改之前先想清楚）：

1. **绝不反向写回**。本脚本只有读索引、读 md（拿原始字节）、写输出目录三种动作。
   没有「把输出目录的改动同步回主库」的代码路径。想双向改内容，去改主库。
   理由：2026-09-15 本库出过一次聚合页被整篇覆盖、静默丢 59 条动态的事故，
   双向同步器写错一次就是整库级灾难，收益（「在 SQL 里改也能生效」）不值这笔账。

2. **绝不写进主库目录**。输出目录若落在 VAULT 内且不是系统目录，会被
   memory_store._active_md_files() 递归扫到 -> 同一篇笔记被索引两次
   （2026-09-16 已实测过一次重复索引事故，索引数 71->72）。故此处硬拦。

3. **只动自己写的文件**。输出目录内除 `<文件名>.md` 与 `.md2fanout.json`
   之外的任何东西（人的手写笔记、附件、.obsidian 配置）一律不碰，
   不参与「多余文件清理」。清理只发生在「上次导出记录里有、这次没有了」的路径上。

输出文件与主库文件的差异（刻意设计）：

    输出 = frontmatter + 正文        （无任何导出器痕迹）
    正文 = 主库正文的**原始字节**（含末尾换行），不 strip、不改写
    frontmatter = 按索引里的**结构化字段**重新序列化，字段顺序固定，
                 外加两个事实字段（chars / source_path）便于在 Obsidian 里检索

为什么 frontmatter 要重建而不是原样抄：
    1. 去掉了内部运行态字段（tier / access_count / version / schema_version），
       人的仓库里不需要看到这些；
    2. 可以顺带补上导出那一刻的客观事实（字符数、源文件相对路径）；
    3. 依赖只是 memory_store 自己的 YAML 工具，锁步演进，不引第三方。

用法：
    python 脚本/md2fanout.py --out D:/kb-view                  # 完整导出
    python 脚本/md2fanout.py --out D:/kb-view --dry-run        # 只看会改哪些文件
    python 脚本/md2fanout.py --out D:/kb-view --level full     # 带上事实源原始字节
    python 脚本/md2fanout.py --out D:/kb-view --level plain    # 只留标题（最干净）
    python 脚本/md2fanout.py --out D:/kb-view --stat           # 只看现状不导出
    python 脚本/md2fanout.py --out D:/kb-view --verify         # 逐字节校验产物 == 索引
    python 脚本/md2fanout.py --rebuild-index --out D:/kb-view  # 先重建索引再导出

三档 level：
    optimize（默认）  frontmatter + 正文，正文取自索引 body_md
    full              多写一段「事实源原始字节」（从主库 md 重读并附加），
                      用于排错比对；不参与 Obsidian 正常阅读
    plain             只写 `# 标题` + 正文，不要 frontmatter（交给 Dataview 之类就没法用了）

实现依赖：memory_store.py（同库，零第三方依赖）。注意必须先设 AI_MEMORY_DIR
再 import，否则会 fallback 到 ~/ai-memory 并在那里静默建空库。
"""

from __future__ import annotations

try:  # 脚本使用计数（写 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
sys.path.insert(0, str(VAULT))
import db_paths  # 数据库位置唯一入口（2026-10-19 收口到「数据库/」）

SYS_VAULT = VAULT
INDEX_PATH = db_paths.resolve("index.sqlite3")
LOCK_PATH = VAULT / ".workbuddy" / "md2fanout.lock"
STATE_NAME = ".md2fanout.json"


def _use_vault(new_vault: Path) -> None:
    """切换主库路径。必须同时改 env，否则 memory_store 的模块常量不会跟着走。"""
    global VAULT, INDEX_PATH, LOCK_PATH
    VAULT = new_vault
    os.environ["AI_MEMORY_DIR"] = str(VAULT)
    INDEX_PATH = db_paths.resolve("index.sqlite3")
    LOCK_PATH = VAULT / ".workbuddy" / "md2fanout.lock"

# 输出目录不能落在主库里，否则会被 memory_store 递归扫到 -> 重复索引。
# 允许的例外：主库的系统目录（本就在扫描白名单外），实测场景是临时输出。
SYSTEM_DIR_NAMES = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__", ".trash_backup", "备份归档"}

INDEX_COLUMNS = (
    "title", "stem", "rel_path", "dir",
    "type", "scope", "tier", "source", "verified",
    "tags", "summary",
    "created", "updated", "version", "access_count", "schema_version",
    "chars", "body_md",
)

LEVELS = ("optimize", "full", "plain")

# 自动生成的索引页 → 需要跳过的字段（它们的生成器没写这些字段，
# 导出时若补上就与主库不一致）。实测口径见 _frontmatter 注释。
AUTO_PAGE_SKIP: dict[str, set[str]] = {
    "记忆索引": {"created", "tier"},
    "路由索引": {"version"},
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


# ────────────────────────── 基础设施 ──────────────────────────


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    if not INDEX_PATH.exists():
        raise SystemExit(
            f"索引不存在：{INDEX_PATH}\n"
            f"先跑一次 `python 脚本/memory_web.py --rebuild-only`，或加 --rebuild-index。"
        )
    conn = sqlite3.connect(f"file:{INDEX_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(f"SELECT {', '.join(INDEX_COLUMNS)} FROM notes ORDER BY rel_path")
    return cur.fetchall()


def _index_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["k"]: r["v"] for r in conn.execute("SELECT k, v FROM meta")}
    except sqlite3.Error:
        return {}


# ────────────────────────── 输出安全 ──────────────────────────


def _resolve_out(raw: str) -> Path:
    out = Path(raw).expanduser().resolve()
    if out == VAULT:
        raise SystemExit(f"输出目录不能是主库本身：{out}")
    if VAULT in out.parents:
        parts = out.relative_to(VAULT).parts
        if not parts or parts[0] not in SYSTEM_DIR_NAMES:
            raise SystemExit(
                f"输出目录落在主库内：{out}\n"
                f"主库递归扫描 *.md，落在里面的导出会被索引两次（2026-09-16 已出过该事故）。\n"
                f"换一个主库之外的路径，或放到主库的 .workbuddy/ 下。"
            )
    if out in VAULT.parents:
        raise SystemExit(f"输出目录不能包含主库（会与主库文件混在一起）：{out}")
    return out


def _read_state(out: Path) -> dict:
    f = out / STATE_NAME
    if not f.exists():
        return {"files": {}}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data.get("files"), dict):
            raise ValueError("files 字段异常")
        return data
    except Exception as e:
        print(f"  [警告] {STATE_NAME} 读不动（{e}），按全新导出处理")
        return {"files": {}}


def _cleanup_stale(out: Path, state: dict, planned: set[str], dry: bool) -> list[str]:
    """删掉「上次导出记录里有、这次没有」的文件，外加因此变空的目录。

    只按 state 记录删 => 人的手写笔记永远不会被误伤。
    """
    removed: list[str] = []
    touched_dirs: set[Path] = set()

    for rel in sorted(state.get("files", {})):
        if rel in planned:
            continue
        p = out / rel
        if p.exists():
            if not dry:
                try:
                    p.unlink()
                except OSError as e:
                    print(f"  [警告] 删除失败 {rel}：{e}")
                    continue
            removed.append(rel)
            # 只登记「删过文件的那些目录」，绝不去动别处的空目录
            d = p.parent
            while d != out and out in d.parents:
                touched_dirs.add(d)
                d = d.parent

    # 清理因删除而变空的目录（只限删过文件的那几层）
    for d in sorted(touched_dirs, key=lambda p: -len(p.parts)):
        try:
            if d.exists() and not any(d.iterdir()):
                if not dry:
                    d.rmdir()
                removed.append(f"{d.relative_to(out).as_posix()}/")
        except OSError:
            pass
    return removed


class _Lock:
    """进程锁：防止两个导出器同时写输出目录（本库标准做法：unlink 当锁用）。"""

    def __init__(self, path: Path, timeout: float = 15.0):
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self):
        # Windows 上 os.open 的 O_EXCL 对已存在文件直接失败
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                # 陈旧锁：持有者已死（容 3 秒误差）
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.timeout:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise SystemExit(f"另一个 md2fanout 正在运行（锁：{self.path}），等超时了。")
                time.sleep(0.3)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.path.unlink(missing_ok=True)
        return False


# ────────────────────────── md 生成 ──────────────────────────


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _text(v) -> str:
    """索引里的值转字符串。sqlite 会把 NULL 给回来，'' 和 None 要分清。"""
    return "" if v is None else str(v)


def _frontmatter(row: sqlite3.Row, chars: int) -> str:
    """重建 frontmatter —— **复用 memory_store 的官方序列化出口**。

    为什么不再自己拼字段（2026-09-17 改）：
        首版自己拼，结果与主库有四处差异 —— tags 被加引号、裁掉了 6 个字段
        （tier / access_count / summary / version / schema_version）、布尔写成
        `False` 而非 `false`、多加了 `chars` / `source_path`。
        主人要「导出后就是现在这种结构和格式」，而复用官方出口即可做到
        **字节级一致**：实测 5 篇抽样 4 篇完全一致，剩下 1 篇的差异是主库
        自己带了多余引号（全库仅 5 处，属历史遗留，导出物反而更规范）。

    参数 chars 已不再使用（保留签名以免改动调用处），字段值一律以 row 为准。
    """
    import memory_store as ms

    def _int(v, default: int = 0) -> int:
        return v if isinstance(v, int) else default

    meta = {
        "title": _text(row["title"]),
        "tags": _split_tags(row["tags"]),
        "created": _text(row["created"]),
        "updated": _text(row["updated"]),
        "tier": _text(row["tier"]),
        "access_count": _int(row["access_count"]),
        "source": _text(row["source"]),
        "summary": _text(row["summary"]),
        "version": _int(row["version"]),
        "type": _text(row["type"]),
        "scope": _text(row["scope"]),
        "verified": bool(row["verified"]),
        "schema_version": _int(row["schema_version"], 1),
    }

    # ① 空值不输出 —— 否则会写出 `created: ""` 这种主库里根本不存在的行
    meta = {k: v for k, v in meta.items() if v != "" and v is not None}

    # ② 自动生成的索引页：字段集由各自的生成器决定，与普通笔记不同。
    #    实测（2026-09-17）：记忆索引不带 created/tier，路由索引不带 version。
    #    这里硬编码的原因：db 只存「值」不存「该条有哪些字段」，而这个差异是
    #    库的既有设计（与 脚本/check_frontmatter.py 的 AUTO_PAGES 豁免同口径）。
    for k in AUTO_PAGE_SKIP.get(_text(row["title"]), ()):
        meta.pop(k, None)

    # 官方出口含字段集规范化（补 type/scope/verified/schema_version + 排序 + 引号规则），
    # 返回值不含首尾的 ---
    return f"---\n{ms._dump_yaml_frontmatter(meta)}\n---\n"


def render_markdown(row: sqlite3.Row, level: str, source_bytes: str | None) -> str:
    """生成一条输出文件，末尾换行固定，保证幂等（同样输入 -> 字节一致）。

    末尾换行必须显式补：`memory_store._parse_frontmatter` 解析时会把正文两侧的
    空白 strip 掉，所以 db 里的 `body_md` **没有末尾换行**，而主库文件末尾是有的
    （`_write_memory` 写盘时补的）。不补就差一个 \\n，实测导致 24 篇里多数出现
    「末尾空行」差异（2026-09-17 逐字节比对发现）。
    """
    body = (row["body_md"] or "").rstrip("\n")

    if level == "plain":
        title = _text(row["title"])
        if body.startswith("#"):
            return body + "\n"
        return f"# {title}\n\n{body}\n"

    fm = _frontmatter(row, len(body))

    if level == "full":
        raw = source_bytes if source_bytes is not None else ""
        return f"{fm}\n{body}\n## 事实源原始字节\n\n```text\n{raw}\n```\n"

    return f"{fm}\n{body}\n"


def _write_text(path: Path, text: str, dry: bool) -> bool:
    """原子写；内容一致就跳过。返回是否真的写了。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    if dry:
        return True
    tmp = path.with_name(path.name + ".tmp-md2fanout")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


# ────────────────────────── 导出主流程 ──────────────────────────


def _source_bytes(row: sqlite3.Row) -> str | None:
    p = VAULT / _text(row["rel_path"])
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _stale_warning(conn: sqlite3.Connection, n_rows: int) -> str:
    """索引陈旧检测。

    索引是构建时快照，会滞后于 md。本库的规矩是「静默失效 = 缺陷」
    （见 2026-09-11 漏记率仪表三次反向结论），所以这里必须出声：
    实测 2026-09-17 首次跑本脚本时，存量索引是 09-16 的旧快照，
    主库实际 72 篇而索引只有 52 条 —— 若不出声，使用者会拿着少了 20 篇
    的「镜像」当完整备份。
    """
    try:
        import memory_store as ms

        on_disk = len(ms._active_md_files())
    except Exception as e:  # 检测本身失败不该阻断导出，但要说明
        return f"  [警告] 无法核对主库实际文件数（{e}），索引可能存在滞后"

    if on_disk == n_rows:
        return ""
    diff = on_disk - n_rows
    direction = f"索引少 {diff} 条（md 里新增的还没进索引）" if diff > 0 else f"索引多 {-diff} 条（md 已删但索引还留着）"
    return (
        f"  [警告] 索引与磁盘不一致：索引 {n_rows} 条，主库活跃 md {on_disk} 条 —— {direction}\n"
        f"          加 --rebuild-index 重建索引后再导出，否则这份镜像不完整。"
    )


def export(out: Path, level: str, dry: bool, no_clean: bool, quiet_stale: bool = False) -> dict:
    t0 = time.perf_counter()
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
    elif not out.exists():
        print(f"  [dry-run] 输出目录尚不存在，将创建：{out}")

    state = _read_state(out)
    prev_files = state.get("files", {})
    first_run = not state.get("generated_at")
    if first_run and not dry:
        # 首次导出报「未变 N」会让人以为什么都没做，明确标出来
        print("  [提示] 输出目录此前没有导出记录 —— 本次为全量首次导出")

    conn = _connect()
    try:
        rows = _load_rows(conn)
        idx_meta = _index_meta(conn)
        stale_msg = "" if quiet_stale else _stale_warning(conn, len(rows))
    finally:
        conn.close()

    if stale_msg:
        print(stale_msg)

    written, unchanged, removed = 0, 0, 0
    planned: set[str] = set()
    files_state: dict[str, str] = {}
    total_chars = 0

    for row in rows:
        rel = str(row["rel_path"]).replace("\\", "/")
        if not rel:
            print(f"  [警告] 跳过无路径记录：{row['title']}")
            continue
        src = _source_bytes(row) if level == "full" else None
        text = render_markdown(row, level, src)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        planned.add(rel)
        files_state[rel] = digest
        total_chars += len(text)

        target = out / rel
        if prev_files.get(rel) == digest and target.exists():
            unchanged += 1
            continue
        if _write_text(target, text, dry):
            written += 1

    if not no_clean:
        stale = _cleanup_stale(out, state, planned, dry)
        removed = len(stale)
        for s in stale[:20]:
            print(f"  [{'dry-run 将删除' if dry else '已删除'}] {s}")
        if len(stale) > 20:
            print(f"  ... 另有 {len(stale) - 20} 项")

    if not dry:
        new_state = {
            "generated_at": _iso_now(),
            "level": level,
            "notes": len(rows),
            "chars": total_chars,
            "index_built_at": idx_meta.get("built_at", ""),
            "source_vault": str(VAULT),
            "files": files_state,
        }
        (out / STATE_NAME).write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    return {
        "notes": len(rows),
        "written": written,
        "unchanged": unchanged,
        "removed": removed,
        "chars": total_chars,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def stat(out: Path, level: str) -> dict:
    """不写任何文件，只报现状（缺哪些、多哪些）。"""
    conn = _connect()
    try:
        rows = _load_rows(conn)
    finally:
        conn.close()

    state = _read_state(out)
    prev = state.get("files", {})
    missing, changed, stale = [], [], []

    for row in rows:
        rel = str(row["rel_path"]).replace("\\", "/")
        text = render_markdown(row, level, _source_bytes(row) if level == "full" else None)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if not (out / rel).exists():
            missing.append(rel)
        elif prev.get(rel) != digest:
            changed.append(rel)

    for rel in prev:
        if not (out / rel).exists():
            stale.append(rel)

    orphan = []
    if out.exists():
        for p in out.rglob("*.md"):
            rel = p.relative_to(out).as_posix()
            if rel not in prev:
                orphan.append(rel)

    return {
        "notes": len(rows),
        "missing": missing,
        "changed": changed,
        "recorded_but_absent": stale,
        "untracked": orphan,
        "state_notes": len(prev),
        "state_generated_at": state.get("generated_at", ""),
    }


def verify(out: Path, level: str) -> int:
    """逐条比对：输出文件 == index 重新渲染的结果。返回不一致条数。"""
    conn = _connect()
    try:
        rows = _load_rows(conn)
    finally:
        conn.close()

    bad = 0
    for row in rows:
        rel = str(row["rel_path"]).replace("\\", "/")
        p = out / rel
        if not p.exists():
            print(f"  [缺失] {rel}")
            bad += 1
            continue
        expect = render_markdown(row, level, _source_bytes(row) if level == "full" else None)
        got = p.read_text(encoding="utf-8")
        if got != expect:
            print(f"  [不一致] {rel}")
            bad += 1
    print(f"  校验 {len(rows)} 条，不一致 {bad} 条")
    return bad


# ────────────────────────── CLI ──────────────────────────


def _rebuild_index() -> dict:
    import memory_web

    # 2026-10-21 修（真 bug）：原来调 `memory_web.build_index()` —— 该函数在
    # 2026-10-19 已改名为 `build_index_from_md()`，并且在库模式下**硬 raise**
    # （它做的是 DROP TABLE notes 再从 md 重建，而库模式下 md 是快照 ——
    # 等于「删了源再从源读」）。留着旧名字的后果不是报错，是 AttributeError
    # 让整个脚本崩掉：`--rebuild-index` 一调用就写不出任何文件，
    # test_db_pipeline 的 T2.1 / T3.1 因此长期假红（T2 写 0 个 → T3 变成「首次导出」）。
    #
    # 库为源之后，「导出前刷新」该做的是重算派生表（links），notes 一行都不该动。
    return memory_web.rebuild_derived()


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 索引 -> 独立 md 文件夹（单向只读导出）")
    ap.add_argument("--out", required=True, help="输出文件夹（Obsidian vault 指向这里）")
    ap.add_argument("--level", choices=LEVELS, default="optimize")
    ap.add_argument("--dry-run", action="store_true", help="只报会改什么，不落盘")
    ap.add_argument("--no-clean", action="store_true", help="不清理多余文件")
    ap.add_argument("--rebuild-index", action="store_true", help="导出前先重算派生表（links）；库为源，不重建 notes")
    ap.add_argument("--stat", action="store_true", help="只看现状，不导出")
    ap.add_argument("--verify", action="store_true", help="逐字节校验产物与索引一致")
    ap.add_argument("--vault", default=str(SYS_VAULT))
    args = ap.parse_args()

    _use_vault(Path(args.vault).expanduser().resolve())

    out = _resolve_out(args.out)
    print(f"主库（事实源）：{VAULT}")
    print(f"索引：{INDEX_PATH}")
    print(f"输出：{out}")
    print(f"档位：{args.level}{'（dry-run）' if args.dry_run else ''}")

    if args.rebuild_index:
        if args.dry_run:
            print("  [dry-run] 跳过索引重建")
        else:
            info = _rebuild_index()
            print(f"索引已重建：{info['notes']} 条笔记 / {info['links']} 条双链，"
                  f"耗时 {info['elapsed_ms']} ms")

    if args.stat:
        s = stat(out, args.level)
        print(f"\n索引条数：{s['notes']}   上次导出记录：{s['state_notes']} 条"
              f"（{s['state_generated_at'] or '无'}）")
        for key, label in (("missing", "输出目录缺失"), ("changed", "内容已变待更新"),
                           ("recorded_but_absent", "有记录但文件不在"), ("untracked", "目录里未登记的文件")):
            items = s[key]
            print(f"  {label}：{len(items)}" + (f"（如 {items[0]}）" if items else ""))
        return 0

    if args.verify:
        print()
        return 1 if verify(out, args.level) else 0

    with _Lock(LOCK_PATH):
        if not args.dry_run:
            out.mkdir(parents=True, exist_ok=True)
        info = export(out, args.level, args.dry_run, args.no_clean, quiet_stale=args.rebuild_index)

    print()
    print(f"笔记 {info['notes']} 条 | "
          f"{'将写' if args.dry_run else '已写'} {info['written']} | "
          f"未变 {info['unchanged']} | 清理 {info['removed']} | "
          f"{info['chars']:,} 字符 | {info['elapsed_ms']} ms")
    if not args.dry_run:
        print(f"状态文件：{out / STATE_NAME}")
        print("提醒：这是单向只读投影 —— 在输出目录里改内容不会回流主库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
