"""Filesystem, cache, locking, and index helpers for ai-memory."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os
import time
import logging
import sys
import threading
import functools

logger = logging.getLogger("ai-memory")
_log_handler = logging.StreamHandler(sys.stderr)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)

# Vault location. 默认取**本文件所在目录** —— 本模块就住在记忆库里，
# 位置本身就是答案，不需要猜。Override with the AI_MEMORY_DIR env var to point
# at another vault, e.g.  AI_MEMORY_DIR=D:/ai记忆 python server.py
#
# 为什么不再回落 ~/ai-memory（2026-10-19 目录收口时改）：旧默认值在「忘了设
# AI_MEMORY_DIR」时会静默在 C:\Users\<user>\ai-memory 建出一个空库（实测踩过两次），
# 而真正要读的库就在脚下。`__file__` 相对一定比一个猜出来的路径更接近真相；
# 真被别人挪走也只会在本文件旁边留下痕迹，不会跑去用户主目录。
#
# 惰性建库（2026-09-14，E5a「消掉静默回落」）：此前这里在 import 期就
# ``mkdir(parents=True, exist_ok=True)``，于是任何忘了设 AI_MEMORY_DIR 的临时脚本
# 一 import 就静默建出一个空库并写入「路由索引.md」（2026-09-11 实测踩到，产物已
# 移入 .trash；与 09-10「基准脚本误建空库」同源）。现在只在真正要写盘时才建库，
# 读路径零副作用；回落默认路径时打一次显眼警告。
_VAULT_ENV = os.environ.get("AI_MEMORY_DIR")
MEMORY_DIR = Path(_VAULT_ENV or Path(__file__).resolve().parent).expanduser().resolve()
MEMORY_LOCK = MEMORY_DIR / ".memory.lock"
LOCK_TIMEOUT = 15

if not _VAULT_ENV:
    logger.warning(
        "AI_MEMORY_DIR 未设置，记忆库回落默认路径 %s。"
        "这不是你要的库就先设 AI_MEMORY_DIR；本进程不会在 import 期建库。",
        MEMORY_DIR,
    )


def _ensure_vault() -> None:
    """写盘路径专用：确保库目录存在（惰性建库，E5a）。读路径不得调用。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# 结构升级（2026-09-04）：笔记按领域放入子目录，server 从"只扫库根"改为
# 递归扫描全部笔记。排除目录名与 .gitignore 同步，防止把系统目录当笔记。
SYSTEM_DIR_NAMES = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__", ".trash_backup", "备份归档", "协同"}
# 注（2026-10-15）：`协同` 是多 agent 线程的运行态目录（契约 / 留言 / thread.json）。
# 它是过程产物不是知识 —— 一条线程来回几轮就是十几份 .md，不排除会被
# `_active_md_files()` 收进索引，检索与导出一起被污染。
# 与 `备份归档` 同源：目录名不该靠「记得别在里面放 .md」这种约定保护，进白名单是根治。
# 注：`备份归档` 是 2026-09-16 起的快照归档目录。此前它没进白名单，靠约定
# 「里面只放不以 .md 结尾的 .bak 文件」来避免被索引。2026-09-17 实测踩到：
# 整库快照（74 篇 .md 副本）放进去后，`_active_md_files()` 从 74 涨到 222，
# 索引与导出双双被污染。补进白名单是根治——目录名不该靠约定保护。

# Wiki 链接正则：拆分时从旧单体 server 遗漏了定义（仅搬了 _extract_wiki_links 的用法），
# 导致 core 模式 memory_stats 调用即崩（NameError: WIKI_LINK_RE is not defined）。
# 2026-09-10 经真实 MCP 通道复测发现后，按 baseline(server.py:39) 原定义恢复。
# 捕获组 1 为页面名（不含可选的 `|别名`）。
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]|]+)?\]\]")

def _db_mode() -> bool:
    """当前是否「库是唯一存储」（2026-10-20）。

    单点判据，供 `_active_md_files` / `_load_memory` / `_save_meta_body` 共用。
    刻意每次都问一遍 vault_db（它自己带 env 开关与库可用性检查），不在这里缓存：
    缓存一旦和真实状态错开，就会变成「代码说走库、实际在读盘」这种只能翻代码
    才能解释的故障。
    """
    try:
        import vault_db

        return vault_db.enabled()
    except Exception:  # noqa: BLE001 —— 适配层不可用就退回老行为，不阻断读
        return False


def _active_md_files() -> list[Path]:
    """列出全部笔记。

    库模式下返回的是**虚拟路径**（`MEMORY_DIR/rel_path`），磁盘上不一定有文件 ——
    这正是「库是原件」的定义。名字保留 `_md_files` 是因为全库 35 处调用点都用它，
    改名换来的只是一次性的大扫荡风险，不是收益。
    """
    if _db_mode():
        try:
            import vault_db

            return vault_db.files()
        except Exception as e:  # noqa: BLE001
            logger.warning("DB-MODE 列目录失败，回落扫盘：%s", e)
    files: list[Path] = []
    for f in MEMORY_DIR.rglob("*.md"):
        try:
            if any(part in SYSTEM_DIR_NAMES for part in f.relative_to(MEMORY_DIR).parts):
                continue
        except ValueError:
            continue
        # 跳过 0 字节空文件（2026-09-15 加固）。Obsidian 点击一个解析不到的
        # [[wikilink]] 会在库根新建同名空笔记；若不过滤，空壳会被当成一条
        # 「无内容记忆」进入 记忆索引/路由索引，看起来像同一条记忆出现两行
        # （实测：含空格的 title 会被 _safe_filename 转成下划线文件名，
        #  [[title]] 于是解析不到 → 死链 → 空壳 → 索引重复）。
        try:
            if f.stat().st_size == 0:
                continue
        except OSError:
            continue
        files.append(f)
    return sorted(files, reverse=True)

