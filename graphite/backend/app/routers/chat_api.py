"""库内聊天 HTTP 层（阶段 8，2026-10-23）。·前缀 `/api/chat`

分层约定（与项目「大文件分层」规约一致）：

    chat_api.py     ← 本文件：只做 HTTP（解参数、鉴权、拼响应）
    chat_acl.py     ← 身份 / 成员 / 等级校验
    chat_db.py      ← 数据读写，不含权限判断
    chat_stickers.py← 贴纸清单

## 三条服务端硬要求

1. **不信前端 payload。** `kind=note` / `kind=image` / `kind=sticker` 的 payload
   全部由服务端重算：引用笔记重新查等级、图片换成服务端记录里的附件、贴纸校验
   id 合法性。前端传什么只当「意图」，不当「事实」—— 否则只读用户手改请求体
   就能把 A 级笔记的摘要贴进聊天。
2. **图片走 JSON base64，不引 multipart。** 服务器 venv 里没有 `python-multipart`，
   为一张图去装一个包（还得走国内镜像）不划算；前端上传前把图压到 1600px/JPEG，
   实际载荷几百 KB。代价是多 33% 的编码开销，换来零依赖。
3. **链接卡片不做服务端抓取。** 抓 URL 元信息 = 一个 SSRF 后门（服务器能访问
   `127.0.0.1:8788`、内网 31.x 全段），要安全就得写完整的私网地址黑名单，
   收益（一张 OG 卡片）远小于风险。所以卡片内容由发送方给，服务端只校验是
   http/https 并抽出域名。
"""
from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from ..paths import ensure_import_path

# 库根进 sys.path 必须在 import chat_db / audit 之前 —— 它们依赖库根下的模块。
# 顺序错了的表现是「进程起不来，报 ModuleNotFoundError: db_paths」，与代码逻辑无关。
_VAULT_ROOT = ensure_import_path()

from .. import chat_acl, chat_db, chat_stickers, chat_stream  # noqa: E402

import audit  # noqa: E402

router = APIRouter(tags=["chat"])

_vault = Path(_VAULT_ROOT)

# 单图上限：base64 解码后 4 MB。手机原图常见 3~8 MB，前端会先压；
# 这里留 4 MB 是为了「经网关转发时别把内存打满」（网关是单线程纯标准库实现）。
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_URL_RE = re.compile(r"https?://[^\s<>\"']{4,500}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _who(request: Request) -> str:
    """当前登录用户名。带了解不出 → 401。"""
    try:
        return chat_acl.require_user(request.headers.get("X-AI-Memory-User-B64"))
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _member(request: Request, vault_id: str) -> tuple[str, dict[str, Any]]:
    """聊天里所有接口的统一入口：解身份 + 成员闸 + 挡只读。

    用 `require_chat_member` 而不是 `require_member`：只读账号整页不给用
    （主人 2026-10-24 定），这一层是真正的闸，前端隐藏只是体验层。
    """
    user = _who(request)
    try:
        member = chat_acl.require_chat_member(user, vault_id)
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return user, member


def _ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _audit(user: str, action: str, *, vault_id: str, target: str = "", detail: dict | None = None, ip: str = "") -> None:
    audit.record(user, action, target=target, vault_id=vault_id, detail=detail or {}, ip=ip)


def _attachment_dir() -> Path:
    """附件目录：<库根>/附件/chat/<YYYYMM>/。

    按月上目录不是为了好看：`ls` 一次只有几百个文件，出问题时能直接翻；
    全塞一个目录，几万张之后连 `du` 都慢。这个目录不参与 sync_push（同步只覆盖
    数据库与 md 快照），聊天图片天然是「服务器本地产物」。
    """
    d = _vault / "附件" / "chat" / datetime.now(timezone.utc).strftime("%Y%m")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _link_payload(url: str, title: str = "", note: str = "") -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="只支持 http/https 链接")
    return {
        "url": url,
        "domain": parsed.netloc.lower(),
        "title": (title or url)[:200],
        "note": note[:200],
        "path": (parsed.path or "/")[:200],
    }


# ---------------------------------------------------------------------------
# 引导：一次拿全页面要的东西
# ---------------------------------------------------------------------------


