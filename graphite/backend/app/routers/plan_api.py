"""项目计划与进度 HTTP 层（阶段 10，2026-10-25）· 前缀 `/api/plans`

分层与 chat 完全同构：

    plan_api.py  ← 本文件：只做 HTTP（解参数、鉴权、拼响应、写审计）
    caps.py      ← 角色能力矩阵（谁能做这个动作）
    chat_acl.py  ← 身份 / 成员闸（他属于这个库吗、账号停用没）
    plan_db.py   ← 数据读写与进度口径，不含权限判断

## 两道闸，缺一不可

1. **能力闸**（`caps.current` + cap 名）：决定「这个角色能不能做这类动作」。
   只读账号有 `page.plans`（能看）但没有 `plan.write`（不能改）——
   这正是路线图阶段 10 那句「只读用户无法发起、编辑、复制」的落点。
2. **成员闸**（`chat_acl.require_member`）：决定「他在不在这个库」。
   能力闸只认角色，不认库 —— 一个 editor 如果能进 A 库，不代表能改 B 库的计划。

只有一道闸的实现是**能跑通但会越权**的典型：只查角色，跨库改别人的计划；
只查成员，只读账号照样能建计划。所以本文件所有写接口统一走 `_enter()`。

## 为什么「复制/导出」也在这一层判

路线图把「只读不能复制」写在验收里。前端把按钮藏掉是体验，但复制这事儿
拦不住手改请求 —— 所以导出接口（`GET .../export`）在服务端按 `plan.write`
拦一道：没有写权限的人拿不到结构化数据，只能看页面。

## 本地单人模式

`users` 表 0 行时 `chat_acl.require_member` 直接给 owner，`caps.role_of`
直接给 admin —— 本机自用不受影响。服务器上开了网关就一定有行。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from ..paths import ensure_import_path

# 库根进 sys.path 必须在 import audit 之前（同 chat_api 的顺序说明）
ensure_import_path()

from .. import caps, chat_acl, plan_db  # noqa: E402

import audit  # noqa: E402

router = APIRouter(tags=["plans"])


# ---------------------------------------------------------------------------
# 闸与审计
# ---------------------------------------------------------------------------


def _ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _deny(who: dict[str, Any], cap: str) -> None:
    """与 caps.require_cap 完全同构的 403 形状 —— 前端按 need 显示「这要有写权限」。"""
    raise HTTPException(
        status_code=403,
        detail={
            "error": f"当前角色（{who.get('role_label') or who.get('role')}）没有这个权限",
            "need": cap,
            "role": who.get("role") or "",
        },
    )


def _who(request: Request) -> dict[str, Any]:
    """只解身份（不判 cap）。带了解不出的头 → 401。"""
    return caps.current(request)


def _enter(
    request: Request, cap: str, vault_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """能力闸 + 成员闸。返回 (身份, 成员)。"""
    who = _who(request)
    if cap not in who["caps"]:
        _deny(who, cap)
    if not vault_id:
        return who, None
    try:
        member = chat_acl.require_member(who["user"], vault_id)
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return who, member


def _audit(
    who: dict[str, Any],
    action: str,
    *,
    vault_id: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    audit.record(
        who.get("user") or "?",
        action,
        target=target,
        vault_id=vault_id,
        detail=detail or {},
        actor_user_id="",
        ip=_ip(request) if request else "",
    )


def _bad(exc: ValueError) -> HTTPException:
    """数据层的 ValueError（标题为空、状态非法、对象不存在）统一转 400。"""
    return HTTPException(status_code=400, detail=str(exc))


def _vault_or_400(raw: str | None) -> str:
    """vault_id 必填。空值不回落「默认库」—— 回落会让一个漏传参数的请求
    静默写进主库，事后在审计里看到的是一条「正常」记录。"""
    vid = (raw or "").strip()
    if not vid:
        raise HTTPException(status_code=400, detail="缺少 vault_id")
    return vid


# ---------------------------------------------------------------------------
# 引导
# ---------------------------------------------------------------------------


@router.get("/plans/bootstrap")
def plans_bootstrap(request: Request) -> dict[str, Any]:
    """页面一次拿全：我能进的库 + 默认库 + 统计 + 计划列表。

    为什么单独一个接口：进页面要发四个请求的话，四个响应到达顺序不定，
    统计条会先按空数据画一遍再跳数（Dashboard 首次进会话那种「数字先 0 再跳」
    的手感）。一次回全，前端只播一轮入场。
    """
    who = _who(request)
    if "page.plans" not in who["caps"]:
        _deny(who, "page.plans")
    vaults = chat_acl.visible_vaults(who["user"])
    try:
        default_vault = chat_acl.default_vault(who["user"])
    except chat_acl.Denied:
        default_vault = ""
    items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    if default_vault:
        items = plan_db.list_plans(default_vault)
        stats = plan_db.stats(default_vault)
    return {
        "vaults": vaults,
        "default_vault": default_vault,
        "plans": items,
        "stats": stats,
        "can_write": "plan.write" in who["caps"],
        "can_moderate": "plan.moderate" in who["caps"],
        "can_archive": "plan.archive" in who["caps"],
        "me": {"user": who["user"], "display_name": who["display_name"], "role": who["role"]},
    }


@router.get("/plans")
def plans_list(request: Request, vault_id: str = "") -> dict[str, Any]:
    """单库的计划列表（切库时用，不重拉 bootstrap 的库清单）。"""
    vid = _vault_or_400(vault_id)
    _enter(request, "page.plans", vid)
    return {"vault_id": vid, "items": plan_db.list_plans(vid), "stats": plan_db.stats(vid)}


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


@router.post("/plans")
def plans_create(
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    try:
        plan = plan_db.create_plan(
            vid,
            str(payload.get("title") or ""),
            who["user"],
            descr=str(payload.get("descr") or ""),
        )
    except ValueError as exc:
        raise _bad(exc) from exc
    _audit(
        who,
        "plan.create",
        vault_id=vid,
        target=str(plan["id"]),
        detail={"title": plan["title"]},
        request=request,
    )
    return plan


@router.get("/plans/{plan_id}")
def plans_detail(request: Request, plan_id: int, vault_id: str = "") -> dict[str, Any]:
    vid = _vault_or_400(vault_id)
    _enter(request, "page.plans", vid)
    plan = plan_db.get_plan(plan_id, vid)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已归档")
    plan["members"] = chat_acl.vault_members(vid)
    return plan


@router.patch("/plans/{plan_id}")
def plans_update(
    request: Request,
    plan_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    patch = {k: payload[k] for k in ("title", "descr", "status") if k in payload}
    try:
        plan = plan_db.update_plan(plan_id, vid, **patch)
    except ValueError as exc:
        raise _bad(exc) from exc
    _audit(
        who,
        "plan.update",
        vault_id=vid,
        target=str(plan_id),
        detail={"fields": sorted(patch), "after": {k: plan.get(k) for k in patch}},
        request=request,
    )
    return plan


@router.post("/plans/{plan_id}/archive")
def plans_archive(
    request: Request,
    plan_id: int,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """归档计划。这是破坏性动作，只给管理员（`plan.archive`）。"""
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.archive", vid)
    try:
        row = plan_db.archive_plan(plan_id, vid)
    except ValueError as exc:
        raise _bad(exc) from exc
    _audit(
        who,
        "plan.archive",
        vault_id=vid,
        target=str(plan_id),
        detail={"title": row.get("title")},
        request=request,
    )
    return {"ok": True, "plan_id": plan_id}


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------


@router.post("/plans/{plan_id}/stages")
def stages_create(
    request: Request,
    plan_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    try:
        stage = plan_db.create_stage(
            plan_id,
            vid,
            str(payload.get("title") or ""),
            status=str(payload.get("status") or "todo"),
            progress=int(payload.get("progress") or 0),
            assignee=str(payload.get("assignee") or ""),
            note=str(payload.get("note") or ""),
            start_date=str(payload.get("start_date") or ""),
            due_date=str(payload.get("due_date") or ""),
        )
    except ValueError as exc:
        raise _bad(exc) from exc
    _audit(
        who,
        "plan.stage.create",
        vault_id=vid,
        target=str(stage["id"]),
        detail={"plan_id": plan_id, "title": stage["title"]},
        request=request,
    )
    return stage


@router.patch("/plans/{plan_id}/stages/{stage_id}")
def stages_update(
    request: Request,
    plan_id: int,
    stage_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    patch = {
        k: payload[k]
        for k in ("title", "status", "progress", "assignee", "note", "start_date", "due_date")
        if k in payload
    }
    try:
        stage = plan_db.update_stage(stage_id, vid, **patch)
    except ValueError as exc:
        raise _bad(exc) from exc
    if int(stage.get("plan_id") or 0) != int(plan_id):
        raise HTTPException(status_code=404, detail="阶段不属于这个计划")
    _audit(
        who,
        "plan.stage.update",
        vault_id=vid,
        target=str(stage_id),
        detail={
            "plan_id": plan_id,
            "fields": sorted(patch),
            "after": {k: stage.get(k) for k in patch},
        },
        request=request,
    )
    return stage


@router.post("/plans/{plan_id}/stages/{stage_id}/archive")
def stages_archive(
    request: Request,
    plan_id: int,
    stage_id: int,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    try:
        row = plan_db.archive_stage(stage_id, vid)
    except ValueError as exc:
        raise _bad(exc) from exc
    if int(row.get("plan_id") or 0) != int(plan_id):
        raise HTTPException(status_code=404, detail="阶段不属于这个计划")
    _audit(
        who,
        "plan.stage.archive",
        vault_id=vid,
        target=str(stage_id),
        detail={"plan_id": plan_id, "title": row.get("title")},
        request=request,
    )
    return {"ok": True, "stage_id": stage_id}


@router.post("/plans/{plan_id}/stages/reorder")
def stages_reorder(
    request: Request,
    plan_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.write", vid)
    raw = payload.get("ids") or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="ids 必须是数组")
    try:
        stages = plan_db.reorder_stages(plan_id, vid, [int(i) for i in raw])
    except (ValueError, TypeError) as exc:
        raise _bad(ValueError(str(exc))) from exc
    _audit(
        who,
        "plan.stage.reorder",
        vault_id=vid,
        target=str(plan_id),
        detail={"ids": raw[:60]},
        request=request,
    )
    return {"ok": True, "stages": stages}


# ---------------------------------------------------------------------------
# 留言
# ---------------------------------------------------------------------------


@router.get("/plans/{plan_id}/comments")
def comments_list(
    request: Request, plan_id: int, vault_id: str = "", before: int = 0, limit: int = 50
) -> dict[str, Any]:
    vid = _vault_or_400(vault_id)
    _enter(request, "page.plans", vid)
    return plan_db.list_comments(plan_id, vid, before=before or None, limit=limit)


@router.post("/plans/{plan_id}/comments")
def comments_create(
    request: Request,
    plan_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    vid = _vault_or_400(payload.get("vault_id"))
    who, _ = _enter(request, "plan.comment", vid)
    stage_id = payload.get("stage_id")
    try:
        item = plan_db.add_comment(
            plan_id,
            vid,
            who["user"],
            str(payload.get("body") or ""),
            stage_id=int(stage_id) if stage_id else None,
        )
    except (ValueError, TypeError) as exc:
        raise _bad(ValueError(str(exc))) from exc
    _audit(
        who,
        "plan.comment.create",
        vault_id=vid,
        target=str(plan_id),
        detail={"comment_id": item.get("id"), "stage_id": stage_id, "chars": len(item.get("body") or "")},
        request=request,
    )
    return item


@router.post("/plans/{plan_id}/comments/{comment_id}/revoke")
def comments_revoke(
    request: Request,
    plan_id: int,
    comment_id: int,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """撤回留言。规则与聊天同一套（`chat_acl.can_revoke`）：

      - 有 `plan.moderate`（管理员）→ 任何人的、不限时间；
      - 其他人 → 只能撤自己的，且发出未超过 5 分钟。

    为什么复用聊天那份规则而不是自写一份：两条线各写一套「多久之内能撤」，
    改的时候必然只改一处，用户看到的就是「聊天撤不回、计划能撤回」这种鬼故事。
    """
    vid = _vault_or_400(payload.get("vault_id"))
    who, member = _enter(request, "page.plans", vid)
    # 归属校验：`plan_db.revoke_comment` 只按 plan_id 定位（plan_id 是全局自增），
    # 不先确认这个计划属于当前库，理论上能拿「我有权限的库」的身份去撤别的库的留言。
    # 成员闸只回答了「你进得了这个库」，没回答「这个 id 是这个库的」。
    if not plan_db.get_plan(plan_id, vid):
        raise HTTPException(status_code=404, detail="计划不存在或已归档")
    target = plan_db.get_comment(comment_id, plan_id)
    if not target:
        raise HTTPException(status_code=404, detail="留言不存在")
    if "plan.moderate" not in who["caps"]:
        allowed, why = chat_acl.can_revoke(
            member or {}, str(target.get("actor") or ""), str(target.get("created") or ""), who["user"]
        )
        if not allowed:
            raise HTTPException(status_code=403, detail=why)
    try:
        item = plan_db.revoke_comment(comment_id, plan_id, who["user"])
    except ValueError as exc:
        raise _bad(exc) from exc
    _audit(
        who,
        "plan.comment.revoke",
        vault_id=vid,
        target=str(plan_id),
        detail={"comment_id": comment_id, "actor_of_comment": target.get("actor")},
        request=request,
    )
    return item


# ---------------------------------------------------------------------------
# 导出（只读账号在这里被拦住）
# ---------------------------------------------------------------------------


@router.get("/plans/{plan_id}/export")
def plans_export(request: Request, plan_id: int, vault_id: str = "") -> dict[str, Any]:
    """导出计划的结构化数据（Markdown 片段，前端另存为 .md）。

    用 `plan.write` 而不是 `page.plans` 拦：路线图把「只读不能复制、不能导出」
    写在验收里。让只读账号拿到一份完整的 JSON，等于把「不能复制」这条作废 ——
    复制按钮藏了，他换个请求就全拿走了。
    """
    vid = _vault_or_400(vault_id)
    who, _ = _enter(request, "plan.write", vid)
    plan = plan_db.get_plan(plan_id, vid)
    if not plan:
        raise HTTPException(status_code=404, detail="计划不存在或已归档")
    _audit(
        who,
        "plan.update",
        vault_id=vid,
        target=str(plan_id),
        detail={"export": True},
        request=request,
    )
    return plan
