"""MCP OAuth 2.1 授权服务器（2026-10-27 阶段 13）。

为什么要有这个文件
------------------
`--require-token` 只解决了「票怎么验」，没解决「谁来发票」。代价是每个客户端都得
先有人手工签一张票、再想办法把票塞进它的请求头。命令行 / IDE 类客户端有配置文件
能塞，网页版 ChatGPT 的连接器表单没有那一格 —— 它只会走 OAuth 自动发现，而发现链
第二步（拉 `/.well-known/oauth-authorization-server`）在我们这里一直是 404，
于是它连「带票试一下」的机会都没有（症状：裸 GET → 401 → 静默，见库内 Bug 23）。

本文件补的就是这个缺口：让「一个网址 + 一次口令」取代「手工签票 + 手工贴票」。

设计取向（三条，都是踩过的坑换来的）
------------------------------------
1. **不自己写 OAuth 端点。** register / authorize / token / metadata 四条的参数
   校验、PKCE 比对、错误码回跳、CORS 全由 SDK 的
   `mcp.server.auth.routes.create_auth_routes` 负责，本文件只实现 provider 协议
   （它问什么我们答什么）。手写这四条最常见的翻车点就在 `redirect_uri` 白名单
   比对和错误回跳上 —— 一个不严就是开放重定向，code 被劫到别人站点。
2. **发的还是现有那张票。** `exchange_authorization_code` 里调 `mcp_auth.issue`，
   与 `issue_token.py` 签发的是同一种 HMAC 票 —— 台账、吊销、scope 闸门、限流
   全部照旧生效，不引入第二套令牌体系。
3. **口令只存哈希。** `.workbuddy/oauth.json` 里是 PBKDF2-HMAC-SHA256 的
   (salt, hash)；明文只在 setup 那一次打印出来，服务端再也拿不到。

依赖的 SDK 内部结构（升级时要复查）
----------------------------------
- `OAuthAuthorizationServerProvider` 协议：11 个方法，本文件全实现（不支持的直接
  显式 raise NotImplementedError，不静默返回 None —— 静默会让客户端以为流程成功）
- `MCPServer._auth_server_provider`：与 `_token_verifier` 一样是实例属性，装配
  入口在 `mcp_compat.enable_oauth_server`
- `AuthSettings.client_registration_options.valid_scopes`：不设就等于不限制请求方
  能要什么 scope，所以这里显式钉死
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger("ai-memory.oauth")

# 授权码 / 待确认请求的有效期：都是短命的一次性凭证
CODE_TTL = 300.0
PENDING_TTL = 600.0

# 口令爆破防线（2026-10-27）：请求经由 cloudflared 进来时 request.client.host 全是
# 127.0.0.1，按 IP 计数等于不设防，所以这里是**全局**窗口计数 —— 宁可冤枉别人，
# 也不能让一个攻击者无限试。10 次失败 / 15 分钟。
FAIL_WINDOW = 900.0
FAIL_MAX = 10

_ROUNDS = 210_000


# ── 口令与配置 ────────────────────────────────────────────────────────────


def hash_password(password: str, *, rounds: int = _ROUNDS) -> str:
    """PBKDF2-HMAC-SHA256。存 salt 与 hash 的十六进制，格式固定三段。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对。格式不对一律 False（不抛异常 —— 配置坏了不该变成 500）。"""
    try:
        algo, rounds_s, salt_hex, want_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(got.hex(), want_hex)
    except (ValueError, AttributeError):
        return False


def default_config_path() -> Path:
    root = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parent)
    return root / ".workbuddy" / "oauth.json"


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ── provider ─────────────────────────────────────────────────────────────


class VaultAuthProvider:
    """最小可用的授权服务器 provider。

    实现的是 `mcp.server.auth.provider.OAuthAuthorizationServerProvider` 协议。
    不继承任何基类：SDK 用的是 Protocol，结构对得上就行 —— 继承反而在 SDK 改基类
    时多一个断点。

    状态存放（三处，各自的寿命不同）：
      - `clients`  → `.workbuddy/oauth_clients.json`（跨重启，只有公开元数据）
      - `_codes`   → 内存（5 分钟一次性）
      - `_pending` → 内存（10 分钟，`/authorize` 与 `/consent` 之间的接力棒）
    """

    def __init__(
        self,
        *,
        secret: bytes,
        ledger: Any,
        resource: str,
        config_path: Path,
        state_dir: Path,
    ) -> None:
        self._secret = secret
        self._ledger = ledger
        self._resource = resource
        self._config_path = Path(config_path)
        self._clients_path = Path(state_dir) / "oauth_clients.json"
        self._lock = threading.Lock()
        self._codes: dict[str, Any] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._fail_times: list[float] = []
        self._clients: dict[str, Any] = self._load_clients()

    # ── 客户端注册（RFC 7591 DCR）────────────────────────────────────────

    def _load_clients(self) -> dict[str, Any]:
        """从磁盘读回客户端注册表。

        必须**还原成 SDK 的模型**，不能留 dict：SDK 的 authorize/token 处理器会直接
        调 `client.validate_redirect_uri(...)`、读 `client.token_endpoint_auth_method`。
        第一版为了省事存了 dict，结果 /authorize 一进来就
        `AttributeError: 'dict' object has no attribute 'validate_redirect_uri'`
        （500，不是 4xx，客户端只会说"连接失败"）。这条是 2026-10-27 实测踩出来的。
        """
        from mcp.shared.auth import OAuthClientInformationFull

        try:
            raw = json.loads(self._clients_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Any] = {}
        for cid, rec in raw.items():
            try:
                out[cid] = OAuthClientInformationFull.model_validate(rec)
            except Exception as e:  # noqa: BLE001 - 单条坏了不该让整表失效
                logger.warning("客户端 %s 反序列化失败，已跳过：%s", cid, e)
        return out

    def _save_clients(self) -> None:
        """原子写：先写 .tmp 再 os.replace（与 mcp_auth.Ledger 同一套做法）。"""
        try:
            payload = {
                cid: (c.model_dump(mode="json") if hasattr(c, "model_dump") else c)
                for cid, c in self._clients.items()
            }
            self._clients_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._clients_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._clients_path)
        except OSError as e:  # 落盘失败不该让授权流程中断（内存里还有）
            logger.warning("客户端注册表写入失败：%s", e)

    async def get_client(self, client_id: str):
        return self._clients.get(client_id)

    async def register_client(self, client_info) -> None:
        # 内存里存**模型对象**（SDK 要调它的方法），落盘时再 dump 成 JSON。
        self._clients[client_info.client_id] = client_info
        self._save_clients()
        logger.info(
            "注册 OAuth 客户端 %s（%s）redirect=%s",
            client_info.client_id,
            getattr(client_info, "client_name", None),
            [str(u) for u in (client_info.redirect_uris or [])],
        )

    # ── 授权：先甩到我们自己的口令页 ─────────────────────────────────────

    async def authorize(self, client, params) -> str:
        """SDK 拿这里的返回值当 302 的 Location。

        返回**相对路径**而不是绝对 URL：这样客户端用哪个入口连进来（公网域名 /
        局域网 IP / Tailscale），口令页就跟着落在同一个源上，不用为每个入口配一遍。
        相对 Location 在 RFC 7231 里是合法的，浏览器会按当前源解析。
        """
        pid = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._pending[pid] = {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "state": params.state,
                "scopes": list(params.scopes or []),
                "code_challenge": params.code_challenge,
                "created": now,
            }
            # 顺手清掉过期的待确认请求，避免长时间运行后字典无界增长
            for k, v in list(self._pending.items()):
                if now - v["created"] > PENDING_TTL:
                    self._pending.pop(k, None)
        return f"/consent?pid={pid}"

    def peek_pending(self, pid: str) -> dict[str, Any] | None:
        """只看不取：口令输错时要能重试，不能一进来就把接力棒吃掉。"""
        with self._lock:
            item = self._pending.get(pid)
        if not item or time.time() - item["created"] > PENDING_TTL:
            return None
        return item

    def client_name(self, client_id: str) -> str:
        client = self._clients.get(client_id)
        name = getattr(client, "client_name", None) if client is not None else None
        return str(name or "某个客户端")

    def take_pending(self, pid: str) -> dict[str, Any] | None:
        """取出并**立即删除**待确认请求（一次性，防重放）。"""
        with self._lock:
            item = self._pending.pop(pid, None)
        if not item or time.time() - item["created"] > PENDING_TTL:
            return None
        return item

    def issue_code(self, pending: dict[str, Any]) -> str:
        code = secrets.token_urlsafe(32)  # 256 bit，远超 RFC 6749 §10.10 的 128 bit 底线
        with self._lock:
            now = time.time()
            self._codes[code] = {
                "code": code,
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "scopes": pending["scopes"],
                "code_challenge": pending["code_challenge"],
                "expires_at": now + CODE_TTL,
                "used": False,
            }
            for k, v in list(self._codes.items()):
                if now - (v["expires_at"] - CODE_TTL) > CODE_TTL * 2:
                    self._codes.pop(k, None)
        return code

    def check_password(self, password: str) -> tuple[bool, str | None]:
        """校验口令。返回 (是否通过, 拒绝原因)。带全局失败窗口。"""
        cfg = load_config(self._config_path)
        stored = str(cfg.get("password_hash") or "")
        if not stored:
            return False, "服务端未配置授权口令"

        now = time.time()
        with self._lock:
            self._fail_times = [t for t in self._fail_times if now - t < FAIL_WINDOW]
            if len(self._fail_times) >= FAIL_MAX:
                wait = int(FAIL_WINDOW - (now - self._fail_times[0])) + 1
                return False, f"尝试次数过多，请 {wait} 秒后再试"

        if verify_password(password, stored):
            with self._lock:
                self._fail_times.clear()
            return True, None

        with self._lock:
            self._fail_times.append(now)
            left = FAIL_MAX - len(self._fail_times)
        return False, f"口令不对（再错 {left} 次将锁定 15 分钟）"

    # ── 码换票 ────────────────────────────────────────────────────────

    async def load_authorization_code(self, client, authorization_code: str):
        from mcp.server.auth.provider import AuthorizationCode

        rec = self._codes.get(authorization_code)
        if not rec or rec["used"]:
            return None
        if rec["client_id"] != client.client_id:
            return None
        if time.time() > rec["expires_at"]:
            self._codes.pop(authorization_code, None)
            return None
        return AuthorizationCode(
            code=rec["code"],
            client_id=rec["client_id"],
            redirect_uri=rec["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            scopes=rec["scopes"],
            code_challenge=rec["code_challenge"],
            expires_at=rec["expires_at"],
        )

    async def exchange_authorization_code(self, client, authorization_code):
        """把授权码换成**我们现有的那种票**（mcp_auth.issue），不是另一套令牌。"""
        import mcp_auth
        from mcp.shared.auth import OAuthToken

        rec = self._codes.get(authorization_code.code)
        if rec is not None:
            rec["used"] = True  # 一次性：PKCE 校验通过后立刻作废

        cfg = load_config(self._config_path)
        scope = str(cfg.get("scope") or mcp_auth.SCOPE_READ)
        ttl_days = int(cfg.get("ttl_days") or 30)
        subject = str(cfg.get("subject") or "oauth-web")

        token, jti, _rec = mcp_auth.issue(
            self._secret,
            self._ledger,
            name=subject,
            scope=scope,
            resource=self._resource,
            ttl_seconds=int(ttl_days * 86400),
            note=f"OAuth 授权码换发（client={client.client_id[:12]}）",
            user_id=cfg.get("user_id") or None,
            vault_id=cfg.get("vault_id") or None,
        )
        logger.info(
            "OAuth 换票成功 client=%s sub=%s scope=%s jti=%s ttl=%dd",
            client.client_id[:12], subject, scope, jti, ttl_days,
        )
        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=int(ttl_days * 86400),
            scope=scope,
        )

    # ── 其余协议方法 ──────────────────────────────────────────────────

    async def load_access_token(self, token: str):
        """给 SDK 看的自查入口。校验复用 mcp_auth.verify，不另写一套判定。"""
        import mcp_auth

        try:
            claims = mcp_auth.verify(self._secret, token, resources=[self._resource], ledger=self._ledger)
        except Exception:  # noqa: BLE001 - 校验失败一律当无效票
            return None
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token,
            client_id=str(claims.get("sub") or "oauth"),
            scopes=str(claims.get("scope") or "").split(),
            expires_at=int(claims["exp"]),
            resource=self._resource,
            subject=str(claims.get("sub") or ""),
            claims=claims,
        )

    async def revoke_token(self, token) -> None:
        """按 jti 拉黑。台账里查不到就静默放过（RFC 7009 要求撤销不泄露票是否存在）。"""
        jti = getattr(token, "claims", None)
        claim_jti = (jti or {}).get("jti") if isinstance(jti, dict) else None
        if claim_jti:
            self._ledger.revoke(str(claim_jti))

    async def load_refresh_token(self, client, refresh_token: str):
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # 我们不刷新：到期就让客户端重新走一次口令页。少一条长期有效的凭证路径。
        raise NotImplementedError("本服务器不签发 refresh_token，请重新授权")

    async def exchange_identity_assertion(self, client, params):
        raise NotImplementedError("未启用 Identity Assertion 授权")


# ── 口令页（挂在 /consent，SDK 的 /authorize 会把浏览器甩到这里）─────────────

_CONSENT_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#0f1216;color:#e6e9ee;
  font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.card{width:min(92vw,420px);padding:28px 26px 24px;background:#171b21;
  border:1px solid #262c35;border-radius:14px}
h1{margin:0 0 6px;font-size:17px;font-weight:600;letter-spacing:.2px}
.sub{margin:0 0 20px;color:#8b95a4;font-size:13px}
label{display:block;margin:0 0 8px;color:#aab3c0;font-size:13px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #2f3742;
  background:#0f1216;color:#e6e9ee;font-size:15px;outline:none}
input:focus{border-color:#4a7dff}
button{margin-top:16px;width:100%;padding:11px 12px;border:0;border-radius:9px;
  background:#4a7dff;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#3d6cf0}
.err{margin:0 0 16px;padding:9px 11px;border-radius:8px;background:#3a1c1f;
  border:1px solid #6b2b30;color:#ffbfc4;font-size:13px}
.foot{margin:18px 0 0;color:#6f7887;font-size:12px;line-height:1.5}
code{background:#0f1216;border:1px solid #262c35;border-radius:5px;padding:1px 5px;font-size:12px}
"""


