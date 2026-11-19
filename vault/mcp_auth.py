"""认证与授权层（阶段 2，2026-10-22）。

**本文件是本项目对 `pyjwt` 的唯一 import 处** —— 与 `mcp_compat.py` 对 `mcp` 的定位
相同，遵循《规则_SDK依赖隔离与无痛升级》。本文件不直接 import mcp：需要 AccessToken /
ToolError / 当前令牌的地方一律经 `mcp_compat` 取，SDK 升级只改那边。

设计依据（都不是自创，逐条有出处）：

1. **MCP 官方安全指南**（modelcontextprotocol.io/docs/tutorials/security/authorization）
   - 「Always validate tokens」：收到 token 不等于 token 有效，也不等于它是发给本服务的。
   - 「Least-privilege scopes」：不要 catch-all scope，**按工具/能力切分并在服务端校验**。
   - 「Don't log credentials」：绝不记录 Authorization 头、token 本身。
   - 「Return proper challenges」：401 要带 WWW-Authenticate（SDK 的
     RequireAuthMiddleware 负责）。
2. **RFC 8707 资源指示器 + RFC 9728**：token 的 `aud` 必须绑到本服务；别处签的
   token 即使签名可验也必须拒（防 confused-deputy / token passthrough）。
3. **PyJWT**：成熟实现，不自己撸 crypto；`algorithms=[...]` 必须白名单，
   绝不允许 `none` 与算法混淆攻击。
4. **反面参照 FastMCP 的 `StaticTokenVerifier`**：它明文存表、无过期、无吊销，
   docstring 自己写「别上生产」。本模块用「JWT 签名 + jti 台账」避开这三点：
   签名防篡改，exp 管过期，台账管吊销与追溯。

为什么不直接上 OAuth 2.1：规范的 OAuth 是给「第三方客户端接入你的服务」用的，
需要授权服务器、DCR、PKCE、发现文档。当前客户端只有主人自己，静态 HMAC 令牌是
九成收益一成成本。阶段 7 要对外发只读券时再评估是否换 OAuth —— 届时本模块的
scope 判定与台账可直接复用。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import mcp_compat

logger = logging.getLogger("ai-memory.auth")

# ── 常量 ────────────────────────────────────────────────────────────────────

ISSUER = "ai-memory"
SCOPE_READ = "tools:read"
SCOPE_WRITE = "tools:write"
ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32

# 只读票能碰的工具 —— **白名单，不是黑名单**。
#
# 为什么白名单（2026-10-22 决策）：黑名单（「除了这几个写工具，其余都放」）在新增
# 工具时会**默认放行**，等于每加一个写工具就悄悄给只读票扩权一次，而且不会有任何
# 报错。白名单的失效方向是「新工具对只读票不可用」，需要人显式决定要不要加进来，
# 属于安全侧的正确默认。
#
# 依据：memory_tools.py 里只有 5 个工具带 readOnlyHint=True；其余读语义工具
# （list / graph / orphans / heat_suggest / smart_search / recent / audit）没打注解，
# 但读代码确认它们只读，故一并列入。
READONLY_TOOLS = frozenset(
    {
        "memory_read",
        "memory_context",
        "memory_search",
        "memory_sql",
        "memory_stats",
        "memory_list",
        "memory_graph",
        "memory_orphans",
        "memory_heat_suggest",
        "memory_smart_search",
        "memory_recent",
        "memory_audit",
    }
)

# 运行时闸门：只有 HTTP + 要求令牌时才打开。stdio 不校验（本地宿主零影响）。
_auth_enabled = False


class TokenError(Exception):
    """验签/授权失败。`reason` 是给人和日志看的短标签，不回给客户端。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── 密钥 ────────────────────────────────────────────────────────────────────


def generate_secret(path: Path) -> bytes:
    """生成 32 字节随机密钥并以 0600 落盘。已存在则不覆盖（防误生成导致全体票失效）。

    **落盘格式是 64 位 hex 文本，不是原始二进制**（2026-10-23 改）。
    原因见 `load_secret` 里的说明：二进制密钥 + `strip()` 会以约 4.6% 的概率
    把末尾的空白字节当噪声削掉，生成即失效，而且只在某些机器上偶发。
    """
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"密钥已存在，拒绝覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(MIN_SECRET_BYTES)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="") as f:
        f.write(key.hex())
    return key


