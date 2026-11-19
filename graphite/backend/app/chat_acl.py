"""库内聊天的身份与权限（阶段 8，2026-10-23）。

## 身份从哪来

Web 侧的身份**不在本进程登录**，是 8766 网关验完 Cookie 后注入请求头
`X-AI-Memory-User-B64`（base64url 的用户名），本模块只解头，不碰口令。
这与 `routers/admin_mcp.py` 的口径完全一致 —— 两处各写一套身份解析，
迟早会出现「管理页认得出你、聊天认不出」这种鬼故事。

直连模式（本机 8787，绕过网关）没有这个头：按「本地单人」处理，
用户名固定 `local`、角色 owner。这与 `acl.auth_enabled()` 的规则同源 ——
库就在自己机器上，加权限只是自欺；服务器上开了网关就一定会带头。

## 三道闸

1. **身份闸**：解不出用户名 → 401（`require_user`）。
2. **成员闸**：`require_member` 查 `vault_members`；查不到 → 403。fail-closed：
   认不出成员就什么都看不见，不做「猜一个默认库」这种事。
3. **等级闸**：引用笔记时按 `vault_members.max_grade` 判 —— 这是「按自己的权限
   发记忆」那句话的落点。viewer 的 max_grade 是 B，他就只能引用 B/C 级笔记，
   引用 A/S 级直接被拒，且拒绝理由要能说清（不是笼统的 403）。

## 本地兜底为什么是「users 表 0 行」而不是「没带头」

`users` 表空 = 这台机器还没启用多用户（本地个人库现状：0 行）；有行 = 已启用
账号体系（服务器：4 个账号）。用数据状态判断，比用「有没有请求头」可靠：
带不带头是网络路径决定的，表里有没有人才是权限模型是否启用的事实。
"""
from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .paths import ensure_import_path, readonly_mode

# 库根先进 sys.path，下一行才 import 得到 db_paths
ensure_import_path()

import db_paths  # noqa: E402

# 等级从高到低。数字越小越敏感；比较用下标。
GRADE_ORDER = ["S", "A", "B", "C"]
DEFAULT_VAULT_ID = "v_72e8dda6ac90427b"
ROLE_DEFAULT_GRADE = {"owner": "S", "editor": "A", "viewer": "B"}

LOCAL_USER = "local"


class Denied(Exception):
    """权限不足。status 由 HTTP 层决定（401 未登录 / 403 无权限）。"""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


# 普通成员撤回自己消息的时间窗（秒）。主人 2026-10-24 定：管理员不受限、
# 可以撤任何人的；其他人只能撤自己刚发的，超时不许撤。
# 5 分钟是「发出去发现打错字/贴错图」的合理容错期，再长就等于没有撤回这回事了。
REVOKE_WINDOW_SECONDS = 300


def can_revoke(member: dict[str, Any], sender: str, created: str, me: str) -> tuple[bool, str]:
    """能不能撤这条。返回 (允许?, 拒绝理由)。

    规则口径（单一出口，前后端都以此为准）：
      - 管理员（owner）：任何人的消息都行，**没有时间限制**；
      - 其他人：只能是自己的，且发出未超过 REVOKE_WINDOW_SECONDS；
      - 别人而且超时 → 拒；别人没超时 → 也拒（时间窗只对「自己的消息」开）。

    为什么把「时间窗」只挂在自己的消息上：允许「5 分钟内撤任何人的消息」等于
    给每个人一个删别人发言的窗口，比不能撤回更糟。
    """
    if member.get("role") == "owner":
        return True, ""
    if sender != me:
        return False, "只能撤回自己的消息（管理员不受限）"
    left = REVOKE_WINDOW_SECONDS - _age_seconds(created)
    if left <= 0:
        return False, f"超过 {REVOKE_WINDOW_SECONDS // 60} 分钟了，撤不回来了（管理员不受此限）"
    return True, ""