@router.get("/chat/bootstrap")
def bootstrap(request: Request, vault_id: str = "") -> dict[str, Any]:
    """前端进页面只打这一个接口：身份、能进的库、当前库、成员、贴纸、未读。"""
    user = _who(request)
    try:
        vaults = chat_acl.visible_vaults(user)
        target = vault_id or chat_acl.default_vault(user)
        member = chat_acl.require_chat_member(user, target)
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    name = next((v["name"] for v in vaults if v["vault_id"] == target), "知识库")
    return {
        "ok": True,
        "me": {"username": member["username"], "display_name": member.get("display_name"), "role": member["role"], "max_grade": member["max_grade"]},
        "vaults": vaults,
        "vault": {"vault_id": target, "name": name},
        "members": chat_acl.vault_members(target),
        "stats": chat_db.stats(target),
        "unread": chat_db.unread_count(target, user),
        "lastReadId": chat_db.last_read(target, user),
        # 撤回策略由服务端下发：前端算按钮显隐必须用同一个数字，
        # 两边各写一个 300 迟早对不上（改了后端忘改前端 = 按钮能点但一定失败）。
        "policy": {
            "revokeWindowSeconds": chat_acl.REVOKE_WINDOW_SECONDS,
            "revokeAnyAsOwner": True,
        },
        **chat_stickers.catalog(),
    }


@router.get("/chat/messages")
def list_messages(request: Request, vault_id: str, before: int = 0, limit: int = 40) -> dict[str, Any]:
    user, _ = _member(request, vault_id)
    data = chat_db.page(vault_id, before=before or None, limit=limit)
    data["ok"] = True
    data["me"] = user
    return data


@router.get("/chat/search")
def search_messages(request: Request, vault_id: str, q: str = "") -> dict[str, Any]:
    _member(request, vault_id)
    return {"ok": True, "items": chat_db.search(vault_id, q)}


@router.get("/chat/notes")
def list_referenceable_notes(request: Request, vault_id: str, q: str = "", limit: int = 20) -> dict[str, Any]:
    """引用选择器：只回当前用户等级够得着的笔记。"""
    _, member = _member(request, vault_id)
    return {"ok": True, "items": chat_acl.searchable_notes(vault_id, member, q, limit)}


# ---------------------------------------------------------------------------
# 发消息
# ---------------------------------------------------------------------------


