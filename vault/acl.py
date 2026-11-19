"""访问控制（阶段 6，2026-10-23）：每条笔记属于哪个库、谁能看哪个等级。

## 一句话规则

**标签打在笔记上（vault_id + grade），权限打在库成员上（vault_members.role +
max_grade）。** 查询时先看你是谁 → 查出你被授权的库与最高可见等级 → 只放行
「库对得上、等级够得着」的行。单篇级别的授权**明确不做**：库规模一大，
按篇授权表会长成一团乱麻，单人维护收不住。

## 三条硬边界

1. **auth 未开启（本地 stdio / 脚本）＝ 全权**。这不是后门，是本地单人环境的
   正确定义 —— 库就在自己机器上，加权限只是自欺。服务器上 `--require-token`
   开着，这里就会真的开始拦。
2. **认不出身份就什么也看不见**（fail-closed）。令牌里的 `vault_id` 声称进了
   某库，但 `vault_members` 里没有对应有效成员行 → 一律拒绝。令牌是签发时的
   快照，成员被撤销后旧票不该还能读 —— 这就是「服务端必须再查一次」的意思。
3. **`max_grade` 缺省按角色给**：owner→S，editor→A，viewer→B。管理员可以逐人
   调高调低（「可修改的能不能看重要的」就是调这一列）。

## 为什么过滤放在 Python 而不是拼 SQL

`vault_db.rows()` 是全库读路径的唯一咽喉（files / load / _snapshot 都从它出发），
在这一处过滤，35 个上层调用点一行不用改。代价是「多取了几十行再丢掉」——
142 行的库完全无所谓，拼 WHERE 反而要处理「老库还没有这两列」的分支。
"""
from __future__ import annotations

import sqlite3

import note_grade

# 主记忆库的 vault_id（与服务器一致，服务器 2026-10-23 实测）。
# 本地 vaults 表目前是 0 行，所以不能在启动时「查表取第一个」——查不到。
DEFAULT_VAULT_ID = "v_72e8dda6ac90427b"

# 角色 → 默认最高可见等级。管理员逐人改的是 vault_members.max_grade。
ROLE_DEFAULT_GRADE = {"owner": "S", "editor": "A", "viewer": "B"}


def _db_path():
    import db_paths

    return db_paths.index_db()


def auth_enabled() -> bool:
    try:
        import mcp_auth

        return bool(mcp_auth.is_enabled())
    except Exception:  # noqa: BLE001 —— 兼容层异常不该让读路径整体挂掉
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def identity() -> dict | None:
    """当前请求的身份。None = 全权（本地直连或 auth 未开）。

    返回 {"user_id","subject","vault_id","role","max_grade"}；认不出成员关系
    时 role 为空串 —— 调用方据此 fail-closed。
    """
    if not auth_enabled():
        return None
    try:
        import mcp_auth

        claims = mcp_auth.current_identity() or {}
    except Exception:  # noqa: BLE001
        claims = {}
    user_id = str(claims.get("user_id") or claims.get("sub") or "")
    vault_id = str(claims.get("vault_id") or DEFAULT_VAULT_ID)
    role, max_grade = "", ""
    if user_id:
        role, max_grade = _member(user_id, vault_id)
    return {
        "user_id": user_id,
        "subject": str(claims.get("sub") or user_id),
        "vault_id": vault_id,
        "role": role,
        "max_grade": max_grade,
    }


def _member(user_id: str, vault_id: str) -> tuple[str, str]:
    db = _db_path()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5.0)
    except sqlite3.Error:
        return "", ""
    try:
        cols = _columns(conn, "vault_members")
        if not cols:
            return "", ""
        # 阶段 7：成员行还在 ≠ 能进。票刻意**不吊销**（启用后原票继续可用、agent
        # 配置不用改），所以「停用能否立刻生效」全看这一处实时判定 —— 把 users.status
        # 一起查出来，非 active 就当没身份（fail-closed，与整份 acl 的规矩一致）。
        status_sel = "u.status" if "status" in _columns(conn, "users") else "''"
        row = conn.execute(
            "SELECT vm.role, %s, %s FROM vault_members vm "
            "LEFT JOIN users u ON u.user_id = vm.user_id "
            "WHERE vm.user_id=? AND vm.vault_id=? "
            "AND vm.revoked_at IS NULL"
            % ("vm.max_grade" if "max_grade" in cols else "''", status_sel),
            (user_id, vault_id),
        ).fetchone()
    except sqlite3.Error:
        return "", ""
    finally:
        conn.close()
    if not row:
        return "", ""
    if str(row[2] or "") != "active":
        return "", ""
    role = str(row[0] or "")
    grade = str(row[1] or ROLE_DEFAULT_GRADE.get(role, "B")).upper()
    if grade not in note_grade.GRADE_RANK:
        grade = ROLE_DEFAULT_GRADE.get(role, "B")
    return role, grade


