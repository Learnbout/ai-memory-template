# -*- coding: utf-8 -*-
"""安装器 · 写应用自身的配置（`backend/data/backend.json`）。

## 为什么安装器要管这个文件

`GRAPHITE_MODE` 的默认值是 `local`（fail-safe），但**装的是哪个包就该用哪种形态**。
安装器是唯一知道「这个包是什么」的角色，所以由它把显式值写进配置文件：

    <安装目录>/backend/data/backend.json
    {
      "mode": "local" | "central",
      "vault_root": "/path/to/记忆库"      ← 不填就让后端就近探测
    }

## 三条约束

- **合并**：文件里可能已经有 `vault_root` / `update_repo` 等用户改过的值，只动我们要动的键；
- **备份**：改前留 `backend.json.bak-<时间戳>`；
- **原子**：先写 `.tmp` 再 `os.replace`，避免写一半断电留下半个 JSON（半截 JSON 会让
  服务启动时报 `JSONDecodeError`，比没有文件更难查）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional


def install_dir() -> Path:
    """安装目录：冻结态是 exe 所在目录，源码态是 graphite-workspace/。

    `AI_MEMORY_INSTALL_DIR` 可覆盖（验收脚本用它把配置写到临时目录，
    这样测试不会改到真实安装）。
    """
    import sys

    override = (os.environ.get("AI_MEMORY_INSTALL_DIR") or "").strip()
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return install_dir() / "backend" / "data" / "backend.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-installer")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_app_config(mode: str,
                     vault_root: Optional[str] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把形态（与可选库根）写进 backend.json；返回 {path, backup, changed}。"""
    if mode not in ("local", "central"):
        raise ValueError("mode 只能是 local 或 central")
    path = config_path()
    existed = path.is_file()
    data: Dict[str, Any] = {}
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            # 半截 JSON：备份一份让用户还能救，重写成干净配置
            bak = path.with_name(path.name + ".broken-" + time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(path, bak)
            data = {}
            print("警告：backend.json 不是合法 JSON（%s），已备份到 %s" % (exc, bak))

    before = json.dumps(data, sort_keys=True, ensure_ascii=False)
    data["mode"] = mode
    if vault_root:
        data["vault_root"] = str(vault_root).replace("\\", "/")
    for k, v in (extra or {}).items():
        data[k] = v
    after = json.dumps(data, sort_keys=True, ensure_ascii=False)

    backup = ""
    if existed and before != after:
        b = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, b)
        backup = str(b)
    if not existed or before != after:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"path": str(path), "backup": backup, "changed": before != after,
            "mode": mode, "vault_root": data.get("vault_root", "")}