def _render_consent(*, pid: str, client_name: str, error: str | None = None) -> str:
    err = f'<p class="err">{_esc(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权访问记忆库</title><style>{_CONSENT_CSS}</style></head>
<body><div class="card">
<h1>授权访问记忆库</h1>
<p class="sub">{_esc(client_name)} 请求读取你的记忆库（只读）</p>
{err}
<form method="post" action="/consent">
  <input type="hidden" name="pid" value="{_esc(pid)}">
  <label for="pw">授权口令</label>
  <input id="pw" name="password" type="password" autocomplete="current-password"
         autofocus required>
  <button type="submit">同意并继续</button>
</form>
<p class="foot">口令只在你本机校验，不会经过第三方。同意后会签发一张只读票，
有效期内该客户端可反复读取；要收回就去服务端把它从台账里吊销。</p>
</div></body></html>"""


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _error_page(msg: str, status: int = 400):
    from starlette.responses import HTMLResponse

    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无法授权</title><style>{_CONSENT_CSS}</style></head>
<body><div class="card"><h1>无法授权</h1>
<p class="err">{_esc(msg)}</p>
<p class="foot">回到客户端重新点一次「连接」即可发起新的授权请求。</p>
</div></body></html>""",
        status_code=status,
    )


def install_consent_route(server: Any, provider: VaultAuthProvider, issuer: str) -> None:
    """把 /consent 挂到 server 上。

    用 SDK 的 `custom_route`：它明确说明「这类路由不经过鉴权」，正好是授权流程
    需要的 —— 口令页必须在拿到票之前就能打开，否则死循环。
    """
    from starlette.responses import HTMLResponse, RedirectResponse

    @server.custom_route("/consent", methods=["GET", "POST"])
    async def _consent(request):  # noqa: ANN001, ANN202 - Starlette handler
        pid = request.query_params.get("pid") or ""
        if request.method == "POST":
            form = await request.form()
            pid = str(form.get("pid") or "")
            password = str(form.get("password") or "")
            pending = provider.peek_pending(pid)
            if pending is None:
                return _error_page("这次授权请求已过期或已被使用，请回客户端重试。")
            ok, reason = provider.check_password(password)
            if not ok:
                return HTMLResponse(
                    _render_consent(pid=pid, client_name=_client_name(provider, pending), error=reason),
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
            taken = provider.take_pending(pid)
            if taken is None:
                return _error_page("这次授权请求已过期或已被使用，请回客户端重试。")
            code = provider.issue_code(taken)
            sep = "&" if "?" in taken["redirect_uri"] else "?"
            query = {"code": code, "iss": issuer}
            if taken.get("state"):
                query["state"] = taken["state"]
            return RedirectResponse(
                url=f"{taken['redirect_uri']}{sep}{urlencode(query)}",
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        pending = provider.peek_pending(pid)
        if pending is None:
            return _error_page("这次授权请求已过期或已被使用，请回客户端重试。")
        return HTMLResponse(
            _render_consent(pid=pid, client_name=_client_name(provider, pending)),
            headers={"Cache-Control": "no-store"},
        )


def _client_name(provider: VaultAuthProvider, pending: dict[str, Any]) -> str:
    return provider.client_name(str(pending.get("client_id") or ""))
