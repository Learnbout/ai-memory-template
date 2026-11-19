"""mcp SDK 兼容层（2026-10-09，G10 迁移后续）：全部 SDK 依赖收口于此。

背景：v1→v2 迁移暴露的问题不是「API 改名」，而是我们三处直连 SDK（server 构造、
工具注册、热重载内部结构）散在两个文件里，其中两处摸了 SDK 内部（_mcp_server /
Connection.for_loop），大版本升级即碎。本层把它们收口成一个文件：

  - 上层（memory_runtime / memory_tools / smoke_test）只 import 本模块；
  - SDK 升级或回滚 = 换 pip 版本即用（v1.30.x 与 v2.x 双分支共存），上层零改动；
  - 新的大版本出来时，只需在本文件加一个分支，并照例用「裸 JSON-RPC 模拟真实
    宿主」打热重载生死点（经验见 知识_MCPv2迁移落地 2026-10-09）。

每个分支都写明依赖哪个内部结构、失效时的退路。
"""

from __future__ import annotations

import functools
import importlib.metadata
import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger("ai-memory.compat")

SDK_VERSION = importlib.metadata.version("mcp")
SDK_MAJOR = int(SDK_VERSION.split(".")[0])

# ToolAnnotations：v1/v2 均在 mcp.types，字段名 v2 起改 snake_case（readOnlyHint →
# read_only_hint），但构造关键字两种写法 pydantic 都接受 v2；v1 只收 camelCase。
# 诊断探针读字段时注意口径差异。
from mcp.types import ToolAnnotations  # noqa: E402,F401

__all__ = [
    "SDK_VERSION",
    "SDK_MAJOR",
    "ToolAnnotations",
    "make_server",
    "register_tool",
    "run_stdio",
    "run_http",
    "enable_bearer_auth",
    "enable_oauth_server",
    "make_access_token",
    "get_access_token",
    "make_tool_error",
    "extract_text",
]


def make_server(name: str, instructions: str | None = None):
    """构造 server 实例。v2 改名 MCPServer（FastMCP 硬删），v1 为 FastMCP。"""
    if SDK_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name, instructions=instructions)
    from mcp.server.fastmcp import FastMCP

    return FastMCP(name, instructions=instructions)


def register_tool(server: Any, fn: Callable, annotations: Any = None) -> Callable:
    """注册一个工具，返回原函数。

    v2：func_metadata.call_fn 原生把同步工具丢 anyio.to_thread（func_metadata.py:164），
    同步函数直接注册即可。
    v1：同步函数会被直接调在事件循环上（阻塞），必须包 async_tool 丢线程池 ——
    这是 2026-09-10 MCP异步改造 的原始动机，回滚 v1 时该行为必须保留。
    """
    if SDK_MAJOR >= 2:
        server.tool(annotations=annotations)(fn)
        return fn

    if inspect.iscoroutinefunction(fn):
        server.tool(annotations=annotations)(fn)
        return fn

    @functools.wraps(fn)
    async def _async_tool(*args, **kwargs):
        import anyio

        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    server.tool(annotations=annotations)(_async_tool)
    return fn


def _apply_v2_born_ready_patch() -> bool:
    """v2：让 legacy 连接 born-ready，语义等价 v1 的 stateless=True。

    依赖内部结构：mcp.server.connection.Connection.for_loop（classmethod）与
    连接对象的 initialized 事件（connection.py:212）。v2 的 legacy 时代连接带
    pre-init 门（runner.py:211）：未握手连接的裸 tools/call 一律 -32602 ——
    热重载后宿主续发的请求正是这种，不打补丁热重载必死（宿主实测）。

    失效退路：SDK 内部结构变了（找不到 for_loop / initialized）就跳过并告警，
    退化为「热重载后需重启宿主」——通道本身不炸，与 v1 时代 stateless 缺失同险级。
    """
    try:
        from mcp.server import connection as _connection_mod

        orig_for_loop = _connection_mod.Connection.for_loop

        def _born_ready_for_loop(cls, *args, **kwargs):
            conn = orig_for_loop(*args, **kwargs)
            evt = getattr(conn, "initialized", None)
            if evt is not None and hasattr(evt, "set"):
                evt.set()
            return conn

        _connection_mod.Connection.for_loop = classmethod(_born_ready_for_loop)
        return True
    except Exception as e:  # noqa: BLE001 - 补丁失败绝不阻塞启动
        logger.warning("born-ready 补丁未生效（SDK 内部结构可能已变）：%s；热重载后将需重启宿主", e)
        return False