def load_secret(path: Path) -> bytes:
    """读密钥。长度不足 32 字节直接拒绝 —— 短密钥让 HMAC 失去意义。

    实现已抽到 `secret_io.py`（零依赖），因为 Web 端（阶段 4）也要读同一把密钥，
    而它不该被 mcp SDK 拖下水。这里保留同名薄封装，外部调用方一行都不用改。
    格式细节（hex / 原始二进制 / 带换行文本的判定顺序）见 `secret_io.load_secret`
    的 docstring，以及 2026-10-23「31 字节事故」的记录。
    """
    from secret_io import load_secret as _load

    return _load(path)


# ── 台账（谁能用 / 能不能吊销）────────────────────────────────────────────────


class Ledger:
    """票台账：jti → {name, scope, issued_at, expires_at, revoked, note}。

    为什么必须有台账（2026-10-22 主人问「我怎么知道哪些人能用」）：
    JWT 单独用只能证明「这张票没被改过、没过期」，**收不回来** —— 签出去的票在到
    期前永久有效，这是纯签名令牌的死穴。台账就是吊销名单：验签通过后还要查这里，
    删掉一条即刻失效。

    存储用 JSON 而非 SQLite：条目数量级是「几个人、几十张票」，用 SQLite 属于
    过度设计；而且这份文件要能被人直接看懂、直接手改。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"tokens": {}}
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            logger.warning("台账文件损坏，按空台账处理（不会自动覆盖）：%s", self.path)
            return {"tokens": {}}
        if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
            return {"tokens": {}}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # 原子替换，避免读到半截文件

    def register(self, jti: str, record: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data["tokens"][jti] = record
            self._write(data)

    def get(self, jti: str) -> dict[str, Any] | None:
        return self._read()["tokens"].get(jti)

    def revoke(self, jti: str) -> bool:
        with self._lock:
            data = self._read()
            rec = data["tokens"].get(jti)
            if rec is None:
                return False
            rec["revoked"] = True
            rec["revoked_at"] = _now_iso()
            self._write(data)
            return True

    def rows(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(self._read()["tokens"].items())


# ── 签发 / 验签 ─────────────────────────────────────────────────────────────


PERMANENT_EXP = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def issue(
    secret: bytes,
    ledger: Ledger,
    *,
    name: str,
    scope: str,
    resource: str,
    ttl_seconds: int,
    issuer: str = ISSUER,
    note: str | None = None,
    user_id: str | None = None,
    vault_id: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """签一张票并登记台账。返回 (token, jti, 台账记录)。

    name 是**票名**（给谁用），不是身份。票可以被转手，这一点无法靠令牌形态解决，
    只能在台账备注里写清「这张给了谁」，和阶段 5 的账号体系配合使用。
    """
    import jwt  # pyjwt：本文件是唯一 import 处

    now = datetime.now(timezone.utc)
    # 有效期三态（2026-10-23 修）：
    #   0   → 永久票（exp 写 9999 年），管理员票与「有效期 0 天」走这条
    #   >0  → 正常有效期
    #   <0  → exp 落在过去，即「签出来就是过期票」。这是测试与吊销演练用的形态。
    # 上一版把 `<=0` 一律当永久，结果负 ttl 也变成永久票：test_mcp_auth 里
    # 「过期票被拒」三条全部失效 —— 校验侧没坏，是签发侧把考卷改了。
    permanent = ttl_seconds == 0
    exp = PERMANENT_EXP if permanent else now + timedelta(seconds=ttl_seconds)
    jti = secrets.token_hex(8)
    claims = {
        "iss": issuer,
        "aud": resource,
        "sub": name,
        "scope": scope,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if user_id:
        claims["user_id"] = user_id
    if vault_id:
        claims["vault_id"] = vault_id
    token = jwt.encode(claims, secret, algorithm=ALGORITHM)
    record = {
        "name": name,
        "scope": scope,
        "issued_at": now.isoformat(timespec="seconds"),
        "expires_at": exp.isoformat(timespec="seconds"),
        "revoked": False,
        "permanent": permanent,
        "note": note or "",
        "user_id": user_id or "",
        "vault_id": vault_id or "",
    }
    ledger.register(jti, record)
    return token, jti, record


def current_identity() -> dict[str, str]:
    """读取当前 MCP 请求的最小身份上下文；未认证或无上下文时返回空值。"""
    token = mcp_compat.get_access_token()
    claims = getattr(token, "claims", None) or {}
    if not isinstance(claims, dict):
        return {}
    return {
        key: str(claims[key]).strip()
        for key in ("sub", "user_id", "vault_id", "jti")
        if str(claims.get(key) or "").strip()
    }


def verify(
    secret: bytes,
    ledger: Ledger,
    token: str,
    *,
    resource: str | Iterable[str],
    issuer: str = ISSUER,
) -> dict[str, Any]:
    """验签 + 查过期 + 查台账吊销。任何一项不过都抛 TokenError。

    `resource` 可以给**一组**资源指示器（2026-10-23 阶段 3 起）：同一个服务现在
    有多个合法入口（局域网 IP、Tailscale 100.x、反代域名），票的 aud 只要命中其中
    任意一个就算「发给本服务」。这不是放宽校验 —— 集合之外的 aud 仍然被拒，
    confused-deputy 的防线还在。PyJWT 的 audience 参数原生支持列表语义
    （token 的 aud 与期望列表求交集）。

    明确不做的事：不信任令牌里的任何自述（scope 是签进签名的，可以信；但能不能
    用仍由服务端按 READONLY_TOOLS 再判一次，双重保险）。
    """
    import jwt  # pyjwt

    audiences = [resource] if isinstance(resource, str) else [str(r) for r in resource]
    if not audiences:
        raise TokenError("no_resource", "校验端没有配置任何合法 aud")

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # 白名单：绝不接受 none / 算法混淆
            audience=audiences,
            issuer=issuer,
            options={"require": ["exp", "iat", "aud", "iss", "jti", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("expired", str(e)) from e
    except jwt.InvalidAudienceError as e:
        raise TokenError("wrong_audience", str(e)) from e
    except jwt.InvalidIssuerError as e:
        raise TokenError("wrong_issuer", str(e)) from e
    except jwt.InvalidSignatureError as e:
        raise TokenError("bad_signature", str(e)) from e
    except jwt.MissingRequiredClaimError as e:
        raise TokenError("missing_claim", str(e)) from e
    except jwt.InvalidTokenError as e:  # 兜底：格式错、算法不符等
        raise TokenError("malformed", str(e)) from e

    jti = str(claims.get("jti") or "")
    rec = ledger.get(jti)
    if rec is None:
        raise TokenError("unknown_jti", "签名有效但台账里没有这张票（可能是别的环境签的）")
    if rec.get("revoked"):
        raise TokenError("revoked", "台账已标吊销")
    return claims


# ── 工具层 scope 判定 ───────────────────────────────────────────────────────


def enable(enabled: bool = True) -> None:
    """打开/关闭工具层校验。只有 HTTP + --require-token 时才打开。"""
    global _auth_enabled
    _auth_enabled = bool(enabled)


def is_enabled() -> bool:
    return _auth_enabled


def scope_allows(tool_name: str, scopes: set[str]) -> bool:
    """这张票能不能调这个工具。纯函数，便于单测。

    规则：有 write scope → 全通；只有 read scope → 只能碰 READONLY_TOOLS；
    什么都没有 → 全拒（fail-closed）。
    """
    if SCOPE_WRITE in scopes:
        return True
    if SCOPE_READ in scopes:
        return tool_name in READONLY_TOOLS
    return False


def require_scope_for(tool_name: str) -> None:
    """工具执行前的闸门。auth 未开启时零成本放行（stdio 就是这条路）。

    fail-closed：auth 开启却拿不到令牌时**拒绝**而不是放行 —— 拿不到令牌意味着
    中间件没生效，此时放行等于把闸门关掉，是最坏的失效方向。
    """
    if not _auth_enabled:
        return

    token = mcp_compat.get_access_token()
    scopes = set(getattr(token, "scopes", None) or [])
    jti = ""
    subject = ""
    claims = getattr(token, "claims", None) or {}
    if isinstance(claims, dict):
        jti = str(claims.get("jti") or "")
        subject = str(claims.get("sub") or "")

    if token is None:
        log_access(jti="", subject="", tool=tool_name, ok=False, reason="no_token_context")
        raise mcp_compat.make_tool_error(
            f"权限不足：调用 {tool_name} 时取不到令牌上下文（服务端认证中间件未生效）"
        )

    if not scope_allows(tool_name, scopes):
        log_access(jti=jti, subject=subject, tool=tool_name, ok=False, reason="scope_denied")
        raise mcp_compat.make_tool_error(
            f"权限不足：这张票的权限是 {' '.join(sorted(scopes)) or '空'}，"
            f"不能调用 {tool_name}（写类工具需要 {SCOPE_WRITE}）"
        )

    # 权限过关了，再算频率。顺序刻意：先判权限再扣令牌，这样一张只能读的票去撞写
    # 工具时不会白扣配额（否则恶意/失控的客户端能靠撞写工具把只读额度耗光）。
    _enforce_rate_limit(tool_name=tool_name, jti=jti or subject or "anon", subject=subject)


# ── 限流 ────────────────────────────────────────────────────────────────────
#
# 为什么需要：认证解决「谁能用」，限流解决「用多快」。没有限流时，一个 Agent 死循环
# 就能把 sqlite 写到发热，而票本身完全合法 —— 这类故障认证层根本看不见。
#
# 为什么用令牌桶而不是固定窗口：固定窗口在窗口边界会放过双倍突发（00:59 打满、
# 01:00 再打满，两秒内两倍配额）。令牌桶按时间连续补充，没有边界漏洞。这是
# mdb-mcp-gateway 明确修过的坑，直接照抄结论而不是自己试。
#
# 为什么写操作更贵：读只碰索引，写要落库、可能触发索引重建，爆炸半径大得多。
# 加权实现成本几乎为零（只是 cost 参数），收益是把写的上限压到读的三分之一。
#
# 已知局限（写在这里，免得以后被当成 bug 排查）：
# 1. 桶在进程内存里，重启即清空。对「防失控」足够；对「防恶意刷」不够 —— 但恶意
#    刷的人没有重启服务的权限，所以当前不构成实际缺口。
# 2. 单进程有效。将来若上多 worker，每个 worker 各算一份，实际额度会翻成 N 倍，
#    届时必须挪到 Redis 或共享文件。
# 3. 桶字典不主动清理。票是人手工签发的（个位数到几十），不会无限增长。

DEFAULT_RATE_CAPACITY = 120.0
DEFAULT_RATE_REFILL = 2.0
WRITE_COST = 3.0


class RateLimiter:
    """按 key（票号）计量的令牌桶。

    每个 key 存 (剩余令牌, 上次更新时间戳)，取出时按经过的秒数线性补充，
    上限不超过桶容量。整个过程在锁内完成 —— 记忆库的并发量很低，锁不会成为瓶颈，
    而错误地并发修改桶会导致令牌凭空多出或消失。
    """

    def __init__(self, capacity: float = DEFAULT_RATE_CAPACITY,
                 refill_per_sec: float = DEFAULT_RATE_REFILL) -> None:
        self._cap = float(capacity)
        self._rate = float(refill_per_sec)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def capacity(self) -> float:
        return self._cap

    @property
    def refill_per_sec(self) -> float:
        return self._rate

    def take(self, key: str, cost: float = 1.0, now: float | None = None) -> tuple[bool, float]:
        """尝试取走 cost 个令牌。

        返回 (是否放行, 建议等待秒数)。放行时第二个元素为 0。
        `now` 可注入，便于测试不 sleep。
        """
        now = time.monotonic() if now is None else now
        cost = max(0.0, float(cost))
        with self._lock:
            tokens, last = self._buckets.get(key, (self._cap, now))
            elapsed = max(0.0, now - last)
            tokens = min(self._cap, tokens + elapsed * self._rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            need = cost - tokens
            wait = need / self._rate if self._rate > 0 else float("inf")
            return False, wait

    def peek(self, key: str, now: float | None = None) -> float:
        """看这张票还剩多少令牌（只读，不消耗）。给测试和排查用。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self._cap, now))
            return min(self._cap, tokens + max(0.0, now - last) * self._rate)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._buckets)


