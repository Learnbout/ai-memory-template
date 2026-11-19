"""设置路由（§5.9）。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from .. import caps, settings_store
from ..schemas import SettingsData

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsData)
def get_settings() -> SettingsData:
    return SettingsData(**settings_store.get_settings())


# 写设置 = 改全站配置（库根路径 vaultPath、MCP 地址 mcpEndpoint、拦写开关 blockWrite）。
# 这三项落到别人手里就是「改库指向」与「改工具去向」，所以只给 admin（阶段 9）。
# GET 不设闸：只读账号也能看到当前配置，知道自己在什么环境里。
@router.put("/settings", response_model=SettingsData)
def update_settings(
    patch: Dict[str, Any] = Body(default_factory=dict),
    who: Dict[str, Any] = Depends(caps.require_cap("settings.write")),
) -> SettingsData:
    return SettingsData(**settings_store.update_settings(patch))


@router.post("/settings/reload", response_model=SettingsData)
def reload_settings(
    who: Dict[str, Any] = Depends(caps.require_cap("settings.reload")),
) -> SettingsData:
    """配置热重载（§19.17）：丢弃内存 _state、从磁盘重读 settings.json。

    场景：临时实例 / 手工编辑改了磁盘文件，常驻内存里还是旧的；
    这条让配置层变更不用杀进程。代码级变更请用 /api/admin/restart。"""
    return SettingsData(**settings_store.reload_settings())
