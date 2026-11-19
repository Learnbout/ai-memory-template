# -*- coding: utf-8 -*-
"""安装器的本地小服务（标准库，零第三方依赖）。

## 为什么是本地网页 + 原生窗口

界面要沿用工作台那套视觉（深色玻璃），又要做成**本地 EXE 窗口**而不是浏览器标签：
所以服务只绑 `127.0.0.1`，界面跑在 WebView2 窗口里，装完关窗。

## 安全

- 只绑回环。安装器能改本机配置，暴露到局域网等于送人一把改机器配置的钥匙。
- 每次启动一个随机端口 + 一次性 token（URL `?t=`，请求头 `X-Install-Token`）。
  防的是「本机另一个网页偷偷 POST 安装接口」，不是防黑客。
- 端口被占时**退回随机端口**而不是崩：窗口程序崩了就是「双击没反应」。

## 接口（2026-11-19 按用户视角重设计后）

    GET  /                      安装界面
    GET  /vendor/gsap.min.js    保留（界面已不用动画，但旧版引着，别 404）
    GET  /api/state             形态 / 默认目录 / 已装检测 / 本机 AI 工具
    GET  /api/probe             四项体检：运行环境 / 磁盘 / 端口 / AI 工具
    POST /api/install           真装（拷文件 + venv + 依赖 + 形态 + 管理员凭据 + 接 AI）
    POST /api/connect/probe     接入向导：探测对方服务器
    POST /api/connect/login     接入向导：登录并取回接入码
    POST /api/open              用默认浏览器打开某地址
    GET  /api/health
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import shutil
import stat
import subprocess
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from . import agents, steps

UI_DIR = Path(__file__).resolve().parent / "ui"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8770/mcp"
DEFAULT_WEB_PORT = 8787


def _read_package_mode() -> str:
    """这个安装包是「自己用」还是「一起用」—— 由打包时写进包里的标记决定。

    候选：exe 同目录 → `_MEIPASS`（onefile 解包目录）→ 源码树 → 环境变量 → 默认 local。
    找不到就是 local（fail-safe：本地包不会把服务暴露出去）。
    """
    meipass = getattr(sys, "_MEIPASS", "")
    cands = [Path(sys.argv[0]).resolve().parent / "package_mode.txt"]
    if meipass:
        cands.append(Path(meipass) / "package_mode.txt")
    cands.append(Path(__file__).resolve().parent / "package_mode.txt")
    for cand in cands:
        try:
            if cand.is_file():
                v = cand.read_text(encoding="utf-8").strip().lower()
                if v in ("local", "central"):
                    return v
        except OSError:
            continue
    return os.environ.get("AI_MEMORY_PACKAGE_MODE", "local").strip().lower() or "local"


def _free_port() -> int:
    pinned = (os.environ.get("AI_MEMORY_INSTALLER_PORT") or "").strip()
    if pinned.isdigit():
        return int(pinned)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_free(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _heading(port: int) -> bool:
    """端口上有没有人响应 HTTP（用来判断 8770 是不是已经有一份在跑）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1.0) as r:
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