_limiter: RateLimiter | None = None


def configure_rate_limit(*, capacity: float | None = None,
                         refill_per_sec: float | None = None,
                         enabled: bool = True) -> None:
    """配置限流。enabled=False 或 capacity<=0 表示关闭（stdio 与调试用）。

    关闭时把 _limiter 置 None，而不是留一个「容量无穷」的桶 —— 少一个分支，
    也少一处「以为关了其实还在扣」的误会。
    """
    global _limiter
    if not enabled or (capacity is not None and capacity <= 0):
        _limiter = None
        return
    _limiter = RateLimiter(
        capacity=DEFAULT_RATE_CAPACITY if capacity is None else capacity,
        refill_per_sec=DEFAULT_RATE_REFILL if refill_per_sec is None else refill_per_sec,
    )


def rate_limiter() -> RateLimiter | None:
    return _limiter


def _enforce_rate_limit(*, tool_name: str, jti: str, subject: str) -> None:
    lim = _limiter
    if lim is None:
        return
    cost = 1.0 if tool_name in READONLY_TOOLS else WRITE_COST
    ok, wait = lim.take(jti, cost=cost)
    if ok:
        return
    log_access(jti=jti, subject=subject, tool=tool_name, ok=False, reason="rate_limited")
    raise mcp_compat.make_tool_error(
        f"限流：这张票调用太频繁，请约 {max(wait, 0.5):.1f} 秒后重试。"
        f"（阈值：{lim.capacity:g} 次突发，{lim.refill_per_sec:g} 次/秒补充；"
        f"写类工具每次计 {WRITE_COST:g} 次）"
    )


