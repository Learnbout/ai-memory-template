"""审计日志 HTTP 层（阶段 8，2026-10-23）·前缀 `/api/audit`

只读，且只有管理员能进。为什么不给「可修改/只读」角色看：
审计记录里带 `detail`（谁被改了什么、哪些账号被停用），本身就是敏感信息 ——
只读用户的权限边界是「看笔记」，不是「看谁在什么时候动过谁的账号」。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .. import caps
from ..paths import ensure_import_path

# 库目录进 sys.path 必须早于 import audit（audit 在库脚本里）。
ensure_import_path()

import audit  # noqa: E402

router = APIRouter(tags=["audit"])


def _admin(request: Request) -> str:
    """管理员闸（阶段 9 收归 caps：与 admin_mcp / 其它路由同一条身份与判定链）。

    之前这里自己写了一遍「无头 + users 空 = local、否则 is_admin」—— 逻辑没错，
    但那是第三份拷贝；caps.role_of 改判定口径时这里不会跟着变，迟早分裂。
    现在只问一个问题：`audit.view` 在不在我的 caps 里。
    """
    who = caps.current(request)
    if "audit.view" not in who["caps"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return who["user"]


@router.get("/audit/stats")
def audit_stats(request: Request, days: int = 7) -> dict[str, Any]:
    _admin(request)
    return {"ok": True, **audit.stats(days)}


@router.get("/audit/actions")
def audit_actions(request: Request) -> dict[str, Any]:
    _admin(request)
    return {"ok": True, "items": audit.action_counts(), "risky": sorted(audit.RISKY_ACTIONS)}


@router.get("/audit/logs")
def audit_logs(
    request: Request,
    since: str = Query("", description="起始时间，ISO8601（含）"),
    until: str = Query("", description="结束时间，ISO8601（含）"),
    actions: str = Query("", description="动作类型，逗号分隔"),
    actor: str = "",
    vault_id: str = "",
    q: str = Query("", description="关键字：操作对象 / detail / 操作者"),
    page: int = 1,
    page_size: int = 40,
) -> dict[str, Any]:
    _admin(request)
    picked = [a.strip() for a in actions.split(",") if a.strip()]
    data = audit.query(
        since=since,
        until=until,
        actions=picked or None,
        actor=actor.strip(),
        vault_id=vault_id.strip(),
        keyword=q.strip(),
        page=page,
        page_size=page_size,
    )
    return {"ok": True, **data}
