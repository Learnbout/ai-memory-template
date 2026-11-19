"""版本检查与一键更新 —— 数据层（阶段 12，2026-10-25）。

## 为什么不是 `git pull`

2026-10-25 实测：服务器 `git: command not found`。一台只跑服务的 AlmaLinux 上
为「偶尔更新一次」去装 git、配凭据、处理盘权限，收益远小于成本。所以这条路
从一开始就是「下压缩包 → 解 → 只挑允许的文件盖过去」。

## 通道事实（都是实测，不是推测）

| 端点 | 结果 | 结论 |
| --- | --- | --- |
| `api.github.com` | 200 / 0.52s | 版本检查可以走 REST |
| `codeload.github.com` | 301，`-L` 后能下包（实测下到 258B 的 tar.gz 并解开） | 更新包从这儿拿 |
| `raw.githubusercontent.com` | **8s 超时** | 不能用「逐文件拉取」那套 |
| 私有仓库无 token | 404 | 必须放一个**只读** token |

## 四条硬规矩（路线图验收标准，逐条对应）

1. **新版本显示版本差异** → `check()` 同时给出本地/远端版本、tag、发版说明。
2. **更新前自动备份** → `apply()` 第一步就是 `backup_current()`，备份不成功直接
   拒绝继续（宁可更不了，也不能没有回滚点）。
3. **更新失败可回滚** → `restore()` 用同一份备份盖回去；上层在探活失败时调用。
4. **禁止客户端执行任意 GitHub 脚本** → 见下。

## 关于「禁止执行任意脚本」，说清楚它能做到什么、做不到什么

能做到：包内**只挑两类文件**（`backend/app/**.py` 与 `frontend/dist/**` 静态资源），
其余一律不落盘 —— `.sh` / `.service` / `.exe` / `install.py` 就算在包里，
也进不了本机。更新器本身从不 `exec` 包内任何东西。

做不到：**被拷贝的 `.py` 会被进程加载，那本质就是执行代码**。这是「一键更新」
的固有风险，无法靠白名单消除。真正的护栏是三件事叠起来：仓库私有、token 只读、
按钮只在主人自己的实例上出现（`update_enabled` 默认关）。谁把 `update_enabled`
打开又指向一个不信任的仓库，等于把服务器交出去 —— 这一点写在文档里，不假装不存在。

## 版本号从哪来

`<code_root>/VERSION`（一行，如 `1.1.0`）。**不在 Python 里写常量**：那会让
「代码里写着 1.0.0、实际跑的是 1.2.0」这种假账在重新部署后依然成立。
仓库里同名文件是唯一的对外声明，服务端读它、发布脚本写它，两边同一个源。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .paths import update_settings

# ── 常量 ───────────────────────────────────────────────────────────────────

VERSION_FILE = "VERSION"

# 下载上限：这套东西的源码+dist 约 3MB，给 80MB 足够，同时挡住「下了一个 G」
MAX_TARBALL = 80 * 1024 * 1024

# 白名单：**只有这两类会落盘**。字符串是相对 package_root 的前缀。
ALLOW_PY_PREFIX = "backend/app/"
ALLOW_DIST_PREFIX = "frontend/dist/"

# dist 里允许的扩展名。写白名单而不是黑名单 —— 黑名单永远漏。
DIST_SUFFIXES = {
    ".js", ".css", ".html", ".htm", ".json", ".map",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}

# 绝不进入代码树的东西（就算在 `.py` 路径下也跳过：字节码会盖住新源码的加载）
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

# 网络
GH_API = "https://api.github.com"
HTTP_TIMEOUT = 20
UA = "graphite-workspace-updater/1"


class UpdateError(RuntimeError):
    """更新链路上任何「该停下来」的错误。上层统一转 400/500 并把 reason 交给前端。"""


# ── 版本 ───────────────────────────────────────────────────────────────────


def _version_path(code_root: str | Path) -> Path:
    return Path(code_root) / VERSION_FILE


def parse_version(raw: str) -> tuple[int, ...]:
    """`"1.2.3"` → `(1,2,3)`。容忍 `v` 前缀与预发布尾巴（`1.2.3-beta` → (1,2,3)）。

    为什么要容忍：tag 是人在 GitHub 界面上打的，`graphite-v1.2.0` / `v1.2.0` /
    `1.2.0` 三种写法都常见。**不认识的格式不抛错、返回空元组**，由调用方判空 ——
    版本号解析失败不该让整个运维页 500。
    """
    m = re.search(r"(\d+(?:\.\d+)*)", str(raw or ""))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def version_key(raw: str) -> tuple[int, ...]:
    """补齐到 3 段，便于比较（`1.2` 与 `1.2.0` 相等）。"""
    t = parse_version(raw)
    return t + (0,) * max(0, 3 - len(t))


def is_newer(remote: str, local: str) -> bool:
    return version_key(remote) > version_key(local)


def current_version(settings: dict[str, Any] | None = None) -> str:
    """读本地版本。文件不存在或为空 → 空串（页面显示「未知」，不编一个 0.0.0）。"""
    s = settings or update_settings()
    p = _version_path(s["code_root"])
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ── 网络 ───────────────────────────────────────────────────────────────────


def github_token() -> str:
    """只读 token。**只从环境变量读**，不进配置文件、不进日志、不进任何接口返回值。

    为什么不在 update_settings() 里：那个函数的返回值会经 `/api/health/paths`
    直接吐给前端。凭据不能出现在「配置自述」里 —— 一次截图就泄露。
    """
    return os.environ.get("GRAPHITE_GH_TOKEN", "").strip()


def _open(url: str, *, accept: str, timeout: int = HTTP_TIMEOUT, binary: bool = False):
    headers = {"User-Agent": UA, "Accept": accept}
    token = github_token()
    if token and url.startswith(GH_API):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 —— 固定 https 域名
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 私有仓库不给 token 时 GitHub 返回 404 而不是 403（故意的，避免探测仓库是否存在）
            raise UpdateError(
                "仓库读不到（404）。私有仓库需要只读 token："
                "在服务器环境变量里放 GRAPHITE_GH_TOKEN，或确认仓库名对。"
            ) from e
        if e.code in (401, 403):
            raise UpdateError(f"GitHub 拒绝访问（HTTP {e.code}），检查 token 是否有效/过期。") from e
        raise UpdateError(f"GitHub 返回 HTTP {e.code}：{url}") from e
    except urllib.error.URLError as e:
        raise UpdateError(f"连不上 GitHub：{e.reason}（服务器出网或 DNS 的问题）") from e


def _get_json(url: str) -> Any:
    with _open(url, accept="application/vnd.github+json") as r:
        return json.loads(r.read().decode("utf-8"))


def latest_release(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """最新 release。**没有 release 就退回落 tag** —— 很多仓库打 tag 但不写 release。

    只认 tag，不认分支 HEAD：分支一提交就变，版本没法比；tag 才是「发了一版」。
    """
    s = settings or update_settings()
    repo = s["repo"]
    prefix = s["tag_prefix"] or ""

    try:
        rels = _get_json(f"{GH_API}/repos/{repo}/releases?per_page=10")
    except UpdateError:
        rels = []
    if isinstance(rels, list):
        for rel in rels:
            tag = str(rel.get("tag_name") or "")
            if rel.get("draft"):
                continue
            if prefix and not tag.startswith(prefix):
                continue
            return {
                "tag": tag,
                "version": tag[len(prefix):] if prefix and tag.startswith(prefix) else tag,
                "name": str(rel.get("name") or tag),
                "notes": str(rel.get("body") or "")[:4000],
                "published": str(rel.get("published_at") or ""),
                "url": str(rel.get("html_url") or ""),
                "source": "release",
            }

    tags = _get_json(f"{GH_API}/repos/{repo}/tags?per_page=30")
    if not isinstance(tags, list):
        raise UpdateError("GitHub 的 tags 响应看不懂")
    for t in tags:
        tag = str(t.get("name") or "")
        if prefix and not tag.startswith(prefix):
            continue
        if not parse_version(tag):
            continue
        return {
            "tag": tag,
            "version": tag[len(prefix):] if prefix and tag.startswith(prefix) else tag,
            "name": tag,
            "notes": "",
            "published": "",
            "url": "",
            "source": "tag",
        }
    raise UpdateError(f"仓库 {repo} 里没找到符合前缀「{prefix or '（无）'}」的版本 tag")


def check(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """一次检查：本地版本 vs 远端最新。**网络失败不抛**，把 reason 放进返回值。

    为什么不抛：运维页要把「检查不了」也显示出来（比如服务器某天出不了网），
    抛异常会让整个板块白屏，用户以为页面坏了。
    """
    s = settings or update_settings()
    local = current_version(s)
    out: dict[str, Any] = {
        "enabled": s["enabled"],
        "repo": s["repo"],
        "packageRoot": s["package_root"],
        "tagPrefix": s["tag_prefix"],
        "current": local,
        "latest": "",
        "behind": False,
        "ok": False,
        "reason": "",
        "notes": "",
        "published": "",
        "url": "",
        "source": "",
        "token": bool(github_token()),
    }
    if not s["enabled"]:
        out["reason"] = "本实例没有开启一键更新（update_enabled=0）。这是默认：只有主实例才该有更新按钮。"
        return out
    try:
        rel = latest_release(s)
    except UpdateError as e:
        out["reason"] = str(e)
        return out
    out.update(
        ok=True,
        latest=rel["version"],
        notes=rel["notes"],
        published=rel["published"],
        url=rel["url"],
        source=rel["source"],
        tag=rel["tag"],
    )
    out["behind"] = bool(rel["version"]) and is_newer(rel["version"], local)
    if not out["behind"] and rel["version"] and version_key(rel["version"]) == version_key(local):
        out["reason"] = "已经是最新"
    elif not out["behind"]:
        out["reason"] = "本地版本不低于远端（可能是本地改了还没发版）"
    return out


# ── 下载 ───────────────────────────────────────────────────────────────────


def download(tag: str, dest_dir: str | Path, settings: dict[str, Any] | None = None) -> Path:
    """下 tarball 到 dest_dir。走 API 的 `/tarball/{ref}`：它会 302 到 codeload，
    urllib 自动跟随；private 仓库由 Authorization 头放行。

    为什么不用 codeload 直链：直链对 private 仓库要 token 且重定向规则更绕；
    API 端点把「认证 + 重定向」两件事都收在自己身上，实测更稳。
    """
    s = settings or update_settings()
    url = f"{GH_API}/repos/{s['repo']}/tarball/{urllib.parse.quote(tag)}"
    dest = Path(dest_dir) / f"{tag}.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _open(url, accept="application/vnd.github+json") as r:
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TARBALL:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise UpdateError(f"更新包超过 {MAX_TARBALL // 1024 // 1024}MB，拒绝")
                f.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise UpdateError("下到的包是空的")
    return dest


# ── 解包与白名单 ───────────────────────────────────────────────────────────


def _rel_of(name: str, package_root: str) -> str | None:
    """tar 内的绝对名 → 相对 package_root 的路径。不在包内返回 None。

    **路径穿越是这里唯一的硬防线**：`../../etc/passwd` 这类成员一旦被当成
    相对路径 join，就能写到树外去。所以先按 `/` 切段做结构判定（`..`、
    绝对路径、盘符），判定在**任何 join 之前**完成。
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return None
    if re.match(r"^[A-Za-z]:", name):
        return None
    parts = name.split("/")
    if any(p in ("..", "") for p in parts[:-1]) or ".." in parts:
        return None
    prefix = f"{package_root}/" if package_root else ""
    if prefix and not name.startswith(prefix):
        return None
    return name[len(prefix):]


