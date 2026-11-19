"""SQLite 数据源：从 `index.sqlite3` 的 notes 表读笔记行（T2，2026-10-19）。

为什么单独一个模块，而不是塞进 vault.py：
    vault.py 已经 999 行，职责是「扫描 + 解析 + 组装 record + 检索」。
    本模块只做**一件事**：把库里的行读出来。
    record 的组装仍留在 vault.py —— `_note_id` / `TYPE_LABEL` / 链接正则这些常量
    必须只有一处定义，否则两条数据源会各长一套逻辑，早晚漂移出「同一篇笔记
    两种解析结果」。

数据源开关（2026-10-20 反转）：
    **默认读库** —— 库是唯一原件，md 只是可导出快照。`GRAPHITE_VAULT_SOURCE=md`
    才退回扫盘（应急 / 对照），此时代码路径与切换前完全一致。
    开关是**进程级**的，不是设置页可改的 —— 因为切换数据源会让索引内存态失效，
    走 HTTP 改容易半途而废；重启一次最干净。

口径差异（db 模式相对 md 模式的已知损失，写在这里免得以后当成 bug 查）：
    1. `sizeKB`：md 模式用 `st_size/1024`（字节），db 只有 `chars`（字符）。
       中文 1 字符 ≈ 3 字节 UTF-8，两者数字不可直接比（实测 119 篇全部有差）。
       彻底修法 = T4 给 notes 表补 `st_size` 列，本文件不动。
    2. ~~`type`：db 的 `type` 列是标题推导值~~ —— **2026-10-20 已修**：
       `memory_web._upsert` 改为「优先取调用方给的 meta，缺了才推导」，
       并把被推平的 3 篇协同记录改回 `log`。现在 db 的 type 就是原值。
    3. `tier`：db 对空值补了 `warm`（`meta.get("tier") or "warm"`），md 模式保留空串。
       1 篇受影响（记忆索引）。
    4. `summary`：md 模式的轻量 YAML 解析器**不还原引号内转义**（保留 `\"`），
       db 存的是 memory_store 解析后的值（已还原成 `"`）。4 篇受影响。
       **db 这边才是对的** —— 是否回头修 md 解析器单独决定，不在 T2 范围内。
    5. `folder`：db 的 `dir` 是「直接父目录名」，vault 的 `folder` 是「顶层目录」。
       已在 `vault._record_from_db_row` 里改用 rel_path 推导，两边一致。

`hasCreated` 反而不损失：db 的 `created` 列存的是**原文值**（memory_web 不回落），
空就是空，比 md 模式还准。
"""

from __future__ import annotations

import os
import sqlite3
import stat
from typing import Any, Dict, List, Optional, Tuple

# 2026-10-20：库文件已从 `.workbuddy/` 收口到 `数据库/`（与记忆库 db_paths 同一口径）。
# 这里必须覆盖两种 root，因为工作台的 vault 根可以是**记忆库本身**，也可以是它的
# **上一级**（本机现状：root = `.../Desktop/ai-memory`，库在 `.../记忆库/数据库/`）。
# 只认一个位置的下场不是报错，而是「库明明在、工作台却整个回落扫盘」——
# 表现为「刚写的东西在 web 上看不见」，最难解释的一类故障。
DB_REL_CANDIDATES = (
    "数据库/index.sqlite3",
    "记忆库/数据库/index.sqlite3",
    ".workbuddy/index.sqlite3",
    "记忆库/.workbuddy/index.sqlite3",
)
DEFAULT_DB_REL = DB_REL_CANDIDATES[0]

# 只读打开，绝不写库 —— 本模块只承担「读数据源」一个职责。
_NOTE_COLUMNS = (
    "title, stem, rel_path, dir, type, scope, tier, source, verified, "
    "tags, summary, created, updated, version, access_count, "
    "schema_version, chars, body_md"
)
# 阶段 6（2026-10-23）：归属与等级。单独一组 —— 旧库可能还没跑过迁移
# （没有这两列），SELECT 整体炸掉就什么都展示不了，所以**必须**降级重试。
_GRADE_COLUMNS = "vault_id, grade"


