"""项目计划与进度 —— 数据层（阶段 10，2026-10-25）。

## 为什么又开一个库文件

和聊天（`chat.sqlite3`）、审计（`audit.sqlite3`）同一个理由，这里不重复推导，
只列本模块多出来的两条：

1. **计划是协作产物，不是笔记。** 计划/阶段/留言会被多人高频改（拖进度、
   改状态、留言），而笔记吃 FTS、有版本链、走 `sync_push` 单向覆盖。
   混进 `index.sqlite3` 的后果是：主人跑一次「刷新服务器」，全员的计划进度
   就回到三天前 —— 而且是静默的。
2. **它要被审计追着走。** 每次状态变更都要落 `audit_log`，审计库已经独立，
   计划数据留在同一个可写库里，备份/恢复才是「一对文件」而不是「一个文件 + 一张表」。

## 三张表与「一个事实源」

```
plans          一次计划（一个目标，包含若干阶段）
  └ stages     计划下的阶段（有 seq 顺序、进度、状态、负责人）
plan_comments  留言（stage_id 为空 = 计划级留言）
```

**进度只在服务端算**（`plan_progress()`）。前端拿 `progress` 字段直接画，
不许自己拿 stages 再平均一遍 —— 两处各算一次，阈值一改就出现「环里 62%、
文字 60%」这种对不上的画面（徽章类名双源那次踩过同型的坑）。

口径：
- 阶段 `progress` 是 0–100 的整数，`done` 状态**强制视为 100**（防止
  「状态已done、进度还挂 80」这种自相矛盾的数据被画出来）；
- 计划进度 = 所有活跃阶段的算术平均，四舍五入到整数；
- 一个阶段都没有的计划进度是 0，不是 null（前端画环不用再判空）。

## 删除一律软删

`plans.archived_at` / `stages.archived_at`：归档时间非空即视为不存在。
为什么不像笔记那样硬删：计划是协作物，删错一个阶段是常事（拖错了、复制多了一份），
而「删了能不能找回」在多人场景里比「表干净」重要得多。审计里记的是删除动作 +
被删对象的标题快照，即使库里后来又恢复，也能对上账。

## 本层不做权限判断

权限在 `caps.py`（角色能力）+ `chat_acl`（成员/停用）里判，调用方先确认身份。
本层保证的是「每条 SQL 都带 vault_id」—— 不给「忘了加 WHERE 就跨库」留机会。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .paths import ensure_import_path

# 库根先进 sys.path，下一行才 import 得到 db_paths（它住在库根，不在 backend 包内）
ensure_import_path()

import db_paths  # noqa: E402

# 库文件名单独成常量：本模块唯一的「路径知识」，别在别处再拼一次
PLAN_DB_NAME = "plan.sqlite3"

MAX_TITLE = 200
MAX_DESC = 2000
MAX_BODY = 4000
MAX_STAGES = 60
MAX_PAGE = 100

# 阶段状态白名单。收成元组而不是校验正则：前端拿到未知状态会渲染成灰色空块，
# 与其让脏数据进库，不如在入口就拒（同 chat_db.KINDS 的理由）。
STAGE_STATUS = ("todo", "doing", "done", "blocked")
PLAN_STATUS = ("active", "done", "paused")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vault_id TEXT NOT NULL,
  title TEXT NOT NULL,
  descr TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  owner TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_vault ON plans(vault_id, id DESC);

CREATE TABLE IF NOT EXISTS stages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL,
  vault_id TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'todo',
  progress INTEGER NOT NULL DEFAULT 0,
  assignee TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  start_date TEXT NOT NULL DEFAULT '',
  due_date TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_plan ON stages(plan_id, seq);
CREATE INDEX IF NOT EXISTS idx_stage_vault ON stages(vault_id, id DESC);

CREATE TABLE IF NOT EXISTS plan_comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL,
  stage_id INTEGER,
  vault_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  revoked_at TEXT,
  revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_pc_plan ON plan_comments(plan_id, id DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan_db_path():
    """计划库路径（唯一出口）。"""
    return db_paths.db_dir() / PLAN_DB_NAME


def connect() -> sqlite3.Connection:
    path = plan_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass  # 只读文件系统等：退回默认日志模式，写入仍可用
    return con


_READY = False


def init() -> None:
    """建表（幂等）。服务启动调一次，写路径也会兜一次。"""
    global _READY
    if _READY:
        return
    with connect() as con:
        con.executescript(_SCHEMA)
        con.commit()
    _READY = True


def reset_cache() -> None:
    """测试用：忘掉「已建表」状态，下次写入重新 init。"""
    global _READY
    _READY = False


# ---------------------------------------------------------------------------
# 计算口径（唯一出口）
# ---------------------------------------------------------------------------


def stage_weight(stage: dict[str, Any]) -> int:
    """阶段的实际进度：done 强制 100，其余取 progress 并夹到 0–100。"""
    if str(stage.get("status") or "") == "done":
        return 100
    try:
        v = int(stage.get("progress") or 0)
    except (TypeError, ValueError):
        v = 0
    return max(0, min(100, v))


def plan_progress(stages: Iterable[dict[str, Any]]) -> int:
    """计划总进度 = 活跃阶段（未归档）进度的算术平均，四舍五入。

    没有阶段 → 0（不是 null）：前端画环不必再判空。
    """
    rows = [s for s in stages if not s.get("archived_at")]
    if not rows:
        return 0
    total = sum(stage_weight(s) for s in rows)
    return int(round(total / len(rows)))


def _stage_status_counts(stages: list[dict[str, Any]]) -> dict[str, int]:
    out = {k: 0 for k in STAGE_STATUS}
    for s in stages:
        st = str(s.get("status") or "todo")
        if st in out:
            out[st] += 1
    return out


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def create_plan(
    vault_id: str, title: str, owner: str, *, descr: str = "", status: str = "active"
) -> dict[str, Any]:
    """建计划。标题必填；状态白名单；返回落库后的完整行（带空 stages 与进度）。"""
    name = (title or "").strip()[:MAX_TITLE]
    if not name:
        raise ValueError("计划标题不能为空")
    if status not in PLAN_STATUS:
        raise ValueError(f"不支持的计划状态：{status}")
    now = _now()
    init()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO plans(vault_id,title,descr,status,owner,created,updated) VALUES(?,?,?,?,?,?,?)",
            (vault_id, name, (descr or "")[:MAX_DESC], status, owner, now, now),
        )
        con.commit()
        row = con.execute("SELECT * FROM plans WHERE id=?", (cur.lastrowid,)).fetchone()
    return _plan_out(row, [])


# 允许被外部改的列（白名单）。不白名单 = 前端传个 `owner` 就能把计划挂到别人名下。
_PLAN_EDITABLE = {"title", "descr", "status"}
_STAGE_EDITABLE = {"title", "status", "progress", "assignee", "note", "start_date", "due_date"}


def update_plan(plan_id: int, vault_id: str, **fields: Any) -> dict[str, Any]:
    """改计划的标题/说明/状态。返回更新后的完整行（带 stages 与进度）。"""
    patch = {k: v for k, v in fields.items() if k in _PLAN_EDITABLE}
    if not patch:
        raise ValueError("没有要改的字段")
    if "title" in patch:
        patch["title"] = str(patch["title"] or "").strip()[:MAX_TITLE]
        if not patch["title"]:
            raise ValueError("计划标题不能为空")
    if "status" in patch and patch["status"] not in PLAN_STATUS:
        raise ValueError(f"不支持的计划状态：{patch['status']}")
    if "descr" in patch:
        patch["descr"] = str(patch["descr"] or "")[:MAX_DESC]
    patch["updated"] = _now()
    sets = ",".join(f"{k}=?" for k in patch)
    init()
    with connect() as con:
        cur = con.execute(
            f"UPDATE plans SET {sets} WHERE id=? AND vault_id=? AND archived_at IS NULL",
            (*patch.values(), int(plan_id), vault_id),
        )
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("计划不存在或已归档")
        row = con.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),)).fetchone()
    return _plan_out(row, _stages_of(int(plan_id), vault_id))


def archive_plan(plan_id: int, vault_id: str) -> dict[str, Any]:
    """归档计划（软删）。已归档再归档 = 幂等返回，不报错。"""
    init()
    now = _now()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM plans WHERE id=? AND vault_id=?", (int(plan_id), vault_id)
        ).fetchone()
        if row is None:
            raise ValueError("计划不存在")
        con.execute(
            "UPDATE plans SET archived_at=?, updated=? WHERE id=? AND archived_at IS NULL",
            (now, now, int(plan_id)),
        )
        con.commit()
        row = con.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),)).fetchone()
    return dict(row)


def create_stage(
    plan_id: int,
    vault_id: str,
    title: str,
    *,
    status: str = "todo",
    progress: int = 0,
    assignee: str = "",
    note: str = "",
    start_date: str = "",
    due_date: str = "",
) -> dict[str, Any]:
    """建阶段。seq 自动排在末尾，不信任前端传的顺序。"""
    name = (title or "").strip()[:MAX_TITLE]
    if not name:
        raise ValueError("阶段标题不能为空")
    if status not in STAGE_STATUS:
        raise ValueError(f"不支持的阶段状态：{status}")
    init()
    now = _now()
    with connect() as con:
        plan = con.execute(
            "SELECT id FROM plans WHERE id=? AND vault_id=? AND archived_at IS NULL",
            (int(plan_id), vault_id),
        ).fetchone()
        if not plan:
            raise ValueError("计划不存在或已归档")
        n = con.execute(
            "SELECT COUNT(*) FROM stages WHERE plan_id=? AND archived_at IS NULL", (int(plan_id),)
        ).fetchone()[0]
        if int(n) >= MAX_STAGES:
            raise ValueError(f"一个计划最多 {MAX_STAGES} 个阶段")
        seq = con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM stages WHERE plan_id=?", (int(plan_id),)
        ).fetchone()[0]
        cur = con.execute(
            "INSERT INTO stages(plan_id,vault_id,seq,title,status,progress,assignee,note,"
            "start_date,due_date,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(plan_id),
                vault_id,
                int(seq),
                name,
                status,
                max(0, min(100, int(progress or 0))),
                (assignee or "")[:80],
                (note or "")[:MAX_DESC],
                (start_date or "")[:20],
                (due_date or "")[:20],
                now,
                now,
            ),
        )
        con.commit()
        row = con.execute("SELECT * FROM stages WHERE id=?", (cur.lastrowid,)).fetchone()
        con.execute("UPDATE plans SET updated=? WHERE id=?", (now, int(plan_id)))
        con.commit()
    return dict(row)


def update_stage(stage_id: int, vault_id: str, **fields: Any) -> dict[str, Any]:
    """改阶段。状态变成 done 时进度同步写 100（一个事实源的落点）。"""
    patch = {k: v for k, v in fields.items() if k in _STAGE_EDITABLE}
    if not patch:
        raise ValueError("没有要改的字段")
    if "title" in patch:
        patch["title"] = str(patch["title"] or "").strip()[:MAX_TITLE]
        if not patch["title"]:
            raise ValueError("阶段标题不能为空")
    if "status" in patch:
        if patch["status"] not in STAGE_STATUS:
            raise ValueError(f"不支持的阶段状态：{patch['status']}")
        if patch["status"] == "done":
            patch["progress"] = 100
    if "progress" in patch:
        try:
            patch["progress"] = max(0, min(100, int(patch["progress"])))
        except (TypeError, ValueError):
            raise ValueError("进度必须是 0–100 的整数") from None
        # 反过来：进度拉满但状态还是 todo，自动标 done —— 否则会画出一条
        # 「100% 进度 + 待开始」的阶段，看的人第一反应是数据坏了。
        if patch["progress"] == 100 and "status" not in patch:
            patch["status"] = "done"
        # 手动把进度从 100 拉下来时，done 状态必须跟着退出
        if patch["progress"] < 100 and "status" not in patch:
            patch["status"] = "doing"
    for k in ("assignee", "note", "start_date", "due_date"):
        if k in patch:
            patch[k] = str(patch[k] or "")[: MAX_DESC if k == "note" else 80]
    patch["updated"] = _now()
    sets = ",".join(f"{k}=?" for k in patch)
    init()
    with connect() as con:
        cur = con.execute(
            f"UPDATE stages SET {sets} WHERE id=? AND vault_id=? AND archived_at IS NULL",
            (*patch.values(), int(stage_id), vault_id),
        )
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("阶段不存在或已归档")
        row = con.execute("SELECT * FROM stages WHERE id=?", (int(stage_id),)).fetchone()
        con.execute("UPDATE plans SET updated=? WHERE id=?", (patch["updated"], int(row["plan_id"])))
        con.commit()
    return dict(row)


def archive_stage(stage_id: int, vault_id: str) -> dict[str, Any]:
    """归档阶段（软删）。返回归档前的行（审计要标题快照）。"""
    init()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM stages WHERE id=? AND vault_id=?", (int(stage_id), vault_id)
        ).fetchone()
        if row is None:
            raise ValueError("阶段不存在")
        if row["archived_at"]:
            return dict(row)
        now = _now()
        con.execute(
            "UPDATE stages SET archived_at=?, updated=? WHERE id=?", (now, now, int(stage_id))
        )
        con.commit()
        out = dict(row)
        out["archived_at"] = now
        con.execute("UPDATE plans SET updated=? WHERE id=?", (now, int(row["plan_id"])))
        con.commit()
    return out


def reorder_stages(plan_id: int, vault_id: str, ordered_ids: list[int]) -> list[dict[str, Any]]:
    """整批重写 seq。只接受「这属于本计划」的 id，其余静默忽略。

    为什么不支持局部移动：拖拽一次就会把中间所有行的 seq 挪动，局部更新更容易
    出现「两行同 seq」这种排序在页面上随刷新而变化的情况。
    """
    init()
    want = [int(i) for i in ordered_ids][:MAX_STAGES]
    if not want:
        raise ValueError("没有要排序的阶段")
    now = _now()
    with connect() as con:
        rows = con.execute(
            "SELECT id FROM stages WHERE plan_id=? AND vault_id=? AND archived_at IS NULL",
            (int(plan_id), vault_id),
        ).fetchall()
        belong = {int(r["id"]) for r in rows}
        seq = 0
        for sid in want:
            if sid not in belong:
                continue
            seq += 1
            con.execute(
                "UPDATE stages SET seq=?, updated=? WHERE id=?", (seq, now, sid)
            )
        # 没出现在 want 里的（并发新加的）排到后面，保持可见而不是被挤掉
        for r in rows:
            if int(r["id"]) not in want:
                seq += 1
                con.execute("UPDATE stages SET seq=?, updated=? WHERE id=?", (seq, now, int(r["id"])))
        con.execute("UPDATE plans SET updated=? WHERE id=?", (now, int(plan_id)))
        con.commit()
    return _stages_of(int(plan_id), vault_id)


def add_comment(
    plan_id: int, vault_id: str, actor: str, body: str, *, stage_id: int | None = None
) -> dict[str, Any]:
    """留言。stage_id 为空 = 计划级留言。"""
    text = (body or "").strip()[:MAX_BODY]
    if not text:
        raise ValueError("留言不能为空")
    init()
    now = _now()
    with connect() as con:
        plan = con.execute(
            "SELECT id FROM plans WHERE id=? AND vault_id=? AND archived_at IS NULL",
            (int(plan_id), vault_id),
        ).fetchone()
        if not plan:
            raise ValueError("计划不存在或已归档")
        if stage_id:
            hit = con.execute(
                "SELECT id FROM stages WHERE id=? AND plan_id=? AND archived_at IS NULL",
                (int(stage_id), int(plan_id)),
            ).fetchone()
            if not hit:
                raise ValueError("阶段不存在（或已归档）")
        cur = con.execute(
            "INSERT INTO plan_comments(plan_id,stage_id,vault_id,actor,body,created) "
            "VALUES(?,?,?,?,?,?)",
            (int(plan_id), int(stage_id) if stage_id else None, vault_id, actor, text, now),
        )
        con.commit()
        row = con.execute("SELECT * FROM plan_comments WHERE id=?", (cur.lastrowid,)).fetchone()
    return _comment_out(row)


def get_comment(comment_id: int, plan_id: int) -> dict[str, Any]:
    """单条留言（撤回前判「是不是自己的、什么时候发的」用）。

    为什么单独开一个出口：撤回接口原本用 `list_comments(limit=100)` 找目标，
    留言一过 100 条，第 101 条之前的就撤不回去了 —— 而且报的是「留言不存在」，
    排查时会以为是数据丢了。按 id 直查，成本 O(1)，语义也对。
    """
    init()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM plan_comments WHERE id=? AND plan_id=?",
            (int(comment_id), int(plan_id)),
        ).fetchone()
    return _comment_out(row)


def revoke_comment(comment_id: int, plan_id: int, actor: str) -> dict[str, Any]:
    """撤回留言（软撤：正文清空，行保留，审计里留痕）。

    为什么不 DELETE：与聊天同口径 —— 撤了之后「谁撤了什么」还能复核。
    """
    init()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM plan_comments WHERE id=? AND plan_id=?",
            (int(comment_id), int(plan_id)),
        ).fetchone()
        if row is None:
            raise ValueError("留言不存在")
        if row["revoked_at"]:
            return _comment_out(row)
        con.execute(
            "UPDATE plan_comments SET body='', revoked_at=?, revoked_by=? WHERE id=?",
            (_now(), actor, int(comment_id)),
        )
        con.commit()
        row = con.execute("SELECT * FROM plan_comments WHERE id=?", (int(comment_id),)).fetchone()
    return _comment_out(row)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _plan_out(row: sqlite3.Row | None, stages: list[dict[str, Any]]) -> dict[str, Any]:
    """计划的对外形态：原始列 + 阶段 + 汇总进度 + 状态计数。

    汇总在这里算一次，前端不再算 —— 见文件头「一个事实源」。
    """
    if row is None:
        return {}
    out = dict(row)
    out["stages"] = stages
    out["progress"] = plan_progress(stages)
    out["stage_counts"] = _stage_status_counts(stages)
    out["stage_total"] = len([s for s in stages if not s.get("archived_at")])
    return out


def _comment_out(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    out = dict(row)
    if out.get("revoked_at"):
        out["body"] = ""
    return out


def _stages_of(plan_id: int, vault_id: str) -> list[dict[str, Any]]:
    init()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM stages WHERE plan_id=? AND vault_id=? AND archived_at IS NULL "
            "ORDER BY seq, id",
            (int(plan_id), vault_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_plans(vault_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """本库所有计划（默认不含归档），每行带阶段与进度。

    一次把阶段全查出来再按 plan_id 分组：库内计划数十级，N+1 次查询省不下什么，
    但会让「列表里的进度和详情页的进度对不上」这种时差型 bug 有机会发生。
    """
    init()
    sql = "SELECT * FROM plans WHERE vault_id=?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    sql += " ORDER BY (status='done') ASC, updated DESC, id DESC"
    with connect() as con:
        plans = [dict(r) for r in con.execute(sql, (vault_id,)).fetchall()]
        if not plans:
            return []
        marks = ",".join("?" * len(plans))
        stages = con.execute(
            f"SELECT * FROM stages WHERE plan_id IN ({marks}) AND archived_at IS NULL ORDER BY seq, id",
            [p["id"] for p in plans],
        ).fetchall()
    by_plan: dict[int, list[dict[str, Any]]] = {}
    for s in stages:
        by_plan.setdefault(int(s["plan_id"]), []).append(dict(s))
    return [_plan_out(p, by_plan.get(int(p["id"]), [])) for p in plans]


def get_plan(plan_id: int, vault_id: str) -> dict[str, Any]:
    """单个计划（含阶段）。不存在/已归档/跨库 → 空字典（调用方转 404）。"""
    init()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM plans WHERE id=? AND vault_id=? AND archived_at IS NULL",
            (int(plan_id), vault_id),
        ).fetchone()
    if row is None:
        return {}
    return _plan_out(row, _stages_of(int(plan_id), vault_id))


def list_comments(
    plan_id: int, vault_id: str, *, before: int | None = None, limit: int = 50
) -> dict[str, Any]:
    """留言，按 id 倒序分页（游标用 id：同一秒连发两条时时间戳分页会漏）。"""
    init()
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    sql = "SELECT * FROM plan_comments WHERE plan_id=? AND vault_id=?"
    args: list[Any] = [int(plan_id), vault_id]
    if before:
        sql += " AND id<?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit + 1)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_comment_out(r) for r in reversed(rows)]
    return {
        "items": items,
        "has_more": has_more,
        "next_before": items[0]["id"] if items else None,
    }


def stats(vault_id: str) -> dict[str, Any]:
    """页面统计条：计划数 / 阶段总数 / 进行中 / 已完成 / 逾期。"""
    init()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as con:
        plans = con.execute(
            "SELECT COUNT(*) FROM plans WHERE vault_id=? AND archived_at IS NULL", (vault_id,)
        ).fetchone()[0]
        row = con.execute(
            "SELECT "
            " COUNT(*) AS total,"
            " SUM(CASE WHEN status='doing' THEN 1 ELSE 0 END) AS doing,"
            " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,"
            " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked,"
            " SUM(CASE WHEN due_date<>'' AND due_date<? AND status<>'done' THEN 1 ELSE 0 END) AS overdue"
            " FROM stages WHERE vault_id=? AND archived_at IS NULL",
            (today, vault_id),
        ).fetchone()
        comments = con.execute(
            "SELECT COUNT(*) FROM plan_comments WHERE vault_id=? AND revoked_at IS NULL",
            (vault_id,),
        ).fetchone()[0]
    return {
        "plans": int(plans or 0),
        "stages": int(row["total"] or 0) if row else 0,
        "doing": int(row["doing"] or 0) if row else 0,
        "done": int(row["done"] or 0) if row else 0,
        "blocked": int(row["blocked"] or 0) if row else 0,
        "overdue": int(row["overdue"] or 0) if row else 0,
        "comments": int(comments or 0),
        "today": today,
    }


def activity(vault_id: str, since: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """最近活动：新计划 / 阶段进展 / 新留言，合成一条按时间倒序的时间线。

    阶段 12.5 通知中心用。**为什么放在 plan_db 而不是通知模块里自己写 SQL**：
    三张表的表名与时间列属于本模块的内部知识，通知模块再认一遍，将来加一列
    就得改两处 —— 这正是「一个事实源」要挡住的事。

    参数 `since` 是 ISO8601 字符串（本库 `created`/`updated` 的统一格式，
    `timespec="seconds"`、带 `+00:00`），空串 = 不设下限。字符串比较能直接用，
    因为格式与长度恒定 —— 一旦谁改成带微秒的写法，比较就会错，所以这里不做
    「灵活解析」，宁可让格式不一致当场显形。

    全部只读，不改任何状态。已归档的计划/阶段不参与（归档 = 不存在）。
    """
    init()
    cap = max(1, min(200, int(limit)))
    since = str(since or "")
    out: list[dict[str, Any]] = []
    with connect() as con:
        for r in con.execute(
            "SELECT id, title, owner, created FROM plans"
            " WHERE vault_id=? AND archived_at IS NULL AND created>?"
            " ORDER BY created DESC LIMIT ?",
            (vault_id, since, cap),
        ):
            out.append({
                "kind": "plan",
                "ts": r["created"],
                "planId": r["id"],
                "planTitle": r["title"],
                "title": r["title"],
                "actor": r["owner"],
                "stageId": None,
                "status": "",
                "progress": None,
                "preview": "",
            })
        for r in con.execute(
            "SELECT s.id, s.title, s.status, s.progress, s.updated, s.plan_id,"
            "       p.title AS plan_title"
            "  FROM stages s JOIN plans p ON p.id = s.plan_id"
            " WHERE s.vault_id=? AND s.archived_at IS NULL AND p.archived_at IS NULL"
            "   AND s.updated>?"
            " ORDER BY s.updated DESC LIMIT ?",
            (vault_id, since, cap),
        ):
            out.append({
                "kind": "stage",
                "ts": r["updated"],
                "planId": r["plan_id"],
                "planTitle": r["plan_title"],
                "title": r["title"],
                "actor": "",
                "stageId": r["id"],
                "status": r["status"],
                # 与 plan_progress 同一口径：done 强制 100
                "progress": 100 if r["status"] == "done" else int(r["progress"] or 0),
                "preview": "",
            })
        for r in con.execute(
            "SELECT c.id, c.actor, c.body, c.created, c.stage_id, c.plan_id,"
            "       p.title AS plan_title"
            "  FROM plan_comments c JOIN plans p ON p.id = c.plan_id"
            " WHERE c.vault_id=? AND c.revoked_at IS NULL AND p.archived_at IS NULL"
            "   AND c.created>?"
            " ORDER BY c.created DESC LIMIT ?",
            (vault_id, since, cap),
        ):
            body = str(r["body"] or "").replace("\n", " ").strip()
            out.append({
                "kind": "comment",
                "ts": r["created"],
                "planId": r["plan_id"],
                "planTitle": r["plan_title"],
                "title": r["plan_title"],
                "actor": r["actor"],
                "stageId": r["stage_id"],
                "status": "",
                "progress": None,
                "preview": body[:80],
            })
    out.sort(key=lambda it: str(it.get("ts") or ""), reverse=True)
    return out[:cap]