def _is_allowed(rel: str) -> bool:
    low = rel.lower()
    if any(part in SKIP_PARTS for part in rel.split("/")):
        return False
    if Path(low).suffix in SKIP_SUFFIXES:
        return False
    if rel.startswith(ALLOW_PY_PREFIX) and low.endswith(".py"):
        return True
    if rel.startswith(ALLOW_DIST_PREFIX) and Path(low).suffix in DIST_SUFFIXES:
        return True
    return False


def _wrapper_prefix(tar: tarfile.TarFile, package_root: str) -> str:
    """GitHub 的 tarball 外面多套一层 `owner-repo-<sha>/`。

    **这是踩过的坑**：`download()` 拿的是 `api.github.com/.../tarball/<ref>`，
    它的顶层目录不是 `graphite/` 而是 `Learnbout-ai-memory-472b86b/`，
    于是所有成员都不以 `graphite/` 开头 → 白名单判定全军覆没 →
    线上报「包里没有可安装的文件」，而下载本身是成功的（2.5MB 就在那儿）。
    本地用自己打的包测不出来，因为手工 tar 没有这一层。

    判定：所有成员的第一段只有一种取值、且它不是 package_root，就当包裹层剥掉。
    不剥也不会错到天上去（白名单空 → 直接拒绝更新），但更新就永远用不了。
    """
    tops = {n.split("/", 1)[0] for n in tar.getnames() if "/" in n}
    if len(tops) == 1:
        only = next(iter(tops))
        if only != package_root:
            return only + "/"
    return ""


