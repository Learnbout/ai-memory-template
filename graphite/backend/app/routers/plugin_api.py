"""插件 HTTP 层（阶段 24，2026-10-25）。

权限边界（三条）：
· 看列表：所有人（前端要按 `requireCaps` 决定自己该不该加载，不给列表就没法判断）
· 安装 / 启停 / 卸载：只有 admin（`admin.manage`）—— 相当于给整站权限，不能下放
· 取插件资源：照常走网关登录，不做额外能力校验（代码本身不是秘密，
  真正拦人的是「谁能让它跑起来」）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import caps, plugin_store

router = APIRouter(tags=["plugins"])

_MEDIA = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


@router.get("/plugins")
def plugins_list() -> Dict[str, Any]:
    return {"ok": True, "items": plugin_store.list_plugins()}


@router.post("/plugins/{pid}/state")
def plugins_state(
    pid: str,
    body: Dict[str, Any],
    who: Dict[str, Any] = Depends(caps.require_cap("admin.manage")),
) -> Dict[str, Any]:
    try:
        out = plugin_store.set_enabled(pid, bool(body.get("enabled")))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="插件不存在")
    return {"ok": True, **out}


@router.delete("/plugins/{pid}")
def plugins_uninstall(
    pid: str,
    who: Dict[str, Any] = Depends(caps.require_cap("admin.manage")),
) -> Dict[str, Any]:
    try:
        out = plugin_store.uninstall(pid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="插件不存在")
    return {"ok": True, **out}


@router.post("/plugins/install")
async def plugins_install(
    request: Request,
    who: Dict[str, Any] = Depends(caps.require_cap("admin.manage")),
) -> Dict[str, Any]:
    """收原始 zip 字节（`Content-Type: application/zip`）。

    刻意不用 multipart：FastAPI 的 `UploadFile` 要 `python-multipart`，
    而它并不在当前依赖里 —— 为了一个上传口多引一个包，不如让前端直接 `fetch(body: blob)`。
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="请求体为空（需要 zip 字节）")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="zip 超过 8 MB")
    try:
        out = plugin_store.install_zip(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "plugin": out}


@router.get("/plugins/{pid}/asset/{name}")
def plugins_asset(pid: str, name: str) -> FileResponse:
    try:
        p = plugin_store.asset_path(pid, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="资源不存在")
    media = _MEDIA.get(Path(p).suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=media)