class _Server(ThreadingHTTPServer):
    """安静一点的 HTTP 服务：吞掉「对端断开」这类无害异常。

    WebView2 加载完会掐 keep-alive，socketserver 默认把 `ConnectionResetError`
    连堆栈写进日志，看起来像崩溃，实际没事。
    """

    def handle_error(self, request, client_address) -> None:  # noqa: D401
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def make_handler(one_time_token: str, state: Dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ai-memory-installer"
        protocol_version = "HTTP/1.1"

        # ---------- 基础 ----------
        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: Path, ctype: str) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if self.headers.get("X-Install-Token") == one_time_token:
                return True
            return ("inst_token=" + one_time_token) in (self.headers.get("Cookie") or "")

        def log_message(self, fmt: str, *args) -> None:
            pass

        # ---------- GET ----------
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            # 2026-11-19：界面改成原生 WinForms，HTML 那份已经删了。
            # 这里明确回一句，免得有人以为「安装器还有网页版」。
            if path in ("/", "/index.html"):
                self._json({"ok": False,
                            "note": "这一版安装器是 Windows 原生窗口，没有网页界面。"}, 404)
                return
            if path == "/api/health":
                self._json({"ok": True})
                return
            if path == "/api/state":
                found = agents.scan()
                self._json({
                    "ok": True,
                    "package_mode": state["package_mode"],
                    "default_path": str(steps.default_target()),
                    "web_url": "http://127.0.0.1:%d/" % DEFAULT_WEB_PORT,
                    "agents": [agents.as_dict(a) for a in found],
                    "existing": steps.detect_existing(),
                })
                return
            if path == "/api/progress":
                # 安装是后台任务，前端每 600ms 拉这个看进度（2026-11-19 修：原来放在 POST 段，
                # 结果前端 GET 它拿到 404，页面就永远停在「正在装」一动不动）
                self._json(dict(state.get("job") or _new_job()))
                return
            if path == "/api/probe":
                self._json(_probe(found_agents=agents.scan()))
                return
            self._json({"error": "not found"}, 404)

        # ---------- POST ----------
        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return

            if path == "/api/install":
                # 先在**动手之前**把能拦的都拦掉（这些检查很快，同步返回没问题）
                early = _precheck(body)
                if early is not None:
                    self._json(early, int(early.pop("code", 400)))
                    return
                if state.get("job", {}).get("running"):
                    self._json({"ok": False, "error_text": "已经在装了，稍等一下。"}, 409)
                    return
                state["job"] = _new_job()
                threading.Thread(target=_run_install_job, args=(body, state, agents, steps),
                                 daemon=True).start()
                self._json({"ok": True, "started": True})
                return
            if path == "/api/connect/probe":
                self._json(_connect_probe(str(body.get("url") or "")))
                return
            if path == "/api/connect/login":
                self._json(_connect_login(str(body.get("url") or ""),
                                          str(body.get("username") or ""),
                                          str(body.get("password") or "")))
                return
            if path == "/api/uninstall":
                # 真卸载：删安装目录（可选保留笔记）+ 桌面快捷方式。原来这里是 alert 占位，
                # 等于让用户自己去翻文件夹删 —— 那是把活推回给人。
                keep_notes = bool(body.get("keep_notes"))
                tgt = steps.detect_existing().get("target") or str(steps.default_target())
                res = _do_uninstall(Path(tgt), keep_notes=keep_notes)
                self._json(res)
                return
            if path == "/api/open":
                url = str(body.get("url") or "")
                if url:
                    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
                self._json({"ok": bool(url)})
                return
            self._json({"error": "not found"}, 404)

    return Handler


# ---------------------------------------------------------------- 安装任务（带进度）
def _new_job() -> Dict[str, Any]:
    return {
        "running": False, "done": False, "ok": None,
        "steps": {"files": None, "app": None, "agents": None, "selfcheck": None},
        "label": "", "started_at": 0.0, "elapsed": 0.0,
        "result": None, "error_text": "", "detail": "",
    }


def _precheck(body: Dict[str, Any]) -> Dict[str, Any] | None:
    """动手之前的快速拦截：返回 None 表示可以开工。"""
    mode = str(body.get("mode") or "local").strip().lower()
    if mode not in ("local", "central"):
        return {"ok": False, "code": 400, "error_text": "安装包信息不完整，请重新下载安装包。"}
    role = str(body.get("role") or "").strip().lower()
    if mode == "central" and role == "client":
        url = str(body.get("server_url") or "").strip()
        if not url.startswith("http"):
            return {"ok": False, "code": 400,
                    "error_text": "「连到已有服务器」需要填对方服务器的网址（例如 http://192.168.1.20:8766）。"
                                  "如果你这台就是服务器本体，请在上面选「就是这台」。"}
    runtime_mode = str(body.get("runtime_mode") or "portable").strip().lower()
    if runtime_mode == "portable" and steps.bundle_runtime() is None:
        return {"ok": False, "code": 400,
                "error_text": "这个安装包没带运行环境。请改用「用电脑上已有的 Python」，或换一个完整安装包。"}
    return None