def _age_seconds(created: str) -> float:
    """created（ISO8601，带时区）距今多少秒。解析失败按「很久以前」处理 —— 失败关闭。"""
    raw = (created or "").strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def _connect() -> sqlite3.Connection:
    # 2026-10-29（阶段 10）：只读副本上必须 ro 打开。本函数只查不写，
    # 但在 index 文件 440 + 服务降权之后，可写打开会在建连阶段就 PermissionError，
    # 而调用方（聊天权限检查）是全站高频路径 —— 那会让整个工作台 500。
    db = db_paths.index_db().as_posix()
    if readonly_mode():
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5.0)
    else:
        con = sqlite3.connect(db, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def decode_user(header_value: str | None) -> str:
    raw = header_value or ""
    if not raw:
        return ""
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8").strip()
    except (ValueError, UnicodeError):
        return ""


def require_user(header_value: str | None) -> str:
    """解请求头。带了解不出 → 401；没带 → 本地 local（见模块说明）。"""
    if not header_value:
        return LOCAL_USER
    user = decode_user(header_value)
    if not user:
        raise Denied("登录凭证无法解析，请重新登录", status=401)
    return user


def multiuser_enabled() -> bool:
    """`users` 表有没有行 = 这台机器是否已启用账号体系。"""
    with _connect() as con:
        if not _table_exists(con, "users"):
            return False
        row = con.execute("SELECT COUNT(*) FROM users").fetchone()
    return bool(row and int(row[0]) > 0)


def user_row(username: str) -> dict[str, Any] | None:
    with _connect() as con:
        if not _table_exists(con, "users"):
            return None
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def member_of(username: str, vault_id: str) -> dict[str, Any] | None:
    """查成员关系；顺带确认账号没被停用（停用账号等同非成员，与 acl._member 同口径）。"""
    with _connect() as con:
        if not _table_exists(con, "vault_members"):
            return None
        row = con.execute(
            "SELECT m.role, m.max_grade, m.user_id, u.status, u.username, u.display_name "
            "FROM vault_members m LEFT JOIN users u ON u.user_id=m.user_id "
            "WHERE m.vault_id=? AND u.username=? AND m.revoked_at IS NULL",
            (vault_id, username),
        ).fetchone()
    if not row:
        return None
    if str(row["status"] or "active") != "active":
        return None
    grade = str(row["max_grade"] or ROLE_DEFAULT_GRADE.get(str(row["role"]), "B")).upper()
    if grade not in GRADE_ORDER:
        grade = "B"
    return {
        "username": row["username"] or username,
        "user_id": row["user_id"] or "",
        "display_name": row["display_name"] or row["username"] or username,
        "role": str(row["role"] or "viewer"),
        "max_grade": grade,
    }


def require_member(username: str, vault_id: str) -> dict[str, Any]:
    """成员闸。本地单人（users 空）直接给 owner。"""
    if not multiuser_enabled():
        return {
            "username": username or LOCAL_USER,
            "user_id": "",
            "display_name": username or LOCAL_USER,
            "role": "owner",
            "max_grade": "S",
            "local": True,
        }
    member = member_of(username, vault_id)
    if not member:
        raise Denied("你不在这个知识库里，或者账号已被停用", status=403)
    return member


def require_chat_member(username: str, vault_id: str) -> dict[str, Any]:
    """聊天专用成员闸：在成员闸之上再挡一层「只读」。

    为什么只读账号连**读**聊天也不给（不是只禁发）：主人 2026-10-24 明确说
    「聊天只读是不能用的」。只读账号在网关那一层连非 GET 都 403，能发出去的
    只有自动轮询——那等于给他一个只能刷屏看的页面。与其留个半残入口，
    不如整页不给，权限语义也干净：viewer = 只能看笔记，不能参与协作频道。

    前端 `canSee()` 已经不给 /chat 入口，这一层是真正的闸（前端隐藏只是体验）。
    """
    member = require_member(username, vault_id)
    if member.get("role") == "viewer":
        raise Denied("只读账号不能使用聊天", status=403)
    return member


def visible_vaults(username: str) -> list[dict[str, Any]]:
    """我能进哪些库（含我的角色）。"""
    with _connect() as con:
        if not _table_exists(con, "vaults"):
            return []
        if not multiuser_enabled():
            rows = con.execute(
                "SELECT vault_id, name, status FROM vaults WHERE status='active' ORDER BY created_at"
            ).fetchall()
            return [
                {"vault_id": r["vault_id"], "name": r["name"], "role": "owner", "max_grade": "S"}
                for r in rows
            ] or [
                {"vault_id": DEFAULT_VAULT_ID, "name": "主库", "role": "owner", "max_grade": "S"}
            ]
        rows = con.execute(
            "SELECT v.vault_id, v.name, m.role, m.max_grade FROM vaults v "
            "JOIN vault_members m ON m.vault_id=v.vault_id "
            "JOIN users u ON u.user_id=m.user_id "
            "WHERE u.username=? AND u.status='active' AND m.revoked_at IS NULL AND v.status='active' "
            "ORDER BY v.created_at",
            (username,),
        ).fetchall()
    out = []
    for r in rows:
        grade = str(r["max_grade"] or ROLE_DEFAULT_GRADE.get(str(r["role"]), "B")).upper()
        out.append(
            {
                "vault_id": r["vault_id"],
                "name": r["name"],
                "role": str(r["role"] or "viewer"),
                "max_grade": grade if grade in GRADE_ORDER else "B",
            }
        )
    return out


def default_vault(username: str) -> str:
    """默认进哪个频道：优先主库，其次我能进的第一个；一个都进不了 → 403。"""
    vs = visible_vaults(username)
    if not vs:
        raise Denied("你没有可进入的知识库", status=403)
    for v in vs:
        if v["vault_id"] == DEFAULT_VAULT_ID:
            return v["vault_id"]
    return str(vs[0]["vault_id"])


def vault_members(vault_id: str) -> list[dict[str, Any]]:
    """频道成员（右栏用）。本地单人模式返回一个虚拟成员。"""
    if not multiuser_enabled():
        return [
            {
                "username": LOCAL_USER,
                "display_name": "本地工作台",
                "role": "owner",
                "max_grade": "S",
                "status": "active",
            }
        ]
    with _connect() as con:
        rows = con.execute(
            "SELECT u.username, u.display_name, u.status, m.role, m.max_grade "
            "FROM vault_members m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.vault_id=? AND m.revoked_at IS NULL ORDER BY m.role DESC, u.username",
            (vault_id,),
        ).fetchall()
    return [
        {
            "username": r["username"],
            "display_name": r["display_name"] or r["username"],
            "role": str(r["role"] or "viewer"),
            "max_grade": str(r["max_grade"] or ROLE_DEFAULT_GRADE.get(str(r["role"]), "B")),
            "status": str(r["status"] or "active"),
        }
        for r in rows
    ]


def grade_rank(grade: str) -> int:
    g = (grade or "C").upper()
    return GRADE_ORDER.index(g) if g in GRADE_ORDER else GRADE_ORDER.index("C")


def can_see_grade(member: dict[str, Any], grade: str) -> bool:
    """我的 max_grade 够不够看这一级。S 最高。"""
    return grade_rank(grade) >= grade_rank(str(member.get("max_grade") or "B"))


def require_grade_for_ref(member: dict[str, Any], grade: str, title: str) -> None:
    if not can_see_grade(member, grade):
        raise Denied(
            f"「{title}」是 {grade} 级笔记，你最高只能引用 "
            f"{member.get('max_grade')} 级 —— 权限不够，找管理员调 max_grade",
            status=403,
        )


def visible_note(username: str, vault_id: str, member: dict[str, Any], title: str) -> dict[str, Any] | None:
    """按标题取一篇笔记的引用信息，**同时**做等级校验。

    为什么在一次查询里做完：分两步调用最容易出现「先读了正文、后判权限」的顺序错误
    （正文已经在内存里了，判不判都泄了）。这里只回 summary，不碰 body_md。
    """
    with _connect() as con:
        row = con.execute(
            "SELECT title, rel_path, grade, type, summary, updated FROM notes WHERE title=? AND vault_id=?",
            (title, vault_id),
        ).fetchone()
    if not row:
        return None
    grade = str(row["grade"] or "B").upper()
    require_grade_for_ref(member, grade, title)
    return {
        "title": row["title"],
        "rel_path": row["rel_path"],
        "grade": grade,
        "type": row["type"],
        "summary": (row["summary"] or "")[:200],
        "updated": row["updated"],
    }


def searchable_notes(vault_id: str, member: dict[str, Any], keyword: str, limit: int = 20) -> list[dict]:
    """引用选择器的搜索结果：只回我能看的等级。"""
    kw = (keyword or "").strip()
    limit = max(1, min(int(limit or 20), 50))
    allowed = [g for g in GRADE_ORDER if can_see_grade(member, g)]
    if not allowed:
        return []
    marks = ",".join("?" * len(allowed))
    sql = (
        "SELECT title, rel_path, grade, type, summary FROM notes "
        f"WHERE vault_id=? AND grade IN ({marks})"
    )
    args: list[Any] = [vault_id, *allowed]
    if kw:
        sql += " AND (title LIKE ? OR summary LIKE ?)"
        args += [f"%{kw}%", f"%{kw}%"]
    sql += " ORDER BY updated DESC LIMIT ?"
    args.append(limit)
    with _connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [
        {
            "title": r["title"],
            "rel_path": r["rel_path"],
            "grade": r["grade"],
            "type": r["type"],
            "summary": (r["summary"] or "")[:160],
        }
        for r in rows
    ]