def run_stdio(server: Any, *, hot_reload_safe: bool = True) -> None:
    """以 stdio 方式阻塞运行 server。

    v2：born-ready 补丁（可关）+ server.run("stdio")。
    v1：hot_reload_safe=True 时走 lowlevel.run(stateless=True)（依赖 _mcp_server
    内部结构，缺失则退回 server.run()）；False 直接 server.run()。
    """
    if SDK_MAJOR >= 2:
        if hot_reload_safe:
            _apply_v2_born_ready_patch()
        server.run(transport="stdio")
        return

    import anyio
    from mcp.server.stdio import stdio_server

    lowlevel = getattr(server, "_mcp_server", None)
    if not hot_reload_safe or lowlevel is None or not hasattr(lowlevel, "create_initialization_options"):
        server.run()
        return

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await lowlevel.run(
                read_stream,
                write_stream,
                lowlevel.create_initialization_options(),
                stateless=True,
            )

    anyio.run(_main)


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _transport_security(host: str, allow_hosts: "list[str] | None"):
    """构造 DNS-rebinding 防护设置（v2 专用）；返回 None 表示交给 SDK 默认。

    2026-10-22 阶段 1 实测 SDK 2.3.0 的真实行为：lowlevel.streamable_http_app 只在
    host 为 127.0.0.1 / localhost / ::1 时**自动**开启防护，并自动写入
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]。绑定 0.0.0.0 或某个具体
    内网 IP 时 transport_security 保持 None —— 也就是**检查是关的**，任何 Host 头都
    放行。这是 SDK 的默认取向（它假设你只在本机跑），不是我们这层的选择。

    为什么仍然保留「None」这条路：内网自用阶段把白名单抄错会直接 400，而错误信息
    在客户端只表现为「连接失败」，排查成本远高于收益。所以策略是
    —— 本机绑定：吃 SDK 默认（防护开）；跨机绑定：默认不设，临时告警一次，
    等阶段 2 上令牌时再把白名单一起钉死（那时本来就要求显式配置）。

    allow_hosts 非空时覆盖上述逻辑，例如反代场景：
    --allow-host mcp.example.net:8770 --allow-host mcp.lan:8770
    """
    if allow_hosts:
        try:
            from mcp.server.transport_security import TransportSecuritySettings
        except Exception as e:  # noqa: BLE001 - 拿不到类就退回 SDK 默认
            logger.warning("拿不到 TransportSecuritySettings，DNS-rebinding 防护未配置：%s", e)
            return None
        hosts = list(allow_hosts)
        origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    if host not in _LOCAL_HOSTS:
        logger.warning(
            "绑定 %s（非本机回环）：SDK 不会自动开启 DNS-rebinding 防护，"
            "当前任何 Host 头都放行。仅限内网使用；要钉白名单请传 --allow-host",
            host,
        )
    return None


def run_http(
    server: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    path: str = "/mcp",
    stateless: bool = True,
    allow_hosts: "list[str] | None" = None,
) -> None:
    """以 Streamable HTTP 方式阻塞运行 server（2026-10-22 阶段 1 新增）。

    设计取向 —— **只加出口，不改 stdio**：本地宿主继续走 run_stdio，跨设备才走本
    函数。两条路共用同一个 server 实例与同一套工具，因此写路径天然一致（都落到
    memory_store，不存在「HTTP 版逻辑」这种第二套实现）。

    关于 stateless 默认 True（实测取此默认，非拍脑袋）：
      - 我们的工具全是「一次调用一次读写」，不需要 server→client 通知、不需要
        SSE 续传、不需要在服务端保存会话上下文；
      - stateless 下每个请求自带完整语义，客户端断线重连/多客户端并发都不必先
        握手建会话，也不会出现「会话过期后所有调用 404」这类坑；
      - 2026-07-28 版 MCP 规范本身就是往「去掉 initialize 握手与 Mcp-Session-Id」
        的方向走的，stateless 与该方向一致。
      需要会话时用 --stateful 打开（SDK 会发 Mcp-Session-Id）。

    v2（SDK 2.3.0 实测）：MCPServer.run(transport="streamable-http", **kwargs) 把
    kwargs 透传给 run_streamable_http_async（host/port/streamable_http_path/
    stateless_http/transport_security），内部起 uvicorn。
    v1：FastMCP 把 host/port 放在 self.settings 里，run() 只收 transport 与
    mount_path，故先改 settings 再 run；settings 结构变了就退回裸调用并告警。
    """
    if SDK_MAJOR >= 2:
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "streamable_http_path": path,
            "stateless_http": stateless,
        }
        sec = _transport_security(host, allow_hosts)
        if sec is not None:
            kwargs["transport_security"] = sec
        server.run(transport="streamable-http", **kwargs)
        return

    # ── v1 兜底分支（本机未装 v1，按 v1.30 结构写，失败即告警不崩）──────────────
    settings = getattr(server, "settings", None)
    if settings is not None:
        for attr, value in (
            ("host", host),
            ("port", port),
            ("streamable_http_path", path),
            ("stateless_http", stateless),
        ):
            try:
                setattr(settings, attr, value)
            except Exception as e:  # noqa: BLE001 - 字段不存在就跳过
                logger.warning("v1 settings.%s 设置失败（SDK 结构可能已变）：%s", attr, e)
    else:
        logger.warning("v1 未找到 server.settings，host/port 沿用 SDK 默认（127.0.0.1:8000）")
    try:
        server.run(transport="streamable-http")
    except TypeError:
        server.run(transport="streamable-http", mount_path=path)


