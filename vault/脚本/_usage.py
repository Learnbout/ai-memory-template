# -*- coding: utf-8 -*-
"""脚本使用计数（供 脚本索引.md 的「使用次数」列）。

设计要点（2026-09-16 建）：
- 计数文件放库内 `.workbuddy/script_usage.json`：该目录不参与 md 扫描与索引，
  不会污染记忆库条目；**该目录在 `.gitignore` 内 → 计数不进 git 备份、不跨机器累积**
  （2026-09-16 备份时核实：早前这里写的「随库 git 备份」有误，已更正）。
- 写入原子（临时文件 + os.replace），**失败一律静默** —— 计数绝不能让脚本本身挂掉。
- 同一进程内同名只记一次（防 import 重入重复计数）。
- 按「脚本文件名」计数，不看调用路径；只记 `.py`。
- 本模块不给自己埋点：管理脚本 `script_usage.py` 需要手工计数场景，
  自我计数会让「用 touch 登记别人」也变成一次自增，统计会失真。

用法：可复用脚本顶部自动调用 `autotrack(__file__)`；埋点失效时用
`python 脚本/script_usage.py touch <脚本名>` 手工补记。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_COUNTED: set[str] = set()


def usage_path() -> Path:
    """定位计数文件：优先环境变量 `AI_MEMORY_DIR`，否则由本文件位置推断库根。

    本文件位于 `<vault>/脚本/_usage.py`，故上两级即库根。
    """
    env = os.environ.get("AI_MEMORY_DIR")
    root = Path(env) if env else Path(__file__).resolve().parent.parent
    return root / ".workbuddy" / "script_usage.json"


def load() -> dict[str, dict]:
    try:
        with open(usage_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: dict[str, dict]) -> bool:
    path = usage_path()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


def record(name: str, count: int = 1, when: str | None = None) -> bool:
    """给某个脚本名 +count，并刷新「最近使用」。"""
    name = (name or "").strip()
    if not name:
        return False
    data = load()
    row = dict(data.get(name) or {})
    try:
        row["count"] = int(row.get("count", 0)) + int(count)
    except Exception:
        row["count"] = int(count)
    ts = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row["last_used"] = ts
    row.setdefault("first_used", ts)
    data[name] = row
    return save(data)


def autotrack(file_path: str) -> None:
    """脚本入口自动调用：按文件名计数，进程内只记一次，任何异常都不外抛。"""
    try:
        name = os.path.basename(str(file_path))
        if not name.endswith(".py") or name.startswith("_"):
            return
        if name in _COUNTED:
            return
        _COUNTED.add(name)
        record(name)
    except Exception:
        pass
