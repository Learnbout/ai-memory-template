"""插件存储（阶段 24，2026-10-25）

── 设计取舍：为什么只有「信任插件」（A 档），没有 iframe 沙箱（B 档）
调研结论写在与《知识_同类项目安全与插件化借鉴（2026-10-25）》里，这里只留结论：
Obsidian 官方文档自己承认「因技术限制无法可靠限制插件权限」，插件继承宿主权限；
而真正的隔离（opaque-origin iframe + postMessage 桥）的代价是**插件碰不到宿主 DOM**，
也就改不了布局与样式 —— 而主人要的正是「能改布局、改样式、加功能」。

两者不可兼得。所以本期只做 A 档，并且把它关在管理员手里：
· 只有 admin 能安装/启停（caps 在路由层校验）
· 安装时必须看得到来源、文件清单与 sha256
· 默认停用，装完不自动跑
· script-src 保持 'self'：插件代码只能从 `/api/plugins/<id>/asset/<name>` 这个同源口子加载

── 目录与状态
· 插件本体：`backend/data/plugins/<id>/`（代码侧，不进用户的库，不会随 sync_push 覆盖）
· 启停状态：`backend/data/plugins/state.json`
· 所有落盘路径都做「resolve 后仍在 plugins 根下」的校验（防 ../ 穿越）

── manifest（plugin.json）最小字段
    { "id": "hello", "name": "示例", "version": "1.0.0",
      "description": "", "author": "",
      "css": "style.css",        // 可选
      "js": "index.js",          // 可选
      "requireCaps": ["page.dashboard"] }   // 可选：不满足时不加载
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths

# 2026-10-27（R2）：去掉导入期求值。原来这两个是模块级常量，import 那一刻就把
# 路径冻死；冻结态下 `paths.DATA_DIR` 会指向解包临时目录，插件装进去退出即蒸发。
def _plugin_root() -> Path:
    return paths.data_dir() / "plugins"


def _state_file() -> Path:
    return _plugin_root() / "state.json"

# 单个插件解压后的总大小上限（防 zip 炸弹：一个 1KB 的 zip 可以解出几个 GB）
_MAX_UNPACKED = 8 * 1024 * 1024
_MAX_FILES = 200
_ALLOWED_SUFFIX = {".js", ".mjs", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md"}


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _ensure() -> None:
    _plugin_root().mkdir(parents=True, exist_ok=True)


def _safe_child(root: Path, name: str) -> Path:
    """把 name 拼到 root 下，并保证解析后仍在 root 内（防 ../ 与符号链接逃逸）"""
    target = (root / name).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError("非法路径")
    return target


def _read_state() -> Dict[str, Any]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": []}
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), list):
        return {"enabled": []}
    return data


def _write_state(state: Dict[str, Any]) -> None:
    _ensure()
    tmp = _state_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _state_file())


def _manifest(pid: str) -> Optional[Dict[str, Any]]:
    try:
        p = _safe_child(_plugin_root(), pid) / "plugin.json"
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_plugins() -> List[Dict[str, Any]]:
    _ensure()
    state = _read_state()
    enabled = set(state.get("enabled") or [])
    out: List[Dict[str, Any]] = []
    for child in sorted(_plugin_root().iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        man = _manifest(child.name)
        if man is None:
            out.append({"id": child.name, "name": child.name, "broken": True, "enabled": False})
            continue
        out.append(
            {
                "id": child.name,
                "name": str(man.get("name") or child.name),
                "version": str(man.get("version") or ""),
                "author": str(man.get("author") or ""),
                "description": str(man.get("description") or ""),
                "css": man.get("css") or None,
                "js": man.get("js") or None,
                "requireCaps": man.get("requireCaps") or [],
                "enabled": child.name in enabled,
                "installedAt": state.get("installedAt", {}).get(child.name),
            }
        )
    return out


def set_enabled(pid: str, enabled: bool) -> Dict[str, Any]:
    _ensure()
    if not _safe_child(_plugin_root(), pid).is_dir():
        raise FileNotFoundError(pid)
    state = _read_state()
    cur = set(state.get("enabled") or [])
    if enabled:
        cur.add(pid)
    else:
        cur.discard(pid)
    state["enabled"] = sorted(cur)
    _write_state(state)
    return {"id": pid, "enabled": enabled}


def uninstall(pid: str) -> Dict[str, Any]:
    target = _safe_child(_plugin_root(), pid)
    if not target.is_dir():
        raise FileNotFoundError(pid)
    state = _read_state()
    state["enabled"] = [x for x in (state.get("enabled") or []) if x != pid]
    (state.get("installedAt") or {}).pop(pid, None)
    _write_state(state)
    shutil.rmtree(target, ignore_errors=True)
    return {"id": pid, "removed": True}


def install_zip(raw: bytes) -> Dict[str, Any]:
    """接收一个 .zip，解到 plugins/<id>/，默认停用。

    安全要点（每一条都对应一个真实攻击面）：
    · 只认 .js/.css/.json/图片等白名单后缀，`.exe/.py/.sh` 一律拒
    · 拒绝绝对路径与 `..`（zip slip）
    · 限制文件数与解压后总大小（zip 炸弹）
    · 已存在的同名插件不允许静默覆盖 —— 覆盖等于「用户以为装的是 A，跑的是 B」
    """
    _ensure()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip：{e}") from e

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise ValueError("zip 是空的")
    if len(infos) > _MAX_FILES:
        raise ValueError(f"文件太多（上限 {_MAX_FILES}）")

    total = 0
    for i in infos:
        name = i.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"非法路径：{i.filename}")
        if Path(name).suffix.lower() not in _ALLOWED_SUFFIX:
            raise ValueError(f"不允许的文件类型：{i.filename}")
        total += i.file_size
        if total > _MAX_UNPACKED:
            raise ValueError(f"解压后超过上限（{_MAX_UNPACKED // 1024 // 1024} MB）")

    # 兼容「zip 里套一层目录」的打包方式（很多人右键压缩整个文件夹）
    names = [i.filename.replace("\\", "/") for i in infos]
    prefix = ""
    if not any(n == "plugin.json" for n in names):
        first = names[0].split("/")[0]
        if all(n.startswith(first + "/") for n in names) and f"{first}/plugin.json" in names:
            prefix = first + "/"

    man_raw = None
    for i in infos:
        if i.filename.replace("\\", "/") == f"{prefix}plugin.json":
            man_raw = zf.read(i).decode("utf-8", "replace")
            break
    if man_raw is None:
        raise ValueError("缺少 plugin.json")
    try:
        man = json.loads(man_raw)
    except ValueError as e:
        raise ValueError(f"plugin.json 不是合法 JSON：{e}") from e

    pid = str(man.get("id") or "").strip()
    if not pid or not all(c.isalnum() or c in "-_." for c in pid):
        raise ValueError("plugin.json 的 id 只能包含字母/数字/-/_/.")

    dest = _safe_child(_plugin_root(), pid)
    if dest.exists():
        raise ValueError(f"插件 {pid} 已存在，请先卸载")

    digest = hashlib.sha256(raw).hexdigest()
    dest.mkdir(parents=True, exist_ok=False)
    for i in infos:
        rel = i.filename.replace("\\", "/")
        if prefix and rel.startswith(prefix):
            rel = rel[len(prefix):]
        if not rel:
            continue
        target = _safe_child(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(i) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)

    state = _read_state()
    state.setdefault("installedAt", {})[pid] = _now()
    _write_state(state)

    return {
        "id": pid,
        "name": str(man.get("name") or pid),
        "version": str(man.get("version") or ""),
        "sha256": digest,
        "bytes": len(raw),
        "files": [i.filename.replace("\\", "/") for i in infos if i.filename.replace("\\", "/").startswith(prefix)],
        "enabled": False,
    }


def asset_path(pid: str, name: str) -> Path:
    """插件资源路径（供 FileResponse 用）。只允许 manifest 里声明过的文件名，
    外加图标 —— 不允许任意读（否则等于给了一个读 plugins 目录的任意文件口）。"""
    man = _manifest(pid)
    if man is None:
        raise FileNotFoundError(pid)
    allowed = {str(man.get("js") or ""), str(man.get("css") or ""), "icon.svg", "icon.png"}
    if name not in allowed or not name:
        raise FileNotFoundError(name)
    p = _safe_child(_safe_child(_plugin_root(), pid), name)
    if not p.is_file():
        raise FileNotFoundError(name)
    return p
