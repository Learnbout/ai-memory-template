"""运维台 HTTP 层（阶段 11，2026-10-25）· 前缀 `/api/ops`

分层与 plan_api 同构：

    routers/ops_api.py  ← 本文件：HTTP（解参数、鉴权、确认词、写审计）
    脚本/ops_store.py   ← 数据层：备份 / 恢复 / 审计轮转 / 库健康 / 令牌回收
    caps.py             ← 能力矩阵（谁有 `page.ops` / `ops.restore`）

**为什么数据层叫 store 不叫 api**：两个文件都叫 `ops_api` 时，`import ops_api`
到底拿到自己还是库内那个，取决于 `sys.path` 顺序与启动目录 —— 这类 bug 只在
某个部署方式下炸一次，然后排查半天。名字撞不起，就不撞。

## 确认词（阶段 11 的「高风险操作二次确认」）

路线图验收要求「高风险操作二次确认」。只在按钮上弹一个 `confirm()` 不算 ——
那玩意儿能把人练成条件反射，闭着眼点确定。这里的口径是**服务端也要求确认词**：

| 接口 | 请求体必须带 | 含义 |
| --- | --- | --- |
| `POST /ops/backups/{name}/restore` | `{"confirm": "恢复"}` | 把库换成这份备份 |
| `POST /ops/audit/rotate` | `{"confirm": "轮转"}` | 归档并清空主审计库 |
| `POST /ops/tokens/sweep` | `{"confirm": "回收"}` | 批量吊销过期令牌 |

前端得让用户手打这两个字才能发请求（见 `DangerConfirm.tsx`）。服务端再验一遍，
是因为**前端拦不住手改请求**：真正需要确认的动作，确认必须发生在能生效的那一层。
缺 `confirm` 或写错 → 400，且理由写清「这不是参数错误，是刻意要你再想一次」。
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..paths import ensure_import_path

# 库根与 <库>/脚本 进 sys.path，才能 import audit / ops_store
ensure_import_path()

from .. import caps  # noqa: E402

import audit  # noqa: E402
import ops_store  # noqa: E402

router = APIRouter(tags=["ops"])

# 确认词表（唯一事实源：前端文案也从这里来，见 GET /ops/meta）
# 阶段 12 加了 update / rollback：一键更新会覆盖本机代码、回滚会把代码换回去，
# 两者都属于「按错了要出事」，所以走同一套「服务端也要看确认词」的口径。
CONFIRM_WORDS = {
    "restore": "恢复",
    "rotate": "轮转",
    "sweep": "回收",
    "update": "更新",
    "rollback": "回滚",
}


# ── 内部工具 ───────────────────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _need_confirm(payload: dict[str, Any], kind: str) -> None:
    """校验确认词。缺了/写错都是 400，且明确说是「刻意拦你」。"""
    want = CONFIRM_WORDS[kind]
    got = str((payload or {}).get("confirm") or "").strip()
    if got != want:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"这个操作要求输入确认词「{want}」",
                "need": want,
                "hint": "不是参数写错，是刻意让这个动作多一道手：它不可逆。",
            },
        )


# ── 元信息 ─────────────────────────────────────────────────────────────────
@router.get("/ops/meta")
def ops_meta(who: dict[str, Any] = Depends(caps.require_cap("page.ops"))) -> dict[str, Any]:
    """页面只需要知道确认词和保留策略，不用自己硬编码一份。"""
    return {
        "ok": True,
        "confirmWords": CONFIRM_WORDS,
        "role": who["role"],
        "restorable": sorted(ops_store.RESTORABLE),
    }


# ── 概览（页面首屏一次拉齐）────────────────────────────────────────────────
@router.get("/ops/overview")
def ops_overview(who: dict[str, Any] = Depends(caps.require_cap("page.ops"))) -> dict[str, Any]:
    """健康 + 备份摘要 + 审计状态。

    一次拉齐而不是三个接口：首屏三个板块的入场动画要一起起跑，分三次请求会
    各到各的、动画只能各自播 —— 那是「三个页面拼在一起」，不是一个页面。
    单次响应在 5KB 量级（备份只给前 12 条），比三次往返省。
    """
    backups = ops_store.list_backups(12)
    return {
        "ok": True,
        "health": ops_store.health(),
        "backups": backups,
        "audit": ops_store.audit_state(),
    }


# ── 备份 ───────────────────────────────────────────────────────────────────
@router.get("/ops/backups")
def ops_backups(
    limit: int = 60, who: dict[str, Any] = Depends(caps.require_cap("page.ops"))
) -> dict[str, Any]:
    return {"ok": True, **ops_store.list_backups(limit)}


@router.post("/ops/backups")
def ops_backup_now(
    request: Request, who: dict[str, Any] = Depends(caps.require_cap("ops.backup"))
) -> dict[str, Any]:
    """立刻备份。不需要确认词 —— 它只增不减，最坏结果是多一个文件。"""
    try:
        res = ops_store.create_backup()
    except Exception as e:  # noqa: BLE001
        audit.record(who["user"], "ops.backup.create", target="index+versions",
                     detail={"ok": False, "error": f"{type(e).__name__}: {e}"}, ip=_client_ip(request))
        raise HTTPException(status_code=500, detail=f"备份失败：{e}") from e
    audit.record(
        who["user"], "ops.backup.create", target=res["stamp"],
        detail={"ok": True, "rows": res["rows"], "rotated": len(res["rotated"]),
                "files": [it.get("file") for it in res["items"] if it.get("file")]},
        ip=_client_ip(request),
    )
    return {"ok": True, **res}


@router.post("/ops/backups/{name}/verify")
def ops_backup_verify(
    name: str, request: Request, who: dict[str, Any] = Depends(caps.require_cap("ops.backup"))
) -> dict[str, Any]:
    try:
        res = ops_store.verify_backup(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit.record(who["user"], "ops.backup.verify", target=name,
                 detail={"ok": res["ok"], "rows": res["rows"], "sha256Match": res["sha256Match"]},
                 ip=_client_ip(request))
    return {"ok": True, **res}


@router.post("/ops/backups/{name}/restore")
def ops_backup_restore(
    name: str,
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    who: dict[str, Any] = Depends(caps.require_cap("ops.restore")),
) -> dict[str, Any]:
    """把库换成这份备份。这是本项目唯一能让人「丢数据」的接口。"""
    _need_confirm(payload, "restore")
    try:
        res = ops_store.restore_backup(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 —— 恢复失败必须把安全网文件名交出去
        audit.record(who["user"], "ops.backup.restore", target=name,
                     detail={"ok": False, "error": f"{type(e).__name__}: {e}"},
                     ip=_client_ip(request))
        raise HTTPException(status_code=500, detail=f"恢复失败：{e}") from e
    audit.record(
        who["user"], "ops.backup.restore", target=name,
        detail={"ok": True, "label": res["label"], "rowsBefore": res["rowsBefore"],
                "rowsAfter": res["rowsAfter"], "safeguard": res["safeguard"]},
        ip=_client_ip(request),
    )
    return {"ok": True, **res}


# ── 审计轮转 ───────────────────────────────────────────────────────────────
@router.get("/ops/audit")
def ops_audit(who: dict[str, Any] = Depends(caps.require_cap("page.ops"))) -> dict[str, Any]:
    return {"ok": True, **ops_store.audit_state()}


@router.post("/ops/audit/rotate")
def ops_audit_rotate(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    who: dict[str, Any] = Depends(caps.require_cap("ops.rotate")),
) -> dict[str, Any]:
    """跑 脚本/audit_rotate.py。

    走子进程而不是 import 进来调函数：那个脚本是给人手跑的 CLI，参数语义
    （`--force` / `--dry-run` / 三条前置校验）已经写在它的 argparse 与注释里。
    HTTP 层再复刻一遍判定，迟早两边不一致 —— 一个说能转、一个说不能。
    """
    _need_confirm(payload, "rotate")
    script = ops_store.CODE_ROOT / "脚本" / "audit_rotate.py"
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"找不到轮转脚本：{script}")

    year = payload.get("year")
    cmd = [sys.executable, str(script)]
    if isinstance(year, int) and 2000 <= year <= 2100:
        cmd += ["--year", str(year)]
    if payload.get("force") is True:
        cmd += ["--force"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=504, detail="轮转超时（180s），请改用命令行执行") from e

    ok = proc.returncode == 0
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    audit.record(
        who["user"], "ops.audit.rotate", target=str(year or "auto"),
        detail={"ok": ok, "code": proc.returncode, "stdout": out[-800:], "stderr": err[-800:]},
        ip=_client_ip(request),
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail={"error": "轮转没有执行", "code": proc.returncode, "stdout": out, "stderr": err},
        )
    return {"ok": True, "code": proc.returncode, "stdout": out, "stderr": err,
            "audit": ops_store.audit_state()}


# ── 令牌回收 ───────────────────────────────────────────────────────────────
@router.post("/ops/tokens/sweep")
def ops_tokens_sweep(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    who: dict[str, Any] = Depends(caps.require_cap("ops.token.sweep")),
) -> dict[str, Any]:
    _need_confirm(payload, "sweep")
    try:
        res = ops_store.sweep_tokens(who["user"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"回收失败：{e}") from e
    audit.record(
        who["user"], "ops.token.sweep", target=str(res["count"]),
        detail={"ok": not res["failed"], "count": res["count"],
                "jtis": [s["jti"] for s in res["swept"]][:20],
                "failed": len(res["failed"])},
        ip=_client_ip(request),
    )
    return {"ok": True, **res}
