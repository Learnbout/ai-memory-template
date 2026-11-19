"""版本检查与一键更新 HTTP 层（阶段 12，2026-10-25）· 前缀 `/api/update`

分层：本文件只做 HTTP（鉴权、确认词、编排、审计）；算法与磁盘操作在
`app/updater.py`。

## 三条设计口径

1. **看状态不需要高权限，动手才要。** `GET /update/status` 走 `page.ops`
   （能进运维台就能看版本）；`apply` / `rollback` 走 `ops.update`（只有 admin）。
   理由：知道「有新版本」是信息，替换本机代码是动作。
2. **确认词与运维台共用一张表。** 直接复用 `ops_api.CONFIRM_WORDS`，不另起一份 ——
   两份文案迟早对不上，而确认词对不上等于这道闸时灵时不灵。
3. **先回响应，再重启。** 重启自己的进程会让这次 HTTP 请求当场断掉，前端只会看到
   「请求失败」并以为更新失败。所以顺序是：写完文件 → 立即 200 → 由 detached
   子进程延迟 2 秒 `systemctl restart`。

## 为什么重启要延迟 + 独立会话

`start_new_session=True` 让子进程脱离本进程的进程组：本进程被 systemd 杀掉时，
它不会被一起带走。延迟 2 秒是为了让这次 HTTP 响应先完整写回 socket。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from .. import caps, updater
from ..paths import update_settings

# 库根与 <库>/脚本 进 sys.path，才能 import audit
from ..paths import ensure_import_path

ensure_import_path()

import audit  # noqa: E402

from .ops_api import CONFIRM_WORDS, _need_confirm  # noqa: E402  —— 确认词单源

router = APIRouter(tags=["update"])

# systemd 单元名白名单：它最终会拼进 shell 命令，不做校验就是命令注入。
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

# 同进程内串行化更新：两个标签页同时点「更新」会互相盖文件，备份也会错位
_APPLY_LOCK = threading.Lock()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _schedule_restart(service: str) -> str:
    """排一次延迟重启。返回 scheduled / manual / no-systemctl / bad-unit。"""
    if not service:
        return "manual"
    if not _UNIT_RE.match(service):
        return "bad-unit"
    if not shutil.which("systemctl"):
        return "no-systemctl"
    try:
        subprocess.Popen(  # noqa: S603 —— 单元名已过白名单，且不走 shell 解释
            ["/bin/sh", "-c", f"sleep 2; systemctl restart {service}"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "manual"
    return "scheduled"


@router.get("/update/status")
def update_status(
    who: dict[str, Any] = Depends(caps.require_cap("page.ops")),
) -> dict[str, Any]:
    """版本状态 + 可回滚点 + 重启能力。**检查失败也返回 200**（reason 里写原因）。"""
    s = update_settings()
    info = updater.check(s)
    # check() 里的 `ok` 是「检查成功没」，外层的 `ok` 是「这次 HTTP 成功没」。
    # 两个同名键撞在一起会互相覆盖（字典展开后者赢），所以这里改名分开 ——
    # 前端要靠 checkOk 区分「没新版本」和「连不上 GitHub」。
    check_ok = bool(info.pop("ok", False))
    info["backups"] = updater.list_backups(s, limit=8)
    info["confirmWords"] = {"update": CONFIRM_WORDS["update"], "rollback": CONFIRM_WORDS["rollback"]}
    info["canApply"] = bool("ops.update" in who["caps"])
    info["service"] = s["service"]
    # 阶段 10 第 5 步：降权后写代码树要经 root oneshot。这里如实报出"这次会走哪条路"，
    # 免得排障时怀疑"按钮点了没反应"到底是功能坏了还是通道没通。
    info["privileged"] = updater.privileged_mode(s)
    info["restartMode"] = (
        "helper" if info["privileged"]
        else ("scheduled" if (s["service"] and shutil.which("systemctl")) else "manual")
    )
    return {"ok": True, "checkOk": check_ok, **info}


@router.post("/update/channel")
def update_channel(
    payload: dict[str, Any] = Body(default={}),  # noqa: ARG001 —— 只为统一前端调用形状
    who: dict[str, Any] = Depends(caps.require_cap("ops.update")),
) -> dict[str, Any]:
    """特权通道自检（不改任何文件）。

    为什么单独一个端点而不塞进 `status`：`status` 是进入运维台就会拉一次的
    只读接口，而自检会**真的触发一次 root oneshot**。把有副作用的动作放进
    每次开页面都跑的接口里，等于让"看一眼"变成"按一下"。
    """
    try:
        return {"ok": True, **updater.channel_check()}
    except updater.UpdateError as e:
        return {"ok": False, "error": str(e)[:600]}


@router.post("/update/apply")
def update_apply(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    who: dict[str, Any] = Depends(caps.require_cap("ops.update")),
) -> dict[str, Any]:
    """下载 → 白名单校验 → 备份 → 覆盖 → 排重启。

    任何一步出错都在**动盘之前**中止；动了盘之后出错由备份兜底（前端可点回滚）。
    """
    _need_confirm(payload, "update")
    s = update_settings()
    if not s["enabled"]:
        raise _bad("本实例没有开启一键更新（update_enabled=0）")

    if not _APPLY_LOCK.acquire(blocking=False):
        raise _bad("已经有一次更新在跑，等它结束")
    tmp: Path | None = None
    try:
        info = updater.check(s)
        if not info["ok"]:
            raise _bad(f"检查版本失败：{info['reason']}")
        if not info["behind"]:
            raise _bad("本地已是最新，不需要更新")
        tag = str(payload.get("tag") or info.get("tag") or "").strip()
        if not tag:
            raise _bad("拿不到要装的 tag")
        # 只有「比本地新」才允许装：防止有人把 tag 改成旧版本，做成一次静默降级
        if not updater.is_newer(info["latest"], info["current"]):
            raise _bad("远端版本不比本地新，已拒绝")

        tmp = updater.temp_dir()
        tar = updater.download(tag, tmp, s)
        plan = updater.inspect(tar, s)
        res = updater.apply(tar, s)
        # 特权通道下重启由 root oneshot 自己延迟执行：它必须等这次 HTTP 响应
        # 先写回 socket，否则进程被杀、前端只看到连接重置，一次成功会显示成失败。
        if res.get("restartVia") == "helper":
            restart = "helper-scheduled" if s["service"] else "manual"
        else:
            restart = _schedule_restart(s["service"])

        audit.record(
            who["user"], "ops.update.apply", target=tag,
            detail={
                "ok": True, "from": info["current"], "to": res["version"],
                "written": res["written"], "py": plan["pyCount"], "dist": plan["distCount"],
                "skipped": plan["skipCount"], "danger": plan["danger"],
                "backup": Path(res["backup"]).name, "restart": restart,
            },
            ip=_ip(request),
        )
        return {
            "ok": True,
            "from": info["current"],
            "to": res["version"],
            "written": res["written"],
            "plan": {k: plan[k] for k in ("pyCount", "distCount", "skipCount", "danger")},
            "backup": Path(res["backup"]).name,
            "restart": restart,
        }
    except updater.UpdateError as e:
        audit.record(who["user"], "ops.update.apply", target=str(payload.get("tag") or ""),
                     detail={"ok": False, "error": str(e)[:400]}, ip=_ip(request))
        raise _bad(str(e)) from e
    finally:
        if tmp is not None:
            _cleanup(tmp)
        _APPLY_LOCK.release()


@router.post("/update/rollback")
def update_rollback(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    who: dict[str, Any] = Depends(caps.require_cap("ops.update")),
) -> dict[str, Any]:
    """把代码换回某个回滚点。`name` 必须是 `list_backups()` 里出现过的目录名 ——
    直接接受任意路径等于给了一个「把 /etc 复制进代码树」的入口。
    """
    _need_confirm(payload, "rollback")
    s = update_settings()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise _bad("缺少备份名")
    allowed = {b["name"] for b in updater.list_backups(s, limit=200)}
    if name not in allowed:
        raise _bad(f"没有这个回滚点：{name}")
    try:
        res = updater.restore(Path(s["backup_root"]) / name, s)
    except updater.UpdateError as e:
        audit.record(who["user"], "ops.update.rollback", target=name,
                     detail={"ok": False, "error": str(e)[:400]}, ip=_ip(request))
        raise _bad(str(e)) from e
    if res.get("restartVia") == "helper":
        restart = "helper-scheduled" if s["service"] else "manual"
    else:
        restart = _schedule_restart(s["service"])
    audit.record(who["user"], "ops.update.rollback", target=name,
                 detail={"ok": True, "version": res["version"], "restart": restart},
                 ip=_ip(request))
    return {"ok": True, **res, "restart": restart}


def _bad(reason: str):
    from fastapi import HTTPException

    return HTTPException(status_code=400, detail=reason)
