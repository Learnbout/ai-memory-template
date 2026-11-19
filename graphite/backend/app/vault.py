"""记忆库索引 + 检索 / 标签 / 反链 / 上下文 / facets / graph（CONTRACT §9.1~§9.6、§9.10）。

**数据源口径（2026-10-20 反转，以主人这一次的裁定为准）**：
- **库是唯一原件** —— `记忆库/数据库/index.sqlite3` 的 `notes` 表是读写主体；
  md 文件只是**可导出的快照**（`脚本/snapshot_md.py` 生成），不再作为数据源。
- **默认读库**：`ensure_index()` 走 `vault_db`，不 `os.walk`。MCP 那边 `memory_write`
  一落库，这边下个请求就能看见，不需要等任何同步动作。
- **扫盘只是应急**：`GRAPHITE_VAULT_SOURCE=md` 显式指定才走老路径（库坏了/要对照时用），
  此时 `_index.source` 会被标成 `md`，`/api/vault/status` 的 `sourceLabel` 会如实显示
  「Markdown 快照」而不是「索引库」——**不能让页面在说另一种事实**。
- 根路径：默认值由 `paths.vault_root()` 解析（本机示例 `/path/to/ai-memory`，
  服务器 `/opt/ai-memory/vault`；可被设置 `vault.vaultPath` 覆盖）。
  库位置由 `vault_db.DB_REL_CANDIDATES` 在根下解析（含 `记忆库/数据库/` 这一层），
  所以根既可以是 `记忆库` 本身，也可以是它的上一级。
- id：`n_` + md5(relPath)[:7]，稳定可复现，库与 md 两条源算出来必须一致（实测 119/119）。
- 缓存：常驻内存；db 模式每次全量重读（跨进程写入没有可靠变更通知，百篇量级
  ~十几毫秒，换来「永远不会显示上一版」）；md 模式才用 `(relPath, mtime, size)` 增量。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .timeutil import relative_time_label
from . import vault_db
from .paths import vault_root

# 2026-10-23：写死路径改走 paths.py（GRAPHITE_VAULT_ROOT > AI_MEMORY_DIR > 内置默认）。
DEFAULT_VAULT_PATH = vault_root()

# 数据源开关（T2，2026-10-19）：db = 读 index.sqlite3（默认，行为不变）；md = 扫磁盘。
# 进程级环境变量，不走设置页 —— 切数据源会让内存索引整体失效，重启一次最干净。
SOURCE_MD = "md"
SOURCE_DB = "db"


class VaultUnavailable(RuntimeError):
    """记忆库（`index.sqlite3`）读不出来 —— **服务级故障，不降级**。

    2026-10-20 主人裁定：「如果库有问题，那代表后端崩了；读 md 也不是最新的，
    因为更新都写进数据库了」。
    所以这里刻意做成异常而不是「返回 False 让调用方回落扫盘」：
    md 文件是**可导出快照**，滞后多少没有任何保证，把它当数据源展示出去
    等于让页面在说另一种事实。宁可 503 让人一眼看见故障，也不要一个"看起来正常"的旧页面。
    `GRAPHITE_VAULT_SOURCE=md` 是**显式**的应急开关，那是另一条路，
    与「库坏了自动回落」是两件事。
    """


# 排除目录（按名字，任意层级）
SKIP_DIRS = {".git", ".obsidian", ".trash", "__pycache__", ".workbuddy", "备份归档"}

# type → 中文标签（§9.2）
TYPE_LABEL = {
    "project": "项目",
    "knowledge": "知识",
    "rule": "规则",
    "log": "日志",
    "note": "笔记",
    "index": "索引",
    "preference": "偏好",
}

ROOT_FOLDER = "(根)"

# tier（抽屉分层）→ 中文标签。vault 里 tier 只出现在部分笔记的 frontmatter 上，
# 缺失的一律归入「未分层」，不臆测成 cold（归档）——归档是有语义的动作，不是默认值。
TIER_LABEL = {"hot": "活跃", "warm": "常温", "cold": "归档"}
TIER_ORDER = ["hot", "warm", "cold"]
TIER_UNKNOWN_LABEL = "未分层"

# 自动生成页（索引类）：由 sync_all / MCP 服务器重建，不是「谁往库里加了什么」。
# 为什么必须单独认出来：这类页面的 frontmatter **没有 created**（生成器不写），
# vault 解析会把它回落到 updated，于是「记忆索引.md」每天都被算出一次「今天新增」
# —— 主人实测发现后追问「记忆索引为什么是新增的？」。它们是生成物，两个桶都不该进。
# 判定留两个信号：frontmatter 的 source 标记（机器写的是 ai-memory-server），
# 外加已知索引名的兜底（脚本索引.md 的 source 是 workbuddy，光靠 source 认不出）。
GENERATED_SOURCES = {"ai-memory-server"}
GENERATED_TITLES = {"记忆索引", "路由索引", "脚本索引"}


def is_generated(rec: Dict[str, Any]) -> bool:
    """这条记录是不是自动生成的索引页（日历的两个桶都要跳过它）。"""
    return (
        str(rec.get("source") or "") in GENERATED_SOURCES
        or str(rec.get("title") or "").strip() in GENERATED_TITLES
    )

# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #
_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.S)
# 与勘察基线口径一致：[[ 后到 ] | # 之前的片段即链接目标
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
# 代码块必须剔除后再找 `[[…]]`（2026-10-20）。理由见 `_link_targets`。
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]*\[([ xX])\][ \t]*(.*)$", re.M)
_INT_RE = re.compile(r"^-?\d+$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")
_TOKEN_SPLIT = re.compile(r"[\s,，。？?！!、；;：:（）()\[\]「」【】\"'`/\|]+")

# search_notes 的 semantic 口径（无向量模型）说明，写进调用 log
SEMANTIC_NOTE = "semantic 口径：标签重叠 + 检索词正文共现计数（无向量模型），非嵌入相似度"


def _link_targets(body: str) -> List[str]:
    """抽出正文里的 `[[目标]]`，**跳过代码块与行内代码**。

    为什么要剥代码（2026-10-20）：库里几篇规则文档把 `[[笔记标题]]` 当语法示例写在
    代码块里，裸正则会把这些示例当成真链接 —— 实测 66 处悬空引用里，`...`、`a`、
    `{title}`、`甲`、`乙`、`标题`、`双链`、`` ` = ` `` 这些全是示例；
    真正「写错了名字」的只有 2 个（连字符/空格不一致）。
    Obsidian 自己也是跳过代码的，不剥就等于把文档里的示例算成图上的边。
    """
    cleaned = _FENCE_RE.sub(" ", body)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    return [t.strip() for t in _LINK_RE.findall(cleaned) if t.strip()]


# --------------------------------------------------------------------------- #
# 轻量 YAML（frontmatter）解析
# --------------------------------------------------------------------------- #
def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _coerce(v: str) -> Any:
    v = v.strip()
    if v == "":
        return ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_unquote(x) for x in inner.split(",") if x.strip() != ""]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if _INT_RE.match(v):
        try:
            return int(v)
        except ValueError:
            return v
    return v


def _parse_frontmatter(block: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not key:
            continue
        data[key] = _coerce(val)
    return data


def _fm_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(_fm_str(x) for x in value)
    return str(value)


# --------------------------------------------------------------------------- #
# 文本读取 / 计数
# --------------------------------------------------------------------------- #
def _read_text(path: str) -> Optional[str]:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return None
    return None


def _word_count(text: str) -> int:
    return len(_CJK_RE.findall(text)) + len(_LATIN_RE.findall(text))


def _terms(query: str) -> List[str]:
    """检索词：英文/数字词小写 + 中文整块与 2-gram。"""
    query = (query or "").strip()
    if not query:
        return []
    out: List[str] = []
    for chunk in _TOKEN_SPLIT.split(query):
        if not chunk:
            continue
        if _CJK_RE.search(chunk):
            out.append(chunk)
            for i in range(len(chunk) - 1):
                out.append(chunk[i : i + 2])
        else:
            out.append(chunk.lower())
    seen = set()
    uniq: List[str] = []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def query_chunks(query: str, limit: int = 4) -> List[str]:
    """给展示用的关键词：按标点/空格切出的整块（非 2-gram），去重后取前 limit 个。"""
    out: List[str] = []
    for chunk in _TOKEN_SPLIT.split(query or ""):
        c = chunk.strip()
        if len(c) < 2 or c in out:
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 单文件解析 → NoteRecord(dict)
# --------------------------------------------------------------------------- #
def _note_id(rel_path: str) -> str:
    return "n_" + hashlib.md5(rel_path.encode("utf-8")).hexdigest()[:7]


def _parse_file(root: str, rel: str, stat: os.stat_result) -> Optional[Dict[str, Any]]:
    full = os.path.join(root, rel.replace("/", os.sep))
    text = _read_text(full)
    if text is None:
        return None

    fm: Dict[str, Any] = {}
    body = text
    m = _FM_RE.match(text)
    if m:
        fm = _parse_frontmatter(m.group(1))
        body = text[m.end() :]

    stem = os.path.splitext(os.path.basename(rel))[0]
    title = _fm_str(fm.get("title")) or stem

    tags_raw = fm.get("tags")
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw if str(t).strip()]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = []

    ntype = _fm_str(fm.get("type")) or "note"
    summary = _fm_str(fm.get("summary"))

    updated = _fm_str(fm.get("updated")) or _fm_str(fm.get("created"))
    if not updated:
        try:
            updated = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(stat.st_mtime))
        except (OSError, ValueError):
            updated = ""
    created_raw = _fm_str(fm.get("created"))
    created = created_raw or updated

    folder = rel.split("/")[0] if "/" in rel else ROOT_FOLDER

    # 链接（扫全文，含 frontmatter，与基线口径一致；代码块已由 _link_targets 剥掉）
    link_targets = _link_targets(text)

    # checkbox
    done_items: List[str] = []
    open_items: List[str] = []
    for st, txt in _CHECKBOX_RE.findall(body):
        txt = re.sub(r"\*\*(.+?)\*\*", r"\1", txt.strip()).strip()
        if st.lower() == "x":
            done_items.append(txt)
        else:
            open_items.append(txt)

    rel_path = "/" + rel
    size_kb = round(stat.st_size / 1024.0, 1)

    return {
        "id": _note_id(rel_path),
        "rel": rel,
        "relPath": rel_path,
        "title": title,
        "stem": stem,
        "folder": folder,
        "type": ntype,
        "typeLabel": TYPE_LABEL.get(ntype, ntype),
        "tags": tags,
        "summary": summary,
        "excerpt": summary[:120],
        "content": body,
        "frontmatter": {k: _fm_str(v) for k, v in fm.items()},
        "fm": fm,
        "updatedAt": updated,
        "updatedLabel": relative_time_label(updated),
        "created": created,
        # created 是「显式写在 frontmatter 里」还是「缺失后回落到 updated」。
        # 日历的「哪天加了哪些文件」必须只认显式 created —— 见 is_generated() 的说明。
        "hasCreated": bool(created_raw),
        "wordCount": _word_count(body),
        "sizeKB": size_kb,
        "accessCount": int(fm.get("access_count")) if isinstance(fm.get("access_count"), int) else 0,
        "verified": bool(fm.get("verified")) if isinstance(fm.get("verified"), bool) else False,
        "version": int(fm.get("version")) if isinstance(fm.get("version"), int) else 0,
        "source": _fm_str(fm.get("source")),
        # 抽屉分层（hot / warm / cold）；缺失为空串，由 tier_counts() 收进「未分层」
        "tier": _fm_str(fm.get("tier")),
        "link_targets": link_targets,
        "done_items": done_items,
        "open_items": open_items,
        # 以下在全局链接解析后回填
        "outlinks": [],
        "dangling": [],
        "inlinks": [],
        "degree": 0,
    }


# --------------------------------------------------------------------------- #
# 索引 + 缓存
# --------------------------------------------------------------------------- #
class _Index:
    def __init__(self) -> None:
        self.root: Optional[str] = None
        self.records: Dict[str, Dict[str, Any]] = {}  # rel -> record
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.file_state: Dict[str, Tuple[float, int]] = {}
        self.built_at: float = 0.0
        self.last_ms: float = 0.0
        # 数据源相关（T2）：source 记录当前索引实际来自哪；db_error 非空即表示
        # db 模式降级回 md 了 —— 这个原因必须能透到 /api/vault/status，
        # 否则「库坏了却看起来正常」是最难查的一类故障。
        self.source: str = SOURCE_MD
        self.db_built_at: str = ""
        self.db_error: Optional[str] = None
        # 库文件指纹（主库 + wal 的 mtime/size）。没有它就只能每次请求全量重读，
        # 实测 119 篇要 55~60 ms —— 前端一个页面十几个请求就是半秒白等。
        self.db_stamp: Optional[Tuple[int, int, int, int]] = None

    def clone_state(self) -> None:
        pass


_lock = threading.RLock()
_index = _Index()
_configured_root: Optional[str] = None


def get_root() -> str:
    global _configured_root
    if _configured_root is None:
        try:
            from . import settings_store

            _configured_root = settings_store.get_vault_path()
        except Exception:
            _configured_root = DEFAULT_VAULT_PATH
    return _configured_root or DEFAULT_VAULT_PATH


def set_root(path: str) -> None:
    """设置 Vault 根路径并使索引失效（§9.9）。"""
    global _configured_root
    path = (path or "").strip() or DEFAULT_VAULT_PATH
    with _lock:
        _configured_root = path
        # 强制下次全量重建
        _index.root = None
        _index.file_state = {}
        # 换 root 就必须换指纹：否则换了库但指纹恰好相同（比如都指向同一份
        # 备份），闸门会放行一整套属于别处的记录。
        _index.db_stamp = None


def _scan(root: str) -> Dict[str, Tuple[float, int]]:
    state: Dict[str, Tuple[float, int]] = {}
    if not root or not os.path.isdir(root):
        return state
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            state[rel] = (st.st_mtime, st.st_size)
    return state


def _resolve_links(records: Dict[str, Dict[str, Any]]) -> None:
    """解析 outlinks / dangling / inlinks / degree。

    解析顺序 = **title 优先，其次文件名 stem**（2026-10-20 补 stem 回落）。
    为什么必须有 stem 回落：Obsidian 的 `[[x]]` 认的是**文件名**，不是 frontmatter 的
    title。本库有笔记两者不一致 ——
    `项目_Graphite_Workspace_多Agent工作台（2026-10-14）.md` 的文件名用下划线，
    而 title 写的是空格版。只按 title 解析的后果是：
      · 23 处引用全部算「悬空」，图上少了 23 条边；
      · 而同一份数据在 `check_integrity.py` 里只报 12 条悬空（那边认 stem），
        两个页面各说各话，看起来像两个 bug。
    同一份库被两套解析器读出两种图，比任何单个 bug 都难查 —— 所以这里对齐 Obsidian。
    """
    title_index: Dict[str, str] = {}
    stem_index: Dict[str, str] = {}
    for rel, rec in records.items():
        title_index[rec["title"]] = rel
        # stem = 文件名去掉 .md；只在 title 查不到时才用，冲突时以「第一个」为准
        stem = rel.rsplit("/", 1)[-1]
        if stem.lower().endswith(".md"):
            stem = stem[:-3]
        stem_index.setdefault(stem, rel)

    for rec in records.values():
        outs: List[str] = []
        dangling: List[str] = []
        seen_out: set = set()
        # `link_hits` 与 `link_targets` 等长，逐项给出解析到的 rel 或 None。
        # 统计（出现次数口径）必须也读这一份 —— 否则 graph() 里再自己解析一遍，
        # 就会出现「图上 890 条边、标题写 1192 条已解析」这种同一页自相矛盾。
        hits: List[Optional[str]] = []
        for target in rec["link_targets"]:
            tgt_rel = title_index.get(target) or stem_index.get(target)
            hits.append(tgt_rel)
            if tgt_rel is None:
                if target not in dangling:
                    dangling.append(target)
                continue
            if tgt_rel not in seen_out:
                seen_out.add(tgt_rel)
                outs.append(tgt_rel)
        rec["outlinks"] = outs
        rec["dangling"] = dangling
        rec["link_hits"] = hits

    for rec in records.values():
        rec["inlinks"] = []
    for rec in records.values():
        for tgt in rec["outlinks"]:
            records[tgt]["inlinks"].append(rec["rel"])

    for rec in records.values():
        deg = set(rec["outlinks"]) | set(rec["inlinks"])
        rec["degree"] = len(deg)


def get_source() -> str:
    """当前数据源：db（默认，2026-10-20 起）| md。非法值回落 db。

    为什么默认反转：记忆库那边 md 已经降级为「库的单向快照」，写完一条笔记后
    快照不会立刻刷新（要等一次 `sync_all`）。工作台若还读盘，就会出现
    「刚写的东西在 web 上看不见」—— 而这恰好是最容易被误判成数据丢失的症状。
    显式 `GRAPHITE_VAULT_SOURCE=md` 仍可退回扫盘（应急：库坏了还能看快照）。
    """
    raw = (os.environ.get("GRAPHITE_VAULT_SOURCE") or "").strip().lower()
    return SOURCE_MD if raw == SOURCE_MD else SOURCE_DB


def db_path(root: str) -> str:
    return vault_db.db_path_for(root)


def _record_from_db_row(row: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """把 notes 表的一行组装成与 `_parse_file` 同形的 record。

    字段名必须逐字对齐 md 模式（`relPath` / `content` / `excerpt` / `hasCreated` …），
    否则前端拿两套数据源会看到两种形状 —— 这是数据源开关最容易埋的雷。

    `prefix` 是库所在 vault 相对工作台 root 的路径（见 `vault_db.rel_prefix`）。
    **只加在 `rel` / `relPath` / `id` 上**：`folder` 必须用库内口径算，
    否则 root 比 vault 高一层时，所有笔记的 folder 都会变成 `记忆库` 这一个值。
    """
    local = row["rel_path"]
    rel = f"{prefix}/{local}" if prefix else local
    rel_path = "/" + rel
    body = row["body_md"]
    title = row["title"] or row["stem"]
    # 注意：**不用 db 的 dir 列**。索引侧的 dir 存的是「直接父目录名」
    # （`日志/2026-08/x.md` → `2026-08`），而本模块对外契约的 folder 是
    # 「顶层目录」（→ `日志`）。契约在 vault.py，数据源要适配契约，不是反过来。
    folder = local.split("/")[0] if "/" in local else ROOT_FOLDER
    updated = row["updated"] or row["created"]
    created = row["created"] or updated

    link_targets = _link_targets(body)

    done_items: List[str] = []
    open_items: List[str] = []
    for st, txt in _CHECKBOX_RE.findall(body):
        txt = re.sub(r"\*\*(.+?)\*\*", r"\1", txt.strip()).strip()
        if st.lower() == "x":
            done_items.append(txt)
        else:
            open_items.append(txt)

    fm: Dict[str, Any] = {
        "title": title,
        "tags": row["tags"],
        "created": row["created"],
        "updated": row["updated"],
        "tier": row["tier"],
        "type": row["type"],
        "scope": row["scope"],
        "source": row["source"],
        "verified": row["verified"],
        "version": row["version"],
        "access_count": row["access_count"],
        "schema_version": row["schema_version"],
        "summary": row["summary"],
    }
    # 阶段 6（2026-10-23）：等级随 fm 下发，进详情的 frontmatter / 属性面板。
    # 旧库没这列时是空串，前端按「无等级」隐藏，不造假值。
    grade = str(row.get("grade") or "").upper()
    if grade:
        fm["grade"] = grade
    fm = {k: v for k, v in fm.items() if v not in ("", None, [])}

    ntype = row["type"] or "note"
    return {
        "id": _note_id(rel_path),
        "rel": rel,
        "relPath": rel_path,
        "title": title,
        "stem": row["stem"],
        "folder": folder,
        "type": ntype,
        "typeLabel": TYPE_LABEL.get(ntype, ntype),
        "tags": row["tags"],
        "summary": row["summary"],
        "excerpt": row["summary"][:120],
        "content": body,
        "frontmatter": {k: _fm_str(v) for k, v in fm.items()},
        "fm": fm,
        "updatedAt": updated,
        "updatedLabel": relative_time_label(updated),
        "created": created,
        # db 的 created 列存的是**原文值**（memory_web `_upsert` 不用回落），
        # 空就是空 —— 所以这里能准确判断「显式与否」，比 md 模式反而干净。
        "hasCreated": bool(row["created"]),
        "wordCount": _word_count(body),
        # 2026-10-20：原来用 chars/1024 顶替字节数，中文 1 字 ≈ 3 字节，显示会小 3 倍。
        # 改成按 UTF-8 字节算正文字节数 —— 与旧的口径（整个文件 st_size/1024）相差
        # 只剩 frontmatter 那几百字节，界面上不再出现「明明 20 KB 显示 7 KB」。
        "sizeKB": round(len(body.encode("utf-8")) / 1024.0, 1),
        "accessCount": row["access_count"],
        "verified": row["verified"],
        "version": row["version"],
        "source": row["source"],
        "tier": row["tier"],
        # 阶段 6：等级随摘要下发（列表徽章用）。旧库无列 → 空串 → 前端隐藏。
        "grade": grade,
        "link_targets": link_targets,
        "done_items": done_items,
        "open_items": open_items,
        "outlinks": [],
        "dangling": [],
        "inlinks": [],
        "degree": 0,
    }


def _rebuild_from_db(root: str, force: bool = False) -> bool:
    """从索引库重建内存记录。成功返回 True；库不可用返回 False。

    **返回 False 不再意味着「回落扫盘」** —— 调用方 `ensure_index` 会直接抛
    `VaultUnavailable`（2026-10-20 主人裁定：md 不是最新，回退等于展示假数据）。

    带一道**文件指纹闸**：库和它的 `-wal` 都没动过就直接沿用内存里的记录。
    没有这道闸，每个 API 请求都要全量读 119 行正文（实测 55~60 ms），
    而其中 99% 的请求碰到的库根本没有任何变化。
    """
    path = db_path(root)
    try:
        st = vault_db.stamp(path)
    except OSError:
        st = None
    if (
        not force
        and st is not None
        and st == _index.db_stamp
        and _index.records
        and _index.root == root
        and _index.source == SOURCE_DB
    ):
        return True
    ok, reason = vault_db.available(path)
    if not ok:
        _index.db_error = reason or "索引库不可用"
        return False
    try:
        rows, meta = vault_db.fetch_rows(path)
    except Exception as exc:  # noqa: BLE001 —— 库坏了不能让工作台整体崩
        _index.db_error = f"读取索引库失败：{exc}"
        return False
    records: Dict[str, Dict[str, Any]] = {}
    prefix = vault_db.rel_prefix(path, root)
    for row in rows:
        rec = _record_from_db_row(row, prefix)
        records[rec["rel"]] = rec
    _resolve_links(records)
    _index.root = root
    _index.source = SOURCE_DB
    _index.records = records
    _index.by_id = {r["id"]: r for r in records.values()}
    _index.file_state = {}
    _index.db_built_at = str(meta.get("built_at") or "")
    _index.db_error = None
    # 指纹要在**读完之后**再取一次：读的过程中库可能刚好被写入，
    # 用读之前那个指纹会把「这一版」永久锁死在缓存里。
    try:
        _index.db_stamp = vault_db.stamp(path)
    except OSError:
        _index.db_stamp = None
    return True


def _rebuild(root: str) -> None:
    state = _scan(root)
    records: Dict[str, Dict[str, Any]] = {}
    for rel, (mt, size) in state.items():
        try:
            st = os.stat(os.path.join(root, rel.replace("/", os.sep)))
        except OSError:
            continue
        try:
            rec = _parse_file(root, rel, st)
        except Exception:
            rec = None
        if rec is not None:
            records[rel] = rec
    _resolve_links(records)
    _index.root = root
    _index.records = records
    _index.by_id = {r["id"]: r for r in records.values()}
    _index.file_state = state


def _incremental(root: str, state: Dict[str, Tuple[float, int]]) -> None:
    old_state = _index.file_state
    changed = [rel for rel, meta in state.items() if old_state.get(rel) != meta]
    removed = [rel for rel in old_state if rel not in state]
    if not changed and not removed:
        _index.file_state = state
        return
    records = dict(_index.records)
    for rel in removed:
        records.pop(rel, None)
    for rel in changed:
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            st = os.stat(full)
            rec = _parse_file(root, rel, st)
        except Exception:
            rec = None
        if rec is None:
            records.pop(rel, None)
        else:
            records[rel] = rec
    _resolve_links(records)
    _index.root = root
    _index.records = records
    _index.by_id = {r["id"]: r for r in records.values()}
    _index.file_state = state


def ensure_index(force: bool = False) -> None:
    """保证索引是最新的。读锁保证并发安全。

    两个数据源分支（2026-10-20 默认反转，因为库已是唯一原件）：
      - **默认 db** → 从 `数据库/index.sqlite3` 全量读，**不扫盘**。MCP 那边
        `memory_write` 一落库，这边下一个请求就能看见，不需要等任何同步动作；
      - `GRAPHITE_VAULT_SOURCE=md` → 老路径（扫盘 + `(relPath, mtime, size)` 增量），
        仅在需要绕开库做对照时用。

    **db 失败不再回落 md（2026-10-20 主人裁定）**：抛 `VaultUnavailable`，
    路由层转 503，前端整站进「记忆库不可用」页。理由见 `VaultUnavailable` 的 docstring。
    注意 db 分支刻意不做增量：跨进程的写入没有可靠的变更通知，增量就得维护
    `(rel_path, updated, chars)` 快照再猜哪些变了，百篇量级下不如每次全读
    （实测十几毫秒），换来「永远不会显示上一版」。
    """
    root = get_root()
    with _lock:
        t0 = time.perf_counter()
        if get_source() == SOURCE_DB:
            ok = False
            try:
                ok = _rebuild_from_db(root, force=force)
            except Exception as exc:  # noqa: BLE001 —— 任何读库异常都算服务级故障
                _index.db_error = f"读取索引库异常：{exc}"
                ok = False
            if ok:
                _index.last_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                _index.built_at = time.time()
                return
            reason = _index.db_error or "索引库不可用"
            _index.last_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            _index.built_at = time.time()
            raise VaultUnavailable(reason)

        # 显式 md 模式（应急 / 对照）：行为与切换前一致
        state = _scan(root)
        if force or _index.root != root or not _index.records:
            _rebuild(root)
        else:
            _incremental(root, state)
        _index.source = SOURCE_MD
        _index.db_built_at = ""
        _index.last_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        _index.built_at = time.time()


def reindex() -> Dict[str, Any]:
    """强制重建索引（§9.10）。"""
    ensure_index(force=True)
    with _lock:
        notes = len(_index.records)
        links = sum(len(r["link_targets"]) for r in _index.records.values())
        return {
            "ok": True,
            "notes": notes,
            "links": links,
            "ms": int(round(_index.last_ms)),
        }


# --------------------------------------------------------------------------- #
# 读取辅助
# --------------------------------------------------------------------------- #
def _records() -> List[Dict[str, Any]]:
    ensure_index()
    with _lock:
        return list(_index.records.values())


def get_record(note_id: str) -> Optional[Dict[str, Any]]:
    ensure_index()
    with _lock:
        return _index.by_id.get(note_id)


def get_record_by_path(path: str) -> Optional[Dict[str, Any]]:
    ensure_index()
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return None
    p = p.lstrip("/")
    with _lock:
        if p in _index.records:
            return _index.records[p]
        for rec in _index.records.values():
            if rec["title"] == path.strip() or rec["stem"] == os.path.splitext(p)[0]:
                return rec
    return None


# --------------------------------------------------------------------------- #
# Summary / Detail（§9.2 / §9.3）
# --------------------------------------------------------------------------- #
def _summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rec["id"],
        "title": rec["title"],
        "relPath": rec["relPath"],
        "folder": rec["folder"],
        "type": rec["type"],
        "typeLabel": rec["typeLabel"],
        "tags": rec["tags"],
        # tier 随摘要下发（2026-10-20）：看板的「热度分层」条形卡点击后要按 tier 筛列表，
        # 前端拿不到这个字段就只能退回关键词搜索，而关键词匹配不到 frontmatter 的 tier。
        # 走 .get 不用下标：DB 源的历史行可能没有这一列。
        "tier": rec.get("tier") or "",
        # 等级（阶段 6，2026-10-23）：随摘要下发，列表徽章 / 属性面板用。
        # md 应急源没有 grade 概念，空串 = 前端隐藏，不造假值。
        "grade": rec.get("grade") or "",
        "excerpt": rec["excerpt"],
        "updatedAt": rec["updatedAt"],
        "updatedLabel": rec["updatedLabel"],
        # createdAt（2026-10-21）：笔记页要按「添加时间」排序，与「最近更改」是两个键。
        # 生成类页面（记忆索引等）frontmatter 不写 created，rec["created"] 会回落成
        # updated —— 这时两个排序键相同，属于如实反映「它没有创建时间」，不另造假值。
        "createdAt": rec.get("created") or rec["updatedAt"],
        "wordCount": rec["wordCount"],
        "sizeKB": rec["sizeKB"],
        "degree": rec["degree"],
    }


def _link_refs(rels: List[str]) -> List[Dict[str, Any]]:
    ensure_index()
    out: List[Dict[str, Any]] = []
    with _lock:
        for rel in rels:
            rec = _index.records.get(rel)
            if rec:
                out.append({"id": rec["id"], "title": rec["title"], "relPath": rec["relPath"]})
    return out


def note_hits(note_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """从 runs 中筛出确实命中过该笔记的历史检索（§9.3，真数据）。"""
    try:
        from . import runs as runs_store
    except Exception:
        return []
    page = runs_store.list_runs(page=1, page_size=500)
    hits: List[Dict[str, Any]] = []
    for r in page["items"]:
        rtype = r.get("type")
        detail = r.get("detail") or {}
        query = None
        mode = "hybrid"
        if rtype == "MCP 调用" and r.get("target") == "search_notes":
            args = detail.get("args") or {}
            query = args.get("query")
            mode = args.get("mode", "hybrid")
        elif rtype == "Agent 检索":
            query = r.get("target")
        if not query:
            continue
        ids = detail.get("resultIds")
        results = None
        if ids is None:
            results = search(query, top_k=5, mode=mode)
            ids = [x["id"] for x in results]
        if note_id not in ids:
            continue
        score = 1.0
        if results is None:
            results = search(query, top_k=5, mode=mode)
        for x in results:
            if x["id"] == note_id:
                score = x["score"]
                break
        at = r.get("timeLabel") or r.get("at", "")
        hits.append({"query": query, "score": round(float(score), 2), "at": at})
        if len(hits) >= limit:
            break
    return hits


def get_note(note_id: str) -> Optional[Dict[str, Any]]:
    rec = get_record(note_id)
    if not rec:
        return None
    detail = _summary(rec)
    outlinks = _link_refs(rec["outlinks"])
    backlinks = _link_refs(rec["inlinks"])
    detail.update(
        {
            "content": rec["content"],
            "readingOnly": True,
            "frontmatter": rec["frontmatter"],
            "outlinks": outlinks,
            "backlinks": backlinks,
            "dangling": rec["dangling"],
            "metadata": {
                "created": rec["created"],
                "updated": rec["updatedAt"],
                "wordCount": rec["wordCount"],
                "sizeKB": rec["sizeKB"],
                "folder": rec["folder"],
                "type": rec["type"],
                "accessCount": rec["accessCount"],
                "verified": rec["verified"],
                "version": rec["version"],
                "source": rec["source"],
                "outCount": len(outlinks),
                "inCount": len(backlinks),
            },
            "taskStats": {"done": len(rec["done_items"]), "open": len(rec["open_items"])},
            "hits": note_hits(note_id),
        }
    )
    return detail


# --------------------------------------------------------------------------- #
# 列表（§9.2 支持 type / folder / scope）
# --------------------------------------------------------------------------- #
def _match_scope(rec: Dict[str, Any], scope: Optional[str]) -> bool:
    if not scope or scope in ("全部", "全部笔记", "全部类型"):
        return True
    s = scope.strip().strip("/")
    if not s:
        return True
    rel = rec["rel"]
    return rel == s or rel.startswith(s + "/") or s in rel


def list_notes(
    ntype: Optional[str] = None,
    folder: Optional[str] = None,
    scope: Optional[str] = None,
    date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按类型 / 目录 / 作用域 / 更新日期筛选笔记。

    `date` 为 `YYYY-MM-DD`，按 frontmatter 的 `updatedAt` 当日匹配（供活动日历下钻用）。
    """
    out = _records()
    if ntype and ntype not in ("全部", "全部类型"):
        out = [r for r in out if r["type"] == ntype]
    if folder and folder not in ("全部", "(全部)"):
        out = [r for r in out if r["folder"] == folder]
    if scope:
        out = [r for r in out if _match_scope(r, scope)]
    if date:
        out = [r for r in out if (r.get("updatedAt") or "")[:10] == date]
        # 按日期下钻时按更新时间倒序更合用
        out.sort(key=lambda r: (r.get("updatedAt") or ""), reverse=True)
        return [_summary(r) for r in out]
    out.sort(key=lambda r: (r["rel"]))
    return [_summary(r) for r in out]