# ── 调用日志 ────────────────────────────────────────────────────────────────


def default_log_path() -> Path:
    return vault_dir() / "日志" / "mcp_access.jsonl"


def log_access(
    *,
    jti: str,
    subject: str,
    tool: str,
    ok: bool,
    reason: str = "",
) -> None:
    """记一笔「哪张票、什么时候、调了什么、成没成」。绝不含 token 本身。

    注意口径（别过度承诺）：**这里没有来源 IP**。工具层拿不到 HTTP 请求头，
    SDK 也没有把 ASGI scope 暴露到工具执行上下文；硬要 IP 得自己写 ASGI 中间件。
    所以这份日志能回答「这张票在干什么」，不能回答「他从哪台机器来」——
    要认人和定位来源，得等阶段 5（账号）与阶段 8（审计，在 HTTP 层记）。
    """
    try:
        path = default_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": _now_iso(),
                "jti": jti,
                "sub": subject,
                "tool": tool,
                "ok": ok,
                "reason": reason,
            },
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - 日志失败绝不阻断业务
        logger.warning("写调用日志失败：%s", e)


# ── SDK 适配：TokenVerifier 实现 ────────────────────────────────────────────


class HMACTokenVerifier:
    """喂给 SDK 的 TokenVerifier。SDK 只要求一个 `async verify_token(str)`。

    返回 None 表示「这张票不认」，SDK 的 RequireAuthMiddleware 据此回 401 并附
    WWW-Authenticate。**绝不在这里抛异常** —— 抛出去会变成 500，客户端看到的是
    「服务端挂了」，而真实原因是「你的票不对」，两者排查方向完全不同。

    `resource` 可以是单个字符串或一组（阶段 3 起支持多入口：LAN IP + Tailscale）。
    **但返回给 SDK 的 AccessToken.resource 一律填主资源**，因为 SDK 的
    BearerAuthBackend 会拿它跟 AuthSettings.resource_server_url 做等值比较
    （mcp/server/auth/middleware/bearer_auth.py 的 `_issued_for_this_resource`），
    填票里真实的那个多入口地址反而会被自己拦下来。真正的多入口判定在上面的
    `verify()` 里已经做完，SDK 这一层只是「一致性自查」，不该重复裁决。
    """

    def __init__(
        self,
        secret: bytes,
        ledger: Ledger,
        *,
        resource: str | Iterable[str],
        issuer: str = ISSUER,
    ) -> None:
        self._secret = secret
        self._ledger = ledger
        self._resources = [resource] if isinstance(resource, str) else [str(r) for r in resource]
        if not self._resources:
            raise ValueError("HMACTokenVerifier 至少要有一个 resource")
        self._primary = self._resources[0]
        self._issuer = issuer

    async def verify_token(self, token: str):
        try:
            claims = verify(
                self._secret,
                self._ledger,
                token,
                resource=self._resources,
                issuer=self._issuer,
            )
        except TokenError as e:
            logger.warning("拒绝令牌：%s", e)
            return None
        except Exception as e:  # noqa: BLE001 - 任何意外都按拒绝处理（fail-closed）
            logger.warning("验签异常，按拒绝处理：%s", e)
            return None

        scope = str(claims.get("scope") or "")
        return mcp_compat.make_access_token(
            token=token,
            client_id=str(claims.get("sub") or ""),
            scopes=scope.split(),
            expires_at=int(claims.get("exp") or 0),
            resource=self._primary,
            subject=str(claims.get("sub") or ""),
            claims=claims,
        )


