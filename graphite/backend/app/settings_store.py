"""设置存储：默认结构（§5.9）+ 持久化到 backend/data/settings.json。"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List

from .paths import data_dir as _data_dir
from .paths import vault_root

# 2026-10-23：data/ 目录与默认库根都改由 paths.py 解析（本机解析结果与改造前相同）。
# 2026-10-27（R2）：去掉导入期求值 —— 原来 `SETTINGS_FILE = os.path.join(DATA_DIR, ...)`
# 在 import 那一刻就定死了路径，冻结态会写进解包临时目录。
def _settings_file() -> str:
    return os.path.join(str(_data_dir()), "settings.json")


def _default() -> Dict[str, Any]:
    return {
        "sections": [
            {
                "key": "appearance",
                "title": "工作台外观",
                "items": [
                    {
                        "key": "theme",
                        "label": "界面主题",
                        "type": "select",
                        "value": "Graphite",
                        "options": ["Graphite"],
                        "hint": "当前固定为 Graphite 灰阶主题",
                    },
                    {
                        "key": "glass",
                        "label": "玻璃模糊效果",
                        "type": "switch",
                        "value": True,
                        "hint": "应用于顶部栏与浮层面板",
                    },
                    {
                        "key": "frostBlur",
                        "label": "面板磨砂模糊度",
                        "type": "slider",
                        "value": 20,
                        "min": 0,
                        "max": 40,
                        "step": 1,
                        "hint": "卡片背景模糊半径（px），0 = 关闭模糊；仅本机浏览器生效",
                    },
                    {
                        "key": "frostOpacity",
                        "label": "面板透明度",
                        "type": "slider",
                        "value": 0.7,
                        "min": 0.3,
                        "max": 1,
                        "step": 0.05,
                        "hint": "卡片底色不透明度，越小土星越明显；仅本机浏览器生效",
                    },
                    {
                        "key": "sidebarCollapsed",
                        "label": "侧边栏默认折叠",
                        "type": "switch",
                        "value": False,
                        "hint": "下次打开工作台时自动折叠导航",
                    },
                    {
                        "key": "mono",
                        "label": "等宽字体（代码 / JSON）",
                        "type": "select",
                        "value": "JetBrains Mono",
                        "options": ["JetBrains Mono", "Cascadia Code", "Consolas"],
                    },
                ],
            },
            {
                "key": "vault",
                "title": "记忆库根目录",
                "items": [
                    {
                        "key": "vaultPath",
                        "label": "记忆库根路径",
                        "type": "text",
                        # 默认值跟机器走（服务器上是 /opt/ai-memory/vault）：
                        # 写死 Windows 路径会让「settings.json 还没生成」的首次启动
                        # 直接指向一个不存在的目录，表现为整个工作台回落扫盘。
                        "value": vault_root(),
                        # 2026-10-20 口径：库是唯一原件，md 只是可导出快照。这一项指的是
                        # **库所在的根**，不是「被扫描的 md 目录」——写错会表现为「库明明在、
                        # 工作台却整个回落扫盘」，症状是「刚写的东西在 web 上看不见」。
                        "hint": "库在 <根>/记忆库/数据库/index.sqlite3（也会在 <根>/数据库 下找）；md 仅为可导出快照",
                    }
                ],
            },
            {
                "key": "mcp",
                "title": "MCP 连接",
                "items": [
                    {
                        "key": "mcpEndpoint",
                        "label": "服务地址",
                        "type": "text",
                        "value": "http://127.0.0.1:8787",
                    }
                ],
            },
            {
                "key": "policy",
                "title": "工具调用策略",
                "items": [
                    {
                        "key": "blockWrite",
                        "label": "拦截写类工具",
                        "type": "switch",
                        "value": True,
                        # 真开关（2026-10-20 起）：判据在 tools.is_write_tool，拦的是
                        # MCP 直连的 memory_write / memory_delete / update_* 这类会改数据的工具。
                        "hint": "拦 memory_write / memory_delete / update_* 等会改数据的工具；内置 write_note 是占位工具，无论开关如何都不写库",
                    }
                ],
            },
            {
                "key": "search",
                "title": "检索默认参数",
                "items": [
                    {"key": "topK", "label": "默认 Top K", "type": "number", "value": 5},
                    {
                        "key": "mode",
                        "label": "默认模式",
                        "type": "select",
                        "value": "hybrid",
                        "options": ["keyword", "semantic", "hybrid"],
                    },
                ],
            },
            {
                "key": "privacy",
                "title": "日志与隐私",
                "items": [
                    {"key": "redactPaths", "label": "日志脱敏路径", "type": "switch", "value": False}
                ],
            },
            {
                "key": "cache",
                "title": "缓存管理",
                "items": [
                    {"key": "cacheSize", "label": "缓存占用", "type": "readonly", "value": "0 MB"},
                    {
                        "key": "reindex",
                        "label": "重读索引",
                        "type": "action",
                        "value": None,
                        # 2026-10-20：库模式下它是「丢掉内存索引、从库重新全量读一遍」，
                        # 不扫盘、更不重建库；写「重建索引」会让人以为在重写库文件。
                        "hint": "丢掉内存索引，从 index.sqlite3 重新全量读取（不扫盘、不写库）",
                    },
                    {
                        "key": "restart",
                        "label": "热重启服务",
                        "type": "action",
                        "value": None,
                        "hint": "改过后端 Python 代码后点这里：进程自我重启，数秒恢复（§19.17）",
                    },
                ],
            },
        ]
    }


# 2026-10-23：默认库根改走 paths.py（GRAPHITE_VAULT_ROOT > AI_MEMORY_DIR > 内置默认）。
_VAULT_DEFAULT = vault_root()


def _refresh_dynamic(data: Dict[str, Any]) -> None:
    """把 cacheSize 换成 Vault 目录真实体积（§9.9）。"""
    from . import vault

    item = _find_item(data, "cache", "cacheSize")
    if item is not None:
        item["value"] = f"{vault.dir_size_mb()} MB"


_state: Dict[str, Any] | None = None


def _merge_defaults(data: Dict[str, Any]) -> bool:
    """把缺省结构里缺失的分区 / 项补进已持久化的数据（不覆盖已有值），返回是否有改动。"""
    defaults = _default()
    changed = False
    sections = data.setdefault("sections", [])
    by_key = {s.get("key"): s for s in sections}
    for dsec in defaults["sections"]:
        sec = by_key.get(dsec["key"])
        if sec is None:
            sections.append(copy.deepcopy(dsec))
            changed = True
            continue
        items = sec.setdefault("items", [])
        have = {i.get("key") for i in items}
        for ditem in dsec["items"]:
            if ditem["key"] not in have:
                items.append(copy.deepcopy(ditem))
                changed = True
    return changed


def _ensure_loaded() -> Dict[str, Any]:
    global _state
    if _state is not None:
        return _state
    settings_file = _settings_file()
    if os.path.isfile(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sections"), list):
                _state = data
                if _merge_defaults(_state):
                    _save()
                return _state
        except (json.JSONDecodeError, OSError):
            pass
    _state = _default()
    _save()
    return _state


def _save() -> None:
    settings_file = _settings_file()
    os.makedirs(os.path.dirname(settings_file), exist_ok=True)
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(_state or {}, f, ensure_ascii=False, indent=2)


def get_settings() -> Dict[str, Any]:
    data = copy.deepcopy(_ensure_loaded())
    _refresh_dynamic(data)
    return data


def reload_settings() -> Dict[str, Any]:
    """丢弃内存缓存、从磁盘重读 settings.json（§19.17 配置热重载）。

    为什么需要它：常驻进程把 settings.json 载进 _state 后只认内存，磁盘上
    被别的实例（如临时验证实例的 _merge_defaults）改了它也不知道 ——
    唯一出路原本是杀进程重启。配置层的变更用这个轻量出口就够了，
    不必动真格的进程重启（那是 /api/admin/restart 的活）。"""
    global _state
    _state = None
    return get_settings()


def get_vault_path() -> str:
    data = _ensure_loaded()
    item = _find_item(data, "vault", "vaultPath")
    value = item.get("value") if item else None
    return str(value).strip() if value else _VAULT_DEFAULT


def get_block_write() -> bool:
    """工具调用策略：是否拦截**写类工具**（默认拦）。

    2026-10-20 之前这个开关是装饰品 —— `tools.invoke_tool` 里对 `write_note`
    硬编码 return，压根没读设置值，改开关对行为零影响。
    现在它是真开关，作用对象是「MCP 直连调用里的写类工具」（`memory_write`、
    `memory_delete`、`update_*`、`create_*` 等，判据在 `tools.is_write_tool`）。
    读不到配置时按「拦」处理：写操作是危险动作，默认收紧比默认放开安全。
    """
    try:
        data = _ensure_loaded()
        item = _find_item(data, "policy", "blockWrite")
        return bool(item.get("value")) if item else True
    except Exception:  # noqa: BLE001 —— 配置层异常不该让调用链崩，按最保守处理
        return True


def get_search_defaults() -> Dict[str, Any]:
    data = _ensure_loaded()
    topk = _find_item(data, "search", "topK")
    mode = _find_item(data, "search", "mode")
    try:
        tk = int(topk.get("value")) if topk else 5
    except (TypeError, ValueError):
        tk = 5
    md = str(mode.get("value")) if mode and mode.get("value") else "hybrid"
    if md not in ("keyword", "semantic", "hybrid"):
        md = "hybrid"
    return {"topK": max(1, min(20, tk)), "mode": md}


def _find_item(data: Dict[str, Any], section_key: str, item_key: str) -> Dict[str, Any] | None:
    for sec in data.get("sections", []):
        if sec.get("key") != section_key:
            continue
        for item in sec.get("items", []):
            if item.get("key") == item_key:
                return item
    return None


def update_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """支持两种入参：
    1) 扁平补丁 {"section.item": value, ...}
    2) 整棵 {"sections": [...]}（直接替换）
    vault.vaultPath 变更后立即让索引失效并重扫（§9.9）。
    """
    global _state
    from . import vault

    data = _ensure_loaded()
    vault_changed = False

    if "sections" in patch and isinstance(patch["sections"], list):
        data = {"sections": copy.deepcopy(patch["sections"])}
        _state = data
        _save()
        vault.set_root(get_vault_path())
        _refresh_dynamic(data)
        return copy.deepcopy(data)

    for dotted, value in patch.items():
        if "." not in dotted:
            continue
        section_key, item_key = dotted.split(".", 1)
        item = _find_item(data, section_key, item_key)
        if item is not None and item.get("type") != "readonly":
            item["value"] = value
            if section_key == "vault" and item_key == "vaultPath":
                vault_changed = True

    _state = data
    _save()
    if vault_changed:
        vault.set_root(get_vault_path())
    _refresh_dynamic(data)
    return copy.deepcopy(data)