def all_summaries() -> List[Dict[str, Any]]:
    return [_summary(r) for r in _records()]


# --------------------------------------------------------------------------- #
# 检索（§9.6）
# --------------------------------------------------------------------------- #
def _field_hits(text: str, term: str) -> int:
    if not text or not term:
        return 0
    return text.lower().count(term.lower())


def _keyword_score(rec: Dict[str, Any], terms: List[str]) -> float:
    title = rec["title"]
    tags = " ".join(rec["tags"])
    summary = rec["summary"]
    content = rec["content"]
    total = 0.0
    hit = 0
    for t in terms:
        s = 0.0
        s += 3.0 * min(_field_hits(title, t), 3)
        s += 2.5 * min(_field_hits(tags, t), 3)
        s += 1.5 * min(_field_hits(summary, t), 3)
        s += 0.6 * min(_field_hits(content, t), 5)
        if s > 0:
            hit += 1
        total += s
    if terms:
        total *= 1.0 + 0.5 * (hit / len(terms))
    return total


def _semantic_score(rec: Dict[str, Any], terms: List[str]) -> float:
    """无向量模型：标签重叠 + 正文/摘要词共现计数。"""
    tags_low = [t.lower() for t in rec["tags"]]
    blob = (rec["summary"] + "\n" + rec["content"]).lower()
    tag_overlap = 0.0
    for t in terms:
        tl = t.lower()
        if any(tl in tg or tg in tl for tg in tags_low):
            tag_overlap += 1.0
    present = [t for t in terms if t.lower() in blob]
    content_hits = sum(min(blob.count(t.lower()), 5) for t in present)
    # 词共现：不同的检索词在正文同时出现的对数
    cooc = 0
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            if present[i] != present[j]:
                cooc += 1
    return 2.2 * tag_overlap + 0.9 * content_hits + 1.2 * cooc


