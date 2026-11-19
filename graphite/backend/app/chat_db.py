"""库内聊天 —— 数据层（阶段 8，2026-10-23）。

## 为什么另开一个库文件，不塞进 index.sqlite3

三条理由，每条单独都足以否决同库：

1. **服务器那份 index.sqlite3 是只读镜像**（阶段 17）。`sync_push.py` 会整文件
   覆盖它 —— 消息混进去，主人一次「刷新服务器」就把全部聊天记录抹了。
   审计库 (`audit.sqlite3`) 当初就是同一个理由分出去的。
2. **路线图已裁决「消息与笔记不同表」**。笔记低频、有版本、吃 FTS；消息高频、
   无版本、要分页。混表会把 2026-10-09 那套分词降噪的成果搞坏。
3. **删消息 vs 删笔记的语义不同**。笔记删除要留版历史；消息撤回只是打个
   `revoked_at`（软撤回），两者混在一张表里，改一个需求就要动另一边的约束。

所以：`数据库/chat.sqlite3`，只放会话数据，不参与 `sync_all` / `sync_push`。

## 频道模型：频道 = 知识库

不建 `channels` 表。一个库一个频道，`vault_id` 就是频道 id —— 「零新增优先」：
现在只有一个主库，建表也只是把 `vaults` 复制一份。将来真要「一个库多个话题」，
再补一张 `topics(vault_id, topic_id, name)`，消息表加一列 `topic_id` 即可，
不需要改现有数据结构（`vault_id` 已经是第一索引列）。

## 可见性

本层**不做权限判断**（那是 `chat_acl.py` 的事）。调用方必须先确认身份，
本层只保证「按 vault_id 隔离」这件事在 SQL 里做死 —— 每条读写都带 vault_id，
不给「忘了加 WHERE」留任何机会。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .paths import ensure_import_path

# 库根先进 sys.path，下一行才 import 得到 db_paths（它住在库根，不在 backend 包内）
ensure_import_path()

import db_paths  # noqa: E402

# 库文件名单独成常量：它是本模块唯一的「路径知识」，
# 别在别处再拼一次 chat.sqlite3（单源于路径解析的规约见 paths.py 顶部）。
CHAT_DB_NAME = "chat.sqlite3"

# 正文长度上限。为什么不是无限：一条消息 1 MB 会把前端渲染拖死，
# 而聊天的用途就是几十个字。要贴长文就贴笔记引用（note 类型）。
MAX_BODY = 4000
# payload（图片元信息 / 笔记引用 / 链接卡片）上限，防止塞 base64 进 JSON
MAX_PAYLOAD = 8000
# 单次分页上限
MAX_PAGE = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vault_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  sender_user_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'text',
  body TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '',
  reply_to INTEGER,
  created TEXT NOT NULL,
  edited_at TEXT,
  revoked_at TEXT,
  revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_vault ON messages(vault_id, id DESC);

CREATE TABLE IF NOT EXISTS reactions(
  message_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  emoji TEXT NOT NULL,
  created TEXT NOT NULL,
  PRIMARY KEY(message_id, actor, emoji)
);

CREATE TABLE IF NOT EXISTS read_state(
  vault_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  last_read_id INTEGER NOT NULL DEFAULT 0,
  updated TEXT NOT NULL,
  PRIMARY KEY(vault_id, actor)
);

CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY,
  message_id INTEGER,
  name TEXT NOT NULL,
  mime TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  rel_path TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_message ON attachments(message_id);
"""

# 允许的消息类型。白名单而不是校验正则：前端拿到未知 kind 会渲染成空白，
# 与其让脏数据进库，不如在入口就拒。
KINDS = ("text", "image", "sticker", "note", "link", "system")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chat_db_path():
    """聊天库路径（唯一出口）。"""
    return db_paths.db_dir() / CHAT_DB_NAME


def connect() -> sqlite3.Connection:
    path = chat_db_path()
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
    """建表（幂等）。服务启动时调一次，写路径也会兜一次。"""
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
# 写
# ---------------------------------------------------------------------------


def send(
    vault_id: str,
    sender: str,
    *,
    kind: str = "text",
    body: str = "",
    payload: dict[str, Any] | None = None,
    reply_to: int | None = None,
    sender_user_id: str = "",
) -> dict[str, Any]:
    """发一条消息。返回落库后的完整行。"""
    if kind not in KINDS:
        raise ValueError(f"不支持的消息类型：{kind}")
    text = (body or "")[:MAX_BODY]
    if kind == "text" and not text.strip():
        raise ValueError("空消息不能发")
    packed = ""
    if payload:
        packed = json.dumps(payload, ensure_ascii=False)[:MAX_PAYLOAD]
    init()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO messages(vault_id,sender,sender_user_id,kind,body,payload,reply_to,created) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (vault_id, sender, sender_user_id, kind, text, packed, reply_to, _now()),
        )
        con.commit()
        row = con.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return _with_reactions(row)


