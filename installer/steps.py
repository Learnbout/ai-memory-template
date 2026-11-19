# -*- coding: utf-8 -*-
"""安装器 · 真正的安装动作（拷文件 / 建环境 / 写配置 / 快捷方式 / 已装检测）。

## 装成什么结构

```
<目标目录>/                       默认 %LOCALAPPDATA%\\ai-memory（用户级，不需要管理员）
├── backend/{app/, requirements.txt, run.py, run_resident.pyw, _restart_bridge.py}
├── frontend/dist/               预构建前端（直接用，不用装 Node）
├── vault/                       记忆库代码（MCP 服务端 + 脚本）
└── .venv/                       独立 Python 环境
```

**目录层级必须与源码树同构**（`<根>/backend` + `<根>/frontend/dist`）：
后端按 `CODE_ROOT/frontend/dist` 找页面，多套一层 `graphite/` 会让 `/` 返回 404 ——
接口全 200、页面打不开，最难查的一种「装好了但用不了」。

## 管理员账号

中心化版安装时让用户当场填账号密码（那是他唯一在场、有权限的时刻）。安装器不直接建号，
而是写进 `backend/data/pending_admin.json`（600），**服务第一次启动时**由
`app/bootstrap_admin.py` 消费并删除。这样既不用等库建好，也不把明文留在盘上。

## 三条铁律

- **只合并不覆盖**：AI 客户端的设置文件只动自己那一条，改前备份。
- **失败不拖垮整体**：单步失败逐条报告，能继续的继续。
- **不碰用户已有数据**：只新增 ai-memory 自己的东西。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# ---------------------------------------------------------------- 找东西
def bundle_runtime() -> Optional[Path]:
    """安装包里自带的那份运行环境（便携模式用）。"""
    src = source_map().get("runtime")
    if src is None:
        p = payload_dir()
        if p is not None and (p / "runtime").is_dir():
            return p / "runtime"
        cand = Path(__file__).resolve().parent / "assets" / "runtime"
        if cand.is_dir():
            return cand
        return None
    return src


def runtime_exe(target: Path) -> Path:
    """装好之后该用哪个 python：优先自带的，其次 .venv 的。"""
    rt = target / ".runtime" / "python.exe"
    if rt.is_file():
        return rt
    venv = target / ".venv" / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    return venv


def runtime_ready(target: Path) -> bool:
    """判断装好的运行环境是不是真能用（能 import 到 fastapi）。"""
    py = runtime_exe(target)
    if not py.is_file():
        return False
    try:
        r = subprocess.run([str(py), "-c", "import fastapi, uvicorn"], capture_output=True, timeout=40)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _install_runtime_portable(target: Path, log: Log) -> List[str]:
    """便携模式：把自带的运行环境整个拷进安装目录，**不需要联网、不装 Python**。"""
    errors: List[str] = []
    src = bundle_runtime()
    if src is None:
        errors.append("安装包里没有自带运行环境（便携模式需要它）")
        return errors
    dst = target / ".runtime"
    if dst.is_dir() and runtime_ready(target):
        log("自带运行环境已就位，跳过")
        return errors
    if dst.is_dir():
        shutil.rmtree(dst, ignore_errors=True)
    log("复制自带运行环境（不用你装 Python）…")
    _copy_tree(src, dst, log)
    py = dst / "python.exe"
    if not py.is_file():
        errors.append("自带运行环境复制后找不到 python.exe")
        return errors
    # 端到端验一次：能 import 到 web 框架才算真的可用
    try:
        r = subprocess.run([str(py), "-c", "import fastapi, uvicorn;print('ok')"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            errors.append("自带运行环境跑不起来：%s" % (r.stderr or "")[-160:])
        else:
            log("自带运行环境可用（离线，未改动系统 Python）")
    except Exception as exc:  # noqa: BLE001
        errors.append("自带运行环境验证失败：%s" % exc)
    return errors


def _install_runtime_system(target: Path, log: Log) -> List[str]:
    """用电脑上已有的 Python：建独立 .venv 装依赖（需要联网）。"""
    errors: List[str] = []
    py = system_python()
    venv_py = target / ".venv" / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    if py is None:
        errors.append("这台机器上没有 Python 3.10 或更高版本（可以改用便携模式）")
        return errors
    if not venv_py.is_file():
        log("用你电脑上的 Python 建独立环境…")
        try:
            subprocess.run([py, "-m", "venv", str(target / ".venv")], check=True, timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            errors.append("独立环境建不起来（%s）" % exc)
            return errors
    # 两份依赖都要装：网页工作台一份、MCP 服务端一份。
    # 少装第二份的后果：AI 客户端连 8770 一律失败（2026-11-19 实锤）。
    reqs = [target / "backend" / "requirements.txt", target / "vault-requirements.txt"]
    reqs = [r for r in reqs if r.is_file()]
    for _r in reqs[1:]:
        try:
            log("安装 MCP 服务端依赖（AI 客户端连的那扇门）…")
            subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", "-r", str(_r)],
                           check=True, timeout=1800,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            errors.append("装 MCP 依赖失败：%s" % exc)
    req = reqs[0] if reqs else None
    if venv_py.is_file() and req is not None and req.is_file():
        log("安装依赖（要联网，可能一两分钟）…")
        try:
            subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", "-r", str(req)],
                           check=True, timeout=1800,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("依赖装完")
        except Exception as exc:  # noqa: BLE001
            errors.append("装依赖失败（可能没网）：%s" % exc)
    return errors


def payload_dir() -> Optional[Path]:
    """要安装的文件在哪：exe 内嵌 payload > exe 同目录 payload/ > 源码树。"""
    meipass = getattr(sys, "_MEIPASS", "")
    here = (Path(sys.argv[0]).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
    cands = []
    if meipass:
        cands.append(Path(meipass) / "payload")
    cands += [here / "payload", Path(__file__).resolve().parent / "payload"]
    for c in cands:
        if (c / "backend").is_dir() and (c / "vault").is_dir():
            return c
    return None


def source_map() -> Dict[str, Optional[Path]]:
    """三份要装的东西各自在哪。

    两种布局都要支持（**不能只看一种**，否则「源码态跑安装器」会静默少装两样）：
      · 打包态  payload/{backend, graphite/frontend/dist, vault}
      · 源码态  <仓库>/backend、<仓库>/frontend/dist、库代码（仓库同级或桌面上的 ai-memory）
    """
    p = payload_dir()
    if p is not None:
        return {"backend": p / "backend",
                "frontend": p / "graphite" / "frontend" / "dist",
                "vault": p / "vault",
                "runtime": p / "runtime" if (p / "runtime").is_dir() else None,
                "vault_req": p / "vault-requirements.txt"}
    repo = Path(__file__).resolve().parent.parent          # graphite-workspace/
    vault = None
    for cand in (repo.parent / "记忆库", repo / "vault",
                 Path.home() / "Desktop" / "ai-memory" / "记忆库",
                 Path.home() / "ai-memory" / "记忆库"):
        if (cand / "db_paths.py").is_file() or (cand / "脚本").is_dir():
            vault = cand
            break
    rt = repo / "installer" / "assets" / "runtime"
    return {"backend": repo / "backend" if (repo / "backend").is_dir() else None,
            "frontend": repo / "frontend" / "dist" if (repo / "frontend" / "dist").is_dir() else None,
            "vault": vault,
            "runtime": rt if rt.is_dir() else None,
            "vault_req": repo / "installer" / "assets" / "vault-requirements.txt"}


def default_target() -> Path:
    """默认装到哪：用户级目录（不需要管理员权限）。"""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or str(Path.home() / ".local" / "share"))
    return Path(base) / "ai-memory"


def default_vault_dir() -> Path:
    """记忆库（用户笔记）默认放哪：文档目录下一个中文名的文件夹，用户找得到。"""
    docs = Path.home() / "Documents"
    if not docs.is_dir():
        docs = Path.home()
    return docs / "我的记忆库"


def system_python() -> Optional[str]:
    """找一个能用的系统 Python。

    **不能用 `sys.executable`**：打包后它是安装器自己（PyInstaller 的 bootloader），
    拿它去 `-m venv` 会得到一个坏的虚拟环境。
    """
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"],
                                 capture_output=True, text=True, timeout=15)
            if out.returncode == 0 and int((out.stdout or "0").strip() or 0) >= 310:
                return exe
        except Exception:  # noqa: BLE001
            continue
    return os.environ.get("AI_MEMORY_PYTHON") or None


def disk_status(path: Path) -> tuple[bool, str]:
    """目标盘剩余空间够不够（至少要 200 MB）。返回人话。"""
    try:
        anchor = path
        while not anchor.exists() and anchor.parent != anchor:
            anchor = anchor.parent
        free = shutil.disk_usage(str(anchor)).free
        gb = free / (1024 ** 3)
        if free < 200 * 1024 * 1024:
            return False, "这个磁盘剩下的空间不够（还有 %.0f MB），至少要 200 MB。" % (free / 1048576)
        return True, "有 %.0f GB 可用" % gb if gb >= 1 else "有 %.0f MB 可用" % (free / 1048576)
    except OSError:
        return True, "可以"


def _ip_candidates() -> List[str]:
    """收集本机所有 IPv4，按「像不像真正的局域网地址」排序。

    为什么要过滤：本机装了 Clash/Mihomo 之类的代理 TUN 后，`socket.connect("8.8.8.8")`
    会走隧道，拿回来的是 **198.18.x.x（fake-IP 段）** —— 那个地址别的机器根本连不上。
    2026-11-19 实测就是它被当成「这台电脑的地址」写进了邀请文案，同事点开必定连不上。
    """
    raw: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            raw.append(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    # ipconfig 兜底：多网卡机器上 getaddrinfo 常常只给一个
    if os.name == "nt":
        try:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=15,
                                 encoding="utf-8", errors="replace")
            import re

            raw += re.findall(r"IPv4[^:]*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", out.stdout or "")
        except Exception:  # noqa: BLE001
            pass

    def bad(ip: str) -> bool:
        if ip.startswith("127.") or ip.startswith("169.254."):
            return True
        if ip.startswith("198.18.") or ip.startswith("198.19."):     # Clash / Mihomo fake-IP
            return True
        if ip.startswith("100."):                                     # Tailscale CGNAT
            return True
        return False

    seen, out = set(), []
    for ip in raw:
        if ip and ip not in seen and not bad(ip):
            seen.add(ip)
            out.append(ip)

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    return sorted(out, key=rank)


def lan_ip() -> str:
    """本机在局域网里的地址。取不到就回 127.0.0.1（并让调用方说明「同事可能连不上」）。"""
    cands = _ip_candidates()
    if cands:
        return cands[0]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))          # 不打 8.8.8.8：那会走代理隧道拿到假地址
        ip = s.getsockname()[0]
        return ip if not ip.startswith(("198.18.", "198.19.")) else "127.0.0.1"
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 起服务并验活
def _http_get(url: str, timeout: float = 2.0) -> Optional[dict]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001
        return None


def start_app(target: Path, *, wait: float = 25.0, log: Log = _noop) -> Dict[str, Any]:
    """把装好的工作台**真启动**，并确认起来的是**我们自己这一份**。

    为什么必须验身份：8787 是固定端口（本地版单实例契约）。如果这台电脑上已经有一个
    旧的工作台在跑，端口就被它占着，而 `http://127.0.0.1:8787/` 照样能打开 —— 打开的是
    **别人的库**。2026-11-19 实测就是这样：完成页「打开我的记忆库」点开显示 215 篇笔记，
    而新装的库是空的。判断依据用 `/api/health/paths` 的 `dataDir` 是否等于我们的目录。
    """
    py = runtime_exe(target)
    runner = target / "backend" / "run_resident.pyw"
    if not py.is_file():
        return {"ok": False, "reason": "没有可用的运行环境（安装可能没完成）"}
    if not runner.is_file():
        return {"ok": False, "reason": "找不到启动脚本（安装可能不完整）"}

    data_dir = (target / "backend" / "data").resolve()
    health = "http://127.0.0.1:8787/api/health/paths"

    existing = _http_get(health, timeout=1.5)
    if existing:
        got = str(existing.get("dataDir", {}).get("value") or "")
        if got and Path(got).resolve() == data_dir:
            log("工作台已经在跑（就是这一份），直接打开")
            return {"ok": True, "url": "http://127.0.0.1:8787/", "started": False}
        return {"ok": False, "busy": True,
                "reason": "8787 端口上已经有一个别的工作台在跑（数据目录是 %s）。"
                          "要么关掉它再试，要么就用它。" % (got or "未知")}

    env = dict(os.environ)
    env.pop("GRAPHITE_PORT", None)          # 本地契约固定 8787
    env["GRAPHITE_MODE"] = "local"
    try:
        kwargs: Dict[str, Any] = {"cwd": str(target), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008        # DETACHED_PROCESS：不跟着安装器退出
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.is_file() else py
        subprocess.Popen([str(exe), str(runner)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "启动失败：%s" % exc}

    log("正在启动工作台…")
    deadline = time.time() + wait
    while time.time() < deadline:
        got = _http_get(health, timeout=1.5)
        if got:
            d = str(got.get("dataDir", {}).get("value") or "")
            if d and Path(d).resolve() == data_dir:
                log("工作台起来了（已确认是这一份）")
                return {"ok": True, "url": "http://127.0.0.1:8787/", "started": True}
        time.sleep(0.6)
    return {"ok": False, "reason": "等了 %d 秒工作台还没起来（可能是被杀毒软件拦了，或者端口被占）" % int(wait)}


def _state_home() -> Path:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or str(Path.home() / ".local" / "share"))
    return Path(base) / "ai-memory-installer"


def remember_target(target: Path) -> None:
    """记住这次装到了哪 —— 用户把安装位置改到 D 盘后，第二次点安装器也能认出来。"""
    try:
        d = _state_home()
        d.mkdir(parents=True, exist_ok=True)
        (d / "last_target.txt").write_text(str(target), encoding="utf-8")
    except OSError:
        pass


def _remembered_target() -> Optional[Path]:
    try:
        p = _state_home() / "last_target.txt"
        if p.is_file():
            t = Path(p.read_text(encoding="utf-8").strip())
            if t:
                return t
    except OSError:
        pass
    return None


def detect_existing() -> Dict[str, Any]:
    """这台电脑上是不是已经装过（装过就别默认重装 —— 用户第二次点安装器九成想「打开」）。

    先看「上次装到哪」（用户改过安装位置也能认出来），再落到默认位置。
    """
    for tgt in (_remembered_target(), Path(default_target())):
        if tgt is None:
            continue
        if (tgt / "backend" / "app" / "main.py").is_file():
            info: Dict[str, Any] = {"installed": True, "target": str(tgt),
                                    "web_url": "http://127.0.0.1:8787/"}
            # 记下**已装的是哪种形态**：只有「已装的形态 == 当前安装包形态」才算「你已经装过了」。
            # 否则一并拦掉的话，用户先装本地版、再想装服务器版就会被「已经装过了」挡在门外。
            try:
                cfg = json.loads((tgt / "backend" / "data" / "backend.json").read_text(encoding="utf-8"))
                info["mode"] = cfg.get("mode") or "local"
            except Exception:  # noqa: BLE001
                info["mode"] = "local"
            try:
                info["notes"] = _count_notes(tgt)
            except Exception:  # noqa: BLE001
                pass
            return info
    return {"installed": False, "target": str(default_target()), "mode": ""}


def _count_notes(target: Path) -> Optional[int]:
    import sqlite3

    cfg = target / "backend" / "data" / "backend.json"
    vault_root = None
    if cfg.is_file():
        try:
            vault_root = json.loads(cfg.read_text(encoding="utf-8")).get("vault_root")
        except Exception:  # noqa: BLE001
            vault_root = None
    if not vault_root:
        return None
    for cand in (Path(vault_root) / "数据库" / "index.sqlite3",
                 Path(vault_root) / "记忆库" / "数据库" / "index.sqlite3"):
        if cand.is_file():
            con = sqlite3.connect("file:%s?mode=ro" % str(cand).replace("\\", "/"), uri=True)
            try:
                return int(con.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
            finally:
                con.close()
    return None


# ---------------------------------------------------------------- 拷文件
def _copy_tree(src: Path, dst: Path, log: Log) -> int:
    n = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if "__pycache__" in item.parts or item.suffix in (".pyc", ".log"):
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        n += 1
    log("复制 %-22s %d 个文件" % (src.name + "/", n))
    return n


def _restrict_file(path: Path) -> None:
    """把文件限制成「只有当前用户能读写」。

    **`os.chmod(0o600)` 在 Windows 上基本无效**（FAT/NTFS 只认只读位，实测写完还是 666），
    真正管得住的是 ACL。所以 Windows 走 icacls：先关继承，再只给当前用户 F（完全）。
    失败不报错 —— 这只是加固，不能因为它把安装搞挂。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(["icacls", str(path), "/inheritance:r"],
                       capture_output=True, timeout=20)
        subprocess.run(["icacls", str(path), "/grant:r", "%s:F" % user],
                       capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _write_pending_admin(target: Path, admin: Dict[str, Any], log: Log) -> None:
    """把管理员凭据写进数据目录，等服务首启消费（尽量收紧权限，明文不久留）。"""
    data_dir = target / "backend" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "pending_admin.json"
    p.write_text(json.dumps({
        "username": str(admin.get("username") or "").strip(),
        "password": str(admin.get("password") or ""),
        "note": "安装器创建",
    }, ensure_ascii=False), encoding="utf-8")
    _restrict_file(p)
    log("管理员凭据已写入（服务首次启动时自动建号，随后立即删除）")


# ---------------------------------------------------------------- 主流程
def install_app(target: Optional[Path] = None, *, mode: str = "local",
                runtime_mode: str = "portable",
                with_deps: bool = True, log: Log = _noop,
                admin: Optional[Dict[str, Any]] = None,
                progress: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """装应用本体。

    `runtime_mode`：
      · `portable`（默认）—— 把自带运行环境拷进安装目录。不用装 Python、不用联网、
        不碰系统任何东西，**删掉这个文件夹就等于卸载干净**。
      · `system`    —— 用电脑上已有的 Python 建独立 .venv（要联网装依赖）。
    `with_deps` 只在 `system` 模式有意义（便携模式的依赖已经预装好）。
    """
    errors: List[str] = []
    tgt = Path(target) if target else default_target()
    try:
        tgt.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "target": str(tgt),
                "errors": ["目标文件夹建不了（%s）。换一个位置试试。" % exc]}

    payload = payload_dir()
    src = source_map()
    if src["backend"] is None:
        return {"ok": False, "target": str(tgt),
                "errors": ["找不到安装内容。请把安装器和 payload 放在一起。"]}

    # 1) 后端
    _copy_tree(src["backend"], tgt / "backend", log)
    # 2) 前端（与源码树同构：<根>/frontend/dist）
    if src["frontend"] is not None:
        _copy_tree(src["frontend"], tgt / "frontend" / "dist", log)
    else:
        errors.append("安装包里缺前端页面（工作台会打不开）")
    # 3) 库代码
    if src["vault"] is not None:
        _copy_tree(src["vault"], tgt / "vault", log)
    else:
        errors.append("安装包里缺记忆库代码")

    if progress is not None:
        # 「放好记忆库文件」这一步到这儿就算完成 —— 让界面上的 ✓ 逐行点亮，
        # 而不是等到最后一次性全亮（用户会以为中间卡住了）。
        progress["files"] = True

    # 3b) 依赖清单（MCP 服务端那份，system 模式要照着装）
    try:
        src_req = src.get("vault_req")
        if src_req is not None and Path(src_req).is_file():
            shutil.copy2(src_req, tgt / "vault-requirements.txt")
    except Exception:  # noqa: BLE001
        pass

    # 4) 运行环境：便携（自带，默认）或 系统（独立 .venv）
    if runtime_mode == "portable":
        errors += _install_runtime_portable(tgt, log)
    else:
        errors += _install_runtime_system(tgt, log)

    # 5) 初始化空库（新装的机器一条笔记都没有，但**表结构必须先有**，
    #    否则工作台一打开就是「索引库不存在」的 503 —— 用户看到的是「装完了却打不开」）
    if runtime_ready(tgt):
        init_py = tgt / "vault" / "脚本" / "db_init.py"
        if init_py.is_file():
            try:
                r = subprocess.run([str(runtime_exe(tgt)), str(init_py)],
                                   capture_output=True, text=True, timeout=180,
                                   cwd=str(tgt / "vault"))
                if r.returncode == 0:
                    log("空库结构已建好")
                else:
                    log("建库结构返回 %d（不影响安装，首启会再试）" % r.returncode)
            except Exception as exc:  # noqa: BLE001
                log("建库结构跳过：%s" % exc)

    # 7) 形态配置
    # vault_root 指向 `<安装目录>/vault` —— 库**代码**就在那儿（db_paths.py 是库根标志物），
    # 数据（index.sqlite3）也生成在同一文件夹里，所以整个安装目录可以整体拷走、整体删除。
    vault_dir = tgt / "vault"
    try:
        cfg_path = tgt / "backend" / "data" / "backend.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg: Dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                bak = cfg_path.with_name(cfg_path.name + ".broken-%d" % int(time.time()))
                shutil.copy2(cfg_path, bak)
                cfg = {}
        cfg["mode"] = mode
        cfg.setdefault("vault_root", str(vault_dir).replace("\\", "/"))
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log("形态配置写入：%s" % mode)
    except OSError as exc:
        errors.append("写配置失败（%s）" % exc)

    # 7) 管理员凭据（只有服务器本体才需要）
    if admin and str(admin.get("username") or "").strip():
        try:
            _write_pending_admin(tgt, admin, log)
        except OSError as exc:
            errors.append("管理员凭据写不进去（%s）" % exc)

    if not errors:
        try:
            remember_target(tgt)
        except Exception:
            pass
    return {"ok": not errors, "target": str(tgt), "errors": errors,
            "runtime_mode": runtime_mode,
            "python": str(runtime_exe(tgt)) if runtime_exe(tgt).is_file() else ""}


# ---------------------------------------------------------------- MCP 服务
MCP_LAUNCHER = """# 由安装器生成：给「自带运行环境」用的 MCP 启动器。
#
# 为什么需要它：embeddable Python 带一份 `._pth`，它会把 sys.path 钉死成
# 【zip + 运行时目录 + site-packages】，**不会**自动把「脚本所在目录」加进去
# （普通 Python 会）。所以直接 `python vault\\server.py` 必然
# `ModuleNotFoundError: memory_runtime`。
# 这个文件放在 .runtime 里（它本来就在 sys.path 上），由它把 vault 目录补进去。
import pathlib
import runpy
import sys

_here = pathlib.Path(__file__).resolve().parent      # <安装目录>/.runtime
_vault = _here.parent / "vault"
if _vault.is_dir():
    sys.path.insert(0, str(_vault))
sys.argv = [str(_vault / "server.py")] + sys.argv[1:]
runpy.run_path(str(_vault / "server.py"), run_name="__main__")
"""


def _write_mcp_launcher(target: Path) -> Path:
    p = target / ".runtime" / "mcp_launch.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MCP_LAUNCHER, encoding="utf-8")
    return p


def _port_free(port: int) -> bool:
    """这个端口空着吗（bind 一下就知道）。"""
    import socket as _socket

    s = _socket.socket()
    s.settimeout(0.3)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def mcp_port_busy(port: int = 8770) -> bool:
    """8770 上是不是已经有人在服务（避免重复起第二份）。"""
    return not _port_free(port)


def start_mcp(target: Path, *, port: int = 8770, wait: float = 25.0,
              log: Log = _noop) -> Dict[str, Any]:
    """把 MCP 服务（给 AI 客户端用的那扇门）真启动起来，并确认它在监听。

    为什么必须做这一步：安装器会给各个 AI 客户端写 `http://127.0.0.1:8770/mcp`，
    但如果那扇门后面没人，客户端只会报「连不上」——用户看到的就是「装了但用不了」。
    """
    py = runtime_exe(target)
    if not py.is_file():
        return {"ok": False, "reason": "没有可用的运行环境"}
    if mcp_port_busy(port):
        log("MCP 服务已经在 %d 上跑着" % port)
        return {"ok": True, "url": "http://127.0.0.1:%d/mcp" % port, "started": False}

    launcher = _write_mcp_launcher(target)
    log("启动 MCP 服务（AI 客户端连的那扇门）…")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        kwargs: Dict[str, Any] = {"cwd": str(target), "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008        # DETACHED_PROCESS
        exe = py.with_name("pythonw.exe")
        subprocess.Popen([str(exe if exe.is_file() else py), str(launcher),
                          "--transport", "http", "--port", str(port)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "MCP 服务启动失败：%s" % exc}

    deadline = time.time() + wait
    while time.time() < deadline:
        if not _port_free(port):
            log("MCP 服务已就绪：http://127.0.0.1:%d/mcp" % port)
            return {"ok": True, "url": "http://127.0.0.1:%d/mcp" % port, "started": True}
        time.sleep(0.6)
    return {"ok": False, "reason": "等了 %d 秒 MCP 服务还没起来" % int(wait)}


# ---------------------------------------------------------------- 快捷方式
def create_shortcuts(target: Path, *, mode: str = "local") -> List[str]:
    """在桌面放三个用『用户自己的话』命名的入口。

    为什么不是开始菜单：用户找的是桌面；名字也不能叫「ai-memory」——
    要叫「打开我的记忆库」这种一看就知道点了会发生什么的话。
    """
    made: List[str] = []
    if os.name != "nt":
        return made
    try:
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        if not desktop.is_dir():
            return made
        pyw = runtime_exe(target)
        if pyw.name == "python.exe":
            alt = pyw.with_name("pythonw.exe")      # 无控制台窗口的版本
            if alt.is_file():
                pyw = alt
        run = target / "backend" / "run_resident.pyw"
        exe = Path(sys.argv[0]).resolve()
        items = [
            ("打开我的记忆库", "http://127.0.0.1:8787/", None),
            ("让新的 AI 连上记忆库", None, exe),
            ("ai-memory 安装与修复", None, exe),
        ]
        for name, url, link_exe in items:
            lnk = desktop / (name + ".lnk")
            if lnk.exists():
                continue
            if url:
                # 网址快捷方式：.url 文件最省事，不需要 COM
                (desktop / (name + ".url")).write_text("[InternetShortcut]\nURL=%s\n" % url,
                                                       encoding="utf-8")
                made.append(name)
                continue
            try:
                ps = (
                    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                    "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.Save()"
                    % (str(lnk).replace("'", "''"), str(link_exe).replace("'", "''"),
                       str(target).replace("'", "''"))
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=30)
                if lnk.exists():
                    made.append(name)
            except Exception:  # noqa: BLE001 —— 快捷方式做不出来不影响安装
                continue
    except Exception:  # noqa: BLE001
        pass
    return made
