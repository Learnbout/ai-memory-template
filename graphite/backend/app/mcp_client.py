"""工作台侧 MCP stdio 客户端管理器（2026-10-15 新增）。

解决什么：`tool_catalog` 此前只做「目录」（AST 解析 + mcp.json 服务占位），
所有 ai-memory / 外部 MCP 工具 `invokable=false`，点了就是「该工具不可调用」。
本模块让后端**自己作为 MCP 客户端**直连 mcp.json 里的 stdio 服务，
握手 → tools/list 枚举真实工具 → tools/call 真实执行。

设计要点：
- 协议：JSON-RPC 2.0，stdio 传输 = **按行分隔的 JSON 消息**（一行一条、行内不得有换行）。
  握手三步：initialize → notifications/initialized → tools/list。
- 一个服务一个常驻子进程 + 一个读线程（读线程按 id 分发响应，忽略服务端通知）；
  每连接一把锁串行请求（UI 用量足够，不追求并发 pipeline）。
- 连接状态机：connecting → connected / error / unsupported；进程死亡自动标记，
  下次调用时 `ensure_connected` 自动重拉（自愈，不需要重启后端）。
- 环境变量：以 mcp.json 该服务的 env 为底、合并宿主环境；**ai-memory 特例**：
  剥掉 AI_MEMORY_CORE_TOOLS —— 那是「宿主注入端」的工具面限制，工作台直连
  应看到全量工具（这正是主人「把所有 mcp 全部连接上」的诉求）。
- type=url 的服务（如 figma，需 OAuth）：标记 unsupported，不编造连接成功。

2026-10-23 中心化改造（两处）：
  1. `MCP_JSON_PATH` 不再写死，改由 `paths.mcp_json()` 解析（见 `paths.py` 的覆盖顺序）。
  2. **支持 type=url / http 的 MCP**（Streamable HTTP）。原来一律 `unsupported`，
     Linux 服务器上连 ai-memory 只剩这条路：服务器那份库由 `ai-memory-mcp.service`
     独占（8770，带令牌与限流），再 spawn 一个 stdio 子进程等于开了第二份库连接。
     实现要点（协议 2025-06-18）：
       - POST JSON-RPC，Header `Accept: application/json, text/event-stream`
         （缺这个头服务器回 406）；
       - 响应可能是整包 JSON，也可能是 SSE（`text/event-stream`），后者要逐行剥 `data:`；
       - 会话 id 在响应头 `Mcp-Session-Id`，后续每个请求都要回带；
       - 认证走 `Authorization: Bearer <token>`，令牌从 env 里取（不落 mcp.json 明文）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .paths import mcp_json

MCP_JSON_PATH = mcp_json()
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "graphite-workspace", "version": "1.1.0"}

# Windows 下不弹控制台窗口（后端本体跑在 pythonw 下，子进程也不能闪黑窗）
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

INIT_TIMEOUT_DEFAULT = 30.0
INIT_TIMEOUT_SLOW = 120.0  # npx 首跑要下载包
CALL_TIMEOUT = 180.0


class _Conn:
    """一个 stdio MCP 服务的连接（子进程 + 读线程 + 待响应表）。"""

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.status = "connecting"  # connecting / connected / error / unsupported
        self.error: Optional[str] = None
        self.tools: List[Dict[str, Any]] = []
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        cfg_env = self.cfg.get("env") if isinstance(self.cfg.get("env"), dict) else {}
        env.update({str(k): str(v) for k, v in cfg_env.items()})
        if self.name == "ai-memory":
            # 剥掉宿主侧的工具面限制：工作台直连要看全量工具
            env.pop("AI_MEMORY_CORE_TOOLS", None)
        return env

    def _command(self) -> Optional[List[str]]:
        cmd = self.cfg.get("command")
        if not cmd:
            return None
        parts = [str(cmd)]
        parts += [str(a) for a in (self.cfg.get("args") or [])]
        # Windows 上 npx 等是 .cmd，CreateProcess 不会自动解析，必须 which 到实路径
        if os.name == "nt" and not os.path.isabs(parts[0]) and os.path.splitext(parts[0])[1] == "":
            found = shutil.which(parts[0])
            if found:
                parts[0] = found
        return parts

    def _cwd(self) -> Optional[str]:
        args = self.cfg.get("args") or []
        if args:
            d = os.path.dirname(str(args[0]))
            if d and os.path.isdir(d):
                return d
        return None

    # ------------------------------------------------------------------ #
    def connect(self, timeout: float) -> None:
        with self._state_lock:
            if self.status == "connected" and self.proc and self.proc.poll() is None:
                return
            self.status = "connecting"
            self.error = None
            self.tools = []
        try:
            argv = self._command()
            if not argv:
                raise RuntimeError("mcp.json 该服务缺少 command")
            self.proc = subprocess.Popen(  # noqa: S603 - 命令来自本机 mcp.json
                argv,
                cwd=self._cwd(),
                env=self._env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as e:  # noqa: BLE001
            self._fail(f"启动失败：{e}")
            return

        t = threading.Thread(target=self._read_loop, daemon=True, name=f"mcp-{self.name}")
        t.start()

        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout,
            )
            self._notify("notifications/initialized")
            listed = self._request("tools/list", {}, timeout)
            tools = listed.get("tools") if isinstance(listed, dict) else None
            if not isinstance(tools, list):
                raise RuntimeError("tools/list 返回异常")
            self.tools = [x for x in tools if isinstance(x, dict) and x.get("name")]
            with self._state_lock:
                self.status = "connected"
        except Exception as e:  # noqa: BLE001
            self._fail(str(e))
            self.stop()

    def _fail(self, msg: str) -> None:
        with self._state_lock:
            self.status = "error"
            self.error = msg
            self.tools = []

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------ #
    def _send(self, obj: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("进程未启动")
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        if not self.alive():
            raise RuntimeError("子进程未运行")
        with self._pending_lock:
            rid = self._next_id
            self._next_id += 1
            slot: Dict[str, Any] = {"event": threading.Event(), "resp": None, "dead": False}
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"{method} 超时（{timeout:.0f}s）")
        with self._pending_lock:
            self._pending.pop(rid, None)
        if slot["dead"]:
            raise RuntimeError("子进程在请求期间退出")
        resp = slot["resp"]
        if not isinstance(resp, dict):
            raise RuntimeError("响应格式异常")
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"{method} 出错：{err.get('message', err)}")
        return resp.get("result")

    def _read_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 混进来的非协议输出，忽略
            if not isinstance(msg, dict):
                continue
            rid = msg.get("id")
            if rid is None:
                continue  # 服务端通知/请求，本客户端不处理
            with self._pending_lock:
                slot = self._pending.get(rid)
                if slot is not None:
                    slot["resp"] = msg
                    slot["event"].set()
        # EOF：进程退出，唤醒所有等待者
        with self._pending_lock:
            for slot in self._pending.values():
                slot["dead"] = True
                slot["event"].set()
        with self._state_lock:
            if self.status == "connected":
                self.status = "error"
                self.error = "子进程退出"

    # ------------------------------------------------------------------ #
    def call(self, tool: str, args: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        result = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        content = result.get("content") if isinstance(result, dict) else None
        is_error = bool(result.get("isError")) if isinstance(result, dict) else False
        return {"content": content if isinstance(content, list) else [], "isError": is_error}

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None


# --------------------------------------------------------------------------- #
# Streamable HTTP 连接（2026-10-23 新增）
# --------------------------------------------------------------------------- #
class _HttpConn:
    """一个 HTTP MCP 服务的连接（无子进程，纯 POST + 会话头）。

    为什么不用官方 SDK 的 ClientSession：后端只依赖 fastapi/uvicorn/pydantic
    三个包（`requirements.txt`），把 mcp SDK 拉进来等于把一棵 40+ 依赖的树塞进
    工作台后端；而这里只需要 initialize / tools/list / tools/call 三个方法，
    协议本身是「一进一出」，标准库 urllib 足够。

    与 `_Conn` 保持同一套属性与出口（status/error/tools/connect/alive/call/stop），
    上层 `_get_conn` / `call_tool` / `status_all` 无需区分两种传输。
    """

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.status = "connecting"
        self.error: Optional[str] = None
        self.tools: List[Dict[str, Any]] = []
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lock = threading.Lock()  # 串行请求：服务端会话不保证并发安全
        self.session_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    def _url(self) -> str:
        url = str(self.cfg.get("url") or self.cfg.get("baseUrl") or "").strip()
        if not url:
            raise RuntimeError("mcp.json 该 HTTP 服务缺少 url")
        return url

    def _headers(self) -> Dict[str, str]:
        hdr = {
            "Content-Type": "application/json",
            # 两个都要：只给 json 时，要求流式的服务器会回 406
            "Accept": "application/json, text/event-stream",
        }
        cfg_headers = self.cfg.get("headers") if isinstance(self.cfg.get("headers"), dict) else {}
        hdr.update({str(k): str(v) for k, v in cfg_headers.items()})
        # 令牌优先从环境变量取（不把明文写进 mcp.json）：
        #   cfg.tokenEnv 指定变量名，缺省 AI_MEMORY_MCP_TOKEN
        if "Authorization" not in hdr:
            env_name = str(self.cfg.get("tokenEnv") or "AI_MEMORY_MCP_TOKEN")
            token = os.environ.get(env_name)
            if token:
                hdr["Authorization"] = f"Bearer {token}"
        if self.session_id:
            hdr["Mcp-Session-Id"] = self.session_id
        return hdr

    # ------------------------------------------------------------------ #
    def _post(self, payload: Dict[str, Any], timeout: float) -> tuple:
        """发一条 JSON-RPC，返回 (状态码, 响应头 dict, 正文 bytes)。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(), data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                pass
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
        except urllib.error.URLError as e:
            raise RuntimeError(f"HTTP 连接失败：{e.reason}") from e

    @staticmethod
    def _parse_body(ctype: str, raw: bytes) -> Optional[Dict[str, Any]]:
        """整包 JSON 或 SSE 都归一成 dict；解析不出返回 None。

        SSE 逐行形如 `event: message` / `data: {...}` / 空行分隔。
        tools/call 的流式响应可能先推若干 notification，再推带 id 的结果 ——
        优先取带 `id` 的那条。
        """
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return None
        if "text/event-stream" in ctype.lower():
            fallback = None
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if "id" in obj or "result" in obj or "error" in obj:
                    return obj
                if fallback is None:
                    fallback = obj
            return fallback
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def _request(self, method: str, params: Optional[Dict[str, Any]], timeout: float) -> Any:
        with self._lock:
            with self._id_lock:
                rid = self._next_id
                self._next_id += 1
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                payload["params"] = params
            code, hdrs, raw = self._post(payload, timeout)
            sid = hdrs.get("mcp-session-id")
            if sid:
                self.session_id = sid
            if code >= 400:
                snippet = raw.decode("utf-8", "replace")[:300]
                raise RuntimeError(f"{method} HTTP {code}：{snippet or '(空响应)'}")
            msg = self._parse_body(hdrs.get("content-type", ""), raw)
            if msg is None:
                raise RuntimeError(f"{method} 响应无法解析")
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"{method} 出错：{err.get('message', err) if isinstance(err, dict) else err}")
            return msg.get("result")

    def _notify(self, method: str, timeout: float = 30.0) -> None:
        """通知类消息：无 id，正常回 202 空体；失败不致命（不阻断握手）。"""
        with self._lock:
            try:
                self._post({"jsonrpc": "2.0", "method": method}, timeout)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def connect(self, timeout: float) -> None:
        with self._state_lock:
            if self.status == "connected":
                return
            self.status = "connecting"
            self.error = None
            self.tools = []
            self.session_id = None
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout,
            )
            self._notify("notifications/initialized")
            listed = self._request("tools/list", {}, timeout)
            tools = listed.get("tools") if isinstance(listed, dict) else None
            if not isinstance(tools, list):
                raise RuntimeError("tools/list 返回异常")
            self.tools = [x for x in tools if isinstance(x, dict) and x.get("name")]
            with self._state_lock:
                self.status = "connected"
        except Exception as e:  # noqa: BLE001
            with self._state_lock:
                self.status = "error"
                self.error = str(e)
                self.tools = []

    def alive(self) -> bool:
        # 无长连接可探活：真实可用性由下一次请求自身证明（失败即转 error，
        # 上层 call_tool 会重连一次）。
        return self.status == "connected"

    def call(self, tool: str, args: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        result = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        content = result.get("content") if isinstance(result, dict) else None
        is_error = bool(result.get("isError")) if isinstance(result, dict) else False
        return {"content": content if isinstance(content, list) else [], "isError": is_error}

    def stop(self) -> None:
        """尽力结束会话（服务器不认也无妨，会话会自己过期）。"""
        sid = self.session_id
        if not sid:
            return
        try:
            req = urllib.request.Request(
                self._url(),
                headers={**self._headers(), "Mcp-Session-Id": sid},
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:  # noqa: BLE001
            pass
        self.session_id = None


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
_conns: Dict[str, Any] = {}
_conn_transport: Dict[str, str] = {}
_manager_lock = threading.Lock()
_bg_started = False

# 认作 HTTP 的 type 写法（各家 mcp.json 五花八门，全收）
_HTTP_TYPES = ("url", "http", "https", "remote", "streamable-http", "streamablehttp")


def _read_servers() -> Dict[str, Dict[str, Any]]:
    try:
        with open(MCP_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers")
        return servers if isinstance(servers, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _transport_of(cfg: Dict[str, Any]) -> str:
    """回 'stdio' / 'http' / 'sse'（sse = 旧 HTTP+SSE 传输，不支持）。"""
    t = str(cfg.get("type") or "stdio").strip().lower()
    if t == "sse":
        return "sse"
    if t in _HTTP_TYPES:
        return "http"
    return "stdio"


def _init_timeout(name: str, cfg: Dict[str, Any]) -> float:
    cmd = str(cfg.get("command", ""))
    if "npx" in cmd or "playwright" in str(cfg.get("args", "")):
        return INIT_TIMEOUT_SLOW
    return INIT_TIMEOUT_DEFAULT


def _get_conn(name: str, cfg: Dict[str, Any]) -> Any:
    """取（或建）连接对象；mcp.json 里传输方式被改过就重建，免得张冠李戴。"""
    transport = _transport_of(cfg)
    with _manager_lock:
        conn = _conns.get(name)
        if conn is not None and _conn_transport.get(name) != transport:
            conn = None  # 类型变了，旧对象作废
        if conn is None:
            conn = _HttpConn(name, cfg) if transport == "http" else _Conn(name, cfg)
            _conns[name] = conn
            _conn_transport[name] = transport
        return conn


def connect_server(name: str) -> Dict[str, Any]:
    """同步连接单个服务（带各自超时），返回状态摘要。"""
    servers = _read_servers()
    cfg = servers.get(name)
    if not isinstance(cfg, dict):
        return {"server": name, "status": "error", "error": f"mcp.json 无该服务", "toolCount": 0}
    if cfg.get("disabled"):
        return {"server": name, "status": "disabled", "error": None, "toolCount": 0}
    transport = _transport_of(cfg)
    if transport == "sse":
        conn = _get_conn(name, cfg)
        with conn._state_lock:
            conn.status = "unsupported"
            conn.error = "旧 HTTP+SSE 传输，请改用 streamable-http（type=url）"
        return {"server": name, "status": "unsupported", "error": conn.error, "toolCount": 0}
    conn = _get_conn(name, cfg)
    conn.stop()  # 重连先清旧连接（stdio 杀进程 / http 删会话）
    conn.connect(_init_timeout(name, cfg))
    return {
        "server": name,
        "status": conn.status,
        "error": conn.error,
        "toolCount": len(conn.tools),
        "transport": transport,
    }


def _background_connect() -> None:
    for name, cfg in _read_servers().items():
        if isinstance(cfg, dict) and not cfg.get("disabled"):
            connect_server(name)


def ensure_background_connect() -> None:
    """进程内只起一次：后台线程逐个连接全部 stdio 服务（按 mcp.json）。"""
    global _bg_started
    with _manager_lock:
        if _bg_started:
            return
        _bg_started = True
    threading.Thread(target=_background_connect, daemon=True, name="mcp-connect-all").start()


def get_state(name: str) -> Dict[str, Any]:
    conn = _conns.get(name)
    if conn is None:
        return {"status": "not-attempted", "error": None, "tools": []}
    return {"status": conn.status, "error": conn.error, "tools": conn.tools}


def status_all() -> Dict[str, Dict[str, Any]]:
    ensure_background_connect()
    return {name: get_state(name) for name in _read_servers()}


def find_server_for_tool(name: str) -> Optional[str]:
    """工具名 → 已连接的服务名（注册时已保证无跨服务重名）。"""
    for server, conn in _conns.items():
        if conn.status == "connected" and any(t.get("name") == name for t in conn.tools):
            return server
    return None


def call_tool(server: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """调用已连接服务的工具；连接断了自动重拉一次再试。"""
    conn = _conns.get(server)
    if conn is None:
        raise RuntimeError(f"服务 {server} 尚未连接")
    if not conn.alive() or conn.status != "connected":
        cfg = _read_servers().get(server, {})
        conn.stop()
        conn.connect(_init_timeout(server, cfg if isinstance(cfg, dict) else {}))
        if conn.status != "connected":
            raise RuntimeError(f"服务 {server} 重连失败：{conn.error}")
    return conn.call(tool, args)


def reconnect(name: Optional[str] = None) -> Dict[str, Any]:
    names = [name] if name else list(_read_servers().keys())
    out = {}
    for n in names:
        out[n] = connect_server(n)
    return out
