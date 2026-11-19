# -*- coding: utf-8 -*-
"""首启建管理员账号（阶段 26 · 安装器联动，2026-11-19）。

## 为什么需要这个文件

安装器在装服务器版时会让用户当场填**管理员账号 + 密码** —— 这是用户在场、有权限、
有动机的那一刻。但那一刻库还没建、服务还没起，没法直接建账号。

于是安装器把凭据写进 `backend/data/pending_admin.json`，**服务第一次启动时**由这里消费：
建好账号 → 删掉文件 → 写一行日志。这样用户装完就有账号能用，不必去翻文档找
「该在哪里建管理员」（那是原始反馈里的原话：**我都没安装，哪来的网页？**）。

## 三条硬约束

1. **只在 center 形态、且只跑一次**：本地版没有账号体系，读到文件直接删掉（避免留明文）。
2. **明文不能久留**：文件权限 600，消费成功立即删除；建号失败也删除并写日志，
   免得像定时炸弹一样每次启动都报错。
3. **失败不能阻断启动**：账号没建成只是少个入口，服务起不来是全盘皆输。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional


def pending_path() -> Path:
    from .paths import data_dir

    return data_dir() / "pending_admin.json"


def write_pending(username: str, password: str, *, note: str = "安装器创建") -> Path:
    """安装器侧调用：把待建管理员写进数据目录（权限 600）。"""
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"username": username, "password": password, "note": note},
                            ensure_ascii=False), encoding="utf-8")
    # Windows 上 chmod 基本无效（NTFS 只认只读位），真正管得住的是 ACL。
    # 这只是加固，失败不报错 —— 别因为设权限把安装搞挂。
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import subprocess

            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(p), "/inheritance:r"], capture_output=True, timeout=20)
                subprocess.run(["icacls", str(p), "/grant:r", "%s:F" % user],
                               capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    return p


def _consume() -> Optional[Dict[str, Any]]:
    p = pending_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    try:
        p.unlink()                                    # 无论成败都删，明文不留在盘上
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def bootstrap_once() -> Optional[str]:
    """服务启动时调用；返回一行人类可读的结果（写进日志），没事可做返回 None。"""
    data = _consume()
    if not data:
        return None
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        return "待建管理员记录不完整，已丢弃"

    from .paths import is_local

    if is_local():
        # 本地版不建账号：文件已删，说明一句就够
        return "本地版不需要账号，已丢弃安装器写入的管理员凭据"

    try:
        from .paths import ensure_import_path

        ensure_import_path()
        import web_auth  # noqa: PLC0415 —— 库根脚本，必须在 sys.path 就绪后再 import

        web_auth.set_user(username, password, note=str(data.get("note") or "安装器创建"))
        # 若 admin_api 可用，一并写进 users 表（多用户判定与审计都看它）
        try:
            import admin_api

            if hasattr(admin_api, "ensure_admin"):
                admin_api.ensure_admin(username)
        except Exception:  # noqa: BLE001 —— 没有这个函数也不影响登录
            pass
        return "已按安装器提供的信息创建管理员账号：%s" % username
    except Exception as exc:  # noqa: BLE001 —— 建号失败不阻断启动
        return "创建管理员账号失败（%s: %s）；可稍后在管理页手动创建" % (type(exc).__name__, exc)