def _raw_scores(pool: List[Dict[str, Any]], terms: List[str], mode: str) -> List[float]:
    if mode == "semantic":
        return [_semantic_score(r, terms) for r in pool]
    if mode == "keyword":
        return [_keyword_score(r, terms) for r in pool]
    # hybrid：两者归一化后加权
    kws = [_keyword_score(r, terms) for r in pool]
    sems = [_semantic_score(r, terms) for r in pool]
    kmax = max(kws) or 1.0
    smax = max(sems) or 1.0
    return [0.5 * (kws[i] / kmax) + 0.5 * (sems[i] / smax) for i in range(len(pool))]


_MODE_SPAN = {"keyword": (0.50, 0.42), "semantic": (0.50, 0.38), "hybrid": (0.55, 0.40)}


def _pick_quote(rec: Dict[str, Any], terms: List[str]) -> str:
    content = rec["content"]
    chunks = re.split(r"[。\n！？!?]", content)
    best = ""
    best_hits = 0
    for s in chunks:
        s = s.strip().lstrip("#>-* \t")
        if len(s) < 6:
            continue
        h = sum(1 for t in terms if len(t) >= 2 and t in s)
        if h > best_hits or (h == best_hits and h > 0 and (not best or len(s) < len(best))):
            best_hits = h
            best = s
    if best:
        return best[:120]
    for s in chunks:
        s = s.strip().lstrip("#>-* \t")
        if len(s) >= 8 and not s.startswith("|"):
            return s[:120]
    return rec["excerpt"]


