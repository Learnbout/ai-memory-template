# -*- coding: utf-8 -*-
"""安装内核：**计划（预演）与执行共用同一套判定**。

这一层是从「AI 融合记忆库 · 本地安装包思路」那份文档借来的三条硬规矩，加上本项目
自己的实际约束：

## 一、程序与数据分离

- **程序层**  `<安装目录>/backend`、`frontend`、`vault`（库**代码**）、`.runtime`
  —— 可以整个删掉重装、可以覆盖更新。
- **数据层**  `<数据目录>` 里的笔记库（`index.sqlite3`、`*.md`）
  —— **首次可以播种，一旦存在永不被覆盖**。

注意「保护语义 = 保护覆盖，不保护创建」。这条是那份文档里最值钱的一条教训：
他们初版写成「一律拒绝写 .md」，结果装完一个记忆文件都没有。所以这里判定的粒度是
**单个文件**：目标已存在 → 跳过并记一笔；目标不存在 → 正常写入。

## 二、预演 = 执行

`plan()` 产出的动作清单，就是 `execute()` 要一件件做的事。预演时显示的数字
（新建几个、跳过几个、备份几个）与真跑时完全同源——如果两处各算一遍，迟早对不上，
而「预演说覆盖 2、实际写了 7」比不给预演更糟。

## 三、三态判定

| 现场 | 判定 | 行为 |
|---|---|---|
| 有 manifest 且版本相同 | 已是最新 | 报「已是最新」，不做无谓覆盖 |
| 有 manifest 版本不同 | 更新 | 只动程序层，数据层文件全部跳过 |
| 有程序但没有 manifest | 装过但没记录（旧版/手装） | 按安装处理，**不动笔记** |
| 目录为空/不存在 | 全新安装 | 正常播种（含初始空库） |

manifest 落 `<安装目录>/.ai-memory-install.json`，记版本、每个文件的 sha256、
install_dir / data_dir、时间戳。放在安装目录而不是数据目录：卸载删程序时它跟着走，
不会在用户的笔记目录里留垃圾。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import steps

MANIFEST_NAME = ".ai-memory-install.json"
SETUP_VERSION = "2.0.0"

Progress = Callable[[str, float], None]     # (说明文字, 0~1 进度)


def _noop(_msg: str, _pct: float) -> None:
    pass


# ---------------------------------------------------------------- manifest
def manifest_path(target: Path) -> Path:
    return target / MANIFEST_NAME


def read_manifest(target: Path) -> Optional[Dict[str, Any]]:
    p = manifest_path(target)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def write_manifest(target: Path, *, mode: str, version: str,
                   data_dir: Path, runtime_mode: str) -> Path:
    files: Dict[str, str] = {}
    for sub in ("backend", "frontend", "vault"):
        base = target / sub
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                files[str(f.relative_to(target)).replace("\\", "/")] = _sha256(f)
    data = {
        "setup_version": SETUP_VERSION,
        "installed_version": version,
        "mode": mode,
        "runtime_mode": runtime_mode,
        "install_dir": str(target),
        "data_dir": str(data_dir),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": files,
    }
    p = manifest_path(target)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- 三态判定
def detect_state(target: Path, package_version: str) -> Dict[str, Any]:
    """返回 {state, detail, notes}。state ∈ fresh / latest / update / legacy。"""
    tgt = Path(target)
    mf = read_manifest(tgt)
    has_program = (tgt / "backend" / "app" / "main.py").is_file()
    has_data = _looks_like_data(tgt)

    if mf and has_program:
        installed = str(mf.get("installed_version") or "")
        if installed and installed == package_version:
            return {"state": "latest", "installed_version": installed,
                    "data_dir": mf.get("data_dir") or str(tgt), "manifest": mf}
        return {"state": "update", "installed_version": installed,
                "data_dir": mf.get("data_dir") or str(tgt), "manifest": mf}
    if has_program:
        return {"state": "legacy", "installed_version": "", "data_dir": str(tgt), "manifest": None}
    if has_data:
        # 目录里有笔记但没有程序：说明用户选了一个已有笔记目录 —— 只装程序，笔记一律不动
        return {"state": "fresh", "installed_version": "", "data_dir": str(tgt), "manifest": None}
    return {"state": "fresh", "installed_version": "", "data_dir": str(tgt), "manifest": None}


def _looks_like_data(target: Path) -> bool:
    if (target / "vault" / "数据库" / "index.sqlite3").is_file():
        return True
    if (target / "数据库" / "index.sqlite3").is_file():
        return True
    try:
        return any(target.glob("*.md"))
    except OSError:
        return False


# ---------------------------------------------------------------- 计划
def _sources() -> Dict[str, Optional[Path]]:
    return steps.source_map()


def plan(target: Path, *, mode: str = "local", runtime_mode: str = "portable",
         package_version: str = "") -> Dict[str, Any]:
    """把「要做什么」算成一份清单。预演和执行都读它。"""
    tgt = Path(target)
    src = _sources()
    actions: List[Dict[str, Any]] = []
    problems: List[str] = []

    def add(kind: str, rel: str, src_path: Optional[Path] = None, note: str = "") -> None:
        actions.append({"kind": kind, "path": rel, "src": str(src_path) if src_path else "",
                        "note": note})

    # 程序层
    for key, sub, label in (("backend", "backend", "后端"),
                            ("frontend", "frontend/dist", "工作台页面"),
                            ("vault", "vault", "记忆库代码")):
        base = src.get(key)
        if base is None or not base.is_dir():
            problems.append("安装包里缺「%s」这部分" % label)
            continue
        for f in base.rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            rel = "%s/%s" % (sub, str(f.relative_to(base)).replace("\\", "/"))
            dest = tgt / rel
            same = dest.is_file() and _sha256(dest) == _sha256(f)
            add("skip" if same else "write", rel, f,
                "内容相同，跳过" if same else ("覆盖" if dest.is_file() else "新建"))

    # 运行环境
    if runtime_mode == "portable":
        rt = steps.bundle_runtime()
        if rt is None:
            problems.append("安装包里没有自带运行环境")
        else:
            need = not steps.runtime_ready(tgt)
            add("runtime" if need else "skip", ".runtime/", rt,
                "复制自带运行环境" if need else "运行环境已就绪")
    else:
        add("venv", ".venv/", None, "用本机 Python 建独立环境")

    # 数据层：只有**不存在**才播种，存在一律跳过
    db = tgt / "vault" / "数据库" / "index.sqlite3"
    add("skip" if db.is_file() else "seed", "vault/数据库/index.sqlite3", None,
        "已有记忆库，绝不动它" if db.is_file() else "建一个空库")

    counts = {"write": 0, "skip": 0, "seed": 0, "runtime": 0, "venv": 0}
    for a in actions:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1

    return {"target": str(tgt), "mode": mode, "runtime_mode": runtime_mode,
            "package_version": package_version,
            "state": detect_state(tgt, package_version),
            "actions": actions, "counts": counts, "problems": problems}


def preview(plan_obj: Dict[str, Any]) -> str:
    """给人看的一行摘要。**预演和执行看的是同一份 plan**。"""
    c = plan_obj["counts"]
    st = plan_obj["state"]["state"]
    label = {"fresh": "全新安装", "update": "更新程序", "latest": "已是最新",
             "legacy": "覆盖旧版程序"}.get(st, st)
    parts = ["%s" % label]
    if c.get("write"):
        parts.append("写入 %d 个文件" % c["write"])
    if c.get("skip"):
        parts.append("跳过 %d 个（内容相同或数据保护）" % c["skip"])
    if c.get("runtime"):
        parts.append("复制运行环境")
    if c.get("venv"):
        parts.append("建独立运行环境")
    if c.get("seed"):
        parts.append("建空库")
    return "｜".join(parts)


# ---------------------------------------------------------------- 执行
def execute(plan_obj: Dict[str, Any], *, progress: Progress = _noop,
            package_version: str = "", mode: str = "local",
            runtime_mode: str = "portable", admin: Optional[Dict[str, Any]] = None,
            with_deps: bool = True) -> Dict[str, Any]:
    """按计划一件件做。返回 {ok, errors, log, target, ...}。"""
    tgt = Path(plan_obj["target"])
    errors: List[str] = list(plan_obj.get("problems") or [])
    log: List[str] = []

    def say(msg: str, pct: float) -> None:
        log.append(msg)
        progress(msg, pct)

    try:
        tgt.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "errors": ["目标文件夹建不了：%s" % exc], "log": log,
                "target": str(tgt)}

    actions = plan_obj["actions"]
    total = max(1, len(actions))
    for i, a in enumerate(actions):
        kind, rel = a["kind"], a["path"]
        pct = 0.05 + 0.75 * (i / total)
        if kind == "skip":
            say("跳过 %s（%s）" % (rel, a.get("note") or ""), pct)
            continue
        if kind == "write":
            src_p = Path(a["src"]) if a.get("src") else None
            if src_p is None or not src_p.is_file():
                errors.append("找不到源文件：%s" % rel)
                continue
            dest = tgt / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_p, dest)
            except OSError as exc:
                errors.append("写不进去 %s（%s）" % (rel, exc))
            continue
        if kind == "runtime":
            say("复制自带运行环境（不用你装 Python）…", pct)
            errors += steps._install_runtime_portable(tgt, lambda m: say(m, pct))
            continue
        if kind == "venv":
            say("用电脑上已有的 Python 建独立环境…", pct)
            errors += steps._install_runtime_system(tgt, lambda m: say(m, pct))
            continue
        if kind == "seed":
            say("建一个空库…", pct)
            errors += _seed_db(tgt, say)
            continue

    # 形态配置
    say("写入形态配置…", 0.85)
    data_dir = Path(plan_obj["state"].get("data_dir") or tgt)
    try:
        cfg_path = tgt / "backend" / "data" / "backend.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg: Dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                shutil.copy2(cfg_path, cfg_path.with_name(cfg_path.name + ".broken"))
                cfg = {}
        cfg["mode"] = mode
        cfg["vault_root"] = str(tgt / "vault").replace("\\", "/")
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        errors.append("写配置失败（%s）" % exc)

    # 管理员凭据（只有服务器本体）
    if admin and str(admin.get("username") or "").strip():
        try:
            steps._write_pending_admin(tgt, admin, lambda m: say(m, 0.88))
        except OSError as exc:
            errors.append("管理员凭据写不进去（%s）" % exc)

    # 清单
    say("记录安装清单（版本与校验值）…", 0.92)
    try:
        write_manifest(tgt, mode=mode, version=package_version, data_dir=data_dir,
                       runtime_mode=runtime_mode)
    except OSError as exc:
        errors.append("写安装清单失败（%s）" % exc)

    return {"ok": not errors, "errors": errors, "log": log, "target": str(tgt),
            "data_dir": str(data_dir)}


def _seed_db(tgt: Path, say: Callable[[str, float], None]) -> List[str]:
    """建空库（只建表结构，不动任何笔记）。"""
    init_py = tgt / "vault" / "脚本" / "db_init.py"
    py = steps.runtime_exe(tgt)
    if not init_py.is_file() or not py.is_file():
        return []
    try:
        r = subprocess.run([str(py), str(init_py)], capture_output=True, text=True,
                           timeout=180, cwd=str(tgt / "vault"))
        if r.returncode == 0:
            say("空库结构已建好", 0.8)
            return []
        return ["建空库结构返回 %d（不影响使用，首启会再试）" % r.returncode]
    except Exception as exc:  # noqa: BLE001
        return ["建空库跳过：%s" % exc]
