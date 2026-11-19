"""Graphite 同源管理员页面：身份来自 8766 网关，不另起登录系统。

阶段 9（2026-10-24）：身份解析收归 `caps`，不再自己解 base64。
原因不是「想统一好看」，是本地直连时会露馅 —— 旧实现在没带身份头时直接 401，
而本地个人库（users 表 0 行）本来就没有网关、也就没有这个头，于是
「导航里看得见 Admin、点进去满屏 401」。现在与 chat_acl / audit_api 同一条口径：
无头 + 未启用账号体系 = 本地单人（local，admin）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from .. import caps
from ..paths import ensure_import_path

# 库目录进 sys.path（判据见 paths.ensure_import_path 的说明：
# vault_root 可能是记忆库的上一级，直接拼 "/脚本" 会 import 失败）
_vault_dir = Path(ensure_import_path())

import admin_api  # noqa: E402
import audit  # noqa: E402

router = APIRouter(tags=["admin-mcp"])


def _user(request: Request) -> str:
    """当前用户名（只解身份，不判权限）。401 由 caps.current 负责抛。"""
    return caps.current(request)["user"]


def _admin(request: Request) -> str:
    who = caps.current(request)
    if "admin.manage" not in who["caps"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return who["user"]


@router.get("/admin/users")
def list_users(request: Request) -> dict[str, Any]:
    return {"ok": True, "users": admin_api.account_rows(_admin(request))}


@router.post("/admin/users")
def create_user(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    admin = _admin(request)
    try:
        return {"ok": True, "user": admin_api.create_account(admin, str(data.get("username") or ""), str(data.get("password") or ""), str(data.get("role") or "editor"), str(data.get("note") or ""))}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/admin/users/{username}")
def update_user(username: str, request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    admin = _admin(request)
    try:
        return {"ok": True, "user": admin_api.update_account(admin, username, str(data.get("password") or ""), str(data.get("role") or ""), str(data.get("note") or ""), data.get("max_grade"))}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/admin/users/{username}")
def delete_user(username: str, request: Request) -> dict[str, Any]:
    try:
        return admin_api.delete_account(_admin(request), username)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _admin_call(fn, *args):
    try:
        return fn(*args)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/snapshot")
def snapshot(request: Request) -> dict[str, Any]:
    return {"ok": True, **_admin_call(admin_api.snapshot, _admin(request))}


@router.post("/admin/users/sync")
def sync_user(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    return {"ok": True, **_admin_call(admin_api.sync_user, _admin(request), str(data.get("username") or ""), str(data.get("display_name") or ""))}


@router.post("/admin/vaults")
def create_vault(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    return {"ok": True, **_admin_call(admin_api.create_vault, _admin(request), str(data.get("name") or ""), str(data.get("owner_user_id") or ""))}


@router.delete("/admin/vaults/{vault_id}")
def archive_vault(vault_id: str, request: Request) -> dict[str, Any]:
    return _admin_call(admin_api.archive_vault, _admin(request), vault_id)


@router.post("/admin/members")
def set_member(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    return _admin_call(admin_api.set_member, _admin(request), str(data.get("vault_id") or ""), str(data.get("user_id") or ""), str(data.get("role") or ""), data.get("max_grade"))


@router.delete("/admin/members/{vault_id}/{user_id}")
def revoke_member(vault_id: str, user_id: str, request: Request) -> dict[str, Any]:
    return _admin_call(admin_api.revoke_member, _admin(request), vault_id, user_id)


@router.post("/admin/tokens")
def issue_token(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        ttl_seconds = int(data.get("ttl_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="有效期必须是整数") from exc
    return {"ok": True, **_admin_call(admin_api.issue_token, _admin(request), str(data.get("user_id") or ""), str(data.get("vault_id") or ""), str(data.get("scope") or "read"), ttl_seconds, str(data.get("note") or ""))}


@router.post("/admin/tokens/revoke")
def revoke_token(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    return _admin_call(admin_api.revoke_token, _admin(request), str(data.get("jti") or ""))

@router.post("/admin/accounts/{username}/token")
def account_token(username: str, request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        days = int(data.get("days") or 365)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="有效期必须是整数天") from exc
    return {"ok": True, **_admin_call(admin_api.issue_token_for_account, _admin(request), username, days, data.get("max_grade"))}

@router.post("/admin/accounts/{username}/status")
def set_account_status(username: str, request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """停用 / 启用账号（阶段 7）。body: {"active": true|false}

    用 POST + 显式 body，不用 DELETE：停用是可逆的状态翻转，走 DELETE 语义会让
    人以为账号被删了。真正的删除仍然走 `DELETE /admin/users/{username}`。
    """
    raw = data.get("active", True)
    if isinstance(raw, str):
        # 严格解析字符串：bool("false") 是 True，前端或 curl 传错会静默变成启用。
        active = raw.strip().lower() not in ("", "0", "false", "no", "off")
    else:
        active = bool(raw)
    return {"ok": True, **_admin_call(admin_api.set_account_status, _admin(request), username, active)}


@router.get("/admin/audit")
def list_audit(request: Request, limit: int = 50, action: str = "") -> dict[str, Any]:
    """最近的审计记录（阶段 7）。

    只给管理员，且不接受任何写入参数 —— 审计是只追加的，这一层只管读。
    """
    _admin(request)
    try:
        n = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        n = 50
    return {"ok": True, "total": audit.count(), "items": audit.tail(n, action or None)}


@router.get("/my-token")
def my_token(request: Request) -> dict[str, Any]:
    return {"ok": True, **admin_api.my_token(_user(request))}