def search(
    query: str,
    top_k: int = 5,
    mode: str = "hybrid",
    path_scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """真实全文检索；top_k 与 path_scope 真生效；quote 取真正文命中句。"""
    if mode not in ("keyword", "semantic", "hybrid"):
        mode = "hybrid"
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(20, top_k))

    pool = _records()
    if path_scope and path_scope not in ("全部", "全部笔记", "全部路径"):
        pool = [r for r in pool if _match_scope(r, path_scope)]
    terms = _terms(query)
    if not pool:
        return []

    raws = _raw_scores(pool, terms, mode)
    base, span = _MODE_SPAN[mode]
    order = sorted(range(len(pool)), key=lambda i: (-raws[i], pool[i]["id"]))
    mx = raws[order[0]] if order else 0.0

    out: List[Dict[str, Any]] = []
    for idx in order[:top_k]:
        raw = raws[idx]
        if mx > 0:
            score = base + span * (raw / mx)
        else:
            score = base
        score = max(base, min(base + span, round(score, 2)))
        rec = pool[idx]
        out.append(
            {
                "id": rec["id"],
                "title": rec["title"],
                "score": score,
                "relPath": rec["relPath"],
                "quote": _pick_quote(rec, terms),
            }
        )
    return out


def search_tags(query: str, top_k: int = 10) -> List[Dict[str, Any]]:
    q = (query or "").lower().strip()
    counter: Dict[str, int] = {}
    for rec in _records():
        for t in rec["tags"]:
            counter[t] = counter.get(t, 0) + 1
    out: List[Dict[str, Any]] = []
    for tag, cnt in counter.items():
        if not q or q in tag.lower():
            out.append({"tag": tag, "count": cnt, "score": round(0.6 + 0.3 * min(cnt, 3) / 3, 2)})
    out.sort(key=lambda x: (-x["count"], x["tag"]))
    return out[: max(1, int(top_k))]


