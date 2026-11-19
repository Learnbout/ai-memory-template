"""阶段 5：管理员 Web 的用户、知识库成员与 MCP 令牌管理。"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import audit
import web_auth

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
DB_PATH = VAULT / "数据库" / "index.sqlite3"


def _mcp_resource() -> str:
    """MCP 入口地址：唯一事实源是 `<库根>/.workbuddy/auth.json` 的 `resource`。

    2026-10-27 修 —— 这里此前写死过一个内网 IP。公网域名上线之后，设置页「我的 MCP token」卡片里
    显示的地址还停在死 IP：用户照着抄进客户端，表现只是「连接超时」，极难归因到
    一行常量。改成与签票端 / 校验端读同一个文件，就不会再出现第二事实源。

    读不到才回落旧值 —— 登录页要能开、管理员要能进，不能因为一个展示字段炸页面。
    """
    try:
        cfg = json.loads((VAULT / ".workbuddy" / "auth.json").read_text(encoding="utf-8"))
        value = str(cfg.get("resource") or "").strip()
        if value:
            return value
    except (OSError, json.JSONDecodeError):
        pass
    # 2026-11-18：兜底不再写死 IP。服务器地址已漂四次
    # （31.50 → 5.119 → 5.129 → 10.2），再写死一个常量就是第五次踩坑。
    # 一律走 `server_host.resolve()`：env `AIM_SSH_HOST` → 缓存 → 扫网段。
    try:
        import server_host

        return "http://" + server_host.resolve(verbose=False) + ":8770/mcp"
    except Exception:  # noqa: BLE001 —— 连地址都解析不出来时宁可显示 127.0.0.1
        return "http://127.0.0.1:8770/mcp"
LEDGER_PATH = VAULT / ".workbuddy" / "tokens.json"
ISSUE_PY = VAULT / "脚本" / "issue_token.py"
ISSUE_PYTHON = Path(os.environ.get("AI_MEMORY_PYTHON") or "/opt/ai-memory/venv/bin/python")

_TOKEN_TABLE_READY = False


def _ensure_account_tokens() -> None:
    """管理员签发的 token 落一张本地表：只服务管理员页，不进 tokens.json 台账。

    台账（tokens.json）刻意只存票号与元信息、不存 token 本体；但管理员页需要
    「账号行直接看到/复制 token」，因此这里另存一份明文。信任域相同（同机同库，
    签名密钥也在这台机器上），换来的是可复查与一键复制。
    """
    global _TOKEN_TABLE_READY
    if _TOKEN_TABLE_READY:
        return
    with _connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS account_tokens("
            "jti TEXT PRIMARY KEY, user_id TEXT NOT NULL, username TEXT NOT NULL, "
            "vault_id TEXT NOT NULL, token TEXT NOT NULL, scope TEXT NOT NULL, "
            "issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, note TEXT)"
        )
        # 明文口令表：仅管理员页/设置页使用。主人 2026-10-23 明确要求「管理员必须能看到
        # 每个用户的密码」，因此这里只能存可逆值 —— 这是自托管单人环境的取舍，不是通用做法。
        con.execute(
            "CREATE TABLE IF NOT EXISTS account_secrets("
            "username TEXT PRIMARY KEY, password TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        con.commit()
    _TOKEN_TABLE_READY = True


def _account_token_rows(username: str | None = None) -> list[dict[str, Any]]:
    _ensure_account_tokens()
    sql = "SELECT * FROM account_tokens"
    args: tuple[Any, ...] = ()
    if username:
        sql += " WHERE username=?"
        args = (username,)
    sql += " ORDER BY issued_at DESC"
    with _connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def record_password(username: str, password: str) -> None:
    """把口令明文记进 account_secrets（管理员可见）。"""
    _ensure_account_tokens()
    with _connect() as con:
        con.execute(
            "INSERT INTO account_secrets(username,password,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET password=excluded.password, updated_at=excluded.updated_at",
            (username, str(password), _now_iso()),
        )
        con.commit()


def password_of(username: str) -> str:
    _ensure_account_tokens()
    with _connect() as con:
        row = con.execute("SELECT password FROM account_secrets WHERE username=?", (username,)).fetchone()
    return str(row["password"]) if row else ""


def rename_account(old: str, new: str) -> None:
    """改名后同步 users / account_tokens / account_secrets 三处，否则权限与票会挂在旧名上。"""
    _ensure_account_tokens()
    with _connect() as con:
        for table in ("users", "account_tokens", "account_secrets"):
            try:
                con.execute(f"UPDATE {table} SET username=? WHERE username=?", (new, old))
            except Exception:
                pass
        con.commit()


def _vault_role_for(web_role: str) -> str:
    return {"admin": "owner", "readonly": "viewer"}.get(web_role, "editor")


def _scope_for(web_role: str) -> str:
    return "read" if web_role == "readonly" else "readwrite"


def _active_vault_id() -> str:
    with _connect() as con:
        row = con.execute("SELECT vault_id FROM vaults WHERE status='active' ORDER BY name LIMIT 1").fetchone()
    return str(row["vault_id"]) if row else ""



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid_of(username: str) -> str:
    """用户名 → user_id（审计要记人，不能只记名字 —— 名字会被改）。"""
    name = str(username or "").strip()
    if not name:
        return ""
    try:
        with _connect() as con:
            row = con.execute("SELECT user_id FROM users WHERE username=?", (name,)).fetchone()
        return str(row["user_id"]) if row else ""
    except sqlite3.Error:
        return ""


def _audit(actor: str, action: str, *, target: str = "", vault_id: str = "",
           detail: dict[str, Any] | None = None) -> None:
    """写审计的统一入口。

    走这一层而不是各处直接调 audit.record，只为把 `actor_user_id` 补上 ——
    用户名可改（网关有改名字段），user_id 不会变，事后追责认的是后者。
    """
    audit.record(actor, action, target=target, vault_id=vault_id, detail=detail,
                 actor_user_id=_uid_of(actor))


def _connect() -> sqlite3.Connection:
    # 2026-10-29（阶段 10）：服务器副本上 index 是 440，可写打开直接 PermissionError。
    # 账号**读**（account_active / 列表）是登录与会话的必经路径，不能因此全站 500；
    # 账号**写**（停用/删号/发票）在副本上本就不该发生 —— ro 连接会让它明确失败，
    # 比被 _ro_guard_* 触发器 ABORT 更容易看懂。
    import web_store as _ws
    if _ws.readonly_mode():
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True, timeout=5.0)
    else:
        con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def account_active(username: str) -> bool:
    """账号是否可用（登录与会话判定用，阶段 7）。

    这是「停用」能不能立刻生效的开关。没有它，停用只改了库里的 status，
    而 Web 会话是无状态 Cookie —— 人要等 Cookie 到寿（最长 12 小时）才会被
    请出去，等于假锁。

    两处特意返回 True（而不是 fail-closed），理由不同：
    - **没有 users 行**：账号在 web_auth.json 里存在，说明管理员建过它，只是还
      没同步进权限目录。这时他本来就读不到任何笔记（acl 认不出成员）。拒掉
      只会让「刚建号、还没签票」的人连登录页都进不去。
    - **库读不出来**：那是另一条故障链，把所有人锁在门外不会让事情变好；
      而此时笔记本身也读不出来，不构成越权。
    """
    name = str(username or "").strip()
    if not name:
        return False
    try:
        with _connect() as con:
            row = con.execute("SELECT status FROM users WHERE username=?", (name,)).fetchone()
    except sqlite3.Error:
        return True
    return (not row) or str(row["status"]) == "active"


def is_admin(username: str) -> bool:
    configured = os.environ.get("AI_MEMORY_ADMIN_USERS", "")
    names = {x.strip() for x in configured.split(",") if x.strip()}
    try:
        data = web_auth.load_config()
        names.update(str(x).strip() for x in data.get("admin_users", []) if str(x).strip())
    except Exception:
        pass
    return username in names


def web_role(username: str) -> str:
    """Web 角色唯一出口（阶段 9，2026-10-24）。返回 admin / editor / readonly / ""。

    为什么要有它：在此之前「这人是什么角色」有三份等价实现 ——
    `admin_api._account_role`、`web_gate._user_role`、前端从 `/api/gate/me` 拿到的
    字符串。三份代码同源不同步的那天，就会出现「网关认为是 editor、后端认成
    admin」这种权限洞。现在后端（caps.role_of）与网关都从这里取。

    返回 `""` 表示**账号不存在于权限目录**（不是 editor）。调用方必须 fail-closed
    按 guest 处理 —— 把它兜底成 editor 等于给一个未知账号开任意可修改权限。
    管理员判定优先走 `is_admin`（它还会认 `AI_MEMORY_ADMIN_USERS` 环境变量）。
    """
    name = str(username or "").strip()
    if not name:
        return ""
    if is_admin(name):
        return "admin"
    try:
        cfg = web_auth.load_config()
    except Exception:  # noqa: BLE001 —— 配置读不出来不该把管理员误降权，但也不该放行
        return ""
    if name not in cfg.get("users", {}):
        return ""
    return _account_role(cfg, name)


def display_name_of(username: str) -> str:
    """展示名：库里有就用库里的，没有就回用户名。读失败不抛（展示层不该因此 500）。"""
    name = str(username or "").strip()
    if not name:
        return ""
    try:
        with _connect() as con:
            row = con.execute("SELECT display_name FROM users WHERE username=?", (name,)).fetchone()
    except sqlite3.Error:
        return name
    if not row:
        return name
    return str(row["display_name"] or name)


def _require_admin(username: str) -> None:
    if is_admin(username):
        return
    # 本地单人模式（users 表 0 行 = 未启用账号体系）：没有「账号」就谈不上「管理员」，
    # 本机主人即全权 —— 与 caps.role_of / chat_acl.require_member / acl 同一条口径。
    # 之前缺这一条的表现：Graphite 管理页在本地直连时满屏 500（PermissionError
    # 从 account_rows 里冒出来没人接）。库读不出来时按 False（fail-closed）不放行。
    if not _multiuser_enabled():
        return
    raise PermissionError("需要管理员权限")


def _multiuser_enabled() -> bool:
    """账号体系是否启用（users 表有行）。库打不开 → True（宁可误拦不可误放）。"""
    try:
        with _connect() as con:
            row = con.execute("SELECT COUNT(*) FROM users").fetchone()
        return bool(row and int(row[0]) > 0)
    except sqlite3.Error:
        return True


def _account_role(cfg: dict[str, Any], username: str) -> str:
    if username in set(cfg.get("admin_users", [])):
        return "admin"
    role = str(cfg.get("users", {}).get(username, {}).get("role") or "editor")
    return role if role in {"editor", "readonly"} else "editor"


def _norm_grade(value: str) -> str:
    """校验可见等级入参：'' = 按角色默认，S/A/B/C = 显式指定。非法显式报错。"""
    g = str(value or "").strip().upper()
    if g in ("", "AUTO"):
        return ""
    if g not in {"S", "A", "B", "C"}:
        raise ValueError("可见等级必须是 S/A/B/C 或留空（按角色默认）")
    return g


def account_rows(username: str) -> list[dict[str, Any]]:
    _require_admin(username)
    cfg = web_auth.load_config()
    _ensure_account_tokens()
    with _connect() as con:
        user_rows = {r["username"]: dict(r) for r in con.execute(
            "SELECT user_id, username, status FROM users")}
        member_rows = {}
        for r in con.execute(
            "SELECT vm.user_id, vm.role, vm.vault_id, vm.max_grade, v.name AS vault_name "
            "FROM vault_members vm JOIN vaults v ON v.vault_id=vm.vault_id "
            "WHERE vm.revoked_at IS NULL"
        ):
            member_rows[r["user_id"]] = dict(r)
        token_rows = {}
        for r in con.execute(
            "SELECT * FROM account_tokens WHERE revoked_at IS NULL ORDER BY issued_at DESC"
        ):
            rec = dict(r)
            token_rows.setdefault(rec["username"], rec)
    rows = []
    for name, record in sorted(cfg.get("users", {}).items()):
        user = user_rows.get(name) or {}
        member = member_rows.get(user.get("user_id")) or {}
        token = token_rows.get(name) or {}
        raw = str(token.get("token") or "")
        rows.append({
            "username": name,
            "role": _account_role(cfg, name),
            "note": str(record.get("note") or ""),
            "updated": str(record.get("updated") or ""),
            # 阶段 7：status 是给前端看的「能不能用」，db_status 是库里的原始值。
            # 之前 status 硬编码 "active" —— 管理页永远显示正常，停用了也看不出来，
            # 这是「有状态列但等于没有」的典型。现在按库里真实值下发。
            "status": "disabled" if str(user.get("status") or "") == "disabled" else "active",
            "user_id": str(user.get("user_id") or ""),
            "db_status": str(user.get("status") or "pending"),
            "member_role": str(member.get("role") or ""),
            # 阶段 6：'' = 按角色默认（owner=S / editor=A / viewer=B）；
            # 前端显示成「自动(S)」这类，管理页可逐人覆盖。
            "max_grade": str(member.get("max_grade") or ""),
            "vault_id": str(member.get("vault_id") or ""),
            "vault_name": str(member.get("vault_name") or ""),
            "token": raw,
            "token_masked": (raw[:14] + "..." + raw[-8:]) if len(raw) > 26 else raw,
            "token_jti": str(token.get("jti") or ""),
            "token_scope": str(token.get("scope") or ""),
            "token_issued_at": str(token.get("issued_at") or ""),
            "token_expires_at": str(token.get("expires_at") or ""),
            "password": password_of(name),
        })
    return rows


def create_account(admin: str, username: str, password: str, role: str, note: str = "") -> dict[str, Any]:
    _require_admin(admin)
    username = username.strip()
    if not username or len(username) > 32 or any(ch.isspace() or ord(ch) < 32 for ch in username):
        raise ValueError("用户名不合法")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    if role not in {"admin", "editor", "readonly"}:
        raise ValueError("角色必须是 admin/editor/readonly")
    if web_auth.user_exists(username):
        raise ValueError("账号已存在")
    web_auth.set_user(username, password, note=note.strip())
    record_password(username, password)
    cfg = web_auth.load_config()
    cfg.setdefault("users", {}).setdefault(username, {})["role"] = role
    admins = set(cfg.get("admin_users", []))
    if role == "admin":
        admins.add(username)
    cfg["admin_users"] = sorted(admins)
    web_auth.save_config(cfg)
    # 阶段 7：删号是**软删**（users 行保留 status='disabled' 供审计追溯），所以
    # 「删了又建同名」会撞上那行 disabled —— 之后签票会报「账号已停用」，管理员
    # 会以为系统坏了。这里显式复活：重新建号本身就是明确授权，而删号时成员行已
    # 打 revoked_at、票已吊销，复活不会带回任何旧权限（user_id 沿用，历史审计仍连得上）。
    now = _now_iso()
    with _connect() as con:
        stale = con.execute(
            "SELECT user_id FROM users WHERE username=? AND status<>'active'", (username,)
        ).fetchone()
        if stale:
            con.execute("UPDATE users SET status='active', updated_at=? WHERE user_id=?",
                        (now, stale["user_id"]))
            con.commit()
    _audit(admin, "account.create", target=username, detail={"role": role, "note": note.strip()})
    return next(row for row in account_rows(admin) if row["username"] == username)


def update_account(admin: str, username: str, password: str = "", role: str = "",
                   note: str = "", max_grade: str | None = None) -> dict[str, Any]:
    _require_admin(admin)
    cfg = web_auth.load_config()
    if username not in cfg.get("users", {}):
        raise ValueError("账号不存在")
    if role and role not in {"admin", "editor", "readonly"}:
        raise ValueError("角色必须是 admin/editor/readonly")
    grade = _norm_grade(max_grade)
    old_role = _account_role(cfg, username)
    new_role = role or old_role
    if old_role == "admin" and new_role != "admin" and len(cfg.get("admin_users", [])) <= 1:
        raise ValueError("不能移除最后一个管理员")
    if password:
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        web_auth.set_user(username, password, note=note or str(cfg["users"][username].get("note") or ""))
        record_password(username, password)
        cfg = web_auth.load_config()
    if note:
        cfg["users"][username]["note"] = note.strip()
    cfg["users"][username]["role"] = new_role
    admins = set(cfg.get("admin_users", []))
    if new_role == "admin":
        admins.add(username)
    else:
        admins.discard(username)
    cfg["admin_users"] = sorted(admins)
    web_auth.save_config(cfg)
    if new_role == "readonly":
        with _connect() as con:
            row = con.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            for token in _token_rows():
                scopes = str(token.get("scope") or "").split()
                if token.get("user_id") == row["user_id"] and not token.get("revoked") and "tools:write" in scopes:
                    revoke_token(admin, str(token["jti"]))
    # 阶段 6：max_grade 跟着账号走 —— 改的是该账号当前有效成员行。
    # None = 本次不动；"" = 恢复按角色默认；S/A/B/C = 显式指定。
    # 这就是「可修改可选是否看重要的」的落点：editor 默认 A，管理员可逐人提到 S。
    if max_grade is not None:
        grade = _norm_grade(max_grade)
        with _connect() as con:
            row = con.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
            if row:
                con.execute(
                    "UPDATE vault_members SET max_grade=? WHERE user_id=? AND revoked_at IS NULL",
                    (grade, row["user_id"]),
                )
                con.commit()
    _audit(admin, "account.update", target=username,
           detail={"role_from": old_role, "role_to": new_role,
                   "password_changed": bool(password), "note_changed": bool(note),
                   "max_grade": "" if max_grade is None else str(max_grade)})
    return next(row for row in account_rows(admin) if row["username"] == username)


def delete_account(admin: str, username: str) -> dict[str, Any]:
    _require_admin(admin)
    if username == admin:
        raise ValueError("不能删除当前登录管理员")
    cfg = web_auth.load_config()
    if username not in cfg.get("users", {}):
        raise ValueError("账号不存在")
    if _account_role(cfg, username) == "admin" and len(cfg.get("admin_users", [])) <= 1:
        raise ValueError("不能删除最后一个管理员")
    uid = _uid_of(username)
    with _connect() as con:
        row = con.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            user_id = str(row["user_id"])
            owned = con.execute(
                "SELECT 1 FROM vaults WHERE owner_user_id=? AND status='active' LIMIT 1", (user_id,)
            ).fetchone()
            if owned:
                raise ValueError("该账号仍是活动知识库所有者，请先归档知识库")
            now = _now_iso()
            con.execute("UPDATE users SET status='disabled', updated_at=? WHERE user_id=?", (now, user_id))
            con.execute(
                "UPDATE vault_members SET revoked_at=COALESCE(revoked_at,?) WHERE user_id=?", (now, user_id)
            )
            con.commit()
            for token in _token_rows():
                if token.get("user_id") == user_id and not token.get("revoked"):
                    revoke_token(admin, str(token["jti"]))
    cfg["users"].pop(username, None)
    cfg["admin_users"] = [name for name in cfg.get("admin_users", []) if name != username]
    web_auth.save_config(cfg)
    _audit(admin, "account.delete", target=username,
           detail={"soft": True, "user_id": uid})
    return {"ok": True, "username": username}


def set_account_status(admin: str, username: str, active: bool, ip: str = "") -> dict[str, Any]:
    """停用 / 启用账号（阶段 7）。停用 ≠ 删除 —— 这个区分是本阶段的全部意义。

    - **删除**：登录账号从 `web_auth.json` 里 pop 掉、成员行打 `revoked_at`、
      票全吊销。人回来要重建号、重设口令、重签票，原来的成员关系也回不来。
    - **停用**：只翻 `users.status`。成员关系、票据、口令原样保留，启用即回到
      停用前那一刻。

    **为什么不吊销票**：三处实时校验（`web_gate._session_user`、`memory_web`
    守卫、`acl._member`）每次请求都读一次 `users.status`，所以停用立即失效、
    启用立即恢复，agent 侧配置一个字都不用改。若改成吊销票，每次启停都要重发
    令牌 —— 那就把「临时冻结」做成了「重新入职」。

    **为什么先 sync_user 再置灰**：`sync_user` 对 disabled 行会顺手改回
    `active`。停用必须「先同步保证行存在、后覆盖成 disabled」，顺序反了就是
    自己给自己解开（这条是读源码时发现的，不是猜的）。
    """
    _require_admin(admin)
    username = str(username or "").strip()
    if not username:
        raise ValueError("账号不能为空")
    cfg = web_auth.load_config()
    if username not in cfg.get("users", {}):
        raise ValueError("账号不存在")
    if not active:
        if username == admin:
            raise ValueError("不能停用当前登录的管理员")
        if _account_role(cfg, username) == "admin" and len(cfg.get("admin_users", [])) <= 1:
            raise ValueError("不能停用最后一个管理员")
    want = "active" if active else "disabled"
    user = sync_user(admin, username)
    user_id = str(user["user_id"])
    now = _now_iso()
    with _connect() as con:
        con.execute("UPDATE users SET status=?, updated_at=? WHERE user_id=?", (want, now, user_id))
        con.commit()
        row = con.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
    _audit(admin, "account.enable" if active else "account.disable", target=username,
           detail={"user_id": user_id, "status": want, "ip": ip})
    return {"ok": True, "username": username, "user_id": user_id,
            "status": str(row["status"]) if row else want}


def _rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with _connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def snapshot(username: str) -> dict[str, Any]:
    _require_admin(username)
    return {
        "users": _rows("SELECT user_id, username, display_name, status, created_at FROM users ORDER BY username"),
        "vaults": _rows("SELECT vault_id, name, owner_user_id, status FROM vaults ORDER BY name"),
        "members": _rows(
            "SELECT vm.vault_id, v.name AS vault_name, vm.user_id, u.username, vm.role, vm.revoked_at "
            "FROM vault_members vm JOIN users u ON u.user_id=vm.user_id "
            "JOIN vaults v ON v.vault_id=vm.vault_id ORDER BY v.name, u.username"
        ),
        "tokens": _token_rows(),
    }


def sync_user(username: str, source_username: str, display_name: str = "") -> dict[str, Any]:
    _require_admin(username)
    source_username = source_username.strip()
    if not source_username or not web_auth.user_exists(source_username):
        raise ValueError("登录账号不存在")
    now = _now_iso()
    user_id = "u_" + uuid.uuid4().hex[:16]
    with _connect() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (source_username,)).fetchone()
        if row:
            if row["status"] != "active":
                con.execute("UPDATE users SET status='active', updated_at=? WHERE user_id=?", (now, row["user_id"]))
                con.commit()
                row = con.execute("SELECT * FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
            return dict(row)
        con.execute(
            "INSERT INTO users(user_id, username, display_name, created_at, updated_at) VALUES(?,?,?,?,?)",
            (user_id, source_username, display_name or source_username, now, now),
        )
        con.commit()
        return dict(con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


def create_vault(username: str, name: str, owner_user_id: str) -> dict[str, Any]:
    _require_admin(username)
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("知识库名称不合法")
    vault_id = "v_" + uuid.uuid4().hex[:16]
    now = _now_iso()
    with _connect() as con:
        if con.execute("SELECT 1 FROM vaults WHERE status='active' LIMIT 1").fetchone():
            raise ValueError("当前服务尚未启用多库数据隔离，只能有一个活动主库")
        if not con.execute("SELECT 1 FROM users WHERE user_id=? AND status='active'", (owner_user_id,)).fetchone():
            raise ValueError("owner 用户不存在")
        con.execute(
            "INSERT INTO vaults(vault_id,name,owner_user_id,created_at,updated_at) VALUES(?,?,?,?,?)",
            (vault_id, name, owner_user_id, now, now),
        )
        con.execute(
            "INSERT INTO vault_members(vault_id,user_id,role,invited_by,created_at) VALUES(?,?,?,?,?)",
            (vault_id, owner_user_id, "owner", owner_user_id, now),
        )
        con.commit()
    return {"vault_id": vault_id, "name": name, "owner_user_id": owner_user_id}


def set_member(username: str, vault_id: str, user_id: str, role: str,
               max_grade: str | None = None) -> dict[str, Any]:
    _require_admin(username)
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError("角色必须是 owner/editor/viewer")
    # max_grade 三态：None = 保持现状（重签 token 时绝不能把管理员选的等级冲掉）；
    # "" = 恢复按角色默认；S/A/B/C = 显式指定。新成员行没有现状，视同 ""。
    grade = None if max_grade is None else _norm_grade(max_grade)
    now = _now_iso()
    with _connect() as con:
        if not con.execute("SELECT 1 FROM vaults WHERE vault_id=?", (vault_id,)).fetchone():
            raise ValueError("知识库不存在")
        if not con.execute("SELECT 1 FROM users WHERE user_id=? AND status='active'", (user_id,)).fetchone():
            raise ValueError("用户不存在")
        admin_row = con.execute("SELECT user_id FROM users WHERE username=? AND status='active'", (username,)).fetchone()
        existing = con.execute(
            "SELECT max_grade FROM vault_members WHERE vault_id=? AND user_id=? AND revoked_at IS NULL",
            (vault_id, user_id),
        ).fetchone()
        target = grade if grade is not None else str(existing["max_grade"] if existing else "")
        con.execute(
            "INSERT INTO vault_members(vault_id,user_id,role,invited_by,created_at,revoked_at,max_grade) "
            "VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(vault_id,user_id) DO UPDATE SET role=excluded.role, "
            "invited_by=excluded.invited_by, revoked_at=NULL, max_grade=excluded.max_grade",
            (vault_id, user_id, role, admin_row["user_id"] if admin_row else None, now, target),
        )
        con.commit()
    if role == "viewer":
        for token in _token_rows():
            if token.get("user_id") == user_id and token.get("vault_id") == vault_id and not token.get("revoked"):
                scopes = str(token.get("scope") or "").split()
                if "tools:write" in scopes:
                    revoke_token(username, str(token["jti"]))
    _audit(username, "member.set", target=user_id, vault_id=vault_id,
           detail={"role": role, "max_grade": target})
    return {"ok": True, "vault_id": vault_id, "user_id": user_id, "role": role,
            "max_grade": target}


def revoke_member(username: str, vault_id: str, user_id: str) -> dict[str, Any]:
    _require_admin(username)
    now = _now_iso()
    with _connect() as con:
        cur = con.execute(
            "UPDATE vault_members SET revoked_at=? WHERE vault_id=? AND user_id=? AND revoked_at IS NULL",
            (now, vault_id, user_id),
        )
        if not cur.rowcount:
            raise ValueError("没有找到有效成员")
        con.commit()
    for token in _token_rows():
        if token.get("user_id") == user_id and token.get("vault_id") == vault_id and not token.get("revoked"):
            revoke_token(username, str(token["jti"]))
    _audit(username, "member.revoke", target=user_id, vault_id=vault_id)
    return {"ok": True, "vault_id": vault_id, "user_id": user_id}


def archive_vault(username: str, vault_id: str) -> dict[str, Any]:
    _require_admin(username)
    now = _now_iso()
    with _connect() as con:
        cur = con.execute(
            "UPDATE vaults SET status='archived', updated_at=? WHERE vault_id=? AND status='active'",
            (now, vault_id),
        )
        if not cur.rowcount:
            raise ValueError("没有找到活动知识库")
        con.execute(
            "UPDATE vault_members SET revoked_at=COALESCE(revoked_at,?) WHERE vault_id=?",
            (now, vault_id),
        )
        con.commit()
    for token in _token_rows():
        if token.get("vault_id") == vault_id and not token.get("revoked"):
            revoke_token(username, str(token["jti"]))
    _audit(username, "vault.archive", target=vault_id, vault_id=vault_id)
    return {"ok": True, "vault_id": vault_id, "status": "archived"}


def _token_rows() -> list[dict[str, Any]]:
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"tokens": {}}
    return [{"jti": jti, **rec} for jti, rec in sorted(data.get("tokens", {}).items())]


def issue_token(username: str, user_id: str, vault_id: str, scope: str, ttl_seconds: int, note: str) -> dict[str, Any]:
    _require_admin(username)
    if scope not in {"read", "readwrite"}:
        raise ValueError("权限必须是 read/readwrite")
    with _connect() as con:
        row = con.execute(
            "SELECT u.username, u.status AS user_status, vm.role, v.status AS vault_status "
            "FROM users u JOIN vault_members vm ON vm.user_id=u.user_id "
            "JOIN vaults v ON v.vault_id=vm.vault_id "
            "WHERE u.user_id=? AND vm.vault_id=? AND vm.revoked_at IS NULL",
            (user_id, vault_id),
        ).fetchone()
    if not row:
        raise ValueError("用户不是该知识库的有效成员")
    if row["user_status"] != "active" or row["vault_status"] != "active":
        raise ValueError("账号或知识库已停用")
    if scope == "readwrite" and row["role"] not in {"owner", "editor"}:
        raise PermissionError("viewer 不能签发读写令牌")
    ttl_value = "0" if int(ttl_seconds) <= 0 else f"{max(60, min(int(ttl_seconds), 31536000))}s"
    cmd = [str(ISSUE_PYTHON), str(ISSUE_PY), "new", "--name", f"{row['username']}@{vault_id}",
           "--scope", scope, "--ttl", ttl_value,
           "--note", note, "--user-id", user_id, "--vault-id", vault_id]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "令牌签发失败")
    token = next((line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("eyJ")), "")
    jti = next((line.split()[-1] for line in proc.stdout.splitlines() if line.startswith("票号")), "")
    resource = next((line.split()[-1] for line in proc.stdout.splitlines() if line.startswith("aud")), "")
    if not token or not jti or not resource:
        raise RuntimeError("令牌签发输出不完整")
    rec = next((r for r in _token_rows() if r.get("jti") == jti), {"jti": jti})
    _ensure_account_tokens()
    issued_at = _now_iso()
    expires_at = ""
    try:
        ttl = int(ttl_seconds)
        expires_at = (
            "9999-12-31T23:59:59+00:00" if ttl <= 0
            else datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + max(60, min(ttl, 31536000)),
                timezone.utc,
            ).isoformat(timespec="seconds")
        )
    except (TypeError, ValueError):
        expires_at = ""
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO account_tokens"
            "(jti,user_id,username,vault_id,token,scope,issued_at,expires_at,revoked_at,note) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,?)",
            (jti, user_id, row["username"], vault_id, token,
             "tools:read tools:write" if scope == "readwrite" else "tools:read",
             issued_at, expires_at, note),
        )
        con.commit()
    # 审计只记「签了哪张票」：jti 可追，令牌本体绝不出现在日志里（_scrub 也会兜住）。
    _audit(username, "token.issue", target=row["username"], vault_id=vault_id,
           detail={"jti": jti, "scope": scope, "ttl_seconds": int(ttl_seconds),
                   "expires_at": expires_at, "note": note})
    return {"token": token, "jti": jti, "record": rec, "resource": resource,
            "issued_at": issued_at, "expires_at": expires_at}


def revoke_token(username: str, jti: str) -> dict[str, Any]:
    _require_admin(username)
    proc = subprocess.run(
        [str(ISSUE_PYTHON), str(ISSUE_PY), "revoke", "--jti", jti],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    if proc.returncode:
        raise ValueError(proc.stderr.strip() or "令牌不存在")
    _ensure_account_tokens()
    with _connect() as con:
        owner = con.execute("SELECT username FROM account_tokens WHERE jti=?", (jti,)).fetchone()
        con.execute("UPDATE account_tokens SET revoked_at=? WHERE jti=? AND revoked_at IS NULL",
                    (_now_iso(), jti))
        con.commit()
    _audit(username, "token.revoke", target=jti,
           detail={"account": str(owner["username"]) if owner else ""})
    return {"ok": True, "jti": jti}


def my_token(username: str) -> dict[str, Any]:
    """普通用户看自己的票：只返回本人信息，不暴露他人。"""
    _ensure_account_tokens()
    with _connect() as con:
        user = con.execute(
            "SELECT user_id, username, status FROM users WHERE username=? AND status='active'",
            (username,),
        ).fetchone()
        if not user:
            return {"username": username, "token": "", "reason": "账户尚未同步到权限目录"}
        token = con.execute(
            "SELECT * FROM account_tokens WHERE user_id=? AND revoked_at IS NULL "
            "ORDER BY issued_at DESC LIMIT 1",
            (user["user_id"],),
        ).fetchone()
        member = con.execute(
            "SELECT vm.role, v.name AS vault_name FROM vault_members vm "
            "JOIN vaults v ON v.vault_id=vm.vault_id "
            "WHERE vm.user_id=? AND vm.revoked_at IS NULL LIMIT 1",
            (user["user_id"],),
        ).fetchone()
    base = {
        "username": username,
        "vault_role": member["role"] if member else "",
        "vault_name": member["vault_name"] if member else "",
        "resource": _mcp_resource(),
    }
    if not token:
        return {**base, "token": "", "reason": "尚未签发 token，请联系管理员"}
    rec = dict(token)
    raw = str(rec.get("token") or "")
    return {
        **base,
        "token": raw,
        "token_masked": (raw[:14] + "..." + raw[-8:]) if len(raw) > 26 else raw,
        "jti": str(rec.get("jti") or ""),
        "scope": str(rec.get("scope") or ""),
        "issued_at": str(rec.get("issued_at") or ""),
        "expires_at": str(rec.get("expires_at") or ""),
        "vault_id": str(rec.get("vault_id") or ""),
    }


DEFAULT_TOKEN_DAYS = 365


def issue_token_for_account(admin: str, account: str, days: int = DEFAULT_TOKEN_DAYS,
                            max_grade: str | None = None) -> dict[str, Any]:
    """一个账号一张有效票：按 Web 角色定库内角色与 scope，旧票先撤销再签新的。

    max_grade 语义透传给 set_member：None = 保持现有等级（重签不冲掉），
    "" = 恢复按角色默认，S/A/B/C = 显式指定（建号一条龙用）。
    """
    _require_admin(admin)
    account = str(account or "").strip()
    cfg = web_auth.load_config()
    if account not in cfg.get("users", {}):
        raise ValueError("登录账号不存在")
    # 阶段 7：停用的人不能签票 —— 否则「停用」会被一次授权动作悄悄解开。
    # 必须放在 sync_user 之前：sync_user 会把 disabled 行顺手改回 active。
    with _connect() as con:
        probe = con.execute("SELECT status FROM users WHERE username=?", (account,)).fetchone()
    if probe and str(probe["status"]) != "active":
        raise ValueError("账号已停用，请先在管理页启用再签发令牌")
    web_role = _account_role(cfg, account)
    if web_role == "admin":
        days = 0  # 管理员永久票
    else:
        try:
            days = int(DEFAULT_TOKEN_DAYS if days is None else days)
        except (TypeError, ValueError) as exc:
            raise ValueError("有效期必须是整数天（0=永久）") from exc
        if days < 0 or days > 3650:
            raise ValueError("有效期范围 0（永久）或 1~3650 天")
    user = sync_user(admin, account)
    vault_id = _active_vault_id()
    if not vault_id:
        raise ValueError("没有可用的知识库")
    vault_role = _vault_role_for(web_role)
    set_member(admin, vault_id, str(user["user_id"]), vault_role, max_grade=max_grade)
    for old in _account_token_rows(account):
        if not old.get("revoked_at"):
            try:
                revoke_token(admin, str(old["jti"]))
            except Exception:  # 台账里已不存在等情况，忽略即可
                pass
    ttl_seconds = 0 if days == 0 else days * 86400
    out = issue_token(admin, str(user["user_id"]), vault_id,
                      _scope_for(web_role), ttl_seconds, f"{account} 自动令牌（{vault_role}）")
    return {
        "ok": True,
        "username": account,
        "user_id": str(user["user_id"]),
        "vault_id": vault_id,
        "vault_role": vault_role,
        "scope": _scope_for(web_role),
        "days": days,
        "token": out["token"],
        "jti": out["jti"],
        "resource": out.get("resource", ""),
        "issued_at": out.get("issued_at", ""),
        "expires_at": out.get("expires_at", ""),
    }


def render_page(username: str) -> str:
    _require_admin(username)
    return """<!doctype html><meta charset='utf-8'><title>MCP 权限管理</title>