def db_path_for(root: str) -> str:
    """索引库位置：优先 `数据库/index.sqlite3`，其次旧位置 `.workbuddy/`。

    返回「第一个存在且非空的」，没有非空的就返回第一个存在的，都不存在时返回
    新位置 —— 让报错路径指向用户该去的地方（而不是一个早已废弃的旧路径）。

    ## 为什么必须跳过 0 字节文件（2026-11-18，同一坑第二例）

    旧实现只看 `os.path.isfile()`。而任何一次 `sqlite3.connect(路径)` 探测都会让
    SQLite **当场建出一个 0 字节文件** —— 它落在靠前的候选位置，`isfile` 就为真，
    真库（`记忆库/数据库/index.sqlite3`）被永久跳过。症状不是「库坏了」，
    而是全站 503 `索引库不可读：no such table: notes`，且重启无效。

    时间线：2026-10-21 第一次（`ai-memory/数据库/index.sqlite3`，改名隔离后回落）、
    2026-10-28 Docker 原型在容器里又建了一个同名空库、
    2026-11-18 复发且被误判成「今天的检索优化搞坏的」。

    空文件按定义不是 SQLite 库（最小合法库 4096 字节，1 页），所以判据取
    「存在 + `st_size > 0`」，比 `isfile` 只多一次 stat。**别退回去只判 isfile。**
    """
    fallback: Optional[str] = None
    for rel in DB_REL_CANDIDATES:
        cand = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(cand):
            continue
        if fallback is None:
            fallback = cand  # 全为空时用它报错，指向用户该去处理的路径
        try:
            if os.stat(cand).st_size > 0:
                return cand
        except OSError:
            continue
    if fallback is not None:
        return fallback
    return os.path.join(root, DEFAULT_DB_REL.replace("/", os.sep))


def rel_prefix(db_path: str, root: str) -> str:
    """库内的 `rel_path` 相对的是**库所在 vault**，不一定等于工作台的 root。

    本机现状：工作台 root = `.../Desktop/ai-memory`，库 = `.../记忆库/数据库/index.sqlite3`，
    库内 `rel_path` = `人设与偏好/AI人格.md`，而 md 模式下的 rel = `记忆库/人设与偏好/AI人格.md`。
    不补这个前缀，两条数据源给出的 `id` / `relPath` 是两套值 ——
    切换数据源后收藏的链接、前端缓存、`/api/note/<id>` 全部对不上，
    而且症状是「点开是空白」，不会报错。
    """
    vault = os.path.dirname(os.path.dirname(os.path.abspath(db_path)))
    try:
        rel = os.path.relpath(vault, os.path.abspath(root))
    except ValueError:  # Windows 跨盘符
        return ""
    if rel in ("", "."):
        return ""
    if rel.startswith(".."):  # 库在 root 之外：前缀无意义，别拼出 `../` 路径
        return ""
    return rel.replace(os.sep, "/")


def stamp(db_path: str) -> Tuple[int, int, int, int]:
    """库文件指纹：(主库 mtime_ns, size, WAL mtime_ns, WAL size)。

    为什么必须带上 `-wal`：本库是 WAL 模式，一次 `memory_write` 先落
    `index.sqlite3-wal`，主库文件的 mtime **可能一直不变**（要等 checkpoint）。
    只盯主库的下场是「刚写的东西在 web 上看不见」，而且刷新也没用 ——
    得等 checkpoint 自己发生。
    """
    wal = db_path + "-wal"
    try:
        st = os.stat(db_path)
        main = (st.st_mtime_ns, st.st_size)
    except OSError:
        main = (0, 0)
    try:
        stw = os.stat(wal)
        w = (stw.st_mtime_ns, stw.st_size)
    except OSError:
        w = (0, 0)
    return (main[0], main[1], w[0], w[1])


def _oserr(exc: OSError) -> str:
    """错误码短串：`errno=13` / `errno=2, winerror=3`。

    为什么带 winerror：Windows 会把「父路径是普通文件」这种 ENOTDIR 映射成
    `FileNotFoundError`（GetFileAttributes 返回 ERROR_PATH_NOT_FOUND=3），
    errno 只有 2，光看它跟真·文件不存在一模一样。winerror 才能把两者分开。
    """
    win = getattr(exc, "winerror", None)
    return f"errno={exc.errno}, winerror={win}" if win else f"errno={exc.errno}"


