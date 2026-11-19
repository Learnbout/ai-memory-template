"""角色能力矩阵（阶段 9，2026-10-24）。

## 为什么要有这个模块

阶段 9 之前，「谁能干什么」散在四处各判一遍：

- 网关 `web_gate._user_role` 判 readonly 非 GET 一律 403（**只按方法**，不看接口）；
- `FloatingNav.canSee` 判导航显隐（**只管前端**，直接敲 URL 就绕过去了）；
- `chat_acl` 判聊天与引用等级（只管聊天这条线）；
- `admin_mcp._admin` 判管理接口（只管管理页）。

四套判断各说各话的后果是：`POST /api/admin/restart`、`PUT /api/settings`、
`POST /api/tools/{name}/invoke` 这三类「能停服务 / 能改库路径 / 能借服务端令牌调
MCP」的接口，此前**只有网关那道方法级粗闸**。一个 editor 拿到会话票就能重启发服务
（自己把自己踢下线）、改 `mcp.mcpEndpoint` 指向别处，或者调 `memory_sql` 直接
把 S 级凭据读走 —— 因为 Graphite 连 MCP 用的是配置里那张管理员令牌，不是调用者的。

## 落点

一张表 `MATRIX`：**角色 → 能力集合**。三处共用：

1. 后端写接口挂 `require_cap("xxx")`（真闸，拦住一切非页面路径）；
2. `GET /api/me` 把同一张表算出的 caps 下发给前端（前端只负责显隐，不承担安全）；
3. 前端 `useCaps()` 读它，导航与页面内按钮统一按 cap 判，不再各写一份 if。

## 三条口径

- **默认拒绝**：矩阵里没有的 cap = 没有。新加接口忘了登记 cap，行为是 403 而不是
  「默认放行」—— 漏登记会在测试里立刻暴露（见 `脚本/test_stage9_caps.py`）。
- **认不出身份就最严**：不在 `MATRIX` 里的角色（`guest`）拿空集。账号在 `users`
  表里、却不在 `web_auth.json` 的权限目录里 → 按 guest 处理，不是按 editor 兜底。
- **本地单人 = 全权**：`users` 表 0 行（本机个人库）时一切按 admin。这与
  `acl.auth_enabled()` / `chat_acl.require_member` 同一判据 —— 库就在自己机器上，
  加权限只是自欺；服务器上开了网关就一定有行。

## 为什么 `tool.invoke` 不给 editor

MCP Lab 的调用接口是**以服务端身份**发出去的（`mcp_client` 用配置文件里的令牌），
调用者是谁对 MCP 一侧不可见。给 editor 开这个口，等于把管理员令牌借给他：
`memory_sql` 一句 `SELECT body_md FROM notes` 就绕过全部等级过滤。
要真正按人降权，得让每次调用带上调用者自己的令牌 —— 那是另一件事，不塞进阶段 9。
所以这里取最简安全解：工具调用只给 admin，页面其余部分（看目录、看状态）editor 仍可看。
"""
from __future__ import annotations

import base64
from typing import Any, Callable

from fastapi import HTTPException, Request

from .paths import ensure_import_path

# 库根先进 sys.path，才 import 得到 db_paths / admin_api / web_auth
ensure_import_path()

import admin_api  # noqa: E402

from . import chat_acl  # noqa: E402

# ── 能力矩阵（唯一事实源）──────────────────────────────────────────────────
#
# 命名：page.* = 页面入口可见；其余 = 动作。新增能力只改这张表 + 前端 Cap 联合类型。
_ADMIN = {
    # 页面
    "page.dashboard",
    "page.mcp-lab",
    "page.agent-search",
    "page.memory-explorer",
    "page.chat",
    "page.run-history",
    "page.admin",
    "page.audit",
    "page.ops",              # 运维台（阶段 11）：备份 / 恢复 / 审计轮转 / 令牌回收
    "page.settings",
    "page.plans",
    # 动作
    "tool.invoke",
    "tool.reconnect",
    "agent.search",
    "settings.write",
    "settings.reload",
    "vault.reindex",
    "runs.clear",
    "system.restart",
    "admin.manage",
    "audit.view",
    "chat.use",
    "chat.moderate",
    # 阶段 11：运维。**只有 admin**，editor 连 page.ops 都没有。
    # 为什么不给 editor 一个「只读的运维页」：这页能列出全部备份文件名与库路径，
    # 而恢复 / 轮转 / 批量吊销都是不可逆动作 —— 把「看一眼」和「能按下去」
    # 放在同一个页面里，迟早有人按错。要看备份列表，通过管理员要一份即可。
    "ops.backup",
    "ops.restore",
    "ops.rotate",
    "ops.token.sweep",
    # 阶段 12：一键更新（下载 GitHub 包覆盖本机代码）。
    # 「能不能按」由这张表决定，「这个实例要不要给按钮」由 update_enabled 决定 ——
    # 两件事分开：别人照文档装一份，即便他是 admin，也不该能点「覆盖本机代码」。
    "ops.update",
    # 阶段 10：项目计划
    "plan.write",
    "plan.comment",
    "plan.moderate",
    "plan.archive",
}

_EDITOR = {
    "page.dashboard",
    "page.mcp-lab",          # 目录与连接状态可看；「调用」按钮另有 tool.invoke 闸
    "page.agent-search",
    "page.memory-explorer",
    "page.chat",
    "page.run-history",
    "page.settings",         # 能进设置页看，但改不了服务端配置（见 settings.write）
    "page.plans",
    "agent.search",
    "chat.use",
    "plan.write",
    "plan.comment",
}