def get_memory_context(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    hits = search(query, top_k=top_k, mode="hybrid")
    out: List[Dict[str, Any]] = []
    for h in hits:
        rec = get_record(h["id"])
        if not rec:
            continue
        block = rec["summary"] or rec["content"][:160]
        out.append(
            {
                "title": rec["title"],
                "relPath": rec["relPath"],
                "score": h["score"],
                "quote": h["quote"],
                "block": block[:160],
            }
        )
    return out


def list_backlinks(note_id: str) -> List[Dict[str, Any]]:
    rec = get_record(note_id)
    if not rec:
        return []
    return _link_refs(rec["inlinks"])


# --------------------------------------------------------------------------- #
# facets（§9.4）/ graph（§9.5）
# --------------------------------------------------------------------------- #
def _folder_counts() -> List[Dict[str, Any]]:
    counter: Dict[str, int] = {}
    for rec in _records():
        counter[rec["folder"]] = counter.get(rec["folder"], 0) + 1
    items = [{"folder": k, "count": v} for k, v in counter.items()]
    items.sort(key=lambda x: (-x["count"], x["folder"]))
    return items


def _tag_counts() -> List[Dict[str, Any]]:
    counter: Dict[str, int] = {}
    for rec in _records():
        for t in rec["tags"]:
            counter[t] = counter.get(t, 0) + 1
    items = [{"tag": k, "count": v} for k, v in counter.items()]
    items.sort(key=lambda x: (-x["count"], x["tag"]))
    return items


def _type_counts() -> List[Dict[str, Any]]:
    counter: Dict[str, int] = {}
    for rec in _records():
        counter[rec["type"]] = counter.get(rec["type"], 0) + 1
    items = [
        {"key": k, "label": TYPE_LABEL.get(k, k), "count": v} for k, v in counter.items()
    ]
    items.sort(key=lambda x: (-x["count"], x["key"]))
    return items


def tier_counts() -> List[Dict[str, Any]]:
    """抽屉分层（tier）分布，按 hot → warm → cold → 未分层 的固定语义序输出。

    与 `_type_counts()` 不同，这里**不按数量排序**：分层是有序的抽屉，
    「活跃 1 / 常温 116」按数量排会变成「常温 / 活跃」，读起来失去层级含义。
    """
    counter: Dict[str, int] = {}
    for rec in _records():
        t = (rec.get("tier") or "").strip().lower()
        if t not in TIER_LABEL:
            t = ""
        counter[t] = counter.get(t, 0) + 1

    def rank(k: str) -> int:
        return TIER_ORDER.index(k) if k in TIER_ORDER else len(TIER_ORDER)

    items = [
        {"key": k, "label": TIER_LABEL.get(k, TIER_UNKNOWN_LABEL), "count": v}
        for k, v in counter.items()
    ]
    items.sort(key=lambda x: rank(x["key"]))
    return items


def _link_stats() -> Tuple[int, int, int]:
    """(总链接数, 可解析链接数, 悬空链接数)，按出现次数计。

    解析结果直接取 `_resolve_links` 写下的 `link_hits`，**不在这里重新解析**：
    重新解析就得复制一份 title/stem 索引逻辑，两份逻辑迟早不一致
    （2026-10-20 之前就是这样：图上少了 26 条边，统计却算它们已解析）。
    """
    recs = _records()
    total = resolved = 0
    for r in recs:
        hits = r.get("link_hits") or []
        total += len(r["link_targets"])
        resolved += sum(1 for h in hits if h)
    return total, resolved, total - resolved


def facets() -> Dict[str, Any]:
    recs = _records()
    total, resolved, dangling = _link_stats()
    orphans = sum(1 for r in recs if r["degree"] == 0)
    return {
        "types": _type_counts(),
        "tags": _tag_counts()[:40],
        "folders": _folder_counts(),
        "totals": {
            "notes": len(recs),
            "links": total,
            "resolvedLinks": resolved,
            "dangling": dangling,
            "orphans": orphans,
        },
    }


def _graph_nodes(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "relPath": r["relPath"],
            "folder": r["folder"],
            "type": r["type"],
            "degree": r["degree"],
            "inDegree": len(r["inlinks"]),
            "outDegree": len(r["outlinks"]),
        }
        for r in recs
    ]