def _parent_note(db_path: str) -> str:
    """路径不存在时补一句父目录状态。

    为什么要多这一句：「文件不在」有好几种完全不同的原因 ——
    配置指错了（父目录也不在）、库还没就位（父目录在、文件没拷过去）、
    父路径根本不是目录。旧文案只有「索引库不存在：/path」，几种情况长得
    一模一样，等于没说。
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    try:
        st = os.stat(parent)
    except FileNotFoundError:
        return "父目录也不存在 —— 大概率是路径配置指错了"
    except PermissionError as exc:
        return f"父目录无权访问（{_oserr(exc)}）—— 文件可能就在，是目录进不去"
    except OSError as exc:
        return f"父目录访问失败（{type(exc).__name__}，{_oserr(exc)}）"
    if not stat.S_ISDIR(st.st_mode):
        return "父路径是个文件，不是目录 —— 路径拼接错了一层"
    return "父目录存在，但库里没有这个文件 —— 库没就位（未播种/拷错位置）"


def _read_probe_note(db_path: str) -> str:
    """SQLite 报「unable to open database file」之后，用裸 open 把真实原因挖出来。

    为什么要再探一次：那句是 SQLite 的甩锅式文案 —— 文件模式 000、目录不可写
    （只读连接打开 WAL 库要能落 `-shm`）、底层 I/O 错误，全都报这一句。
    只看它分不清是权限还是别的问题，而这两者的修法完全不同。
    """
    try:
        with open(db_path, "rb") as fh:
            fh.read(16)
    except PermissionError as exc:
        return f"（裸读探测：权限被拒绝，{_oserr(exc)} —— 文件在，当前用户读不了）"
    except OSError as exc:
        return f"（裸读探测：{type(exc).__name__}，{_oserr(exc)} —— {exc}）"
    return "（裸读探测：文件本身可读，失败发生在 SQLite 打开或查询阶段；若为 WAL 库，留意所在目录是否可写）"


def available(db_path: str) -> Tuple[bool, Optional[str]]:
    """库能不能读。返回 (是否可用, 不可用原因)。

    ## 诊断口径（2026-10-28 修）：「不存在」和「没权限」必须分开报

    旧实现是一行 `if not db_path or not os.path.isfile(db_path):
    return False, f"索引库不存在：{db_path}"`。看着没问题，实际会把权限问题
    伪装成路径问题 —— `os.path.isfile()` 内部是
    `try: os.stat(path) except (OSError, ValueError): return False`，
    **EACCES 和 ENOENT 一起被吞成 False**。于是这几种情况全部显示成
    「索引库不存在」：

      - SELinux 拒绝（容器 MCS 类别与宿主目录标签不匹配）
      - 父目录缺 x 位（进不去目录 = stat 子路径直接 EACCES）
      - ACL / 文件模式 000

    2026-10-28 就是这么被坑的：容器里报「索引库不存在：/vault/index.sqlite3」，
    实际文件好好在那儿，是另一个容器把 `:Z` 卷的标签改成了别的类别。
    排障被引到「路径配错了」上，白绕一圈。

    `os.path.exists()` 有**同一个毛病**，不能拿来替换。正确做法是自己
    `os.stat()` 并 catch，按 errno 分支说人话。
    """
    if not db_path:
        return False, "索引库路径未配置（vault_root 与 GRAPHITE_VAULT_DB 都是空）"

    try:
        st = os.stat(db_path)
    except FileNotFoundError:
        return False, f"索引库不存在：{db_path} —— {_parent_note(db_path)}"
    except PermissionError as exc:
        return False, (
            f"索引库无权访问（权限被拒绝，{_oserr(exc)}）：{db_path} —— "
            "文件在这个路径上，但当前用户读不到，**不是路径配错**。"
            "常见原因：SELinux 标签不匹配（容器 MCS 类别与宿主目录不一致）、"
            "父目录缺 x 位、ACL 拒绝。"
        )
    except OSError as exc:
        return False, (
            f"索引库访问失败（{type(exc).__name__}，{_oserr(exc)}）：{db_path} —— {exc}"
        )

    if stat.S_ISDIR(st.st_mode):
        return False, f"索引库路径是个目录，不是 SQLite 文件：{db_path}"

    try:
        conn = _connect(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # 带上路径：2026-11-18 那次全站 503 的文案只有「no such table: notes」，
        # 谁都不知道工作台到底在读哪个库 —— 而根因恰恰是读错了库（0 字节空库抢位）。
        msg = f"索引库不可读：{exc}（{db_path}）"
        if "unable to open" in str(exc).lower():
            msg += _read_probe_note(db_path)
        return False, msg
    if n <= 0:
        # 新装的机器就是 0 篇笔记 —— 这**不是**故障，是「还没开始记」。
        # 2026-11-19 实测：全新安装后第一次打开工作台，全站 503 说「记忆库不可用」，
        # 用户以为装坏了。现在放行，由前端画空状态引导。
        return True, None
    return True, None


def _connect(db_path: str) -> sqlite3.Connection:
    # mode=ro：只读连接。哪怕调用方手滑写了 UPDATE，SQLite 也会直接报错。
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _split_tags(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_rows(db_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """读出全部笔记行 + meta。返回 (rows, meta)。

    每行是**扁平 dict**，字段名与 db 列一一对应，不做任何 record 组装。
    正文列 `body_md` 原样带出（调用方决定要不要留）。
    """
    conn = _connect(db_path)
    try:
        rows: List[Dict[str, Any]] = []
        has_grade = True
        try:
            cursor = conn.execute(f"SELECT {_NOTE_COLUMNS}, {_GRADE_COLUMNS} FROM notes")
        except sqlite3.OperationalError:
            # 旧库没跑过阶段 6 迁移：没有 vault_id/grade 列。降级为老列集，
            # 等级按空串下发（前端隐藏等级徽章），绝不让整个工作台白屏。
            has_grade = False
            cursor = conn.execute(f"SELECT {_NOTE_COLUMNS} FROM notes")
        for r in cursor:
            row = {
                "title": str(r["title"] or ""),
                "stem": str(r["stem"] or ""),
                "rel_path": str(r["rel_path"] or "").replace("\\", "/").lstrip("/"),
                "dir": str(r["dir"] or ""),
                "type": str(r["type"] or "") or "note",
                "scope": str(r["scope"] or ""),
                "tier": str(r["tier"] or ""),
                "source": str(r["source"] or ""),
                "verified": bool(r["verified"]),
                "tags": _split_tags(r["tags"]),
                "summary": str(r["summary"] or ""),
                "created": str(r["created"] or ""),
                "updated": str(r["updated"] or ""),
                "version": _as_int(r["version"]),
                "access_count": _as_int(r["access_count"]),
                "schema_version": _as_int(r["schema_version"], 1),
                "chars": _as_int(r["chars"]),
                "body_md": str(r["body_md"] or ""),
            }
            if has_grade:
                row["vault_id"] = str(r["vault_id"] or "")
                row["grade"] = str(r["grade"] or "")
            else:
                row["vault_id"] = ""
                row["grade"] = ""
            rows.append(row)
        meta: Dict[str, Any] = {}
        try:
            for r in conn.execute("SELECT k, v FROM meta"):
                meta[str(r["k"])] = r["v"]
        except sqlite3.Error:
            # meta 表缺失不算致命：索引本身能读就够了，只是少一个构建时间。
            meta = {}
        return rows, meta
    finally:
        conn.close()


def stats(db_path: str) -> Dict[str, Any]:
    """给 /api/vault/status 用的轻量统计（不读正文）。"""
    conn = _connect(db_path)
    try:
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        chars = conn.execute("SELECT COALESCE(SUM(chars), 0) FROM notes").fetchone()[0]
        meta = {str(r["k"]): r["v"] for r in conn.execute("SELECT k, v FROM meta")}
        try:
            links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        except sqlite3.Error:
            links = 0
        return {
            "notes": int(notes),
            "chars": int(chars),
            "links": int(links),
            "builtAt": str(meta.get("built_at") or ""),
            "dbBytes": os.path.getsize(db_path) if os.path.isfile(db_path) else 0,
        }
    finally:
        conn.close()