@router.post("/chat/messages")
def post_message(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    vault_id = str(data.get("vault_id") or "")
    user, member = _member(request, vault_id)

    kind = str(data.get("kind") or "text")
    body = str(data.get("body") or "")
    payload: dict[str, Any] = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    reply_to = data.get("reply_to")
    try:
        reply_to = int(reply_to) if reply_to else None
    except (TypeError, ValueError):
        reply_to = None

    try:
        payload = _normalize_payload(kind, payload, body, user, member, vault_id)
    except chat_acl.Denied as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    try:
        msg = chat_db.send(
            vault_id,
            user,
            kind=kind,
            body=body,
            payload=payload,
            reply_to=reply_to,
            sender_user_id=str(member.get("user_id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    att_id = payload.get("attachment_id")
    if att_id:
        chat_db.set_message_attachment(int(msg["id"]), str(att_id))
    chat_db.mark_read(vault_id, user, int(msg["id"]))

    _audit(
        user,
        "chat.message.send",
        vault_id=vault_id,
        target=str(msg["id"]),
        detail={"kind": kind, "chars": len(body), "has_ref": bool(payload.get("title"))},
        ip=_ip(request),
    )
    # SSE 广播（阶段 10）：新消息推给该库所有订阅者（尽力而为，失败不影响落库）
    chat_stream.publish(vault_id, msg)
    return {"ok": True, "message": msg}


def _normalize_payload(
    kind: str, payload: dict[str, Any], body: str, user: str, member: dict[str, Any], vault_id: str
) -> dict[str, Any]:
    """按类型重算 payload —— 服务端说了算（见模块说明第 1 条）。"""
    if kind == "note":
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="引用笔记要指定标题")
        ref = chat_acl.visible_note(user, vault_id, member, title)
        if not ref:
            raise HTTPException(status_code=404, detail=f"这个库里没有「{title}」")
        return ref

    if kind == "sticker":
        sid = str(payload.get("sticker") or "")
        if not chat_stickers.is_sticker(sid):
            raise HTTPException(status_code=400, detail=f"未知贴纸：{sid}")
        return {"sticker": sid}

    if kind == "image":
        att_id = str(payload.get("attachment_id") or "")
        att = chat_db.attachment(att_id)
        if not att:
            raise HTTPException(status_code=400, detail="图片没有上传成功，重发一次")
        if att.get("uploaded_by") != user:
            raise HTTPException(status_code=403, detail="不能引用别人上传的附件")
        return {
            "attachment_id": att_id,
            "name": att["name"],
            "mime": att["mime"],
            "bytes": att["bytes"],
            "width": int(payload.get("width") or 0),
            "height": int(payload.get("height") or 0),
        }

    if kind == "link":
        url = str(payload.get("url") or "").strip()
        if not url:
            found = _URL_RE.search(body or "")
            url = found.group(0) if found else ""
        if not url:
            raise HTTPException(status_code=400, detail="链接卡片要有 url")
        return _link_payload(url, str(payload.get("title") or ""), str(payload.get("note") or ""))

    # text / system：不挂任何 payload
    return {}


@router.post("/chat/messages/{message_id}/reaction")
def react(message_id: int, request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    vault_id = str(data.get("vault_id") or "")
    user, _ = _member(request, vault_id)
    try:
        out = chat_db.toggle_reaction(message_id, user, str(data.get("emoji") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # SSE 广播（阶段 10）：表情是「同 id 变更」，推整条消息让订阅端幂等合并
    full = chat_db.get_message(message_id)
    if full and full.get("vault_id") == vault_id:
        chat_stream.publish(vault_id, full)
    return {"ok": True, **out}


@router.post("/chat/messages/{message_id}/revoke")
def revoke_message(message_id: int, request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """撤回：管理员不限时、可撤任何人；其他人只能撤自己的且限时（见 chat_acl.can_revoke）。"""
    vault_id = str(data.get("vault_id") or "")
    user, member = _member(request, vault_id)
    with chat_db.connect() as con:
        row = con.execute(
            "SELECT sender, vault_id, created, revoked_at FROM messages WHERE id=?", (message_id,)
        ).fetchone()
    if not row or row["vault_id"] != vault_id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if row["revoked_at"]:
        # 幂等：已撤回的再点一次不算错，直接把现状回给前端（不然连点会弹红色的 403）
        return {"ok": True, "message": chat_db.revoke(message_id, user), "already": True}
    allowed, reason = chat_acl.can_revoke(member, str(row["sender"]), str(row["created"]), user)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    out = chat_db.revoke(message_id, user)
    # SSE 广播（阶段 10）：撤回是「同 id 变更」，订阅端按 id 合并后原地变灰
    chat_stream.publish(vault_id, out)
    _audit(
        user,
        "chat.message.revoke",
        vault_id=vault_id,
        target=str(message_id),
        detail={"owner_of_message": row["sender"]},
        ip=_ip(request),
    )
    return {"ok": True, "message": out}


@router.post("/chat/read")
def mark_read(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    vault_id = str(data.get("vault_id") or "")
    user, _ = _member(request, vault_id)
    try:
        last = int(data.get("last_id") or 0)
    except (TypeError, ValueError):
        last = 0
    chat_db.mark_read(vault_id, user, last)
    return {"ok": True, "last_read_id": last}


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------


@router.post("/chat/upload")
def upload_image(request: Request, data: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """接收 base64 图片。前端已压过尺寸（见模块说明第 2 条）。"""
    vault_id = str(data.get("vault_id") or "")
    user, _ = _member(request, vault_id)

    mime = str(data.get("mime") or "").lower()
    if mime not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的图片类型：{mime or '未知'}")
    raw_b64 = str(data.get("data") or "")
    if "," in raw_b64 and raw_b64.strip().lower().startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="图片数据损坏（base64 解不开）") from exc
    if not blob:
        raise HTTPException(status_code=400, detail="空文件")
    if len(blob) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"图片太大（{len(blob) // 1024} KB），单张上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB",
        )

    att_id = uuid.uuid4().hex[:20]
    ext = IMAGE_TYPES[mime]
    folder = _attachment_dir()
    filename = f"{att_id}.{ext}"
    target = folder / filename
    target.write_bytes(blob)

    rel = target.relative_to(_vault).as_posix()
    name = str(data.get("name") or filename)[:120]
    chat_db.add_attachment(att_id, name, mime, len(blob), rel, user)
    _audit(
        user,
        "chat.image.upload",
        vault_id=vault_id,
        target=att_id,
        detail={"bytes": len(blob), "mime": mime},
        ip=_ip(request),
    )
    return {
        "ok": True,
        "attachment_id": att_id,
        "url": f"/api/chat/attachment/{att_id}",
        "bytes": len(blob),
        "name": name,
        "mime": mime,
    }


@router.get("/chat/attachment/{att_id}")
def get_attachment(att_id: str, request: Request) -> FileResponse:
    """取图。**必须鉴权** —— 否则谁猜到 id 谁就能扒图（id 是随机 20 hex，但不该靠这个）。"""
    _who(request)
    att = chat_db.attachment(att_id)
    if not att:
        raise HTTPException(status_code=404, detail="附件不存在")
    path = (_vault / str(att["rel_path"])).resolve()
    if _vault.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="附件文件已丢失")
    return FileResponse(path, media_type=str(att["mime"]), filename=str(att["name"]))