def _run_install_job(body: Dict[str, Any], state: Dict[str, Any], agents_mod, steps_mod) -> None:
    """后台真装。每完成一步就更新 state['job']，前端据此画进度。"""
    import traceback

    job = state["job"]
    job.update({"running": True, "done": False, "started_at": time.time(), "ok": None})

    def log(msg: str) -> None:
        job["label"] = str(msg)[:120]
        job["elapsed"] = round(time.time() - job["started_at"], 1)
        print("[install] %s" % msg, flush=True)

    try:
        res = _do_install(body, agents_mod=agents_mod, steps_mod=steps_mod,
                          progress=job["steps"], log=log)
        job["result"] = res
        job["ok"] = bool(res.get("ok"))
        job["error_text"] = res.get("error_text") or ""
        job["detail"] = res.get("detail") or ""
        job["steps"] = res.get("steps") or job["steps"]
    except Exception as exc:  # noqa: BLE001 —— 后台线程炸了也要让前端知道
        job["ok"] = False
        job["error_text"] = "装的时候出了意外（%s）。可以重试这一步。" % type(exc).__name__
        job["detail"] = traceback.format_exc()[-2000:]
    finally:
        job["running"] = False
        job["done"] = True
        job["label"] = ""
        job["elapsed"] = round(time.time() - job["started_at"], 1)


# ---------------------------------------------------------------- 体检
def _probe(found_agents=None) -> Dict[str, Any]:
    """四项体检。全部用「用户能看懂的话」返回，不吐错误码。"""
    agents_list = found_agents if found_agents is not None else agents.scan()
    # 运行环境：自带运行环境在包里 → 一键即可（用户什么都不用装）
    portable = steps.bundle_runtime() is not None
    py = steps.system_python()
    if portable:
        env_ok = True
        env_msg = "安装包自带运行环境，不用另装 Python"
    elif py is not None:
        env_ok = True
        env_msg = "会用你电脑上已有的 Python（这个包没带运行环境）"
    else:
        env_ok = False
        env_msg = ("这个安装包没带运行环境，这台机器上也没找到 Python 3.10+。"
                   "换一个完整安装包（自带运行环境），或者先装 Python。")
    # 磁盘：默认目标盘剩余空间
    target = steps.default_target()
    disk_ok, disk_msg = steps.disk_status(target)
    # 端口：8770（给 AI 用）/ 8787（工作台）
    busy = []
    if not _port_free(8770) and not _heading(8770):
        busy.append("8770")
    if not _port_free(DEFAULT_WEB_PORT):
        busy.append(str(DEFAULT_WEB_PORT))
    port_ok = not busy
    port_msg = ("空着，能用" if port_ok
                else "有个号码被别的程序占着（不是你要处理的事，我们能自动换一个）。")
    return {
        "ok": True,
        "env_ok": env_ok, "env_msg": env_msg,
        "portable": portable,
        "disk_ok": disk_ok, "disk_msg": disk_msg,
        "port_ok": port_ok, "port_msg": port_msg,
        "agents": [agents.as_dict(a) for a in agents_list if a.found],
        "path": str(target),
        "python": py or "",
    }