# ── 路径与配置 ──────────────────────────────────────────────────────────────


def vault_dir() -> Path:
    return Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parent)


def default_ledger_path() -> Path:
    return vault_dir() / ".workbuddy" / "tokens.json"


def default_secret_path() -> Path:
    """密钥默认放 `<库根>/.workbuddy/token.key`（0600）。

    为什么不放 `.env` 或代码里：这是要开源的项目，任何「跟着代码走」的密钥等于
    公开。`.workbuddy/` 本来就不入 git（它装的是运行态），且 systemd 用绝对路径读。
    """
    return vault_dir() / ".workbuddy" / "token.key"


def config_path() -> Path:
    return vault_dir() / ".workbuddy" / "auth.json"


def _read_auth_config() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_resources(
    cli_values: Iterable[str] | None = None, *, default: str | None = None
) -> list[str]:
    """解析**全部**合法资源指示器（阶段 3 起可以多个入口）。

    顺序：`--resource-url`（可重复 / 可用逗号分隔）
        > 环境变量 AI_MEMORY_TOKEN_AUD（逗号分隔）
        > auth.json 的 `resources` 列表 > auth.json 的 `resource` 单值
        > default。

    为什么要支持多个：同一个服务现在至少有两条合法入口 —— 局域网
    `http://<lan-ip>:8770/mcp` 和 Tailscale `http://100.x.x.x:8770/mcp`。
    如果只认一个 aud，换了入口就全 401，而报错只说「无效令牌」，极难查。
    集合之外的地址仍然被拒，防线没有放宽。

    **签发端与校验端必须得到同一个集合**（签发只用第一个，校验用全部），
    所以两端都走这一组函数。
    """
    out: list[str] = []

    def _add(values: Iterable[str]) -> None:
        for v in values:
            s = str(v or "").strip()
            if s and s not in out:
                out.append(s)

    if cli_values:
        vals = [cli_values] if isinstance(cli_values, str) else list(cli_values)
        for one in vals:
            _add(str(one).split(","))
        if out:
            return out

    env = os.environ.get("AI_MEMORY_TOKEN_AUD", "").strip()
    if env:
        _add(env.split(","))
        if out:
            return out

    cfg = _read_auth_config()
    listed = cfg.get("resources")
    if isinstance(listed, list):
        _add(listed)
    if not out:
        _add([cfg.get("resource") or ""])
    if out:
        return out

    if default:
        _add([default])
    if out:
        return out

    raise TokenError(
        "no_resource",
        f"没有可用的 aud（资源指示器）。请传 --resource-url，或设 AI_MEMORY_TOKEN_AUD，"
        f"或在 {config_path()} 里写 {{\"resource\": \"http://<地址>:<端口>/mcp\"}}",
    )