def graph() -> Dict[str, Any]:
    recs = _records()
    total, resolved, dangling = _link_stats()
    orphans = sum(1 for r in recs if r["degree"] == 0)
    max_degree = max((r["degree"] for r in recs), default=0)

    # 去重后再给前端：同一对笔记之间出现多次 [[x]] 是常态（`项目状态` 与
    # `记忆索引` 之间就有 2 处引用），展开成多条会在画布上叠成一根线，
    # 却让标题上的「连线数」虚高。统计口径仍按出现次数（`_link_stats` 的契约）。
    # 端点直接用 `outlinks`（已按 title→stem 解析过），不再自己查一遍 title ——
    # 那等于把「怎么算一条边」定义两遍，正是上一版少 26 条边的原因。
    by_rel = {r["rel"]: r for r in recs}
    links: List[Dict[str, str]] = []
    seen: set = set()
    for r in recs:
        for tgt_rel in r["outlinks"]:
            trec = by_rel.get(tgt_rel)
            if trec is None:
                continue
            key = (r["id"], trec["id"])
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": r["id"], "target": trec["id"]})

    # 悬空按「标题」去重：`dangling` 是出现次数（66），标题只有几个 ——
    # 页面上写「66 条悬空」会让人以为有 66 篇笔记缺失。
    missing_titles = {t for r in recs for t in r["dangling"]}

    return {
        "nodes": _graph_nodes(recs),
        "links": links,
        "folders": _folder_counts(),
        "stats": {
            "nodes": len(recs),
            "links": resolved,
            "uniqueLinks": len(links),
            "dangling": dangling,
            "danglingTitles": len(missing_titles),
            "orphans": orphans,
            "maxDegree": max_degree,
        },
    }


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
def get_status() -> Dict[str, Any]:
    """库状态。**这里不抛异常** —— 前端要靠它得知「为什么整个站进错误页」。

    其余数据接口在库不可用时一律 503（见 `ensure_index` / `VaultUnavailable`），
    但状态接口必须能正常回答，否则前端连原因都拿不到，只能显示一个白屏。
    """
    root = get_root()
    if not root or not os.path.isdir(root):
        return {
            "mounted": False,
            "noteCount": 0,
            "path": root,
            "label": "本地记忆库",
            "error": f"记忆库根路径不可读：{root}",
            "source": get_source(),
            "sourceLabel": "索引库 (index.sqlite3)" if get_source() == SOURCE_DB else "Markdown 快照",
        }
    try:
        ensure_index()
    except VaultUnavailable as e:
        with _lock:
            db_built_at = _index.db_built_at
            last_ms = _index.last_ms
            count = len(_index.records)
        return {
            "mounted": False,
            "noteCount": count,
            "path": root,
            "label": "本地记忆库",
            "error": str(e),
            "source": get_source(),
            "sourceLabel": "索引库 (index.sqlite3)" if get_source() == SOURCE_DB else "Markdown 快照",
            "dbBuiltAt": db_built_at,
            "indexMs": last_ms,
        }
    with _lock:
        count = len(_index.records)
        source = _index.source
        db_error = _index.db_error
        db_built_at = _index.db_built_at
        last_ms = _index.last_ms
    return {
        "mounted": True,
        "noteCount": count,
        "path": root,
        "label": "本地记忆库",
        "source": source,
        "sourceLabel": "索引库 (index.sqlite3)" if source == SOURCE_DB else "Markdown 快照",
        # 只在**显式** md 模式下才可能有值（应急开关）；db 模式不可用会直接 503
        "error": db_error,
        "dbBuiltAt": db_built_at,
        "indexMs": last_ms,
    }


def dir_size_mb() -> float:
    root = get_root()
    if not root or not os.path.isdir(root):
        return 0.0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                continue
    return round(total / (1024.0 * 1024.0), 1)


def last_scan_ms() -> float:
    return _index.last_ms