# ---------------------------------------------------------------- 安装
def _do_install(body: Dict[str, Any], *, agents_mod, steps_mod,
                progress: Dict[str, Any] | None = None,
                log: Any = None) -> Dict[str, Any]:
    """真装。`progress` 是给前端看的四步状态，`log` 是给人看的当前动作。"""
    progress = progress if progress is not None else {}
    mode = str(body.get("mode") or "local").strip().lower()
    if mode not in ("local", "central"):
        return {"ok": False, "error_text": "安装包信息不完整，请重新下载安装包。"}
    role = str(body.get("role") or "").strip().lower()
    target = str(body.get("install_path") or "").strip() or None
    picked = set(body.get("targets") or [])
    admin = body.get("admin") or None

    # 「连到别人已经装好的服务器」这一路，地址是必需的 —— 而且**在动手之前**就拦下来。
    # 服务器本体那一路不许要地址和令牌：全新服务器此刻既没有网页也没有令牌。
    server_url = str(body.get("server_url") or "").strip()
    if mode == "central" and role == "client":
        if not server_url.startswith("http"):
            return {"ok": False, "code": 400,
                    "error_text": "「连到已有服务器」需要填对方服务器的网址（例如 http://192.168.1.20:8766）。"
                                  "如果你这台就是服务器本体，请在上面选「就是这台」。",
                    "steps": {}}

    steps_map: Dict[str, Any] = {}
    logs: list[str] = []
    _outer_log = log

    def log(m: str) -> None:            # noqa: F811 —— 覆盖同名形参，内部统一走这个
        logs.append(m)
        print("[install] %s" % m, flush=True)
        if _outer_log is not None:
            _outer_log(m)

    # 1) 放文件
    # `AI_MEMORY_INSTALL_SKIP_DEPS=1` 给自动化验收用：跳过 pip（要联网、要一两分钟），
    # 其余步骤照跑 —— 验收要验的是「装出来的东西对不对」，不是「网快不快」。
    skip_deps = os.environ.get("AI_MEMORY_INSTALL_SKIP_DEPS", "").strip() in ("1", "true", "yes")
    # 运行环境策略：`portable`（默认，自带、离线、可整个拷走）或 `system`（用本机 Python）
    runtime_mode = str(body.get("runtime_mode") or "portable").strip().lower()
    if runtime_mode not in ("portable", "system"):
        runtime_mode = "portable"
    if runtime_mode == "portable" and steps_mod.bundle_runtime() is None:
        # 包里有没带运行环境就直接说清楚，而不是装到一半失败
        return {"ok": False, "steps": {},
                "error_text": "这个安装包没有自带运行环境。请改用「用电脑上已有的 Python」，"
                              "或者换一个完整安装包。"}
    res = steps_mod.install_app(target, mode=mode, runtime_mode=runtime_mode,
                               with_deps=not skip_deps, log=log, progress=progress,
                               admin=admin if (mode == "central" and role == "server") else None)
    steps_map["files"] = res.get("ok", False)
    progress["files"] = steps_map["files"]
    if not res.get("ok"):
        return {"ok": False, "steps": steps_map,
                "error_text": _human_error(res.get("errors") or []),
                "detail": "\n".join(logs)[-4000:]}

    tgt = Path(res["target"])
    steps_map["app"] = True
    progress["app"] = True

    # 2) 接 AI 工具
    if mode == "local":
        url = DEFAULT_LOCAL_URL
    elif role == "client":
        url = server_url.rstrip("/") + "/mcp" if not server_url.endswith("/mcp") else server_url
    else:
        url = "http://127.0.0.1:8770/mcp"
    found = [a for a in agents_mod.scan() if a.found]
    chosen = [a for a in found if not picked or a.key in picked]
    results = agents_mod.apply(chosen, url, "")
    agent_ok = all(not r.get("error") for r in results)
    steps_map["agents"] = agent_ok
    progress["agents"] = agent_ok
    for r in results:
        log("AI %s → %s%s" % (r.get("agent"), r.get("action") or r.get("error") or r.get("reason"), ""))

    # 3) 自检：装出来的东西是否完整
    need = [tgt / "backend" / "app" / "main.py",
            tgt / "backend" / "run.py",
            tgt / "frontend" / "dist" / "index.html",
            tgt / "vault" / "db_paths.py"]
    missing = [str(p.relative_to(tgt)) for p in need if not p.is_file()]
    steps_map["selfcheck"] = not missing
    progress["selfcheck"] = not missing
    if missing:
        return {"ok": False, "steps": steps_map,
                "error_text": "有几样东西没装进文件（%s）。可以重试这一步，或者换一个安装位置。" % "、".join(missing),
                "detail": "\n".join(logs)[-4000:]}

    # 4) 真把工作台起来 + 快捷方式 + 完成页信息
    #
    # 2026-11-19 修：原来这里只是**拼一个假设的地址**（8787 或 lan:8766），从没验证过。
    # 实测后果：若本机已有一个旧工作台占着 8787，完成页「打开我的记忆库」打开的是**别人的库**；
    # 服务器版拼出来的还是 Clash 假 IP。现在必须先起真服务、验明身份，再报地址。
    launched = steps_mod.start_app(tgt, log=log)
    progress["app"] = bool(launched.get("ok"))
    if not launched.get("ok"):
        steps_map["app"] = False
    steps_mod.create_shortcuts(tgt, mode=mode)
    lan = steps_mod.lan_ip()
    if launched.get("ok"):
        local_url = launched["url"]
    else:
        local_url = ""
    # 中心化·服务器本体：同事那一侧的入口。只有在**真的**本地工作台起来时才给，
    # 并且明确说「同事要连同一个网络」。
    if mode == "central" and role == "server" and launched.get("ok"):
        web_url = "http://%s:%d/" % (lan, DEFAULT_WEB_PORT)
        invite_text = ("ai-memory 记忆库装好了，浏览器打开：%s\n"
                       "（要和这台电脑连同一个网络）\n"
                       "账号我另外发你，第一次登录会让你改密码。" % web_url)
    else:
        web_url = local_url
        invite_text = ""
    return {
        "ok": True, "steps": steps_map, "target": str(tgt),
        "web_url": web_url, "web_ok": bool(launched.get("ok")),
        "web_reason": launched.get("reason") or "",
        "local_url": local_url,
        "invite_url": web_url if invite_text else "",
        "invite_text": invite_text,
        "detail": "\n".join(logs)[-4000:],
    }


