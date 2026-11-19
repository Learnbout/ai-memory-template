"""7 个 MCP 工具定义 + 调用执行器（CONTRACT §5.4 / §5.5 / §9.6）。

顺序固定：search_notes / read_note / list_notes / get_memory_context /
search_tags / write_note(高风险) / list_backlinks。

行为（§9.6，读的一律落到真实数据源）：
- 除 write_note 外全部真读：库模式直接读 `index.sqlite3`（库是唯一原件），
  md 快照模式才是扫盘。数据源口径见 vault.py 顶部 docstring。
- **write_note 只是历史遗留的占位工具**：本工作台的 web 端对库是只读的，
  真正的写入口是 ai-memory MCP 的 `memory_write`（MCP Lab 里直连可调）。
  这里保留 `write_note` 是为了让工具目录与旧链接不断链，命中即返回
  "警告 + WRITE_BLOCKED"，**不假装写成功**。
- 非法入参（top_k 越界 / 未知工具 / 缺必填）→ status "失败" + error，code = INVALID_ARGUMENT / UNKNOWN_TOOL。
- elapsedMs 为真实计时。
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime
from typing import Any, Dict, List

from . import runs as runs_store
from . import settings_store
from . import vault

# --------------------------------------------------------------------------- #
# 写类工具判定（2026-10-20：让「工具调用策略」开关真正起作用）
#
# 背景：设置页那个「拦截写操作」开关以前是装饰品 —— 执行器对内置 write_note
# 硬编码 return，从不读设置值，用户改开关对行为零影响。现在按名字判定写类工具，
# 由 `settings_store.get_block_write()` 决定放不放行。
#
# 判据分两份：
#   · 精确集合 —— ai-memory 的写工具名不带 write_/update_ 前缀（memory_batch_tag、
#     memory_rebuild_links 这种），只能逐个列，别指望前缀能捞到；
#   · 前缀 —— 外部 MCP（filesystem / git 之类）习惯用 write_file / create_issue 命名。
# 刻意**不**收 `set_` / `add_` 前缀：`get_settings` 里就含 `set_`，用 in 匹配会误伤读工具。
# --------------------------------------------------------------------------- #
_WRITE_EXACT = {
    "write_note",
    "memory_write",
    "memory_update_metadata",
    "memory_delete",
    "memory_archive",
    "memory_restore",
    "memory_batch_tag",
    "memory_batch_tier",
    "memory_archive_old",
    "memory_rebuild_links",
    "memory_index_draft",
}
_WRITE_PREFIXES = (
    "write_",
    "create_",
    "update_",
    "delete_",
    "remove_",
    "append_",
    "insert_",
    "rename_",
    "move_",
    "edit_",
    "archive_",
    "restore_",
)


def is_write_tool(name: str) -> bool:
    """是不是写类工具（会改动记忆库/外部系统）。读工具一律返回 False。"""
    n = (name or "").strip().lower()
    return n in _WRITE_EXACT or n.startswith(_WRITE_PREFIXES)

# --------------------------------------------------------------------------- #
# 工具定义
# --------------------------------------------------------------------------- #
TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "search_notes",
        "category": "检索类",
        "risk": "normal",
        "summary": "按关键词 / 语义检索知识库笔记",
        "params": [
            {"name": "query", "type": "string", "required": True, "desc": "自然语言查询"},
            {"name": "top_k", "type": "integer", "required": False, "default": 5, "desc": "返回条数"},
            {
                "name": "mode",
                "type": "enum",
                "enum": ["keyword", "semantic", "hybrid"],
                "required": False,
                "desc": "检索模式",
            },
            {"name": "path_scope", "type": "string", "required": False, "desc": "路径范围"},
        ],
        "example": {
            "query": "MCP 工具调用失败时如何重试",
            "top_k": 5,
            "mode": "hybrid",
            "path_scope": "/知识库",
        },
    },
    {
        "name": "read_note",
        "category": "读写类",
        "risk": "normal",
        "summary": "按 id / 路径 / 标题读取笔记正文",
        "params": [
            {"name": "id", "type": "string", "required": False, "desc": "笔记 id，如 n_7f21ac"},
            {"name": "path", "type": "string", "required": False, "desc": "相对路径，如 /项目/xxx.md"},
            {"name": "title", "type": "string", "required": False, "desc": "frontmatter 标题"},
        ],
        "example": {"id": "n_7f21ac"},
    },
    {
        "name": "list_notes",
        "category": "元数据",
        "risk": "normal",
        "summary": "列出笔记（可按类型 / 路径筛选）",
        "params": [
            {
                "name": "type",
                "type": "enum",
                "enum": [
                    "全部类型",
                    "project",
                    "knowledge",
                    "note",
                    "log",
                    "rule",
                    "index",
                    "preference",
                ],
                "required": False,
                "desc": "笔记类型",
            },
            {"name": "path_scope", "type": "string", "required": False, "desc": "路径范围"},
        ],
        "example": {"type": "project"},
    },
    {
        "name": "get_memory_context",
        "category": "检索类",
        "risk": "normal",
        "summary": "拉取与查询相关的记忆上下文区块",
        "params": [
            {"name": "query", "type": "string", "required": True, "desc": "自然语言查询"},
            {"name": "top_k", "type": "integer", "required": False, "default": 3, "desc": "返回区块数"},
        ],
        "example": {"query": "工具权限分级", "top_k": 3},
    },
    {
        "name": "search_tags",
        "category": "元数据",
        "risk": "normal",
        "summary": "按名称检索标签及其出现次数",
        "params": [
            {"name": "query", "type": "string", "required": False, "desc": "标签关键词"},
            {"name": "top_k", "type": "integer", "required": False, "default": 10, "desc": "返回条数"},
        ],
        "example": {"query": "mcp", "top_k": 10},
    },
    {
        "name": "write_note",
        "category": "读写类",
        "risk": "high",
        "summary": "写入 / 更新笔记（占位工具：web 端只读，命中即拦截，不落库）",
        "params": [
            {"name": "path", "type": "string", "required": True, "desc": "目标相对路径"},
            {"name": "content", "type": "string", "required": True, "desc": "markdown 正文"},
        ],
        "example": {"path": "/项目/新建笔记.md", "content": "## 背景\n..."},
    },
    {
        "name": "list_backlinks",
        "category": "元数据",
        "risk": "normal",
        "summary": "列出指向某笔记的反链",
        "params": [
            {"name": "id", "type": "string", "required": True, "desc": "笔记 id"},
        ],
        "example": {"id": "n_7f21ac"},
    },
]

TOOLS_BY_NAME: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOL_DEFS}


def get_tools() -> List[Dict[str, Any]]:
    return TOOL_DEFS


# --------------------------------------------------------------------------- #
# 调用执行
# --------------------------------------------------------------------------- #
def _mock_id() -> str:
    return "req_" + "".join(random.choice("0123456789abcdef") for _ in range(7))


def _ts() -> str:
    now = datetime.now()
    return f"{now.strftime('%H:%M:%S')}.{now.microsecond // 1000:03d}"


class _ValidateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _int_arg(args: Dict[str, Any], name: str, default: int) -> int:
    raw = args.get(name, default)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _ValidateError("INVALID_ARGUMENT", f"{name} 必须是整数")


def _check_top_k(top_k: int) -> None:
    if top_k < 1 or top_k > 20:
        raise _ValidateError("INVALID_ARGUMENT", "top_k 必须在 1~20 之间")


def _run_tool(name: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    if name == "search_notes":
        query = args.get("query", "")
        if not query:
            raise _ValidateError("INVALID_ARGUMENT", "query 为必填参数")
        top_k = _int_arg(args, "top_k", 5)
        _check_top_k(top_k)
        mode = args.get("mode", "hybrid")
        if mode not in ("keyword", "semantic", "hybrid"):
            raise _ValidateError("INVALID_ARGUMENT", "mode 必须是 keyword/semantic/hybrid")
        scope = args.get("path_scope")
        res = vault.search(query, top_k=top_k, mode=mode, path_scope=scope)
        return [
            {"title": r["title"], "score": r["score"], "relPath": r["relPath"], "quote": r["quote"]}
            for r in res
        ]

    if name == "read_note":
        nid = args.get("id", "")
        rec = None
        if nid:
            rec = vault.get_record(nid)
        if rec is None and args.get("path"):
            rec = vault.get_record_by_path(args["path"])
        if rec is None and args.get("title"):
            rec = vault.get_record_by_path(args["title"])
        if rec is None:
            raise _ValidateError("NOT_FOUND", f"未找到笔记: {nid or args.get('path') or args.get('title')}")
        return [
            {
                "id": rec["id"],
                "title": rec["title"],
                "relPath": rec["relPath"],
                "quote": rec["excerpt"],
                "content": rec["content"],
            }
        ]

    if name == "list_notes":
        scope = args.get("path_scope") or args.get("scope")
        summaries = vault.list_notes(
            ntype=args.get("type"),
            folder=args.get("folder"),
            scope=scope,
        )
        return [
            {"title": s["title"], "relPath": s["relPath"], "type": s["type"], "tags": s["tags"]}
            for s in summaries
        ]

    if name == "get_memory_context":
        query = args.get("query", "")
        if not query:
            raise _ValidateError("INVALID_ARGUMENT", "query 为必填参数")
        top_k = _int_arg(args, "top_k", 3)
        _check_top_k(top_k)
        return vault.get_memory_context(query, top_k=top_k)

    if name == "search_tags":
        top_k = _int_arg(args, "top_k", 10)
        _check_top_k(top_k)
        return vault.search_tags(args.get("query", ""), top_k=top_k)

    if name == "list_backlinks":
        nid = args.get("id", "")
        if not nid:
            raise _ValidateError("INVALID_ARGUMENT", "id 为必填参数")
        links = vault.list_backlinks(nid)
        return [{"id": l["id"], "title": l["title"], "relPath": l["relPath"]} for l in links]

    raise _ValidateError("UNKNOWN_TOOL", f"未知工具: {name}")


def _mcp_content_to_items(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """MCP tools/call 的 content → 目录结果条目。text 尽量 parse 成 JSON
    （ai-memory 工具返回 JSON 字符串），parse 不了就原样包成 {"text": ...}。"""
    items: List[Dict[str, Any]] = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            txt = str(c.get("text", ""))
            try:
                parsed = json.loads(txt)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                items.extend(x for x in parsed if isinstance(x, dict))
            elif isinstance(parsed, dict):
                items.append(parsed)
            elif txt:
                items.append({"text": txt})
        elif c.get("type") == "image":
            items.append({"text": "（图片内容，请看 raw 标签页）"})
    return items


def invoke_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    args = args or {}
    t0 = time.perf_counter()
    mock_id = _mock_id()
    log: List[str] = []

    def done(status: str, result: List[Dict[str, Any]], error: Dict[str, str] | None):
        elapsed_ms = int(round((time.perf_counter() - t0) * 1000))
        if elapsed_ms < 1:
            elapsed_ms = 1
        log.append(f"{_ts()} → 完成 {elapsed_ms}ms")
        runs_store.add_run(
            rtype="MCP 调用",
            target=name,
            status=status,
            elapsed_ms=elapsed_ms,
            step_label="1",
            detail={"args": args, "mockId": mock_id, "resultCount": len(result), "error": error},
        )
        return {
            "status": status,
            "elapsedMs": elapsed_ms,
            "mockId": mock_id,
            "toolName": name,
            "result": result,
            "error": error,
            "log": log,
        }

    if name not in TOOLS_BY_NAME:
        # 非内置工具：先查工作台自己的 MCP 直连（mcp_client），连上了就真调；
        # 没连上才是「不可调用」。
        from . import mcp_client as _mcp
        from . import tool_catalog as _catalog

        server = _mcp.find_server_for_tool(name)
        if server:
            # 写类工具先过策略闸（2026-10-20）：开关为真时**不执行**，直接回警告。
            # 放在 call_tool 之前 —— 拦在事后等于已经把库改了再报"已拦截"。
            if is_write_tool(name) and settings_store.get_block_write():
                log.append(f"{_ts()} → 命中策略：blockWrite=true，拦截写类工具 {name}")
                return done(
                    "警告",
                    [],
                    {
                        "code": "WRITE_BLOCKED",
                        "message": (
                            f"写操作已被策略拦截：「{name}」属于写类工具。"
                            "到 设置 → 工具调用策略 关掉「拦截写类工具」才允许执行。"
                        ),
                    },
                )
            log.append(f"{_ts()} → 经 MCP stdio 直连 {server}，tools/call {name}")
            try:
                resp = _mcp.call_tool(server, name, args)
            except Exception as e:  # noqa: BLE001 —— MCP 链路错误如实报失败
                log.append(f"{_ts()} → MCP 调用异常：{e}")
                return done("失败", [], {"code": "MCP_ERROR", "message": str(e)})
            items = _mcp_content_to_items(resp.get("content"))
            log.append(f"{_ts()} → MCP 返回 {len(resp.get('content') or [])} 个 content 块")
            if resp.get("isError"):
                msg = "工具执行报错（isError=true），详见 raw"
                for c in resp.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "text":
                        msg = str(c.get("text", msg))
                        break
                return done("失败", items, {"code": "TOOL_ERROR", "message": msg[:500]})
            log.append(f"{_ts()} → 命中 {len(items)} 条")
            return done("成功", items, None)

        source_label = _catalog.find_non_invokable().get(name)
        if source_label:
            log.append(f"{_ts()} → 工具不可调用：{source_label}")
            return done(
                "失败",
                [],
                {
                    "code": "NOT_INVOKABLE",
                    "message": (
                        f"该工具属于 {source_label}，当前未连接（工作台会在后台自动重连，"
                        "也可稍后重试）"
                    ),
                },
            )
        log.append(f"{_ts()} → 校验工具名 FAIL")
        return done("失败", [], {"code": "UNKNOWN_TOOL", "message": f"未知工具: {name}"})

    # 写操作拦截：内置 write_note 是**占位工具**（没有写入实现），无论开关如何都不落库。
    # 真正会改数据的是 MCP 直连的 memory_write 等，那条路由上面的 blockWrite 闸管。
    if name == "write_note":
        log.append(f"{_ts()} → 校验参数 OK")
        log.append(f"{_ts()} → 命中：内置 write_note 是占位工具（无写入实现），不落库")
        return done(
            "警告",
            [],
            {
                "code": "WRITE_BLOCKED",
                "message": "内置 write_note 只是占位工具，不会写库；写库请调 ai-memory 的 memory_write（MCP Lab 可直连）",
            },
        )

    log.append(f"{_ts()} → 校验参数 OK")
    if name == "search_notes":
        vault.ensure_index()
        mode = args.get("mode", "hybrid")
        scope = args.get("path_scope") or "全部"
        log.append(f"{_ts()} → 真读 Vault 索引（{len(vault._records())} 篇），mode={mode}, path_scope={scope}")
        if mode == "semantic":
            log.append(f"{_ts()} → {vault.SEMANTIC_NOTE}")
    elif name in ("read_note", "list_notes", "get_memory_context", "search_tags", "list_backlinks"):
        vault.ensure_index()

    try:
        result = _run_tool(name, args)
    except _ValidateError as e:
        log.append(f"{_ts()} → 参数校验失败：{e.code}")
        return done("失败", [], {"code": e.code, "message": e.message})

    log.append(f"{_ts()} → 命中 {len(result)} 条")
    return done("成功", result, None)