def enable_bearer_auth(
    server: Any,
    verifier: Any,
    *,
    resource: str,
    required_scopes: "list[str] | None" = None,
) -> None:
    """给 server 装 Bearer 令牌校验（2026-10-22 阶段 2）。

    v2 走 SDK 原生链路：`RequireAuthMiddleware` + `BearerAuthBackend`，由
    `streamable_http_app(auth=self.settings.auth, token_verifier=self._token_verifier)`
    读取。所以这里做的事就是**把两个属性填上**，不自己写 ASGI 中间件 ——
    401 的 WWW-Authenticate、RFC 9728 元数据这些细节交给 SDK，比手写可靠。

    依赖的内部结构：`server.settings.auth`（Settings 可写）与 `server._token_verifier`。
    已在 2.3.0 实测可赋值。SDK 改名时的退路：抛 RuntimeError 让启动失败并打印原因，
    **绝不静默降级成无认证** —— 「以为上了锁其实没上」是这里唯一不能接受的失效。

    validate_token_resource=True：让 SDK 再按 resource_server_url 校验一次 aud。
    我们自己的 verifier 已经验过 aud，这里是第二道 —— 两道判据同一个值时不会冲突，
    值不同则说明配置错了，早失败比晚失败好。
    """
    if SDK_MAJOR < 2:
        raise RuntimeError(
            "v1 SDK 没有 TokenVerifier 机制，--require-token 仅支持 mcp>=2.0；"
            "请升级 SDK 或去掉该参数（不要在没有认证的情况下暴露到内网以外）"
        )
    try:
        from mcp.server.auth.settings import AuthSettings
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"拿不到 AuthSettings，无法启用令牌校验：{e}") from e

    from urllib.parse import urlsplit

    parts = urlsplit(resource)
    issuer_url = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else resource

    try:
        settings = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource,
            required_scopes=list(required_scopes or []),
            validate_token_resource=True,
        )
        server.settings.auth = settings
        server._token_verifier = verifier
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"启用令牌校验失败（SDK 结构可能已变）：{e}") from e
    logger.info("Bearer 令牌校验已启用：resource=%s scopes=%s", resource, required_scopes)


def _patch_metadata_advert() -> None:
    """修正 SDK 元数据里两个「对我们不成立」的宣告（2026-10-27 阶段 13）。

    SDK 的 `build_metadata` 把两样东西写死了，两条都会坑到真实客户端：

    1. `token_endpoint_auth_methods_supported` 只有 client_secret_post/basic，
       **没有 "none"**。可我们是公开客户端模型（注册时不发 secret，靠 PKCE 保安全）。
       严格按元数据办事的客户端看到没有 "none"，会以为「必须要 secret」——要么报
       「不支持的认证方式」，要么干脆不发请求。网页版 ChatGPT 正是公开客户端。
    2. `grant_types_supported` 里写了 refresh_token，而我们**刻意不签刷新票**
       （少一条长期有效的凭证路径）。客户端按元数据去刷新时会打到
       NotImplementedError，变成 5xx。

    做法是给模块级函数打补丁而不是改 site-packages：`create_auth_routes` 里是
    全局名查找，替换函数即可生效；升级 SDK 后若函数改名，try/except 兜住并告警，
    不影响服务启动（退化回「宣告不准」而不是「起不来」）。
    """
    try:
        from mcp.server.auth import routes as _routes

        if getattr(_routes, "_ai_memory_advert_patched", False):
            return
        original = _routes.build_metadata

        def _patched(*args: Any, **kwargs: Any):
            md = original(*args, **kwargs)
            methods = list(md.token_endpoint_auth_methods_supported or [])
            if "none" not in methods:
                methods.insert(0, "none")
            md.token_endpoint_auth_methods_supported = methods
            if md.grant_types_supported:
                md.grant_types_supported = [
                    g for g in md.grant_types_supported if g != "refresh_token"
                ]
            return md

        _routes.build_metadata = _patched
        _routes._ai_memory_advert_patched = True  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 - 元数据不准不等于服务不能用
        logger.warning("修正 OAuth 元数据宣告失败（SDK 结构可能已变）：%s", e)