_READONLY = {
    "page.dashboard",
    "page.memory-explorer",
    "page.settings",
    # 阶段 10 的口径：只读**能看**计划（与聊天不同 —— 聊天整页不给）。
    # 为什么这里放行：计划是「团队在干什么」的公共信息，只读成员最需要知道的
    # 恰恰是进度；挡掉它，这个角色就只剩下翻笔记一个用途。
    # 但他不能建、不能改、不能留言、也不能导出（导出算复制）。
    "page.plans",
}

MATRIX: dict[str, frozenset[str]] = {
    "admin": frozenset(_ADMIN),
    "editor": frozenset(_EDITOR),
    "readonly": frozenset(_READONLY),
    # 认不出的角色 → 空集（fail-closed）。显式写出来，避免「忘了这个键」变成 KeyError 500。
    "guest": frozenset(),
}

ALL_CAPS: tuple[str, ...] = tuple(sorted(_ADMIN))
ROLE_LABELS = {"admin": "管理员", "editor": "可修改", "readonly": "只读", "guest": "无权限"}

LOCAL_USER = chat_acl.LOCAL_USER

# 本地版**没有**的能力：多人协作那套（聊天 / 账号管理 / 审计 / 运维台）连页面带动作一起摘掉。
#
# 为什么必须在**能力表这一层**摘，而不是只靠「路由不注册」：
# 路由不注册解决的是「后端不接受请求」（访问得到 404），但前端的导航是**按 caps 渲染**的 ——
# caps 里还留着 `page.chat`，底栏就照样画出一个聊天图标，点进去才是 404。
# 2026-11-19 实际就是这个症状：本地版导航栏里还挂着聊天/管理/审计/运维四个入口。
LOCAL_EXCLUDED = frozenset({
    "page.chat", "page.admin", "page.audit", "page.ops",
    "chat.use", "chat.moderate", "audit.view",
    "ops.backup", "ops.restore", "ops.rotate", "ops.token.sweep", "ops.update",
    "admin.manage", "system.restart",
})


def _is_local_mode() -> bool:
    from .paths import is_local

    return is_local()


def caps_of(role: str) -> list[str]:
    """角色 → 能力列表（顺序稳定，便于前端缓存比较）。

    本地版额外做一次减法：把聊天 / 管理 / 审计 / 运维台相关能力摘掉 —— 这些页面本地版
    根本不注册路由，留着它们只会让导航栏多出四个「点了就 404」的入口。
    """
    caps = set(MATRIX.get(str(role or "guest"), MATRIX["guest"]))
    if _is_local_mode():
        caps -= LOCAL_EXCLUDED
    return sorted(caps)


def decode_user(header_value: str | None) -> str:
    """解网关注入的用户名。解不出返回空串（不抛）。"""
    raw = (header_value or "").strip()
    if not raw:
        return ""
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8").strip()
    except (ValueError, UnicodeError):
        return ""


def role_of(username: str) -> str:
    """用户名 → Web 角色。

    顺序有意为之：先问「这台机器有没有启用账号体系」，再问「这个人是哪个角色」。
    反过来会把本地单人环境判成 guest（本地没有 web_auth.json），整站全 403。

    阶段 26（2026-11-18）：本地版**没有登录概念**，一律全权 —— 它的安全边界是
    「只监听 127.0.0.1」（由 run.py / run_resident 保证），不是角色表。
    中心化版才需要按 users / web_auth 判角色。
    """
    from .paths import is_local

    if is_local():
        return "admin"
    if not chat_acl.multiuser_enabled():
        return "admin"
    if not username:
        return "guest"
    try:
        role = admin_api.web_role(username)
    except Exception:  # noqa: BLE001 —— 权限目录读不出来时按最严，不放行
        return "guest"
    return role or "guest"


def current(request: Request) -> dict[str, Any]:
    """解析当前请求的身份与能力（只读，不抛 403）。

    带了解不出的头 → 401；没带头且已启用账号体系 → 401（网关一定会带头，
    没带说明绕过了网关，那是异常路径，不能当匿名放行）。
    """
    header = request.headers.get("X-AI-Memory-User-B64")
    multiuser = chat_acl.multiuser_enabled()
    if header and not decode_user(header):
        raise HTTPException(status_code=401, detail="登录凭证无法解析，请重新登录")
    if not header and multiuser:
        raise HTTPException(status_code=401, detail="未登录：请求没有经过登录网关")

    user = decode_user(header) or LOCAL_USER
    role = role_of(user) if multiuser else "admin"
    try:
        display = admin_api.display_name_of(user) or user
    except Exception:  # noqa: BLE001
        display = user
    return {
        "user": user,
        "display_name": display,
        "role": role,
        "role_label": ROLE_LABELS.get(role, "无权限"),
        "caps": caps_of(role),
        "local": not multiuser,
        "multiuser": multiuser,
    }


def require_cap(cap: str) -> Callable[[Request], dict[str, Any]]:
    """FastAPI 依赖：要求某个能力，缺了就 403。

    403 的 detail 是结构化对象（不是一句话）：前端要按 `need` 显示「这页需要管理员」，
    而不是把「需要管理员权限」当成通用文案贴在每个地方。
    """
    if cap not in MATRIX["admin"]:
        raise RuntimeError(f"未登记的能力：{cap}（先在 caps.MATRIX 里加，再接依赖）")

    def _dep(request: Request) -> dict[str, Any]:
        who = current(request)
        if cap in who["caps"]:
            return who
        raise HTTPException(
            status_code=403,
            detail={
                "error": f"当前角色（{who['role_label']}）没有这个权限",
                "need": cap,
                "role": who["role"],
            },
        )

    return _dep