def set_message_attachment(message_id: int, attachment_id: str) -> None:
    """把已上传的附件挂到消息上（上传先于发送，见 chat_api 的上传约定）。"""
    with connect() as con:
        con.execute("UPDATE attachments SET message_id=? WHERE id=?", (message_id, attachment_id))
        con.commit()


def toggle_reaction(message_id: int, actor: str, emoji: str) -> dict[str, Any]:
    """点一次加、再点一次撤。返回该消息的最新表情统计。"""
    emoji = (emoji or "").strip()[:16]
    if not emoji:
        raise ValueError("表情不能为空")
    init()
    with connect() as con:
        hit = con.execute(
            "SELECT 1 FROM reactions WHERE message_id=? AND actor=? AND emoji=?",
            (message_id, actor, emoji),
        ).fetchone()
        if hit:
            con.execute(
                "DELETE FROM reactions WHERE message_id=? AND actor=? AND emoji=?",
                (message_id, actor, emoji),
            )
        else:
            con.execute(
                "INSERT INTO reactions(message_id,actor,emoji,created) VALUES(?,?,?,?)",
                (message_id, actor, emoji, _now()),
            )
        con.commit()
    return {"message_id": message_id, "reactions": reactions_of([message_id]).get(message_id, [])}


def revoke(message_id: int, actor: str) -> dict[str, Any]:
    """软撤回：正文清空、记 revoked_at/by，行本身保留。

    为什么不 DELETE：路线图阶段 8 的验收写着「撤回保留审计痕迹」。硬删之后
    审计里那条「谁撤了什么」就成了孤证 —— 出事时无法复核撤的到底是哪条。
    """
    init()
    with connect() as con:
        row = con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("消息不存在")
        if row["revoked_at"]:
            return _with_reactions(row)
        con.execute(
            "UPDATE messages SET body='', payload='', revoked_at=?, revoked_by=? WHERE id=?",
            (_now(), actor, message_id),
        )
        con.commit()
        row = con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    return _with_reactions(row)


def mark_read(vault_id: str, actor: str, last_id: int) -> None:
    init()
    with connect() as con:
        con.execute(
            "INSERT INTO read_state(vault_id,actor,last_read_id,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(vault_id,actor) DO UPDATE SET last_read_id=excluded.last_read_id, updated=excluded.updated",
            (vault_id, actor, int(last_id), _now()),
        )
        con.commit()


def add_attachment(
    att_id: str, name: str, mime: str, size: int, rel_path: str, uploader: str
) -> dict[str, Any]:
    init()
    with connect() as con:
        con.execute(
            "INSERT INTO attachments(id,message_id,name,mime,bytes,rel_path,uploaded_by,created) "
            "VALUES(?,NULL,?,?,?,?,?,?)",
            (att_id, name, mime, size, rel_path, uploader, _now()),
        )
        con.commit()
        row = con.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    """行 → 字典，payload 顺手解成对象。撤回的消息不给正文。"""
    if row is None:
        return {}
    out = dict(row)
    payload = out.pop("payload", "") or ""
    if payload:
        try:
            out["payload"] = json.loads(payload)
        except json.JSONDecodeError:
            out["payload"] = {}
    else:
        out["payload"] = {}
    if out.get("revoked_at"):
        out["body"] = ""
        out["payload"] = {}
    return out


def _with_reactions(row: sqlite3.Row | None) -> dict[str, Any]:
    """单条消息的**对外完整形态**（与 page() 的 items 同构，含 reactions）。

    为什么单开一个出口：send() / revoke() 原先直接返回 _row()，形状里没有
    reactions 字段，前端把它 append 进列表后读 m.reactions.length 就崩
    （2026-10-23 真机验收：发一条消息，整个消息流变白）。
    读接口与写接口返回两种形状，就是「只有做完某个动作才崩」的经典来源 ——
    统一走这一个出口，别在写接口里手搓返回体。
    """
    out = _row(row)
    if not out:
        return {}
    out["reactions"] = reactions_of([int(out["id"])]).get(int(out["id"]), [])
    return out


