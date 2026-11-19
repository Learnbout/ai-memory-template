"""通知中心 —— 数据层（阶段 12.5，2026-10-25）。

## 它补的是什么洞

顶栏那个铃铛此前是个死按钮：`Badge dot` 永远亮着，点下去什么都不发生。
**一个永远亮的红点等于没有红点** —— 人两天就学会无视它。这一层给它真数据。

## 为什么单开一个库文件

和 chat / audit / plan 三次同一个理由：`sync_push` 只覆盖 `index` + `versions`。
这里还多一条特殊性 —— **已读水位是个人的、而且不该跨机同步**。放独立文件后，
「我在这台电脑上看过了」不会随笔记推送跑到服务器，服务器仍按它自己的水位算。

## 水位口径（唯一事实源）

`notice_seen(actor, kind)` = 该人在该类事件上「已看到」的最新时间戳。
未读 = 事件 ts > 水位。首次（无记录）时**把水位播种成现在**并返回空列表：
第一次打开通知面板看到一片空白，比看到 60 条历史「新消息」更接近事实。

聊天不在这里算。它有自己的 `chat_db.read_state`（按消息 id 判读），
两处口径必须一致，否则会出现「通知说 5 条、点进去只有 3 条」——
所以聊天未读一律问 `chat_db.unread_count()`，本模块只做转发。

## 本层不做权限判断

权限在 `caps` / `chat_acl`。本层只接收已确认的 `actor` 与 `vault_id`。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .paths import ensure_import_path

ensure_import_path()

import db_paths  # noqa: E402

from . import chat_db, plan_db  # noqa: E402

NOTICE_DB_NAME = "notices.sqlite3"

# 参与水位判定的动态类别。聊天不在此列（见模块注释）。
KINDS: tuple[str, ...] = ("plan", "stage", "comment")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notice_seen(
  actor TEXT NOT NULL,
  kind  TEXT NOT NULL,
  last_ts TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(actor, kind)
);
"""


def _now() -> str:
    # 与 plan_db / chat_db 同格式（seconds、带 +00:00），字符串比较才成立
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shift(ts: str, seconds: int) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def notice_db_path():
    """通知库路径（唯一出口）。"""
    return db_paths.db_dir() / NOTICE_DB_NAME


def connect() -> sqlite3.Connection:
    path = notice_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return con


_READY = False


def init() -> None:
    global _READY
    if _READY:
        return
    with connect() as con:
        con.executescript(_SCHEMA)
        con.commit()
    _READY = True


def reset_cache() -> None:
    """测试用：忘掉「已建表」状态。"""
    global _READY
    _READY = False


# ---------------------------------------------------------------------------
# 水位
# ---------------------------------------------------------------------------


def seen_map(actor: str) -> dict[str, str]:
    """该人各类事件的已读水位。没记录 = 该 kind 尚无水位。"""
    init()
    with connect() as con:
        rows = con.execute(
            "SELECT kind, last_ts FROM notice_seen WHERE actor=?", (str(actor),)
        ).fetchall()
    return {str(r["kind"]): str(r["last_ts"] or "") for r in rows}


def mark_seen(actor: str, kinds: Iterable[str] | None = None, ts: str = "") -> dict[str, str]:
    """把水位推到现在（或指定时间）。只前进，不回退。

    为什么「只前进」：两个标签页同时开着时，后点的那个若是回退，前一个刚看过的
    东西会重新变未读 —— 用户会以为系统在乱跳。
    """
    init()
    want = [k for k in (kinds if kinds is not None else KINDS) if k in KINDS]
    now = ts or _now()
    old = seen_map(actor)
    with connect() as con:
        for k in want:
            prev = old.get(k, "")
            new = now if now > prev else prev
            con.execute(
                "INSERT INTO notice_seen(actor,kind,last_ts,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(actor,kind) DO UPDATE SET last_ts=excluded.last_ts, updated=excluded.updated",
                (str(actor), k, new, _now()),
            )
        con.commit()
    return seen_map(actor)


def _seed_missing(actor: str) -> dict[str, str]:
    """首次访问：给缺水位的 kind 播种成现在，返回补完后的水位表。

    播种而不是「缺就全算新」是刻意的：若按「缺 = 全部历史都算新」，
    主人第一次点开铃铛会看到几十条「新项目/新留言」，全是三天前的旧事。
    通知的价值在「相对上次」，不在「历史总量」——后者是页面该干的事。

    **为什么种子要往前退 1 秒**：本库时间戳是秒级（`timespec="seconds"`），
    比较用 `ts > 水位`。若种子取「此刻」，那么同一秒内发生的事件算不进未读 ——
    表现为「刚建完计划，铃铛没反应」。退 1 秒的代价是种子前 1 秒的旧事件可能
    多显示一次，而无害；漏掉新事件则会让人当场认定这功能坏了。
    """
    cur = seen_map(actor)
    missing = [k for k in KINDS if k not in cur]
    if missing:
        cur = mark_seen(actor, missing, ts=_shift(_now(), -1))
    return cur


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def feed(
    vault_id: str,
    actor: str,
    *,
    can_chat: bool = False,
    limit: int = 40,
) -> dict[str, Any]:
    """通知面板一次拉齐：未读计数 + 条目 + 首次标记。

    计数与条目同时算，是为了让「顶部写 3 条、下面只列 2 条」这种不一致不可能发生
    （同一次查询的同一份数据）。条目按类别各自与水位比，取并集再统一截断。
    """
    init()
    cap = max(1, min(100, int(limit)))

    before = seen_map(actor)
    first = any(k not in before for k in KINDS)
    marks = _seed_missing(actor)

    rows = plan_db.activity(vault_id, "", cap * 2) if vault_id else []
    items: list[dict[str, Any]] = []
    counts = {k: 0 for k in KINDS}
    for it in rows:
        kind = str(it.get("kind") or "")
        if kind not in KINDS:
            continue
        ts = str(it.get("ts") or "")
        if ts > marks.get(kind, ""):
            counts[kind] += 1
            if len(items) < cap:
                items.append(it)

    chat_unread = 0
    if can_chat and vault_id:
        try:
            chat_unread = int(chat_db.unread_count(vault_id, actor) or 0)
        except Exception:  # noqa: BLE001 —— 聊天库读不出来不该拖垮通知面板
            chat_unread = 0

    total = chat_unread + sum(counts.values())
    return {
        "ok": True,
        "first": first,
        "unread": {"total": total, "chat": chat_unread, **counts},
        "items": items,
        "vault_id": vault_id,
        "role_actor": actor,
    }


def clear(actor: str, vault_id: str, *, can_chat: bool = False) -> dict[str, Any]:
    """「全部已读」：三类水位推到现在；聊天水位推到最新消息 id。

    聊天用 `mark_read(last_id)`（它按 id 判，不按时间），所以这里要读一次最新 id。
    两个动作都成功才算清完 —— 只清一半的话红点会「消了又亮」。
    """
    marks = mark_seen(actor)
    chat_cleared = False
    if can_chat and vault_id:
        try:
            last_id = 0
            with chat_db.connect() as con:
                row = con.execute(
                    "SELECT MAX(id) FROM messages WHERE vault_id=?", (vault_id,)
                ).fetchone()
                last_id = int(row[0] or 0) if row else 0
            if last_id:
                chat_db.mark_read(vault_id, actor, last_id)
            chat_cleared = True
        except Exception:  # noqa: BLE001
            chat_cleared = False
    return {"ok": True, "seen": marks, "chat_cleared": chat_cleared}