def _folder_for_title(title: str) -> str | None:
    """Infer a target subfolder for new notes based on their title prefix."""
    if title.startswith("项目_"):
        return "项目"
    if title.startswith("规则_"):
        return "规则"
    # 决策笔记（2026-10-09 主人要求）：单独领域目录，与 日志/项目 平级。
    # 只加目录路由、**不动 _TYPE_PREFIX_MAP**（type 仍推导为 note）——
    # type 是 13 字段冻结集成员，新增枚举值会波及 DB/Web 前端，故目录承载语义即可。
    if title.startswith("决策_"):
        return "决策"
    if title.startswith("知识_"):
        return "知识"
    if title.startswith("日志_") or re.match(r"^\d{4}-\d{2}-\d{2}", title):
        return "日志"
    if title in {"AI人格", "用户画像"}:
        return "人设与偏好"
    return None

ROUTING_INDEX_TITLE = "路由索引"
# 路由索引瘦身（2026-09-10）：该文件曾是全库最贵的一条（10,705 字符、占全库 8.1%、
# 占库根 22.8%），却几乎没被读过（access_count=1）。去掉 tags 列、summary 截断，
# 定位所需信息（标题+目录+日期+摘要）仍完整。
ROUTING_SUMMARY_MAX = 60

# 热度时间衰减系数（2026-10-23 补回）。`_heat_score()` 用它算
# reads / (1 + λ * 距今天数)：λ 越大，旧笔记掉得越快。
# 为什么现在才补：基线提交 fc8bea1 里有 `HEAT_DECAY_LAMBDA = 0.2`，
# 2026-09-10 那次自动备份（663d8f7）把它删掉了，而三处调用它的工具函数
# （memory_heat / 复习队列 / 相关推荐）**没删** —— 于是只要走那条分支就
# NameError。py_compile 与日常读写都碰不到它，靠静态检查（pyflakes）才挖出来。
# 值就用历史原值 0.2，不重新拍脑袋定一个。
HEAT_DECAY_LAMBDA = 0.2

# ── frontmatter 字段集冻结（2026-09-10，schema_version 1）────────────────────
# 这是「知识库数据库化-本地Web」的前置阻塞项：DB/Web 一旦按当前字段建表，
# 之后每加一个字段都要改前端。故先冻结字段集与输出顺序，再上库。
# 字段顺序即输出顺序；新增字段必须同时升 SCHEMA_VERSION 并同步规则文档。
SCHEMA_VERSION = 1
FROZEN_FIELD_ORDER = [
    "title", "tags", "created", "updated", "tier", "access_count",
    "source", "summary", "version", "type", "scope", "verified", "schema_version",
]

# type 由标题前缀自动推导，**不新增 memory_write 参数**（AI 零写入成本）。
# 理由：类型已被标题前缀与目录编码，再让 AI 手填会形成双重事实源，必然漂移。
_TYPE_PREFIX_MAP = [
    ("规则_", "rule"),
    ("项目_", "project"),
    ("知识_", "knowledge"),
    ("工具_", "tool"),
    ("修复_", "fix"),
    ("运维_", "ops"),
    ("模板_", "template"),
    ("日志_", "log"),
    ("工作动态_", "log"),
]
_TYPE_FIXED_TITLES = {
    "共享记忆库规则": "rule",
    "记忆库总规范": "rule",
    "用户画像": "preference",
    "AI人格": "preference",
    # 库根系统页：标题不含领域前缀，需显式指定（否则会落成 note）
    "近期工作动态": "log",
    "项目状态": "index",
}
_GLOBAL_SCOPE_TITLES = {"AI人格", "用户画像", "共享记忆库规则", "记忆库总规范"}


def _derive_type(title: str) -> str:
    """按标题前缀推导 type；索引与时间线单独识别，兜底 note。"""
    t = (title or "").strip()
    if not t:
        return "note"
    if t in _TYPE_FIXED_TITLES:
        return _TYPE_FIXED_TITLES[t]
    for prefix, value in _TYPE_PREFIX_MAP:
        if t.startswith(prefix):
            return value
    if re.match(r"^\d{4}-\d{2}-\d{2}", t):
        return "log"
    if t.endswith("索引") or t == ROUTING_INDEX_TITLE:
        return "index"
    return "note"


def _derive_scope(title: str, path: Path | None = None) -> str:
    """scope=global 的条目在任何项目都适用（规则、人设与偏好）；其余为 project。

    只加字段、不动目录：2026-09-04 刚完成两级结构升级、09-10 刚修完落盘路由，
    再动目录会连带影响路由索引、双链解析与落盘规则。
    """
    if path is not None:
        try:
            rel = path.relative_to(MEMORY_DIR)
            if rel.parts and rel.parts[0] in {"规则", "人设与偏好"}:
                return "global"
        except ValueError:
            pass
    t = (title or "").strip()
    if t.startswith("规则_") or t in _GLOBAL_SCOPE_TITLES:
        return "global"
    return "project"