def page(
    vault_id: str, *, before: int | None = None, limit: int = 40
) -> dict[str, Any]:
    """按 id 倒序取一页（前端自己反转顺序渲染）。

    游标用 id 不用时间戳：同一秒内连发两条时，时间戳分页会漏消息或重复。
    """
    init()
    limit = max(1, min(int(limit or 40), MAX_PAGE))
    sql = "SELECT * FROM messages WHERE vault_id=?"
    args: list[Any] = [vault_id]
    if before:
        sql += " AND id<?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit + 1)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_row(r) for r in reversed(rows)]
    ids = [it["id"] for it in items]
    rx = reactions_of(ids)
    for it in items:
        it["reactions"] = rx.get(it["id"], [])
    return {"items": items, "has_more": has_more, "next_before": items[0]["id"] if items else None}


def since(vault_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """补发用：`id > after_id` 的消息（升序、含 reactions）。

    SSE 断线重连时按 Last-Event-ID 补发缺口（阶段 10）。与 page() 一样
    走 idx_msg_vault 索引，成本与历史长度无关。
    """
    init()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE vault_id=? AND id>? ORDER BY id LIMIT ?",
            (vault_id, int(after_id), max(1, min(int(limit), 1000))),
        ).fetchall()
    items = [_row(r) for r in rows]
    rx = reactions_of([it["id"] for it in items])
    for it in items:
        it["reactions"] = rx.get(it["id"], [])
    return items


def get_message(message_id: int) -> dict[str, Any]:
    """单条消息的对外完整形态（含 reactions）。SSE 广播用（阶段 10）。"""
    init()
    with connect() as con:
        row = con.execute("SELECT * FROM messages WHERE id=?", (int(message_id),)).fetchone()
    return _with_reactions(row)


def reactions_of(ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    """批量取表情统计：{message_id: [{emoji, count, actors:[...]}]}"""
    id_list = [int(i) for i in ids]
    if not id_list:
        return {}
    marks = ",".join("?" * len(id_list))
    with connect() as con:
        rows = con.execute(
            f"SELECT message_id, emoji, actor FROM reactions WHERE message_id IN ({marks}) "
            "ORDER BY created",
            id_list,
        ).fetchall()
    grouped: dict[int, dict[str, list[str]]] = {}
    for r in rows:
        grouped.setdefault(int(r["message_id"]), {}).setdefault(r["emoji"], []).append(r["actor"])
    return {
        mid: [{"emoji": e, "count": len(actors), "actors": actors} for e, actors in per.items()]
        for mid, per in grouped.items()
    }


def last_read(vault_id: str, actor: str) -> int:
    init()
    with connect() as con:
        row = con.execute(
            "SELECT last_read_id FROM read_state WHERE vault_id=? AND actor=?", (vault_id, actor)
        ).fetchone()
    return int(row[0]) if row else 0


def unread_count(vault_id: str, actor: str) -> int:
    last = last_read(vault_id, actor)
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM messages WHERE vault_id=? AND id>? AND sender<>?", (vault_id, last, actor)
        ).fetchone()
    return int(row[0]) if row else 0


def stats(vault_id: str) -> dict[str, Any]:
    """频道概况：总条数 / 今日条数 / 参与者 / 表情总数。"""
    init()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM messages WHERE vault_id=?", (vault_id,)).fetchone()[0]
        today_n = con.execute(
            "SELECT COUNT(*) FROM messages WHERE vault_id=? AND created LIKE ?", (vault_id, today + "%")
        ).fetchone()[0]
        senders = con.execute(
            "SELECT sender, COUNT(*) AS n FROM messages WHERE vault_id=? GROUP BY sender ORDER BY n DESC LIMIT 12",
            (vault_id,),
        ).fetchall()
        rx = con.execute(
            "SELECT COUNT(*) FROM reactions r JOIN messages m ON m.id=r.message_id WHERE m.vault_id=?",
            (vault_id,),
        ).fetchone()[0]
    return {
        "total": int(total),
        "today": int(today_n),
        "reactors": int(rx),
        "senders": [{"sender": r["sender"], "count": int(r["n"])} for r in senders],
    }


def search(vault_id: str, keyword: str, limit: int = 50) -> list[dict[str, Any]]:
    """消息内搜索。**刻意不进笔记 FTS** —— 路线图「消息不进入笔记 FTS」的硬要求。

    LIKE 够用：单频道消息量级在千条上下，全表扫是毫秒级；真到十万条再上
    独立 FTS 表也不迟，那时也是本库自己的 FTS，不碰 notes_fts。
    """
    init()
    kw = (keyword or "").strip()
    if not kw:
        return []
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE vault_id=? AND revoked_at IS NULL AND body LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (vault_id, f"%{kw}%", limit),
        ).fetchall()
    items = [_row(r) for r in rows]
    rx = reactions_of([it["id"] for it in items])
    for it in items:
        it["reactions"] = rx.get(it["id"], [])
    return items


def attachment(att_id: str) -> dict[str, Any]:
    init()
    with connect() as con:
        row = con.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    return dict(row) if row else {}