def enable_oauth_server(
    server: Any,
    *,
    secret: bytes,
    ledger: Any,
    resource: str,
    issuer_url: str,
    config_path: Any,
    state_dir: Any,
    scopes: "list[str] | None" = None,
):
    """把授权服务器接到 server 上（2026-10-27 阶段 13）。

    只做三件事，全部是「填属性 / 挂路由」，不手写任何 OAuth 端点：
      1. `settings.auth.client_registration_options` —— 决定 /register 是否开、以及
         允许客户端申请哪些 scope（不设 = 不限制，等于把 scope 的决定权交出去，
         所以这里显式钉死）
      2. `server._auth_server_provider` —— SDK 看到它就自动 extend
         `create_auth_routes(...)`，四个端点一次到位
      3. 挂 /consent 口令页（走 custom_route，明确免鉴权）

    与 enable_bearer_auth 的失败策略**刻意不同**：那边装不上必须让进程起不来
    （「必须有锁」），这边装不上只告警 —— OAuth 是增量入口，它坏不该把已有的
    带票老路一起拖垮。
    """
    if SDK_MAJOR < 2:
        raise RuntimeError("v1 SDK 没有 auth_server_provider 机制，OAuth 需 mcp>=2.0")

    from mcp.server.auth.settings import ClientRegistrationOptions

    import oauth_server

    _patch_metadata_advert()

    allowed = list(scopes or ["tools:read"])
    settings = getattr(server, "settings", None)
    auth = getattr(settings, "auth", None)
    if auth is None:
        raise RuntimeError("未启用 Bearer 校验，OAuth 无从谈起（先调 enable_bearer_auth）")

    provider = oauth_server.VaultAuthProvider(
        secret=secret,
        ledger=ledger,
        resource=resource,
        config_path=config_path,
        state_dir=state_dir,
    )
    try:
        auth.client_registration_options = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=allowed,
            default_scopes=allowed[:1],
        )
        server._auth_server_provider = provider
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"启用 OAuth 授权服务器失败（SDK 结构可能已变）：{e}") from e

    oauth_server.install_consent_route(server, provider, issuer_url)
    logger.info("OAuth 授权服务器已启用：issuer=%s scopes=%s", issuer_url, allowed)
    return provider


def make_access_token(
    *,
    token: str,
    client_id: str,
    scopes: "list[str]",
    expires_at: int,
    resource: str,
    subject: str,
    claims: "dict[str, Any]",
):
    """构造 SDK 的 AccessToken。字段名变了就退回 None（调用方据此 fail-closed）。"""
    try:
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=resource,
            subject=subject,
            claims=claims,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("构造 AccessToken 失败（SDK 字段可能已变）：%s", e)
        return None


def get_access_token():
    """取当前请求的 AccessToken。拿不到返回 None。

    拿不到时调用方必须**拒绝**而不是放行 —— 见 mcp_auth.require_scope_for。
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token as _get

        return _get()
    except Exception:  # noqa: BLE001
        return None


def make_tool_error(message: str) -> Exception:
    """构造一个「客户端能看到原文」的工具错误。

    SDK 的 Tool.run 只把 ToolError 的原文透给客户端，其它异常一律替换成
    `Error executing tool <name>` 并把原文吃掉 —— 权限拒绝必须让调用方看见原因
    （「假成功」和「只说崩了不说为什么」都是坏体验）。
    """
    try:
        from mcp.server.mcpserver.exceptions import ToolError

        return ToolError(message)
    except Exception:  # noqa: BLE001
        return PermissionError(message)


def extract_text(res: Any) -> str:
    """call_tool 结果 → 真实文本（测试与探针用）。

    v2：CallToolResult（content[0].text）。
    v1.30：FastMCP.call_tool 直接返回内容块列表（res[0].text）。
    兜底 str() —— 但 repr 会把换行渲染成字面 \\n（09-16 老坑），只作最后退路。
    """
    content = getattr(res, "content", None)
    if content and getattr(content[0], "text", None) is not None:
        return content[0].text
    if isinstance(res, list) and res and getattr(res[0], "text", None) is not None:
        return res[0].text
    if isinstance(res, tuple) and len(res) == 2:
        first = res[0]
        if isinstance(first, list) and first:
            block = first[0]
            if getattr(block, "text", None) is not None:
                return block.text
    return str(res)