<style>body{font:14px system-ui;max-width:1100px;margin:30px auto;color:#222}input,select,button{padding:7px;margin:3px}section{border:1px solid #ddd;padding:16px;margin:14px 0;border-radius:8px}pre{background:#f5f5f5;padding:12px;white-space:pre-wrap}.muted{color:#666}</style>
<h1>MCP 权限管理</h1><p class='muted'>当前管理员：%s</p>
<section><h2>同步登录用户</h2><input id='su' placeholder='登录用户名'><input id='sd' placeholder='显示名'><button onclick='syncUser()'>同步</button></section>
<section><h2>创建知识库</h2><input id='vn' placeholder='知识库名称'><input id='vo' placeholder='owner user_id'><button onclick='createVault()'>创建</button></section>
<section><h2>分配成员角色</h2><input id='mv' placeholder='vault_id'><input id='mu' placeholder='user_id'><select id='mr'><option>viewer</option><option>editor</option><option>owner</option></select><button onclick='setMember()'>保存</button></section>
<section><h2>生成 MCP 令牌</h2><input id='tu' placeholder='user_id'><input id='tv' placeholder='vault_id'><select id='ts'><option value='read'>只读</option><option value='readwrite'>读写</option></select><input id='tt' value='7776000' placeholder='有效期秒数'><input id='tn' placeholder='备注'><button onclick='issue()'>生成</button><pre id='out'></pre></section>
<section><h2>吊销令牌</h2><input id='rj' placeholder='jti'><button onclick='revoke()'>吊销</button></section>
<section><h2>当前数据</h2><pre id='data'>加载中...</pre></section>
<script>
const j=(u,o)=>fetch(u,{method:o?'POST':'GET',headers:{'Content-Type':'application/json'},body:o?JSON.stringify(o):undefined}).then(async r=>{let x=await r.json();if(!r.ok)throw Error(x.error||r.status);return x});
async function load(){document.querySelector('#data').textContent=JSON.stringify(await j('/api/admin/snapshot'),null,2)}
async function act(fn){try{await fn();await load()}catch(e){alert(e.message)}}
function syncUser(){return act(async()=>await j('/api/admin/users/sync',{username:su.value,display_name:sd.value}))}
function createVault(){return act(async()=>await j('/api/admin/vaults',{name:vn.value,owner_user_id:vo.value}))}
function setMember(){return act(async()=>await j('/api/admin/members',{vault_id:mv.value,user_id:mu.value,role:mr.value}))}
function issue(){return act(async()=>{let x=await j('/api/admin/tokens',{user_id:tu.value,vault_id:tv.value,scope:ts.value,ttl_seconds:Number(tt.value),note:tn.value});out.textContent='令牌只显示一次：\n'+x.token+'\n\nMCP 地址：'+x.resource+'/\n票号：'+x.jti})}
function revoke(){return act(async()=>await j('/api/admin/tokens/revoke',{jti:rj.value}))} load();
</script>""" % html.escape(username)
