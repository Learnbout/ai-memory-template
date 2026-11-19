"""当前身份与能力（阶段 9）。

前端只认这一个接口，不再自己猜角色：

- 之前 `FloatingNav` 数着 `role === 'readonly'` 过滤导航，`Settings` 又按
  `acct.auth` 决定挂不挂账号分区，两处各判一次；页面内按钮则**完全不判**，
  只等着被网关/后端 403（用户看到的是「点了没反应」或一句「请求失败」）。
- 现在 `/api/me` 一次给出 `role` + `caps`，前端用 `useCaps()` 读同一份，
  导航和按钮走同一个判断；后端对此**不看前端脸色**，每个写接口另有 `require_cap`。

为什么不做成 `/api/gate/me` 的扩展：那个接口在网关进程里，直连模式（本机 8787、
绕过网关）根本没有它。放在 Graphite 后端，直连与经网关两种访问都能拿到，
且 `local` 字段能把「本地单人全权」这层语义讲明白。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import caps

router = APIRouter(tags=["me"])


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    who = caps.current(request)
    return {"ok": True, **who}
