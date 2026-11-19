"""通知中心 HTTP 层（阶段 12.5，2026-10-25）· 前缀 `/api/notices`

分层：本文件只做 HTTP（解身份、定库、拼响应）；数据在 `app/notices.py`。

## 为什么不挂 `require_cap`

通知面板是给**所有已登录角色**用的：只读成员也该知道「项目有新留言了」。
`require_cap` 的语义是「某类页面/动作的准入」，套在这里会让只读用户整个铃铛
403 —— 那正好把最需要「团队在干什么」的人挡在外面。

所以这里用 `caps.current()`：**必须登录**（网关会注入身份，绕过网关直接打
8788 在多人模式下 401），但不要求特定能力。聊天未读另有闸：`chat.use`
不在能力集里就不去问聊天库（只读用户看不见聊天页，给他算未读是噪音）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import caps, chat_acl, notices

router = APIRouter(tags=["notices"])


def _vault_or_empty(user: str) -> str:
    """通知只覆盖「我的默认库」。多库场景要等库切换器接进来再说 ——
    先按默认库算，比把全部库混成一锅（同一个计划标题出现两次）要好。
    """
    try:
        return chat_acl.default_vault(user)
    except Exception:  # noqa: BLE001 —— Denied / 无成员：通知就是空的，不报错
        return ""


@router.get("/notices")
def notices_get(request: Request, limit: int = 40) -> dict[str, Any]:
    who = caps.current(request)
    vault_id = _vault_or_empty(who["user"])
    return notices.feed(
        vault_id,
        who["user"],
        can_chat="chat.use" in who["caps"],
        limit=limit,
    )


@router.post("/notices/seen")
def notices_seen(request: Request) -> dict[str, Any]:
    """全部已读。POST 而不是 GET —— 它会写库（水位前进），GET 会被预取/刷新误触。"""
    who = caps.current(request)
    vault_id = _vault_or_empty(who["user"])
    return notices.clear(
        who["user"],
        vault_id,
        can_chat="chat.use" in who["caps"],
    )