def _members(tar: tarfile.TarFile, package_root: str):
    """逐个给出 (成员, 相对 package_root 的路径)。第二个元素 None = 不在包内。

    路径穿越、绝对路径、符号链接的判定全在 `_rel_of` 里，这里只负责剥包裹层。
    """
    wrap = _wrapper_prefix(tar, package_root)
    for m in tar.getmembers():
        name = m.name
        if wrap:
            if not name.startswith(wrap):
                continue
            name = name[len(wrap):]
        yield m, _rel_of(name, package_root)


def inspect(tar_path: str | Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """只看不写：这个包里有什么、会被装哪些、被忽略哪些。"""
    s = settings or update_settings()
    pkg = s["package_root"]
    take: list[str] = []
    skip: list[str] = []
    danger: list[str] = []
    has_version = False
    with tarfile.open(tar_path, "r:gz") as tar:
        for m, rel in _members(tar, pkg):
            if m.isdir():
                continue
            if m.issym() or m.islnk():
                danger.append(f"{m.name}（符号链接）")
                continue
            if not m.isfile():
                continue
            if rel is None:
                continue
            # VERSION 单独探：它**不在**白名单里（不该被整文件覆盖），
            # 但它是「这个包能不能告诉你版本」的判据，所以要单独看一眼。
            if rel == VERSION_FILE:
                has_version = True
                continue
            if _is_allowed(rel):
                take.append(rel)
            else:
                skip.append(rel)
                if Path(rel.lower()).suffix in {".sh", ".bat", ".ps1", ".service", ".exe", ".dll"}:
                    danger.append(rel)
    if not take:
        raise UpdateError(
            f"包里没有可安装的文件（期望 {pkg}/backend/app/**.py 或 {pkg}/frontend/dist/**）"
        )
    return {
        "take": take,
        "skip": skip[:200],
        "skipCount": len(skip),
        "danger": danger[:40],
        "pyCount": sum(1 for x in take if x.startswith(ALLOW_PY_PREFIX)),
        "distCount": sum(1 for x in take if x.startswith(ALLOW_DIST_PREFIX)),
        "hasVersion": has_version,
    }


def _read_version_in_package(tar_path: str | Path, settings: dict[str, Any] | None = None) -> str:
    s = settings or update_settings()
    with tarfile.open(tar_path, "r:gz") as tar:
        for m, rel in _members(tar, s["package_root"]):
            if m.isfile() and rel == VERSION_FILE:
                f = tar.extractfile(m)
                if f:
                    return f.read(64).decode("utf-8", "replace").strip()
    return ""


# ── 特权通道（阶段 10 第 5 步，2026-10-29）─────────────────────────────────
#
# 降权之后（四服务以 aim 运行）代码树是 root:root，Web 进程写不进去。
# 这里不是"顺便加个开关"，而是把动作换了个执行者：
#
#   aim 进程：下载 → 白名单 → 解到暂存区 → 写申请单 → 触发 oneshot → 读回执
#   root oneshot（graphite-update.service）：备份 → 落盘 → 写 VERSION → 清字节码 → 重启
#
# 判据用**文件系统事实**（代码根可不可写）而不是环境变量：环境变量描述的是
# "我以为我在哪"，可写与否才是"我实际能不能落盘"。两者不一致时以磁盘为准，
# 否则就会出现"配置说走特权通道、实际代码根其实可写"这种两套路径都试一遍的乱象。

SPOOL_ENV = "GRAPHITE_UPDATE_SPOOL"
SPOOL_DEFAULT = "/opt/ai-memory/update-spool"
HELPER_UNIT = "graphite-update.service"
HELPER_WAIT = 45  # 秒；一次备份 + 数百个小文件实测 1~3s，给 45s 是给慢盘留余量


def spool_dir() -> Path:
    """申请单/回执/暂存区的根。**纯 getter**：不 mkdir、不写文件。"""
    raw = (os.environ.get(SPOOL_ENV) or "").strip()
    return Path(raw) if raw else Path(SPOOL_DEFAULT)


def privileged_mode(settings: dict[str, Any] | None = None) -> bool:
    """要不要走 root oneshot。

    三个条件同时成立才算：
    1. 是 POSIX 且不是 root（Windows 上没有 systemctl，root 自己就能写不用求人）；
    2. 代码根**写不进去** —— 这是唯一的事实判据；
    3. 允许用环境变量强制开（`GRAPHITE_UPDATE_PRIVILEGED=1`），用于"代码根恰好可写
       但仍想走通道"的验收场景。强制**关**同样用这个变量（`=0`）。
    """
    force = (os.environ.get("GRAPHITE_UPDATE_PRIVILEGED") or "").strip().lower()
    if force in ("0", "false", "no", "off"):
        return False
    if force in ("1", "true", "yes", "on"):
        return True
    if os.name != "posix":
        return False
    if getattr(os, "geteuid", lambda: 1)() == 0:
        return False
    s = settings or update_settings()
    probe = Path(s["code_root"]) / "backend" / "app"
    return not os.access(probe, os.W_OK)


def _whoami() -> str:
    """给 journal 留个人名。拿不到就报 uid，不编。"""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001 —— Windows 没 pwd，拿不到就不写名字
        return str(getattr(os, "getuid", lambda: "?")())


def _stage(tar_path: str | Path, settings: dict[str, Any]) -> tuple[Path, list[str]]:
    """把包里**通过白名单**的文件解到 `<spool>/staging/<ts>_<rand>/`。

    为什么先解出来而不是直接交给 root 解包：解析 tar 这件事本身有攻击面
    （路径穿越、符号链接、炸弹），让它发生在 aim 这一侧 —— 解错了也只是
    在自己可写的暂存区里乱，不会污染代码树。root 那一侧只做"已经解好的
    普通文件的搬运"，它仍然会**再验一遍**（两层都过才落盘）。
    """
    s = settings
    staging = spool_dir() / "staging" / (
        time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True, exist_ok=False)
    taken: list[str] = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for m, rel in _members(tar, s["package_root"]):
            if not m.isfile() or m.issym() or m.islnk():
                continue
            if rel is None or not _is_allowed(rel):
                continue
            f = tar.extractfile(m)
            if not f:
                continue
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 大小上限：单文件超了就整包拒绝，不静默截断
            data = f.read(MAX_TARBALL + 1)
            if len(data) > MAX_TARBALL:
                raise UpdateError(f"包内单文件超过上限：{rel}")
            dst.write_bytes(data)
            taken.append(rel)
    if not taken:
        raise UpdateError("暂存区是空的（白名单一个都没命中）")
    return staging, taken


def _call_helper(payload: dict[str, Any], nonce: str) -> dict[str, Any]:
    """写申请单 → 触发 root oneshot → 等回执。**唯一的特权入口。**

    四个细节都是踩过坑才写上的：

    - `--no-block`：oneshot 里会 `sleep` 一段时间再重启服务，同步等它等于
      让这次 HTTP 请求陪着等（而且重启把自己杀了，响应写不回去）。
    - 先删旧回执：上一次失败的 `result.json` 留着，这一次会读到上一单的结果
      并当成成功。nonce 是第二道保险，两道都要有。
    - 用 nonce 比对：只认自己这一单的回执。
    - 超时**不重试**：申请单可能已经被执行了，重开会把"装了一次"变成"装两次"，
      备份也会多一份错位的。失败就报失败，让人去看 journal。
    """
    spool = spool_dir()
    try:
        spool.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpdateError(f"特权通道的暂存目录不可用：{spool}（{e}）") from e
    req = spool / "request.json"
    res = spool / "result.json"
    res.unlink(missing_ok=True)
    try:
        req.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        raise UpdateError(f"写不了申请单：{req}（{e}）") from e

    try:
        cp = subprocess.run(  # noqa: S603 —— 固定 argv，不过 shell
            ["systemctl", "start", "--no-block", HELPER_UNIT],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError as e:
        req.unlink(missing_ok=True)
        raise UpdateError(
            f"本机没有 systemctl，无法触发特权通道（{HELPER_UNIT}）。"
            "若代码根本来就可写，用 GRAPHITE_UPDATE_PRIVILEGED=0 关掉特权模式。"
        ) from e
    except subprocess.SubprocessError as e:
        req.unlink(missing_ok=True)
        raise UpdateError(f"触发特权通道失败：{e}") from e
    if cp.returncode != 0:
        req.unlink(missing_ok=True)
        msg = (cp.stderr or cp.stdout or "").strip()[:300]
        raise UpdateError(
            f"特权通道被拒（systemctl start {HELPER_UNIT} 返回 {cp.returncode}）：{msg}"
        )

    deadline = time.time() + HELPER_WAIT
    while time.time() < deadline:
        if res.is_file():
            try:
                got = json.loads(res.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.2)  # 正在写，或者残留的半截文件，再等一轮
                continue
            if not isinstance(got, dict) or got.get("nonce") != nonce:
                time.sleep(0.2)
                continue
            res.unlink(missing_ok=True)
            if not got.get("ok"):
                raise UpdateError("特权端拒绝执行：" + str(got.get("error") or "（没给原因）")[:400])
            return got
        time.sleep(0.2)
    raise UpdateError(
        f"等了 {HELPER_WAIT} 秒没收到特权端回执。查：journalctl -u {HELPER_UNIT} -n 50"
    )


def channel_check() -> dict[str, Any]:
    """特权通道自检（op=noop）。**不改任何文件**，只问一句"通道通不通"。

    存在的理由：通道坏掉时最可能的症状是"更新按钮报错"或者"报成功但版本没变"，
    两种都要翻日志才能定位。有一个可以随时按一下的自检，这类问题从"排查"
    变成"看一眼"。
    """
    if not privileged_mode():
        return {"ok": True, "mode": "direct", "detail": "代码根可写，无需特权通道"}
    nonce = uuid.uuid4().hex
    got = _call_helper(
        {"nonce": nonce, "op": "noop", "requestedBy": _whoami(),
         "requestedAt": int(time.time())},
        nonce,
    )
    return {
        "ok": True,
        "mode": "helper",
        "euid": got.get("euid"),
        "codeRoot": got.get("codeRoot"),
        "spool": got.get("spool"),
        "backupRoot": got.get("backupRoot"),
    }


# ── 备份 / 覆盖 / 回滚 ─────────────────────────────────────────────────────


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def backup_current(settings: dict[str, Any] | None = None, *, label: str = "") -> Path:
    """把现役 `backend/app` 与 `frontend/dist` 复制到 `<backup_root>/update_<ts>/`。

    用**复制**不用移动：移动一旦中途失败，现役代码就半残了；复制失败只是白占点盘。
    """
    s = settings or update_settings()
    root = Path(s["code_root"])
    ts = _stamp() + (f"_{label}" if label else "")
    dest = Path(s["backup_root"]) / f"update_{ts}"
    n = 2
    while dest.exists():  # 同秒两次备份不能同名覆盖（阶段 11 踩过 os.replace 的坑）
        dest = Path(s["backup_root"]) / f"update_{ts}-{n}"
        n += 1
    dest.mkdir(parents=True, exist_ok=False)
    for name, src in (("app", root / "backend" / "app"), ("dist", root / "frontend" / "dist")):
        if src.is_dir():
            shutil.copytree(src, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    ver = _version_path(root)
    if ver.is_file():
        shutil.copy2(ver, dest / VERSION_FILE)
    # 备份成功判据：app 目录至少要有 main.py，否则这个回滚点是假的
    if not (dest / "app" / "main.py").is_file():
        raise UpdateError(f"备份没成功（{dest} 里找不到 app/main.py），已中止更新")
    return dest


def apply(tar_path: str | Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """备份 → 逐文件覆盖 → 写 VERSION。**不重启、不探活** —— 那两步在进程外做。

    为什么把「重启」留给上层：本进程重启自己会立刻断掉这次 HTTP 响应，
    前端只会看到「请求失败」。正确顺序是「写完 → 先回 200 → 再由独立进程拉起」。

    两条实现路径，判据是代码根能不能写（见 `privileged_mode`）：

    - **直接写**（本机、或 root 运行）：老路径，`write_bytes` 逐个覆盖。
    - **特权通道**（服务器，进程是 aim）：解到暂存区 → 交 root oneshot。
      返回值里 `restartVia` 会写成 `"helper"`，调用方据此**不要再排重启**，
      因为重启由特权端自己延迟执行（它得等这次 HTTP 响应先写回去）。
    """
    s = settings or update_settings()
    info = inspect(tar_path, s)
    if privileged_mode(s):
        return _apply_via_helper(tar_path, s, info)
    root = Path(s["code_root"])
    pkg = s["package_root"]
    backup = backup_current(s, label="pre_update")

    written = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        for m, rel in _members(tar, pkg):
            if not m.isfile() or m.issym() or m.islnk():
                continue
            if rel is None or not _is_allowed(rel):
                continue
            target = (root / rel).resolve()
            # 二次确认：解析后仍在代码根下（防 Windows 盘符/大小写之类的意外）
            if root.resolve() not in target.parents and target != root.resolve():
                raise UpdateError(f"拒绝写入代码根以外的路径：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(m)
            if not f:
                continue
            data = f.read()
            target.write_bytes(data)
            written += 1

    new_ver = _read_version_in_package(tar_path, s)
    if new_ver:
        _version_path(root).write_text(new_ver, encoding="utf-8")

    # 清字节码：留着旧 .pyc 的话，同名的旧模块可能比新源码先被加载
    for pyc in (root / "backend" / "app").rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)

    return {
        "written": written,
        "version": new_ver or current_version(s),
        "backup": str(backup),
        "skipped": info["skipCount"],
        "danger": info["danger"],
        "restartVia": "local",
    }


def _apply_via_helper(tar_path: str | Path, s: dict[str, Any],
                      info: dict[str, Any]) -> dict[str, Any]:
    """走 root oneshot 装包。备份、VERSION、清字节码都在特权端做。"""
    staging, files = _stage(tar_path, s)
    new_ver = _read_version_in_package(tar_path, s)
    nonce = uuid.uuid4().hex
    try:
        got = _call_helper(
            {
                "nonce": nonce,
                "op": "apply",
                "staging": str(staging),
                "version": new_ver,
                "restart": s["service"],
                "requestedBy": _whoami(),
                "requestedAt": int(time.time()),
            },
            nonce,
        )
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # 暂存区正常情况下已被特权端删掉；这里兜一手，免得 spool 越积越多
    shutil.rmtree(staging, ignore_errors=True)

    installed = got.get("files") or []
    if len(installed) != len(files):
        # 不是致命错误（两边白名单可能对同一棵树给出不同结论），但必须留痕：
        # 少了文件意味着"这次更新装的东西比你以为的少"。
        print(
            f"[updater] 警告：特权端装了 {len(installed)} 个文件，本地计划 {len(files)} 个",
            file=sys.stderr,
        )
    return {
        "written": int(got.get("written") or 0),
        "version": str(got.get("version") or current_version(s)),
        "backup": str(got.get("backup") or ""),
        "skipped": info["skipCount"],
        "danger": info["danger"],
        "restartVia": "helper",
    }


def restore(backup_dir: str | Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """把备份盖回去。只认 `<backup>/app` 与 `<backup>/dist` 两个目录。"""
    s = settings or update_settings()
    src = Path(backup_dir)
    if privileged_mode(s):
        return _restore_via_helper(src, s)
    root = Path(s["code_root"])
    if not (src / "app" / "main.py").is_file():
        raise UpdateError(f"这不是一份可回滚的备份：{src}")
    for name, dst in (
        ("app", root / "backend" / "app"),
        ("dist", root / "frontend" / "dist"),
    ):
        if (src / name).is_dir():
            shutil.copytree(src / name, dst, dirs_exist_ok=True)
    ver = src / VERSION_FILE
    if ver.is_file():
        shutil.copy2(ver, _version_path(root))
    for pyc in (root / "backend" / "app").rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    return {"restoredFrom": str(src), "version": current_version(s), "restartVia": "local"}


def _restore_via_helper(src: Path, s: dict[str, Any]) -> dict[str, Any]:
    """走 root oneshot 回滚。只传目录名，不传路径 —— 特权端只认备份区里的名字。"""
    base = Path(s["backup_root"]).resolve()
    if src.resolve().parent != base:
        raise UpdateError(f"回滚点必须是 {base} 下的目录：{src}")
    name = src.name
    nonce = uuid.uuid4().hex
    got = _call_helper(
        {
            "nonce": nonce,
            "op": "restore",
            "source": name,
            "restart": s["service"],
            "requestedBy": _whoami(),
            "requestedAt": int(time.time()),
        },
        nonce,
    )
    return {
        "restoredFrom": str(src),
        "version": str(got.get("version") or ""),
        "safetyBackup": str(got.get("backup") or ""),
        "restartVia": "helper",
    }


def list_backups(settings: dict[str, Any] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """列更新回滚点（运维台要显示「可回滚到哪」）。"""
    s = settings or update_settings()
    base = Path(s["backup_root"])
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(base.glob("update_*"), key=lambda p: p.name, reverse=True):
        if not (d / "app" / "main.py").is_file():
            continue
        try:
            ver = (d / VERSION_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            ver = ""
        out.append({"name": d.name, "version": ver, "path": str(d)})
        if len(out) >= limit:
            break
    return out


def temp_dir() -> Path:
    """下载/解包的临时目录（调用方负责清理）。留在系统临时区，不污染代码树。"""
    return Path(tempfile.mkdtemp(prefix="gw_update_"))