def _human_error(errors: list[str]) -> str:
    joined = "；".join(str(e) for e in errors)
    if "Python" in joined:
        return "这台机器上没找到 Python 3.10+。先装 Python（安装时勾上 Add to PATH），再回来点一次。"
    if "依赖" in joined:
        return "装依赖的时候取不到文件，多半是网断了或公司网络拦着。连上网再点一次「重试这一步」。"
    if "权限" in joined or "Permission" in joined:
        return "有个文件写不进去（可能被别的程序占着，或者没权限）。换一个安装位置试试。"
    return "装到一半出错了：%s。已经放好的东西我们没动，可以重试这一步。" % joined[:200]


# ---------------------------------------------------------------- 接入向导
def _normalize_url(url: str) -> str:
    """用户粘的网址补全：没写端口补 8766（网页入口），去掉尾部斜杠。

    为什么替用户补：普通人拿到的是别人随口说的「192.168.1.20」，未必带端口；
    少一截就连不上，然后他会以为对方服务器坏了。
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if not url.lower().startswith("http"):
        url = "http://" + url
    rest = url.split("://", 1)[1]
    if ":" not in rest and "/" not in rest:
        url = url + ":8766"
    return url


def _connect_probe(url: str) -> Dict[str, Any]:
    url = _normalize_url(url)
    if not url.startswith("http"):
        return {"ok": False}
    try:
        with urllib.request.urlopen(url + "/api/vault/status", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return {"ok": True, "title": data.get("label") or "对方的记忆库",
                "notes": data.get("noteCount")}
    except Exception:  # noqa: BLE001
        # 有的是网关（要登录才让看），退一步只探连通性
        try:
            with urllib.request.urlopen(url + "/", timeout=5) as r:
                return {"ok": r.status < 500, "title": "对方的记忆库", "notes": None}
        except Exception:  # noqa: BLE001
            return {"ok": False}


def _connect_login(url: str, username: str, password: str) -> Dict[str, Any]:
    """用账号密码换回「接入码」（MCP token）。拿不到也让用户继续（只读也能用）。"""
    url = _normalize_url(url)
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    try:
        req = urllib.request.Request(url + "/api/gate/login", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            cookie = r.headers.get("Set-Cookie") or ""
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "登录没成功（%s）。账号密码确认一下，或者让管理员确认服务在跑。"
                                      % type(exc).__name__}
    if not body.get("ok", True):
        return {"ok": False, "error": body.get("error") or "账号或密码不对。"}
    token = str(body.get("token") or body.get("mcp_token") or "")
    if not token:
        try:
            req2 = urllib.request.Request(url + "/api/my-token",
                                          headers={"Cookie": cookie} if cookie else {})
            with urllib.request.urlopen(req2, timeout=8) as r2:
                token = str(json.loads(r2.read().decode("utf-8", "replace") or "{}").get("token") or "")
        except Exception:  # noqa: BLE001
            token = ""
    return {"ok": True, "token": token, "cookie": cookie}



def _procs_under(path: Path) -> list[int]:
    """找出「正在使用这个目录里文件」的进程。

    为什么要这么找，而不是按进程名 kill：工作台是用**安装目录里自带的** python 跑的，
    进程名可能是 pythonw.exe / python.exe，但机器上别的地方也有一堆同名进程（本鲸鱼
    自己的工具、用户的脚本）。按名字杀会误伤，按「可执行文件在这个目录里」或
    「命令行里带这个目录」才准。

    这是 2026-11-19 卸载失败的直接原因：`httptools\\parser\\parser.cp312-win_amd64.pyd`
    正被那个 python 进程**映射进内存**，Windows 对已映射的文件直接拒绝删除（WinError 5），
    光重试没用，必须先让进程退出。
    """
    if os.name != "nt":
        return []
    ps = (
        "$p='%s';"
        "Get-CimInstance Win32_Process | Where-Object {"
        " ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($p)) -or"
        " ($_.CommandLine -and $_.CommandLine.Contains($p))"
        "} | Select-Object -ExpandProperty ProcessId"
        % str(path).replace("'", "''")
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=40,
                           encoding="utf-8", errors="replace")
        me = os.getpid()
        out = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit() and int(line) != me:
                out.append(int(line))
        return out
    except Exception:  # noqa: BLE001
        return []


def _force_rmtree(path: Path, *, attempts: int = 6, wait: float = 1.5) -> tuple[bool, str]:
    """删目录，遇到「文件被占用」就退一步再来。

    三件事按顺序做：①先清只读位（Windows 上只读文件也会 WinError 5）；
    ②每轮之间等一会儿，让刚被 kill 的进程真正释放句柄；③最后仍失败就把**剩下哪些文件**
    报出来，而不是只给一句「删不干净」。
    """
    last = ""
    for i in range(attempts):
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(wait)
    # 试着把还能删的删掉，剩下什么就报什么
    left = []
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file():
                left.append(str(f.relative_to(path)))
                if len(left) >= 5:
                    break
    detail = ("；".join(left) if left else "")
    return False, last + (("　还留着：" + detail) if detail else "")


def _on_rm_error(func, p, exc_info):
    """rmtree 的 onerror：先摘只读属性再重试一次。"""
    try:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        func(p)
    except Exception:  # noqa: BLE001
        pass


def _do_uninstall(target: Path, *, keep_notes: bool) -> Dict[str, Any]:
    """删程序本体。`keep_notes=True` 时先把笔记数据复制出来。"""
    if not target.is_dir():
        return {"ok": False, "error_text": "找不到安装目录：%s" % target}
    notes_kept = ""
    try:
        if keep_notes:
            src = target / "vault" / "数据库"
            if src.is_dir():
                dst = target.parent / ("ai-memory-笔记备份-%d" % int(time.time()))
                shutil.copytree(src, dst)
                notes_kept = "你的笔记备份在：%s" % dst
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_text": "备份笔记时出错（%s），先不删。" % exc}

    # ① 先把这个目录里的程序停掉（不然它加载的 .pyd/.dll 一律删不动）
    killed = _procs_under(target)
    for pid in killed:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    if killed:
        time.sleep(1.5)          # 等句柄真的释放，急不得

    # ② 删（带重试 + 只读清理）
    ok, detail = _force_rmtree(target)
    if not ok:
        # 再补一轮：万一有进程是刚起来的
        for pid in _procs_under(target):
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2.0)
        ok, detail = _force_rmtree(target, attempts=4)
    if not ok:
        return {"ok": False,
                "error_text": "删不干净：有文件正被别的程序占用（%s）。"
                              "关掉「打开我的记忆库」那个窗口、或者关掉工作台，再点一次就行。"
                              % detail[:200]}

    # ③ 桌面快捷方式
    try:
        for desk in (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop",
                     Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"):
            for name in ("打开我的记忆库", "让新的 AI 连上记忆库", "ai-memory 安装与修复"):
                for suffix in (".lnk", ".url"):
                    f = desk / (name + suffix)
                    if f.is_file():
                        f.unlink()
    except Exception:  # noqa: BLE001
        pass
    note = notes_kept
    if killed:
        note += ("　（顺手关掉了 %d 个正在用它的进程）" % len(killed))
    return {"ok": True, "note": note}

# ---------------------------------------------------------------- 应用本体
class Installer:
    def __init__(self, package_mode: str | None = None) -> None:
        self.package_mode = package_mode or _read_package_mode()
        self.state: Dict[str, Any] = {"package_mode": self.package_mode,
                                      "mode": self.package_mode, "last": []}
        self.token = secrets.token_urlsafe(18)
        self.port = _free_port()
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)

    def start(self) -> str:
        try:
            self.httpd = _Server(("127.0.0.1", self.port), make_handler(self.token, self.state))
        except OSError as exc:
            print("[installer] 端口 %d 不可用（%s），改用随机端口" % (self.port, exc), flush=True)
            self.httpd = _Server(("127.0.0.1", 0), make_handler(self.token, self.state))
            self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.httpd = None


def serve(open_browser: bool = True) -> int:
    """浏览器模式（自动化验收 / 没有 WebView2 的机器用）。"""
    app = Installer()
    url = app.start()
    print("安装器已启动：%s" % url, flush=True)
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return app.port


if __name__ == "__main__":
    # 允许 `python -m installer.server` 直接起浏览器模式（自动化验收与无 WebView2 的机器用）
    raise SystemExit(0 if serve() is not None else 1)