def _normalize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """冻结字段集的唯一出口：补齐缺省字段并按固定顺序输出。

    所有写路径（写入 / 访问计数落盘 / 索引刷新 / 归档 / 元数据更新）都经此处，
    保证全库 frontmatter 结构一致——这是 DB/Web 能直接建表的前提。
    """
    out = dict(meta)
    title = str(out.get("title", "") or "")
    if not out.get("type"):
        out["type"] = _derive_type(title)
    if not out.get("scope"):
        out["scope"] = _derive_scope(title)
    if "verified" not in out:
        out["verified"] = False
    out["schema_version"] = SCHEMA_VERSION
    ordered: dict[str, Any] = {}
    for key in FROZEN_FIELD_ORDER:
        if key in out:
            ordered[key] = out[key]
    for key, value in out.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _refresh_routing_index() -> None:
    """Rebuild 路由索引.md with one compact line per note (title|folder|date|summary).

    This is the "tree index / routing layer" for token-efficient retrieval:
    agents read this small file first, then only fetch the matching note.
    Auto-maintained; edit notes directly, never this file by hand.
    """
    try:
        entries = _iter_entries()
        entries.sort(key=lambda item: str(item[0].parent.name), reverse=False)
        lines = [
            "# 路由索引（自动维护）",
            "",
            "> 两级检索：先在此文件定位（每条 = 标题 | 目录 | 日期 | 一句话摘要），",
            "> 再按需 [[记忆索引]]/memory_read 读取叶子笔记。请勿手工编辑本文件。",
            "",
        ]
        for path, meta, _body in entries:
            # 链接目标取**文件名 stem**，不用 frontmatter title（2026-09-15 修）。
            # 原因：Obsidian 的 [[wikilink]] 按文件名解析；title 含空格时
            # _safe_filename 会把空格转成下划线，于是 [[含空格的 title]] 解析不到
            # 任何文件 → 死链 → 在 Obsidian 里点它会在库根新建一个同名空笔记，
            # 该空壳又会被索引，形成"同一条记忆出现两行"的假象。
            link = path.stem
            if link == ROUTING_INDEX_TITLE:
                continue
            folder = "库根" if path.parent == MEMORY_DIR else path.parent.name
            created = str(meta.get("created", ""))[:10]
            summary = str(meta.get("summary", "")).strip().replace("\n", " ")
            # 双链还原为纯文本：路由索引不承载链接解析，且截断可能切断 [[…]]，
            # 与下一行的链接拼成假死链（2026-09-10 瘦身时被体检报表实测抓到）。
            summary = WIKI_LINK_RE.sub(lambda m: m.group(1), summary)
            if len(summary) > ROUTING_SUMMARY_MAX:
                summary = summary[:ROUTING_SUMMARY_MAX]
                if summary.rfind("[[") > summary.rfind("]]"):
                    summary = summary[:summary.rfind("[[")]
                summary += "…"
            lines.append(f"- [[{link}]] | {folder} | {created} | {summary}")
        lines.append("")
        lines.append("> 目录路由：规则_* → 规则/；项目_* → 项目/；知识_* → 知识/；AI人格/用户画像 → 人设与偏好/；")
        lines.append("> 时间线归档 → 日志/；其余新笔记默认库根，可在规则中指定目录。")
        body = "\n".join(lines).rstrip() + "\n"
        idx_path = MEMORY_DIR / f"{ROUTING_INDEX_TITLE}.md"
        old_body = None
        if idx_path.exists():
            meta, old_body = _load_memory(idx_path)
        else:
            meta = {"title": ROUTING_INDEX_TITLE, "tags": ["index", "routing", "navigation"],
                    "tier": "warm", "source": "ai-memory-server"}
        # 2026-10-21：正文没实质变化就不写。
        # 原来这里无条件把 updated 改成 now → 写库 → version+1 → 导出 md 时文本变了
        # → 文件被重写。结果是 121 篇笔记一个字没动，这两个索引页每天照样在 git 里
        # 变脏两次，纯噪音（实测 sync_all 空转也会「重写 2 篇」）。
        # 比的必须是 body，不能是 updated —— updated 永远不同，比它等于没比。
        # summary 为空时仍然往下走：那是 2026-09-10 补字段覆盖率的逻辑，得让它生效一次。
        if (old_body is not None
                and str(old_body).rstrip("\n") == body.rstrip("\n")
                and meta.get("summary")):
            return
        meta["updated"] = datetime.now(timezone.utc).isoformat()
        # 路由索引是全库唯一长期缺 summary 的条目（2026-09-10 体检两次报出）。
        # 每次刷新固定写入：补齐字段覆盖率，也让 memory_search 能召回它。
        meta["summary"] = (
            "两级检索路由表：每条 = 标题 | 目录 | 日期 | 一句话摘要，"
            "先在此定位再按需读取叶子笔记；自动维护，勿手工编辑。"
        )
        # 2026-10-20：两个投影（记忆索引 / 路由索引）从「重写 md 文件」改为
        # 「重算 body 写回库」。库模式下文件已经不参与读取，再写文件就是造垃圾；
        # 而库模式下这两个投影就是 notes 表里的两行，天然可被检索与路由使用。
        _save_meta_body(idx_path, meta, body)
    except Exception as e:
        logger.warning("_refresh_routing_index failed: %s", e)

def _safe_filename(title: str) -> str:
    """把标题转成安全的文件名。

    关键保证：标题里无论出现多少 ``/`` 或 ``\\``，都会被整体替换为 ``_``，
    绝不会裂出子目录（例如 ``a/b/c`` -> ``a_b_c.md`` 而非 ``a/b/c.md``）。
    末尾再兜底只取 basename，防止将来正则被改坏后仍意外创建嵌套目录。
    """
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("._")
    safe = safe[:80] or "memory"
    # 双保险：即便上方正则失效，也只取最后一段，杜绝斜杠裂目录
    safe = safe.replace("\\", "/").split("/")[-1]
    # 提醒：若修改本函数的消毒/双链/空壳生成行为，须同步更新
    # `记忆写入格式规范.md` 的「标题与文件名规范」章节，保持代码与规范一致。
    return f"{safe}.md"

def _resolve_unique_path(directory: Path, title: str) -> Path:
    """Pick a filename for a new entry, avoiding collisions with files that
    belong to a different title (P0-4).

    ``_safe_filename`` truncates at 80 chars, so two distinct titles can map to
    the same filename and silently overwrite each other. If the target already
    exists on disk AND its frontmatter title differs from ours, append _2/_3/...
    until free. If it exists and the title matches (e.g. a stale stub), reuse it.
    """
    base = _safe_filename(title)
    candidate = directory / base
    if not candidate.exists():
        return candidate
    try:
        meta, _ = _load_memory(candidate)
        if _entry_title(meta, candidate) == title or candidate.stem == title:
            return candidate
    except Exception:
        pass
    n = 2
    while True:
        alt = directory / f"{candidate.stem}_{n}{candidate.suffix}"
        if not alt.exists():
            return alt
        n += 1

def _extract_wiki_links(body: str) -> list[str]:
    """Extract [[wiki link]] page names from body text, deduplicated."""
    # Skip fenced code blocks and inline code so syntax examples do not
    # create phantom links (e.g. [[placeholder]] written inside backticks).
    body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    body = re.sub(r"~~~.*?~~~", "", body, flags=re.DOTALL)
    body = re.sub(r"`[^`]*`", "", body)
    links = WIKI_LINK_RE.findall(body)
    seen: set[str] = set()
    result: list[str] = []
    for link in links:
        normalized = link.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result

