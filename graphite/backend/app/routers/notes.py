"""笔记 / Vault 状态路由（§9.1 ~ §9.5、§9.10）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import caps, vault
from ..schemas import (
    FacetsData,
    GraphData,
    NoteDetail,
    NoteSummary,
    ReindexResult,
    VaultStatus,
)

router = APIRouter(tags=["notes"])


@router.get("/vault/status", response_model=VaultStatus)
def vault_status() -> VaultStatus:
    return VaultStatus(**vault.get_status())


@router.post("/vault/reindex", response_model=ReindexResult)
def vault_reindex(who: Dict[str, Any] = Depends(caps.require_cap("vault.reindex"))) -> ReindexResult:
    """丢内存索引、从库重读。不写库，但会短时占满 IO 并改变所有人的读数时机 → 只给 admin。"""
    return ReindexResult(**vault.reindex())


@router.get("/notes", response_model=List[NoteSummary])
def notes(
    type: Optional[str] = Query(default=None),
    folder: Optional[str] = Query(default=None),
    scope: Optional[str] = Query(default=None),
) -> List[NoteSummary]:
    return [NoteSummary(**n) for n in vault.list_notes(ntype=type, folder=folder, scope=scope)]


@router.get("/notes/facets", response_model=FacetsData)
def notes_facets() -> FacetsData:
    return FacetsData(**vault.facets())


@router.get("/graph", response_model=GraphData)
def graph() -> GraphData:
    return GraphData(**vault.graph())


@router.get("/notes/{note_id}", response_model=NoteDetail)
def note_detail(note_id: str) -> NoteDetail:
    note = vault.get_note(note_id)
    if not note:
        # 回退：允许用「库内相对路径」或「标题」打开（2026-10-24）。
        # 起因：聊天引用卡片把标题塞进了 ?note=，而规范参数是 `n_` + md5(rel_path)[:7]，
        # 点开必定 404。前端已改传 rel_path 并在本地解析，这里再兜一层 ——
        # 分享出去的旧链接（只有标题）也能打开。回退只查已建索引的记录，不扫盘。
        rec = vault.get_record_by_path(note_id)
        if rec:
            note = vault.get_note(rec["id"])
    if not note:
        raise HTTPException(status_code=404, detail=f"未找到笔记: {note_id}")
    return NoteDetail(**note)
