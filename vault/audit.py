"""审计日志（阶段 7，2026-10-23）：谁、什么时候、对谁、做了什么。

## 为什么不和业务库同文件

`index.sqlite3` 会被 `sync_push.py` 整文件覆盖（服务器是只读镜像）。审计记录
混进去，一次推送就抹干净 —— 出事时最需要查的那份数据，恰好最容易被覆盖。
所以审计单独落 `数据库/audit.sqlite3`（见 `db_paths.audit_db()`）。

## 三条硬边界

1. **只追加。** 库内挂了 `audit_no_update` / `audit_no_delete` 两个触发器，
   连管理员也改不了、删不掉。否则「有日志」等于「随时能擦掉日志」，等于没有。
2. **不记敏感原文。** `detail` 里出现 password / token / secret / key / salt
   这类键，值一律替换成 `***`；值本身以 `eyJ` 开头（JWT 形态）也替换。审计
   要能回答「谁改了什么」，不需要能回答「凭据是什么」。
3. **写失败不阻断业务，但必须吵。** 账号操作不该因为审计盘满而全站 500；
   可静默失败又等于没审计，所以失败一律打 stderr（服务器上会进 journal），
   由 `record()` 返回 False 让调用方有机会把失败再往上抛。

## 多进程

gate(8766) / mcp(8770) / graphite(8788) 三个进程都会写这个文件。SQLite 用
WAL + busy_timeout 处理并发写，单条 INSERT 是毫秒级，冲突概率极低。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Any

import db_paths

_LOCK = threading.Lock()
_READY = False

# 出现这些键名就脱敏。用「包含」而不是「等于」：access_token / token_b64
# 这类变体都得兜住，漏一个就等于把凭据写进了永久日志。
_SENSITIVE_PARTS = ("password", "passwd", "token", "secret", "salt", "hash", "key")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  actor_user_id TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  vault_id TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target, ts DESC);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = db_paths.audit_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass  # 只读文件系统等情况：退回默认日志模式，写入仍然可用
    return con


def _ensure() -> None:
    global _READY
    if _READY:
        return
    with _connect() as con:
        con.executescript(_SCHEMA)
        con.commit()
    _READY = True


def _scrub(value: Any, depth: int = 0) -> Any:
    """递归脱敏。深度上限 6：detail 是人工构造的小字典，不接外部深层结构，
    加个上限只是防止调用方误传巨型对象把这里拖死。"""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(part in key for part in _SENSITIVE_PARTS):
                out[str(k)] = "***"
            else:
                out[str(k)] = _scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and value.startswith("eyJ"):
        return "***"  # JWT 本体，无论挂在哪个键上
    return value


def record(
    actor: str,
    action: str,
    *,
    target: str = "",
    vault_id: str = "",
    detail: dict[str, Any] | None = None,
    actor_user_id: str = "",
    ip: str = "",
) -> bool:
    """写一条审计。返回 False 表示没写成（已打 stderr）。"""
    try:
        _ensure()
        payload = ""
        if detail:
            payload = json.dumps(_scrub(detail), ensure_ascii=False, sort_keys=True)[:4000]
        with _LOCK, _connect() as con:
            con.execute(
                "INSERT INTO audit_log(ts,actor,actor_user_id,action,target,vault_id,detail,ip) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_now(), str(actor or "?"), str(actor_user_id or ""), str(action or "?"),
                 str(target or ""), str(vault_id or ""), payload, str(ip or "")),
            )
            con.commit()
        return True
    except Exception as exc:  # noqa: BLE001 —— 审计失败不该让业务 500
        print(f"[audit] 写入失败 action={action} target={target}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False


def tail(limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    """最近的记录（管理页展示用）。limit 硬上限 500，防止一次拉爆页面。"""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    _ensure()
    sql = "SELECT * FROM audit_log"
    args: tuple[Any, ...] = ()
    if action:
        sql += " WHERE action=?"
        args = (str(action),)
    sql += " ORDER BY id DESC LIMIT ?"
    with _connect() as con:
        rows = [dict(r) for r in con.execute(sql, args + (limit,)).fetchall()]
    for row in rows:
        if row.get("detail"):
            try:
                row["detail"] = json.loads(row["detail"])
            except json.JSONDecodeError:
                pass
    return rows


def count(action: str | None = None) -> int:
    _ensure()
    with _connect() as con:
        if action:
            row = con.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()
        else:
            row = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 查询层（阶段 8，2026-10-23）：给「审计日志」页面用
#
# 与 `tail()` 的分工：tail 是「最近 N 条」的快速预览（管理页角落用的）；
# query 支持筛选、分页、关键字，给独立页面用。两者都只读。
# ---------------------------------------------------------------------------

# 审计动作的中文名。为什么写在这里而不是前端：文案跟着动作一起演进，
# 前端硬编码一份必然出现「后端加了新动作、页面显示原始英文 key」。
ACTION_LABELS: dict[str, str] = {
    "account.create": "创建账号",
    "account.update": "修改账号",
    "account.delete": "删除账号",
    "account.disable": "停用账号",
    "account.enable": "启用账号",
    "token.issue": "签发令牌",
    "token.revoke": "吊销令牌",
    "member.set": "设置成员",
    "member.revoke": "移除成员",
    "vault.create": "创建知识库",
    "vault.archive": "归档知识库",
    "chat.message.send": "发送消息",
    "chat.message.revoke": "撤回消息",
    "chat.image.upload": "上传图片",
    # 阶段 10（2026-10-25）：项目计划与进度
    "plan.create": "创建计划",
    "plan.update": "修改计划",
    "plan.archive": "归档计划",
    "plan.stage.create": "新建阶段",
    "plan.stage.update": "更新阶段",
    "plan.stage.archive": "删除阶段",
    "plan.stage.reorder": "调整阶段顺序",
    "plan.comment.create": "计划留言",
    "plan.comment.revoke": "撤回留言",
    "note.write": "写入笔记",
    "note.delete": "删除笔记",
    "note.read": "读取笔记",
    # 阶段 11（2026-10-25）：登录留痕与运维动作
    # 登录为什么也进审计：阶段 8 的审计只回答「谁改了数据」，「谁在什么时候
    # 试图进来」这条线一直是空的 —— 看到三条 account.delete 才发现有人在试
    # 管理员口令时，已经没法回溯他试了多少次、从哪个 IP 来。
    "auth.login": "登录成功",
    "auth.login.fail": "登录失败",
    "auth.logout": "退出登录",
    "ops.backup.create": "创建备份",
    "ops.backup.verify": "校验备份",
    "ops.backup.restore": "恢复备份",
    "ops.audit.rotate": "轮转审计库",
    "ops.token.sweep": "回收过期令牌",
}

# 高风险动作：页面默认给它们打红标（改动前先看一眼这些）
RISKY_ACTIONS = {
    "account.delete",
    "account.disable",
    "token.revoke",
    "member.revoke",
    "vault.archive",
    "note.delete",
    "chat.message.revoke",
    # 阶段 10：计划归档与阶段删除都是「看不见就再也找不回入口」的动作，
    # 给红标让人在列表里一眼看到（数据本身是软删，能恢复，但入口只有管理员知道）。
    "plan.archive",
    "plan.stage.archive",
    "plan.comment.revoke",
    # 阶段 11：恢复会把这台机器上的库换成一份旧快照（当前数据直接消失，
    # 只能靠「恢复前自动再备份一次」兜底）；轮转与批量吊销同样不可逆。
    "ops.backup.restore",
    "ops.audit.rotate",
    "ops.token.sweep",
}


def archive_files() -> list:
    """所有审计库文件：主库 + 归档（`audit_<年>.sqlite3`）。

    轮转口径见 `脚本/audit_rotate.py`：跨年时**整文件改名**，不删行 ——
    库内触发器禁止 DELETE，删行等于破掉「只追加」这条硬约束；换文件则不碰。
    因此查询必须能跨多个文件，否则轮转一次旧记录就在页面上「消失」了。
    """
    import db_paths

    base = db_paths.audit_db()
    folder = base.parent
    found = []
    if base.exists():
        found.append(base)
    if folder.is_dir():
        for p in sorted(folder.glob("audit_*.sqlite3")):
            if p.name != base.name and p not in found:
                found.append(p)
    return found


def _connect_file(path) -> sqlite3.Connection:
    con = sqlite3.connect(path.as_posix(), timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def query(
    *,
    since: str = "",
    until: str = "",
    actions: list[str] | None = None,
    actor: str = "",
    vault_id: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 40,
) -> dict:
    """跨归档文件筛选 + 分页。

    分页口径：每个文件各取 `page*page_size` 条再归并 —— 因为不知道要的那一页
    落在哪个文件里。代价是多读几倍的行数，但审计是低频页面，够用。
    """
    _ensure()
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(int(page_size), 200))
    except (TypeError, ValueError):
        page_size = 40

    where = []
    args: list = []
    if since:
        where.append("ts >= ?")
        args.append(str(since))
    if until:
        where.append("ts <= ?")
        args.append(str(until))
    if actions:
        clean = [str(a) for a in actions if a]
        if clean:
            where.append("action IN (%s)" % ",".join("?" * len(clean)))
            args.extend(clean)
    if actor:
        where.append("actor = ?")
        args.append(str(actor))
    if vault_id:
        where.append("vault_id = ?")
        args.append(str(vault_id))
    if keyword:
        where.append("(target LIKE ? OR detail LIKE ? OR actor LIKE ?)")
        kw = f"%{keyword}%"
        args.extend([kw, kw, kw])

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    need = page * page_size
    merged: list[dict] = []
    total = 0
    for path in archive_files():
        try:
            with _connect_file(path) as con:
                cnt = con.execute(f"SELECT COUNT(*) FROM audit_log{clause}", args).fetchone()[0]
                total += int(cnt or 0)
                for row in con.execute(
                    f"SELECT * FROM audit_log{clause} ORDER BY ts DESC, id DESC LIMIT ?", args + [need]
                ).fetchall():
                    merged.append(dict(row))
        except sqlite3.Error:
            continue  # 单个归档文件损坏不该让整页打不开
    merged.sort(key=lambda r: (r.get("ts") or "", r.get("id") or 0), reverse=True)
    start = (page - 1) * page_size
    items = merged[start : start + page_size]
    for row in items:
        if row.get("detail"):
            try:
                row["detail"] = json.loads(row["detail"])
            except (json.JSONDecodeError, TypeError):
                pass
        row["action_label"] = ACTION_LABELS.get(str(row.get("action") or ""), str(row.get("action") or ""))
        row["risky"] = str(row.get("action") or "") in RISKY_ACTIONS
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": start + len(items) < total,
        "files": [p.name for p in archive_files()],
    }


def action_counts() -> list[dict]:
    """各动作的条数（筛选项上的计数）。"""
    _ensure()
    agg: dict[str, int] = {}
    for path in archive_files():
        try:
            with _connect_file(path) as con:
                for row in con.execute("SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action"):
                    agg[str(row["action"])] = agg.get(str(row["action"]), 0) + int(row["n"])
        except sqlite3.Error:
            continue
    out = [
        {
            "action": k,
            "label": ACTION_LABELS.get(k, k),
            "count": v,
            "risky": k in RISKY_ACTIONS,
        }
        for k, v in agg.items()
    ]
    out.sort(key=lambda r: r["count"], reverse=True)
    return out


def stats(days: int = 7) -> dict:
    """概览数字：总数、今日、近 N 天、活跃操作者、高风险动作数。"""
    _ensure()
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    window_start = (now - timedelta(days=max(1, int(days or 7)))).isoformat(timespec="seconds")

    total = 0
    today_n = 0
    window_n = 0
    actors: dict[str, int] = {}
    risky = 0
    for path in archive_files():
        try:
            with _connect_file(path) as con:
                total += int(con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] or 0)
                today_n += int(
                    con.execute("SELECT COUNT(*) FROM audit_log WHERE ts LIKE ?", (today + "%",)).fetchone()[0] or 0
                )
                window_n += int(
                    con.execute("SELECT COUNT(*) FROM audit_log WHERE ts >= ?", (window_start,)).fetchone()[0] or 0
                )
                for row in con.execute("SELECT actor, COUNT(*) AS n FROM audit_log GROUP BY actor"):
                    actors[str(row["actor"])] = actors.get(str(row["actor"]), 0) + int(row["n"])
                marks = ",".join("?" * len(RISKY_ACTIONS)) or "''"
                risky += int(
                    con.execute(
                        f"SELECT COUNT(*) FROM audit_log WHERE action IN ({marks})", tuple(RISKY_ACTIONS)
                    ).fetchone()[0]
                    or 0
                )
        except sqlite3.Error:
            continue
    top = sorted(actors.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {
        "total": total,
        "today": today_n,
        "window": window_n,
        "windowDays": max(1, int(days or 7)),
        "risky": risky,
        "actors": [{"actor": a, "count": n} for a, n in top],
        "files": len(archive_files()),
    }