def _parse_dt_utc(value: Any) -> datetime | None:
    """Parse an ISO datetime string, treating naive (tzinfo-less) values as UTC.

    Legacy files may store ``updated``/``created`` without a timezone; comparing
    those against ``datetime.now(timezone.utc)`` raises TypeError and is silently
    swallowed by callers, making the entry appear stale or invisible. This helper
    normalizes naive timestamps to UTC so all downstream comparisons work.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _days_since_utc(value: Any) -> int:
    """Whole days between an ISO timestamp and now (UTC); 999 if unparseable."""
    dt = _parse_dt_utc(value)
    if dt is None:
        return 999
    return (datetime.now(timezone.utc) - dt).days

def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically: temp file in the same dir, then os.replace.

    Plain ``write_text`` truncates the target first; a crash or a concurrent
    reader can observe an empty or half-written file. Writing to a sibling
    temp file and atomically replacing avoids partial reads (fixes P0-1).

    2026-09-10 并发加固（由 smoke_test 的并发写用例暴露）：
    - tmp 名带 pid + 线程 id，避免多线程共用同一个 ``.tmp`` 互相踩踏
    - ``os.replace`` 在 Windows 上可能因目标被短暂占用而抛 WinError 5/32，
      重试若干次后仍失败才向上抛
    """
    _ensure_vault()  # 惰性建库（E5a）：写盘前才把库目录建出来
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    # newline="\n" 必须显式给：Windows 上默认会把 \n 静默翻成 \r\n —— 这是库文件
    # 行尾不一致的隐患（2026-10-13 squeeze_table_padding 已实锤同款 bug 改坏过
    # 项目状态.md；此处是记忆库核心写盘出口，2026-10-13 主人授权收口）。
    tmp.write_text(content, encoding="utf-8", newline="\n")
    last: OSError | None = None
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.02)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last is not None:
        raise last

def _heat_score(reads: int, updated_str: str) -> float:
    """Heat score with time decay. Higher = more actively used."""
    if not updated_str:
        return float(reads)
    days_since = _days_since_utc(updated_str)
    return reads / (1 + HEAT_DECAY_LAMBDA * max(days_since, 0))

def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")

def _split_inline_list(value: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None

    for ch in value:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue

        if ch in {'"', "'"}:
            quote = ch
            current.append(ch)
            continue

        if ch == ",":
            items.append("".join(current).strip())
            current = []
            continue

        current.append(ch)

    if current:
        items.append("".join(current).strip())

    return items

def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""

    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}

    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_inline_list(inner)]

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            # 与 _yaml_quote_scalar 的转义顺序严格互逆（2026-09-10 反斜杠风暴修复）。
            # 写入端顺序：\ -> \\ 再 " -> \"；故读取端须先 \" -> " 再 \\ -> \
            # 此前只剥外层引号、不解转义，导致含 " 的 summary 每轮读写反斜杠翻倍，
            # 单条笔记曾被撑到 131,274 字符（占全库 68.6%）。
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner

    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass

    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            pass

    return value