def _user_status(user_id: str) -> str:
    """账号状态（阶段 7，只给「读不到时怎么说清楚」用）。

    `_member` 里已经把非 active 一律当无身份处理，但那样报错会退化成
    「你不在任何知识库的成员名单里」—— 而被停用根本不是「没授权」，是
    「授权还在、人被冻了」。两种结论对应的下一步动作完全不同（一个要找
    管理员加授权，一个要找管理员启用账号），所以这里单独查一次。
    """
    db = _db_path()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        if "status" not in _columns(conn, "users"):
            return ""
        row = conn.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return str(row[0] or "") if row else ""


def is_admin() -> bool:
    """本库的管理员 = owner。auth 未开启时也算（本地全权）。"""
    who = identity()
    if who is None:
        return True
    return who.get("role") == "owner"


def visible_grades() -> str:
    who = identity()
    if who is None:
        return "S"
    return who.get("max_grade") or ""


def can_read(vault_id: str, grade: str) -> bool:
    who = identity()
    if who is None:
        return True
    if not who.get("role"):
        return False
    if str(vault_id or DEFAULT_VAULT_ID) != str(who.get("vault_id") or DEFAULT_VAULT_ID):
        return False
    return note_grade.rank(grade) <= note_grade.rank(who.get("max_grade"))


def can_write() -> bool:
    """能改 = owner 或 editor。viewer 一律拒绝写。"""
    who = identity()
    if who is None:
        return True
    return who.get("role") in {"owner", "editor"}


def filter_rows(rows):
    """按可见性过滤 notes 行（rows() 的输出）。无身份时原样返回。"""
    who = identity()
    if who is None:
        return rows
    if not who.get("role"):
        return []
    vault = str(who.get("vault_id") or DEFAULT_VAULT_ID)
    limit = note_grade.rank(who.get("max_grade"))
    out = []
    for r in rows:
        keys = r.keys() if hasattr(r, "keys") else []
        vid = str(r["vault_id"]) if "vault_id" in keys and r["vault_id"] else DEFAULT_VAULT_ID
        if vid != vault:
            continue
        grade = str(r["grade"]) if "grade" in keys and r["grade"] else note_grade.DEFAULT_GRADE
        if note_grade.rank(grade) <= limit:
            out.append(r)
    return out


def explain_denied(title: str) -> str:
    """读不到时给一句人话：是不存在，还是等级不够/不是这个库。

    「读不到」和「无权读」对用户是两种完全不同的结论，含糊过去会让上层
    误判成「库里没有这条」，进而编造内容 —— 所以这里必须查证后再说。
    """
    db = _db_path()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        cols = _columns(conn, "notes")
        row = conn.execute(
            "SELECT %s, %s FROM notes WHERE title=?"
            % ("vault_id" if "vault_id" in cols else "''",
               "grade" if "grade" in cols else "''"),
            (title,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if not row:
        return ""
    vid = str(row[0] or DEFAULT_VAULT_ID)
    grade = str(row[1] or note_grade.DEFAULT_GRADE).upper()
    who = identity()
    if who is None:
        return ""
    # 顺序有意如此：先判「这篇不属于你能进的库」，再判「你根本不在任何库」。
    # 反过来的话，跨库读会报「你不在任何知识库」，而实际上你只是**没这个库**的
    # 权限 —— 两种结论对排查完全是两回事（前者要加授权，后者是令牌绑错了库）。
    if vid != str(who.get("vault_id") or DEFAULT_VAULT_ID):
        return "无权访问：%s 属于另一个知识库（%s），当前令牌只授权了 %s。" % (
            title, vid, who.get("vault_id"))
    if not who.get("role"):
        if _user_status(str(who.get("user_id") or "")) == "disabled":
            return "无权访问：账号 %s 已被管理员停用（%s）。请联系管理员启用后再试。" % (
                who.get("subject") or who.get("user_id"), title)
        return "无权访问：当前身份不在任何知识库的有效成员名单里（%s）。" % title
    return "无权访问：%s 的等级是 %s（%s），当前身份最高只能看 %s 级。" % (
        title, grade, note_grade.GRADE_DESC.get(grade, ""), who.get("max_grade"))