def resolve_resource(cli_value: str | None, *, default: str | None = None) -> str:
    """取**主**资源（签发端用）。

    一张票只绑一个 aud —— 这是 RFC 8707 的本意，也是防 confused-deputy 的核心。
    校验端才需要认识多个入口。
    """
    return resolve_resources([cli_value] if cli_value else None, default=default)[0]


def save_resource(resource: str, extras: Iterable[str] | None = None) -> Path:
    """把主资源（以及可选的其它入口）写进 auth.json，让两端共用同一个集合。

    `resource` 里可以直接用逗号分隔多个入口：第一个是主资源（签发用），
    全部写入 `resources`（校验用）。
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_auth_config()

    items: list[str] = []
    for part in str(resource).split(","):
        part = part.strip()
        if part and part not in items:
            items.append(part)
    for e in extras or []:
        e = str(e or "").strip()
        if e and e not in items:
            items.append(e)
    if not items:
        raise TokenError("no_resource", "save_resource 收到空的资源指示器")

    data["resource"] = items[0]
    data["resources"] = items
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def ttl_for_scope(scope: str) -> int:
    """默认有效期：读写票 90 天，只读票 7 天（2026-10-22 与主人约定）。"""
    return 7 * 86400 if SCOPE_WRITE not in scope.split() else 90 * 86400


def now_ts() -> float:
    return time.time()