def _parse_yaml_frontmatter(raw: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if ":" not in line:
            i += 1
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        i += 1

        if not key:
            continue

        if value:
            meta[key] = _parse_scalar(value)
            continue

        if i < len(lines) and lines[i].lstrip().startswith("- "):
            items: list[Any] = []
            while i < len(lines):
                item_line = lines[i]
                item_stripped = item_line.strip()
                if not item_stripped:
                    i += 1
                    continue
                if not item_line.lstrip().startswith("- "):
                    break
                items.append(_parse_scalar(item_line.lstrip()[2:].strip()))
                i += 1
            meta[key] = items
            continue

        block: list[str] = []
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                block.append("")
                i += 1
                continue
            if next_line.startswith(" ") or next_line.startswith("\t"):
                block.append(next_line.strip())
                i += 1
                continue
            break
        meta[key] = "\n".join(block).strip()

    return meta

def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    content = _strip_bom(content)
    if not content.startswith("---"):
        return {}, content.strip()

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()

    raw_meta = parts[1].strip()
    body = parts[2].lstrip("\r\n")

    if not raw_meta:
        return {}, body.strip()

    try:
        meta = json.loads(raw_meta)
        if isinstance(meta, dict):
            return meta, body.strip()
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return _parse_yaml_frontmatter(raw_meta), body.strip()

_KNOWN_TITLES: list[str] = []
_ACCESS_CACHE: dict[str, int] = {}  # title -> pending access_count increments
_ACCESS_FLUSH_THRESHOLD = 10
# 2026-11-18：只读会话也要能把访问计数落库。此前只有「凑够 10 个不同标题」或
# 「发生一次写操作」才会刷盘 —— 于是只查不写的会话里 access_count 全丢，
# 「从未读取」统计偏低、tier 建议不可信。现在多一道时间闸：
# 最早那条待刷记录超过 _ACCESS_FLUSH_SECONDS 秒，就在下一次读时顺手刷掉。
_ACCESS_FLUSH_SECONDS = 300.0
_ACCESS_FIRST_TS = 0.0
_ENTRY_CACHE: dict[str, tuple] = {}  # title -> (path, meta, body)
_CACHE_VALID = False
_DIR_MTIME = 0.0  # last seen MEMORY_DIR mtime, for cross-process cache invalidation
_CACHE_LOCK = threading.RLock()
_ACCESS_LOCK = threading.RLock()
_TAG_AUTO_MAP: dict[str, str] = {
    "python": "python", "py": "python", "flask": "python", "django": "python", "fastapi": "python",
    "javascript": "javascript", "js": "javascript", "typescript": "typescript", "ts": "typescript",
    "node": "nodejs", "node.js": "nodejs",
    "docker": "docker", "container": "docker", "compose": "docker",
    "android": "android", "hyperos": "android", "root": "android",
    "network": "network", "路由器": "network", "路由": "network", "dhcp": "network", "wifi": "network",
    "database": "database", "mysql": "database", "sqlite": "database", "redis": "database",
    "frontend": "frontend", "vue": "frontend", "react": "frontend", "html": "frontend", "css": "frontend",
    "backend": "backend", "api": "backend", "laravel": "backend",
    "tool": "tool", "cli": "tool", "gui": "tool",
    "project": "project",
    "fix": "fix", "bug": "fix", "修复": "fix", "排障": "fix",
    "tutorial": "tutorial", "教程": "tutorial", "guide": "tutorial",
    "linux": "linux", "ubuntu": "linux", "debian": "linux", "bash": "linux",
    "windows": "windows", "win": "windows",
    "security": "security", "渗透": "security", "pentest": "security", "hack": "security",
    "mcp": "mcp", "ai": "ai", "llm": "ai", "gpt": "ai",
    "git": "git", "github": "git",
    "obsidian": "obsidian", "markdown": "markdown",
    "deploy": "deploy", "部署": "deploy", "docker": "deploy",
    "美化": "美化", "theme": "美化", "customization": "美化",
}

CORE_PAGES = {"记忆索引", "近期工作动态", "用户画像", "AI身份档案", "Codex 身份档案",
                  "Claude Code 身份档案", "记忆库总规范", "共享记忆库规则",
                  "记忆半自动整理流程", "AI交互配置", "AI 对话自动归档提示词",
                  "本地共享记忆库 MCP Server", "工具_记忆库MCP服务器"}

_lock_state = threading.local()  # re-entrancy depth per thread

def _acquire_lock() -> bool:
    """Cross-process file lock with staleness check.

    Re-entrant within the same thread: nested acquisitions (e.g. memory_write
    -> _maybe_auto_archive -> memory_archive) only bump a depth counter instead
    of re-creating the lock file, which would deadlock the current design.
    """
    if getattr(_lock_state, "depth", 0) > 0:
        _lock_state.depth += 1
        return True
    _ensure_vault()  # 惰性建库（E5a）：锁文件落在库根，目录得先存在
    start = time.time()
    while True:
        try:
            with open(MEMORY_LOCK, "x", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "time": time.time()}, f)
            _lock_state.depth = 1
            return True
        except FileExistsError:
            try:
                with open(MEMORY_LOCK, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stale = time.time() - data.get("time", 0) > LOCK_TIMEOUT
            except (json.JSONDecodeError, OSError, ValueError):
                # 读到空/半写的锁文件（另一进程正在创建），按过期处理
                stale = True
            if stale:
                try:
                    MEMORY_LOCK.unlink(missing_ok=True)
                    continue
                except OSError:
                    # Windows 上另一线程/进程持有句柄时 unlink 会失败（WinError 32）。
                    # 此前这个异常会直接冒泡成工具层报错，中断写入；改为等一拍重试，
                    # 整体仍受 LOCK_TIMEOUT 约束。（2026-09-10 由 smoke_test 锁残留用例暴露）
                    if time.time() - start > LOCK_TIMEOUT:
                        return False
                    time.sleep(0.05)
                    continue
            time.sleep(0.2)
            if time.time() - start > LOCK_TIMEOUT:
                return False
    return False

def _release_lock() -> None:
    depth = getattr(_lock_state, "depth", 0)
    if depth > 1:
        _lock_state.depth = depth - 1
        return
    _lock_state.depth = 0
    try:
        MEMORY_LOCK.unlink(missing_ok=True)
    except OSError:
        pass

def _rebuild_title_index() -> None:
    global _KNOWN_TITLES
    with _CACHE_LOCK:
        _KNOWN_TITLES = []
        for f in _active_md_files():
            try:
                meta, _ = _load_memory(f)
                _KNOWN_TITLES.append(_entry_title(meta, f))
            except Exception:
                logger.debug("_rebuild_title_index: skipped %s", f.name)


def note_access(title: str) -> bool:
    """记一次访问计数；返回「现在该刷盘了吗」。

    两道判据任一成立即刷：
    · 待刷标题数达到 `_ACCESS_FLUSH_THRESHOLD`（原口径，防一次会话读太多篇时内存涨）
    · 最早一条待刷记录已等超过 `_ACCESS_FLUSH_SECONDS`（2026-11-18 新增，
      治「只读会话的访问计数永远不落盘」—— 此前只有写操作或凑够 10 个标题才刷）

    为什么放进 store 而不是让 memory_tools 直接摸 `_ACCESS_CACHE`：
    时间戳必须与缓存一起维护，两处各写一遍必然漂移。
    """
    global _ACCESS_FIRST_TS
    with _ACCESS_LOCK:
        if not _ACCESS_CACHE:
            _ACCESS_FIRST_TS = time.time()
        _ACCESS_CACHE[title] = _ACCESS_CACHE.get(title, 0) + 1
        due_by_count = len(_ACCESS_CACHE) >= _ACCESS_FLUSH_THRESHOLD
        due_by_time = (time.time() - _ACCESS_FIRST_TS) >= _ACCESS_FLUSH_SECONDS
        return due_by_count or due_by_time


def _flush_access_counts() -> int:
    """Flush pending access_count increments to disk. Returns number of entries flushed."""
    global _ACCESS_FIRST_TS
    with _ACCESS_LOCK:
        if not _ACCESS_CACHE:
            return 0
        flushed = 0
        for f in _active_md_files():
            try:
                meta, body = _load_memory(f)
                title = _entry_title(meta, f)
                if title in _ACCESS_CACHE:
                    old_count = meta.get("access_count", 0)
                    if not isinstance(old_count, int):
                        old_count = 0
                    meta["access_count"] = old_count + _ACCESS_CACHE.pop(title)
                    # 2026-11-18 修：**这里绝不动 `updated`**。
                    # 原实现写 `meta["updated"] = now()`，于是「读一篇笔记」会把它的
                    # updated 刷成现在 —— 而 memory_search 正是按 updated 倒序排的。
                    # 后果是自我强化回路：读过 → 时间被刷新 → 排得更前 → 更容易被读，
                    # 真实的内容变更时间被读行为污染（实测两篇 11-16 的笔记被刷成 11-18，
                    # 内容一个字没动）。计数是统计量，不该改写「内容何时变更」这个语义。
                    # 2026-10-20：走统一出口。原来是直接 _atomic_write_text，
                    # 切到库模式后那样写只会改文件、库不动 —— 「访问计数越用越旧」。
                    _save_meta_body(f, meta, body)
                    flushed += 1
            except Exception as e:
                logger.debug("_flush_access_counts: skipped %s: %s", f.name, e)
        _ACCESS_CACHE.clear()
        _ACCESS_FIRST_TS = 0.0
        return flushed

def _auto_suggest_tags(content: str, existing_tags: list[str]) -> list[str]:
    """标签不再自动生成（2026-11-18 决定「标签退场」）。

    为什么改：实测 4,217 次检索里 **0 次**用过 `tag` 过滤；而库里攒出 296 个标签、
    其中 166 个只出现一次（56%）。也就是说自动打标带来的是「每次写入多写 7.7 个
    孤儿标签」，对找东西没有贡献。分类职责交给目录 / type / 双链（写在正文里、
    不易漂移的结构信号）。

    保留调用点与签名：显式传入的 tags 仍然照存（谁想手工标就标），只是**不再自动推断**。
    """
    _ = content
    return list(existing_tags or [])

def _locked_write(fn):
    """Decorator to wrap write operations with lock.

    Uses functools.wraps so FastMCP registers the tool under the original
    function name (not "wrapper"); otherwise @mcp.tool() would register every
    locked tool as a single "wrapper" tool that overwrites the previous one.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        locked = _acquire_lock()
        try:
            return fn(*args, **kwargs)
        finally:
            if locked:
                _release_lock()
    return wrapper

def _entry_title(meta: dict[str, Any], path: Path) -> str:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return path.stem

def _entry_tags(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags", [])
    if isinstance(tags, list):
        return [str(tag) for tag in tags if str(tag).strip()]
    if isinstance(tags, str) and tags.strip():
        return [tags.strip()]
    return []

def _entry_bucket(meta: dict[str, Any], path: Path) -> tuple[str, str]:
    title = _entry_title(meta, path)
    tags = {tag.lower() for tag in _entry_tags(meta)}
    title_lower = title.lower()

    if (
        title == "记忆索引"
        or "index" in tags
        or "moc" in tags
        or "navigation" in tags
        or title.endswith("索引")
    ):
        return "索引", "导航入口或总索引"

    if (
        title == "近期工作动态"
        or "timeline" in tags
        or "activity" in tags
        or "log" in tags
        or title.startswith("2026-")
    ):
        return "时间线", "按日期记录阶段性工作"

    if title.startswith("项目_") or "project" in tags:
        return "项目", "长期项目记忆"

    if title.startswith("工具_") or "tool" in tags or "mcp" in tags or "infra" in tags:
        return "工具", "工具、服务或基础设施"

    if title.startswith("运维_") or "network" in tags or "router" in tags or "docker" in tags:
        return "运维", "运维与环境配置"

    if title.startswith("修复_") or "fix" in tags or "issue" in tags:
        return "修复", "故障修复与排障"

    if (
        title.startswith("规则_")
        or title in {"共享记忆库规则", "记忆库总规范", "AI 对话自动归档提示词"}
        or "workflow" in tags
        or "standard" in tags
        or "structure" in tags
        or "memory" in tags
        or "prompt" in tags
    ):
        return "规则", "流程、规范与模板"

    if (
        title in {"用户画像", "AI身份档案", "Codex 身份档案", "Claude Code 身份档案", "AI交互配置"}
        or "identity" in tags
        or "persona" in tags
        or "preference" in tags
    ):
        return "身份与配置", "用户、AI 身份与交互设定"

    if title.startswith("模板_") or "template" in tags:
        return "模板", "可复用模板"

    if title_lower.startswith("ai") or "ai" in tags:
        return "AI相关", "与 AI 身份、配置或协作相关"

    return "其他", "暂未明确归类"

def _load_memory(path: Path) -> tuple[dict[str, Any], str]:
    """读一篇笔记的 (meta, body)。

    库模式下从 `notes` 表取，**磁盘上有没有这个文件无关**。取不到（库里确实没
    这一行）才回落解析文件 —— 这条回落只服务于「刚迁移完、库里还缺行」的过渡期，
    以及没有库的临时库（smoke_test / bench 用），不是正常路径。
    """
    if _db_mode():
        try:
            import vault_db

            hit = vault_db.load(path)
            if hit is not None:
                return hit
        except Exception as e:  # noqa: BLE001
            logger.warning("DB-MODE 读行失败 path=%s err=%s（回落读盘）", path, e)
    return _parse_frontmatter(_read_text(path))

def _invalidate_cache() -> None:
    global _CACHE_VALID
    with _CACHE_LOCK:
        _CACHE_VALID = False


def _tree_stamp() -> float:
    """全树 ``.md`` 文件 mtime 之和，作为跨进程缓存失效判据。

    P0 修复（2026-09-10）：此前只比对库根 ``st_mtime``，于是
    **Obsidian 直接编辑子目录笔记 / 手动改 md / ``git pull`` 只含子目录变更**
    三种情况都不触发失效，AI 会一直读到陈旧内容——违背
    「Markdown 是唯一事实源、可人工编辑」的设计原则。
    实测证据：往 ``知识/`` 新增文件后 ``memory_stats`` 报 49 而直读扫描为 50。

    P0 二次修复（2026-09-20，随「聚合页周拆分」引入二级目录，两处一起改）：

    1) **判据从「目录 mtime」改为「文件 mtime」**。原因：本机实测**目录 mtime
       读取会滞后** —— 往 ``日志/2026-09/`` 写文件后立刻读该目录 mtime，值与
       写入前**完全相同**（1789897665.181 → 1789897665.181），隔几秒再读才变。
       沿用目录 mtime 的话，新文件会在缓存未失效时**静默不可见**：实测写新文件
       后 ``_iter_entries()`` 报 80 而实际 81。文件 mtime 无此问题 ——
       新建 / 原地修改 / 删除三种变更均能立即反映（均已实测）。
    2) **遍历改递归**。上一版只累加一级子目录 mtime，二级目录改动根本不在判据内。

    为什么用 ``os.scandir`` 手写递归而不是 ``Path.rglob``：后者对每个条目构造
    Path、走 ``relative_to`` / ``parts`` 判断，实测 **18.9 ms**；本实现在
    DirEntry 上直接 stat（自带 d_type 缓存），实测 **1.03 ms**，与改前「只看
    一级」的 0.96 ms 基本持平 —— 递归与换成文件 mtime 都不引入可感开销。

    求和而非取最大值：时间戳精度只到秒，一次会话内多处改动可能落在同一秒内，
    求和能区分出「变了但最大值没变」的情况。系统目录（.trash/.git/…）不参与，
    避免运行期临时文件反复打穿缓存。
    """
    stamp = 0.0
    if _db_mode():
        # 库模式（2026-10-20）：判据换成库文件的 (mtime_ns, size)。
        # 磁盘上的 md 已不参与读取，继续扫盘会让缓存对着一个不相干的变化失效
        # （或者更糟：对着一个不相干的状态保持有效）。
        try:
            import vault_db

            return vault_db.stamp()  # type: ignore[return-value]
        except Exception as e:  # noqa: BLE001
            logger.warning("DB-MODE 取库戳失败，回落扫盘：%s", e)
    stack = [MEMORY_DIR]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.name in SYSTEM_DIR_NAMES:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                    except OSError:
                        continue
                    if not entry.name.endswith(".md"):
                        continue
                    try:
                        stamp += entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
        except OSError:
            continue
    return stamp


def _iter_entries() -> list[tuple[Path, dict[str, Any], str]]:
    with _CACHE_LOCK:
        global _KNOWN_TITLES
        if not _KNOWN_TITLES:
            _rebuild_title_index()
        global _CACHE_VALID, _ENTRY_CACHE, _DIR_MTIME
        # Cross-process invalidation: if another process (Obsidian edit, other MCP
        # client, git operation) changed ANY first-level directory since we cached,
        # rebuild. 只比库根会漏掉子目录改动（见 _tree_stamp 注释）。
        stamp = _tree_stamp()
        if _CACHE_VALID and _ENTRY_CACHE and stamp == _DIR_MTIME:
            return list(_ENTRY_CACHE.values())
        _DIR_MTIME = stamp
        _ENTRY_CACHE.clear()
        entries: list[tuple[Path, dict[str, Any], str]] = []
        for f in _active_md_files():
            try:
                meta, body = _load_memory(f)
            except Exception as e:
                logger.debug("_iter_entries: skipped %s: %s", f.name, e)
                continue
            t = _entry_title(meta, f)
            entries.append((f, meta, body))
            _ENTRY_CACHE[t] = (f, meta, body)
        _CACHE_VALID = True
        return entries

def _yaml_quote_scalar(value: Any) -> str:
    """Quote a scalar for YAML safety (handles colons, #, brackets, quotes, leading/trailing space)."""
    s = str(value)
    needs_quote = (
        s == ""
        or s.strip() != s
        or re.search(r'[:#\[\]\{\},&*?|<>=!%@`"\']', s) is not None
    )
    if needs_quote:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    return s


def _dump_yaml_frontmatter(meta: dict[str, Any]) -> str:
    """Serialize frontmatter as YAML (Obsidian-native) instead of JSON.

    Improves on the old JSON frontmatter so Obsidian property/tag/dataview
    panels recognize the metadata (improvement #1).
    """
    # links 冗余已取消（2026-09-06）：正文显式 [[双链]] 是唯一事实源。
    # 在序列化出口统一丢弃，覆盖 flush/update_meta/archive 等所有旁路写路径。
    meta.pop("links", None)
    # 字段集冻结（2026-09-10）：唯一规范化出口，补齐 type/scope/verified/schema_version
    # 并按固定顺序输出，保证全库 frontmatter 同构（DB/Web 建表前提）。
    meta = _normalize_meta(meta)
    lines: list[str] = []
    for key, value in meta.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                items = [_yaml_quote_scalar(v) for v in value]
                lines.append(f"{key}: [{', '.join(items)}]")
        else:
            lines.append(f"{key}: {_yaml_quote_scalar(value)}")
    return "\n".join(lines)


SUMMARY_MAX_CHARS = 400


def _clamp_summary(summary: str) -> str:
    """防御性护栏（2026-09-10）：summary 异常膨胀会指数级污染 frontmatter。

    历史事故：一条含双引号的 summary 被转义 bug 反复翻倍撑到 131,274 字符，
    占全库字符的 68.6%。这里对超长 summary 截断并告警，作为最后一道保险。
    """
    s = str(summary).strip()
    if len(s) > SUMMARY_MAX_CHARS:
        logger.warning("summary 超长已截断：%d -> %d 字符（疑异常膨胀）", len(s), SUMMARY_MAX_CHARS)
        # 必须 -1：截断后要追加一个 "…"，不减就会产出 SUMMARY_MAX_CHARS+1 = 401 字符，
        # 与 check_frontmatter.py 的「>400 即报错」判据直接打架（护栏自己违规）。
        # 同库另两处截断函数（memory_tools._clip / _cli_visible_runner）本来就是 `[:n-1]`。
        return s[: SUMMARY_MAX_CHARS - 1] + "…"
    return s


def _compose_meta(title: str, tags: list[str], source: str | None, created: str | None = None,
                  summary: str | None = None, tier: str | None = None, access_count: int = 0,
                  version: int | None = None, verified: bool | None = None) -> dict[str, Any]:
    """构造 meta 字典（**纯函数、不落盘、不读盘**）。

    2026-10-19 从 `_build_frontmatter` 里拆出来的唯一理由：写路径切成「db 为主」后，
    同一份 meta 要同时喂给两个出口 —— ① `index.sqlite3` 的 notes 行；② 导出的 md
    文件。若两边各自构造，`updated` 会差几微秒、tags 可能一个有序一个无序、
    summary 可能一边截断一边没截 —— 这类偏差不会报错，只会在下次「按 md 重建索引」
    时变成一条条无法解释的差异（本库已因「两套口径」误算过两次召回）。
    所以：meta 只算一次，两个出口都吃这一份。
    """
    now = datetime.now(timezone.utc).isoformat()
    meta: dict[str, Any] = {
        "title": title,
        "tags": tags,
        "created": created or now,
        "updated": now,
        "tier": tier or "warm",
        "access_count": access_count,
        "verified": bool(verified) if verified is not None else False,
    }
    if source:
        meta["source"] = source
    if summary:
        meta["summary"] = _clamp_summary(summary)
    if version is not None:
        meta["version"] = version
    return meta


def _prepare_meta(path: Path, title: str, tags: list[str], source: str | None,
                  created: str | None = None, summary: str | None = None,
                  tier: str | None = None, access_count: int = 0,
                  version: int | None = None, verified: bool | None = None) -> dict[str, Any]:
    """合并访问计数 + 保留旧字段值，产出**最终**那一份 meta（不落盘）。

    两件副作用必须只发生一次，所以单独成函数：
    · `_ACCESS_CACHE.pop` 是**破坏性**的 —— 谁先取谁拿到增量，第二次取就是 0；
    · version / verified 的「未显式传就沿用旧值」要读一次磁盘。
    db 为主时先写库、再导出 md，两处都吃这一份，不会出现「库里有 5 次访问、md 里 3 次」。
    """
    with _ACCESS_LOCK:
        cached = _ACCESS_CACHE.pop(title, 0)
    if cached:
        access_count += cached
    if version is None or verified is None:
        try:
            em, _ = _load_memory(path)
            if version is None:
                version = em.get("version")
            if verified is None and isinstance(em.get("verified"), bool):
                verified = em["verified"]
        except Exception:
            pass
    return _compose_meta(title, tags, source, created=created, summary=summary, tier=tier,
                         access_count=access_count, version=version, verified=verified)


def _build_frontmatter(title: str, tags: list[str], source: str | None, created: str | None = None,
                       summary: str | None = None, tier: str | None = None, access_count: int = 0,
                       version: int | None = None, verified: bool | None = None) -> str:
    """构造 frontmatter 文本。

    type / scope / schema_version 由 ``_normalize_meta`` 按标题前缀自动补齐，
    调用方无需（也不应）手填；verified 语义：None = 未表态（缺省 false），
    True = 主人明确确认或本机实测通过。不用 confidence 三档——单人场景无法
    稳定判定 high/medium/low，反而会造出第二套可信度口径。
    """
    return _dump_yaml_frontmatter(_compose_meta(
        title, tags, source, created=created, summary=summary, tier=tier,
        access_count=access_count, version=version, verified=verified))


def _write_memory_meta(path: Path, meta: dict[str, Any], content: str) -> None:
    """按**给定的 meta** 把一篇笔记渲染成 md 写到磁盘（原子写）。

    2026-10-20 起本函数的唯一用途是**快照导出**（`脚本/snapshot_md.py`），
    不再参与写入路径。名字没改，但语义已经从「落盘」变成「渲染」—— 调用它就是
    在造一份**只读投影**，改动它永远不会回流到库。
    """
    _atomic_write_text(path, f"---\n{_dump_yaml_frontmatter(meta)}\n---\n\n{content}")


def _save_meta_body(path: Path, meta: dict[str, Any], body: str) -> None:
    """**统一落盘出口**：库模式写 `notes` 行，否则写 md 文件。

    为什么把「写库」和「写文件」收在一个函数里：库里 4 个不同的写入方
    （memory_write / 访问计数回写 / 记忆索引重建 / 路由索引重建）各自
    `_atomic_write_text` 的话，切到库模式必须逐个改，漏一个就出现
    「库里有、文件里也有但两份不一样」。收口之后它们只认这一个出口。
    """
    if _db_mode():
        import vault_db

        vault_db.save(path, meta, body)
        return
    _atomic_write_text(path, f"---\n{_dump_yaml_frontmatter(meta)}\n---\n\n{body}")


def _write_memory(path: Path, title: str, tags: list[str], source: str | None, content: str,
                  created: str | None = None, summary: str | None = None,
                  tier: str | None = None, access_count: int = 0,
                  version: int | None = None, verified: bool | None = None) -> dict[str, Any]:
    """算 meta 并落盘（md 为主路径 / 库模式共用），返回实际写入的 meta。"""
    meta = _prepare_meta(path, title, tags, source, created=created, summary=summary,
                         tier=tier, access_count=access_count, version=version,
                         verified=verified)
    _save_meta_body(path, meta, content)
    return meta

def _clean_link_name(link: str) -> str:
    """Normalize a wiki link name: strip whitespace and stray backslashes (improvement #2)."""
    return link.strip().replace("\\", "")


def _refresh_index() -> None:
    """Rebuild 记忆索引.md body from current entries (direct write).

    Called after every successful memory_write so the index stays in sync
    without manual maintenance (improvement #7).
    """
    try:
        entries = _iter_entries()
        order = ["索引", "规则", "身份与配置", "项目", "工具", "运维", "修复",
                 "时间线", "模板", "AI相关", "其他"]
        groups: dict[str, list[str]] = {b: [] for b in order}
        for path, meta, _body in entries:
            # 同 _refresh_routing_index：链接目标必须取**文件名 stem**，
            # 否则 title 含空格时 [[title]] 会变成 Obsidian 死链（2026-09-15 修）。
            title = path.stem
            bucket, _reason = _entry_bucket(meta, path)
            groups.setdefault(bucket, []).append(title)
        lines = ["# 记忆索引（自动维护）", "",
                 "> 本索引由 ai-memory MCP 在每次写入后自动更新。如确需手工调整，请改各笔记自身而非此处。", ""]
        groups.setdefault("索引", [])
        groups["索引"] = [t for t in groups["索引"] if t != ROUTING_INDEX_TITLE]
        groups["索引"].append(ROUTING_INDEX_TITLE)
        for bucket in order:
            items = groups.get(bucket, [])
            if not items:
                continue
            lines.append(f"## {bucket}")
            items = sorted(set(items), key=lambda x: (x != ROUTING_INDEX_TITLE, x.lower()))
            for title in items:
                lines.append(f"- [[{title}]]")
            lines.append("")
        body = "\n".join(lines).rstrip() + "\n"
        idx_path = MEMORY_DIR / "记忆索引.md"
        if idx_path.exists():
            meta, old_body = _load_memory(idx_path)
            # 2026-10-21：同 _refresh_routing_index —— 正文没变就不写，
            # 否则每次刷新都把 updated/version 往前挪，导出后 git 里天天变脏。
            if str(old_body).rstrip("\n") == body.rstrip("\n"):
                return
        else:
            meta = {"title": "记忆索引", "tags": ["index", "moc", "navigation"],
                    "tier": "warm", "source": "ai-memory-server"}
        meta["updated"] = datetime.now(timezone.utc).isoformat()
        _save_meta_body(idx_path, meta, body)
    except Exception as e:
        logger.warning("_refresh_index failed: %s", e)

__all__ = [name for name in globals() if not name.startswith('__')]
