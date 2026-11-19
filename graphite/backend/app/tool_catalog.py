"""MCP 工具全量目录（新增契约，优先于 CONTRACT §5.4 的 7 条内置工具）。

三类来源，全部进 `GET /api/tools`：

- workspace：本工作台内置 7 个（`tools.TOOL_DEFS`），`invokable=true`。
- ai-memory：**从真实源码 AST 解析** `memory_tools.py` 的顶层 `def memory_*`，
  拿函数名 / docstring 首行 / 参数；再读 `memory_runtime.py` 的 `_CORE_TOOL_NAMES`
  打「核心 / 隐藏」标签；再读 mcp.json 的 `AI_MEMORY_CORE_TOOLS` 决定是否追加
  「已暴露 / 未暴露」。**2026-10-15 起：工作台作为 MCP 客户端直连该服务
  （`mcp_client.py`），连上后工具 `invokable=true`，可从 MCP Lab 真实调用。**
- 外部 MCP：mcp.json 里除 `ai-memory` 外的服务。连上的枚举真实 `tools/list`
  工具（`invokable=true`）；没连上的只产出「服务占位项」，**不编造工具名**。

降级：源码 / mcp.json 读不到时不抛 500，返回空数组并在 `GET /api/tools/sources`
的来源 `note` 里写明原因；MCP 连不上也只是回退到占位态，不影响目录本身。
"""

from __future__ import annotations

import ast
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from . import mcp_client
from . import tools as tools_mod
from .paths import mcp_json, vault_root

# 2026-10-23：两处写死路径改走 paths.py（详见该文件开头的「为什么单开一个文件」）。
MCP_JSON_PATH = mcp_json()
AI_MEMORY_DIR_DEFAULT = vault_root()

AI_MEMORY_NOTE = "属于 ai-memory MCP，需宿主侧连接该服务后才能调用"
AI_MEMORY_NOTE_CONNECTED = "工作台已直连该服务，全部工具可调用（直连不经宿主 AI_MEMORY_CORE_TOOLS 过滤）"
EXTERNAL_NOTE = "外部 MCP 服务，需在宿主侧连接后才能枚举其工具列表"
EXTERNAL_NOTE_UNSUPPORTED = "旧 HTTP+SSE 传输，请改用 streamable-http（type=url）"

# ai-memory 工具分类兜底映射（工具名必须来自 AST 解析结果，本表只做语义兜底）。
_CATEGORY_MAP: Dict[str, str] = {
    "memory_search": "检索类",
    "memory_read": "读写类",
    "memory_write": "读写类",
    "memory_stats": "元数据",
    "memory_sql": "元数据",
    "memory_context": "检索类",
    "memory_list": "检索类",
    "memory_delete": "读写类",
    "memory_update_metadata": "读写类",
    "memory_audit": "元数据",
    "memory_index_draft": "元数据",
    "memory_archive": "读写类",
    "memory_restore": "读写类",
    "memory_heat_suggest": "元数据",
    "memory_graph": "检索类",
    "memory_orphans": "检索类",
    "memory_batch_tag": "读写类",
    "memory_batch_tier": "读写类",
    "memory_archive_old": "读写类",
    "memory_smart_search": "检索类",
    "memory_recent": "检索类",
    "memory_rebuild_links": "元数据",
}

_MISSING = object()


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_mcp() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """返回 (servers, error)。读不到 / 解析失败 → (None, 原因)。"""
    if not os.path.isfile(MCP_JSON_PATH):
        return None, f"未找到 {MCP_JSON_PATH}"
    try:
        with open(MCP_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return None, f"读取 mcp.json 失败：{e}"
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None, "mcp.json 缺少 mcpServers 字段"
    return servers, None


def _ai_memory_dir(servers: Optional[Dict[str, Any]]) -> str:
    if servers:
        cfg = servers.get("ai-memory") or {}
        env = cfg.get("env") if isinstance(cfg, dict) else None
        if isinstance(env, dict):
            d = env.get("AI_MEMORY_DIR")
            if d:
                return str(d)
    return AI_MEMORY_DIR_DEFAULT


def _ai_memory_core_only(servers: Optional[Dict[str, Any]]) -> bool:
    if not servers:
        return False
    cfg = servers.get("ai-memory") or {}
    env = cfg.get("env") if isinstance(cfg, dict) else None
    if isinstance(env, dict):
        return _truthy(env.get("AI_MEMORY_CORE_TOOLS"))
    return False


# --------------------------------------------------------------------------- #
# AST 解析
# --------------------------------------------------------------------------- #
def _annotation_type(node: Optional[ast.AST]) -> str:
    """从注解文本取类型，取不到给 "string"（做最小归一化到 JSON 风格）。"""
    if node is None:
        return "string"
    try:
        text = ast.unparse(node).lower()
    except Exception:  # noqa: BLE001 - 注解异常一律降级
        return "string"
    if "bool" in text:
        return "boolean"
    if "int" in text:
        return "integer"
    if "float" in text:
        return "number"
    if "str" in text:
        return "string"
    if any(k in text for k in ("list", "sequence", "tuple", "set")):
        return "array"
    if any(k in text for k in ("dict", "mapping")):
        return "object"
    return "string"


def _literal(node: Optional[ast.AST]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    return _MISSING


def _params_from_func(node: ast.FunctionDef) -> List[Dict[str, Any]]:
    params: List[Dict[str, Any]] = []
    a = node.args
    positional = a.posonlyargs + a.args
    defaults = a.defaults
    offset = len(positional) - len(defaults)
    for i, arg in enumerate(positional):
        if arg.arg in ("self", "cls"):
            continue
        required = i < offset
        p: Dict[str, Any] = {
            "name": arg.arg,
            "type": _annotation_type(arg.annotation),
            "required": required,
            "desc": "",
        }
        if not required:
            lit = _literal(defaults[i - offset])
            if lit is not _MISSING:
                p["default"] = lit
        params.append(p)
    for j, arg in enumerate(a.kwonlyargs):
        d = a.kw_defaults[j]
        required = d is None
        p = {
            "name": arg.arg,
            "type": _annotation_type(arg.annotation),
            "required": required,
            "desc": "",
        }
        if d is not None:
            lit = _literal(d)
            if lit is not _MISSING:
                p["default"] = lit
        params.append(p)
    return params


def _first_doc_line(node: ast.FunctionDef) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def parse_ai_memory_tools(tools_path: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """AST 解析顶层 `def memory_*`：函数名 / docstring 首行 / 参数。"""
    if not os.path.isfile(tools_path):
        return [], f"未找到 ai-memory 源码：{tools_path}"
    try:
        with open(tools_path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError) as e:
        return [], f"解析 ai-memory 源码失败：{e}"

    parsed: List[Dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("memory_"):
            parsed.append(
                {
                    "name": node.name,
                    "summary": _first_doc_line(node),
                    "params": _params_from_func(node),
                }
            )
    return parsed, None


def read_core_tool_names(runtime_path: str) -> Tuple[Set[str], Optional[str]]:
    """从 memory_runtime.py 读 `_CORE_TOOL_NAMES` 集合（AST，不执行源码）。"""
    if not os.path.isfile(runtime_path):
        return set(), f"未找到 ai-memory 运行时源码：{runtime_path}"
    try:
        with open(runtime_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as e:
        return set(), f"解析 _CORE_TOOL_NAMES 失败：{e}"

    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_CORE_TOOL_NAMES" in names and isinstance(node.value, ast.Set):
                out: Set[str] = set()
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        out.add(elt.value)
                return out, None
    return set(), "memory_runtime.py 未找到 _CORE_TOOL_NAMES 集合"


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def classify(name: str) -> str:
    if name in _CATEGORY_MAP:
        return _CATEGORY_MAP[name]
    short = name.replace("memory_", "")
    if any(k in short for k in ("write", "update", "delete", "archive", "restore", "batch")):
        return "读写类"
    if any(k in short for k in ("stats", "sql", "audit", "index", "heat_suggest")):
        return "元数据"
    if any(k in short for k in ("search", "list", "graph", "orphans", "recent")):
        return "检索类"
    return "元数据"


# --------------------------------------------------------------------------- #
# 目录构建
# --------------------------------------------------------------------------- #
def _enrich_builtin(t: Dict[str, Any]) -> Dict[str, Any]:
    e = dict(t)
    e["source"] = "workspace"
    e["sourceLabel"] = "本工作台"
    e["badges"] = ["内置"]
    e["invokable"] = True
    e["note"] = None
    e["externalServer"] = False
    return e


def _build_ai_memory(
    servers: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    """返回 (工具列表, 错误, 统计 {toolCount, exposedCount, coreOnly, dir})。"""
    base_dir = _ai_memory_dir(servers)
    tools_path = os.path.join(base_dir, "memory_tools.py")
    runtime_path = os.path.join(base_dir, "memory_runtime.py")
    core_only = _ai_memory_core_only(servers)

    parsed, tools_err = parse_ai_memory_tools(tools_path)
    core_names, core_err = read_core_tool_names(runtime_path)
    err = tools_err or core_err

    # 工作台直连状态：connected 时按服务真实 tools/list 放行 invokable
    state = mcp_client.get_state("ai-memory")
    connected_tools: Set[str] = set()
    if state["status"] == "connected":
        connected_tools = {t.get("name", "") for t in state["tools"]}

    out: List[Dict[str, Any]] = []
    for item in parsed:
        name = item["name"]
        is_core = name in core_names
        badges = ["核心"] if is_core else ["隐藏"]
        if connected_tools:
            if name in connected_tools:
                badges.append("已连接")
                note = AI_MEMORY_NOTE_CONNECTED
                invokable = True
            else:
                # 服务在线但没列这个工具名（目录与实现脱节时不硬放行）
                badges.append("服务未列出")
                note = AI_MEMORY_NOTE + "；服务在线但 tools/list 未包含该工具名"
                invokable = False
        else:
            invokable = False
            if core_only:
                badges.append("已暴露" if is_core else "未暴露")
                if is_core:
                    note = AI_MEMORY_NOTE + "；当前宿主设置 AI_MEMORY_CORE_TOOLS 为真值，该核心工具已挂载到宿主"
                else:
                    note = AI_MEMORY_NOTE + "；当前宿主设置 AI_MEMORY_CORE_TOOLS 为真值，仅暴露核心工具，该工具未挂载到宿主"
            elif is_core:
                note = AI_MEMORY_NOTE + "；该工具属于核心工具集"
            else:
                note = AI_MEMORY_NOTE
            if state["status"] == "error":
                note += f"；工作台直连失败：{state['error']}"
        out.append(
            {
                "name": name,
                "category": classify(name),
                "risk": "normal",
                "summary": item["summary"],
                "params": item["params"],
                "example": {},
                "source": "ai-memory",
                "sourceLabel": "ai-memory MCP",
                "badges": badges,
                "invokable": invokable,
                "note": note,
                "externalServer": False,
            }
        )

    stats = {
        "toolCount": len(out),
        "exposedCount": (len(core_names) if core_only else len(out)) if not err else 0,
        "coreOnly": core_only,
        "dir": base_dir,
        "connected": bool(connected_tools),
        "invokableCount": sum(1 for t in out if t["invokable"]),
    }
    return out, err, stats


def _params_from_json_schema(schema: Any) -> List[Dict[str, Any]]:
    """MCP inputSchema（JSON Schema）→ 目录 params（只取一层的 name/type/required/desc）。"""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    required = set(schema.get("required") or [])
    out: List[Dict[str, Any]] = []
    for pname, p in (props or {}).items():
        p = p if isinstance(p, dict) else {}
        out.append(
            {
                "name": str(pname),
                "type": str(p.get("type", "string")),
                "required": pname in required,
                "desc": str(p.get("description", "")),
            }
        )
    return out


def _build_external(servers: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not servers:
        return []
    taken = {t["name"] for t in tools_mod.get_tools()}
    out: List[Dict[str, Any]] = []
    for name, cfg in servers.items():
        if name == "ai-memory":
            continue
        cfg = cfg if isinstance(cfg, dict) else {}
        state = mcp_client.get_state(name)
        if state["status"] == "connected" and state["tools"]:
            # 连上了：用服务真实 tools/list 换掉占位项（不编造，以服务返回为准）
            for t in state["tools"]:
                tname = str(t.get("name", ""))
                if not tname or tname in taken:
                    continue  # 与内置/他服务重名的工具跳过，不冒领
                taken.add(tname)
                out.append(
                    {
                        "name": tname,
                        "category": "外部 MCP",
                        "risk": "normal",
                        "summary": str(t.get("description") or f"{name} / {tname}"),
                        "params": _params_from_json_schema(t.get("inputSchema")),
                        "example": {},
                        "source": name,
                        "sourceLabel": name,
                        "badges": ["外部 MCP", "已连接"],
                        "invokable": True,
                        "note": None,
                        "externalServer": True,
                    }
                )
            continue
        note = EXTERNAL_NOTE
        badges = ["外部 MCP", "未连接"]
        if state["status"] == "unsupported":
            note = state["error"] or EXTERNAL_NOTE_UNSUPPORTED
        elif state["status"] == "error":
            note = f"工作台直连失败：{state['error']}"
        out.append(
            {
                "name": name,
                "category": "外部 MCP",
                "risk": "normal",
                "summary": cfg.get("description") or f"{name} 外部 MCP 服务",
                "params": [],
                "example": {},
                "source": name,
                "sourceLabel": name,
                "badges": badges,
                "invokable": False,
                "note": note,
                "externalServer": True,
            }
        )
    return out


def get_full_catalog() -> List[Dict[str, Any]]:
    """workspace(7) + ai-memory(AST) + 外部服务（连上 = 真实工具，没连上 = 占位）。"""
    mcp_client.ensure_background_connect()
    catalog: List[Dict[str, Any]] = [ _enrich_builtin(t) for t in tools_mod.get_tools() ]
    servers, _err = _read_mcp()
    ai_tools, _ai_err, _stats = _build_ai_memory(servers)
    catalog.extend(ai_tools)
    catalog.extend(_build_external(servers))
    return catalog


def find_non_invokable() -> Dict[str, str]:
    """name → sourceLabel（非 invokable 的工具），供 invoke 保护查询。"""
    out: Dict[str, str] = {}
    for t in get_full_catalog():
        if not t.get("invokable", True):
            out[t["name"]] = t.get("sourceLabel") or t.get("source") or "外部服务"
    return out


# --------------------------------------------------------------------------- #
# 来源汇总
# --------------------------------------------------------------------------- #
def get_sources() -> List[Dict[str, Any]]:
    mcp_client.ensure_background_connect()
    workspace = {
        "key": "workspace",
        "label": "本工作台",
        "kind": "builtin",
        "connected": True,
        "toolCount": len(tools_mod.get_tools()),
        "invokableCount": len(tools_mod.get_tools()),
        "exposedCount": None,
        "description": "Graphite Workspace 内置的 Vault 工具",
        "note": None,
    }

    servers, err = _read_mcp()
    if servers is None:
        # 读不到 mcp.json：只返回 workspace 一条，不报错。
        return [workspace]

    _ai_tools, ai_err, ai_stats = _build_ai_memory(servers)
    ai_cfg = servers.get("ai-memory") or {}
    ai_cfg = ai_cfg if isinstance(ai_cfg, dict) else {}
    ai_state = mcp_client.get_state("ai-memory")
    ai_connected = ai_state["status"] == "connected"
    ai_note = ai_err
    if ai_err:
        ai_note = f"ai-memory 工具未解析：{ai_err}"
    elif ai_connected:
        ai_note = f"工作台已直连（服务端 tools/list 枚举 {len(ai_state['tools'])} 个工具）"
    elif ai_state["status"] == "error":
        ai_note = f"工作台直连失败：{ai_state['error']}"
    ai_source = {
        "key": "ai-memory",
        "label": "ai-memory MCP",
        "kind": ai_cfg.get("type", "stdio"),
        "connected": ai_connected,
        "toolCount": ai_stats["toolCount"],
        "invokableCount": ai_stats.get("invokableCount", 0),
        "exposedCount": ai_stats["exposedCount"],
        "description": ai_cfg.get("description", ""),
        "note": ai_note,
    }

    out = [workspace, ai_source]
    for name, cfg in servers.items():
        if name == "ai-memory":
            continue
        cfg = cfg if isinstance(cfg, dict) else {}
        state = mcp_client.get_state(name)
        connected = state["status"] == "connected"
        out.append(
            {
                "key": name,
                "label": name,
                "kind": cfg.get("type", "stdio"),
                "connected": connected,
                "toolCount": len(state["tools"]) if connected else 0,
                "invokableCount": len(state["tools"]) if connected else 0,
                "exposedCount": None,
                "description": cfg.get("description", ""),
                "note": (state["error"] if state["status"] == "error" else None),
            }
        )
    return out
