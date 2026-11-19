# -*- coding: utf-8 -*-
"""安装器 · 本机 Agent 扫描与 MCP 配置写入（阶段 26）。

## 这个模块解决的问题

用户装完 ai-memory 之后，最烦的一步是「去每个 AI 客户端里手动改一段 JSON」。
每家的配置文件名字、字段、层级都不一样，改错一个逗号整个客户端就不认了。
所以安装器要做两件事：

  1. **扫描**：看看这台机器上到底装了哪些 AI 客户端（有配置文件才算装了）；
  2. **一键写**：把 `ai-memory` 这一条 MCP 配置**合并**进各自的配置文件。

## 三条铁律（每条都对应一次真实翻车）

- **只合并不覆盖**：先读原文件 → 只动 `ai-memory` 这一个键 → 其余原样写回。
  整份覆盖会把用户自己配的其它 MCP 服务抹掉，那是不可原谅的错。
- **改前先备份**：`<文件>.bak-<时间戳>`，用户随时能回退。
- **可撤销**：`remove()` 只删自己写的那一条，不碰别的。

## 为什么配置格式要分两种

- 绝大多数客户端吃 JSON：`{"mcpServers": {"名字": {...}}}`
- Codex CLI 吃 TOML：`[mcp_servers.名字]` 段
- DSH 的 MCP 配置在 profile 里，格式与上面都不同 → 标成 `manual`，安装器只提示、
  不猜着写（写错了客户端会静默不加载，用户根本不知道发生了什么）。

## 本地版 vs 中心化版

- 本地版：MCP 地址就是本机 `http://127.0.0.1:8770/mcp`，**不需要令牌**；
- 中心化版：地址是服务器地址，**必须带令牌**（写进 header 或环境变量）。
本模块不判断模式，只按调用方给的 `url` / `token` 写，判断在安装器主流程里做。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path.home()
SERVER_NAME = "ai-memory"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config"))


@dataclass
class Agent:
    """一个可被自动配置的 AI 客户端。"""

    key: str
    name: str
    # 候选配置路径：第一个存在的就用它（不同平台/版本路径不同）
    paths: List[Path]
    fmt: str = "json"           # json | toml | manual
    root_key: str = "mcpServers"
    # 远程 MCP 条目的写法各家不同：
    #   Claude/Cursor/WorkBuddy → {"type": "url", "url": ...}
    #   opencode                → {"type": "remote", "url": ...}
    entry_type: str = "url"
    note: str = ""
    # 扫描后才填
    found: bool = False
    config_path: Optional[Path] = None
    writable: bool = False
    configured: bool = False


def targets() -> List[Agent]:
    """返回全部候选客户端（按「用户常见的概率」排序，界面按这个顺序显示）。"""
    return [
        Agent("workbuddy", "WorkBuddy", [HOME / ".workbuddy" / "mcp.json"]),
        Agent("claude-code", "Claude Code", [HOME / ".claude.json"]),
        Agent("claude-desktop", "Claude Desktop",
              [_appdata() / "Claude" / "claude_desktop_config.json",
               _xdg_config() / "claude" / "claude_desktop_config.json",
               HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]),
        Agent("codex", "Codex CLI", [HOME / ".codex" / "config.toml"], fmt="toml"),
        Agent("cursor", "Cursor", [HOME / ".cursor" / "mcp.json"]),
        Agent("windsurf", "Windsurf", [HOME / ".codeium" / "windsurf" / "mcp_config.json"]),
        Agent("gemini", "Gemini CLI", [HOME / ".gemini" / "settings.json"]),
        Agent("qwen", "Qwen Code", [HOME / ".qwen" / "settings.json"]),
        Agent("cline", "Cline（VS Code 插件）",
              [_appdata() / "Code" / "User" / "globalStorage"
               / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
               _xdg_config() / "Code" / "User" / "globalStorage"
               / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"]),
        # opencode（2026-11-19 加）：配置是 JSONC，写在 ~/.config/opencode/opencode.jsonc，
        # MCP 条目在 "mcp" 下、类型叫 "remote" 而不是 "url"。它把自己安装在哪不重要 ——
        # 判「装没装」看 PATH 里有没有 opencode（见 scan()）。
        Agent("opencode", "opencode",
              [_xdg_config() / "opencode" / "opencode.jsonc",
               _xdg_config() / "opencode" / "opencode.json"],
              root_key="mcp", entry_type="remote"),
        Agent("dsh", "DSH（DeepSeek Harness）", [HOME / ".dsh"], fmt="manual",
              note="DSH 的 MCP 配置在 profile 里，格式与 JSON 不同；安装器只做提示，不自动写"),
    ]


# ---------------------------------------------------------------- 扫描
def _pick_path(agent: Agent) -> Optional[Path]:
    for p in agent.paths:
        if p.exists():
            return p
    return None


def _json_configured(path: Path, root_key: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return False
    return SERVER_NAME in (data.get(root_key) or {})


def _toml_configured(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return False
    return ("[mcp_servers.%s]" % SERVER_NAME) in text or ('"%s"' % SERVER_NAME) in text and "mcp_servers" in text


def scan() -> List[Agent]:
    """扫描本机：找出「装了且我们能写」的客户端。"""
    out: List[Agent] = []
    for a in targets():
        p = _pick_path(a)
        if p is None and a.key == "opencode":
            # opencode 的配置文件只有在「第一次加 MCP」后才存在，所以不能只看文件。
            # 判据换成「PATH 里有没有 opencode」—— 它自己的安装器会把它放进 PATH。
            exe = shutil.which("opencode")
            if exe:
                p = a.paths[0]          # 首次写入就落在 JSONC 上（opencode 自己也用这个）
        if p is None:
            out.append(a)
            continue
        a.found = True
        a.config_path = p
        a.writable = p.is_file() and os.access(p, os.W_OK)
        if a.fmt == "json":
            a.configured = _json_configured(p, a.root_key)
        elif a.fmt == "toml":
            a.configured = _toml_configured(p)
        out.append(a)
    return out


# ---------------------------------------------------------------- 写入
def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + ".bak-" + stamp)
    shutil.copy2(path, bak)
    return bak


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp-installer")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _entry(url: str, token: str = "", transport: str = "url",
           entry_type: str = "url") -> Dict[str, Any]:
    """生成一条 MCP 服务配置。

    `transport="url"` 是 Streamable HTTP（中心化版/本地版的服务端都是这个）；
    `transport="stdio"` 供不支持 http 的老客户端用（此时 url 是 python 启动命令）。
    """
    if transport == "stdio":
        return {"command": "python3", "args": ["-m", "memory_runtime", "--transport", "stdio"]}
    entry: Dict[str, Any] = {"type": entry_type, "url": url}
    if token:
        entry["headers"] = {"Authorization": "Bearer " + token}
    return entry


def apply_json(agent: Agent, url: str, token: str = "") -> Dict[str, Any]:
    assert agent.config_path is not None
    path = agent.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.read_text(encoding="utf-8-sig") if path.is_file() else "{}"
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("配置文件不是合法 JSON，未改动：%s (%s)" % (path, exc))
    bak = _backup(path) if path.is_file() else None
    bucket = data.setdefault(agent.root_key, {})
    existed = SERVER_NAME in bucket
    bucket[SERVER_NAME] = _entry(url, token, entry_type=agent.entry_type)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"agent": agent.key, "path": str(path), "backup": str(bak) if bak else "",
            "action": "updated" if existed else "added"}


def apply_toml(agent: Agent, url: str, token: str = "") -> Dict[str, Any]:
    """Codex CLI：往 TOML 末尾追加一个独立段。

    为什么用「追加段」而不是解析重写：TOML 没有标准库解析器，
    自己写个半吊子解析器去重写整份文件，风险远大于收益。追加一个**独立段**是安全的 ——
    TOML 允许段顺序任意，且我们只在段不存在时追加。
    """
    assert agent.config_path is not None
    path = agent.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if ("[mcp_servers.%s]" % SERVER_NAME) in text:
        return {"agent": agent.key, "path": str(path), "backup": "", "action": "already"}
    bak = _backup(path) if path.is_file() else None
    block = ['', '[mcp_servers.%s]' % SERVER_NAME, 'url = "%s"' % url]
    if token:
        block.append('bearer_token_env_var = "AI_MEMORY_MCP_TOKEN"')
    _atomic_write(path, text.rstrip("\n") + "\n" + "\n".join(block) + "\n")
    return {"agent": agent.key, "path": str(path), "backup": str(bak) if bak else "", "action": "added"}


def apply(agents: List[Agent], url: str, token: str = "") -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for a in agents:
        if not a.found or a.fmt == "manual":
            results.append({"agent": a.key, "skipped": True,
                            "reason": "未安装" if not a.found else "该客户端需手动配置"})
            continue
        try:
            if a.fmt == "toml":
                results.append(apply_toml(a, url, token))
            else:
                results.append(apply_json(a, url, token))
        except Exception as exc:  # noqa: BLE001 —— 单个客户端失败不能拖垮整次安装
            results.append({"agent": a.key, "error": "%s: %s" % (type(exc).__name__, exc)})
    return results


def remove(agents: List[Agent]) -> List[Dict[str, Any]]:
    """撤销：只删自己写的那一条。"""
    out: List[Dict[str, Any]] = []
    for a in agents:
        if not a.found or a.config_path is None or a.fmt == "manual":
            continue
        try:
            if a.fmt == "json":
                data = json.loads(a.config_path.read_text(encoding="utf-8") or "{}")
                bucket = data.get(a.root_key) or {}
                if SERVER_NAME in bucket:
                    _backup(a.config_path)
                    bucket.pop(SERVER_NAME, None)
                    _atomic_write(a.config_path,
                                  json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                    out.append({"agent": a.key, "action": "removed"})
                else:
                    out.append({"agent": a.key, "action": "absent"})
            elif a.fmt == "toml":
                text = a.config_path.read_text(encoding="utf-8")
                head = "[mcp_servers.%s]" % SERVER_NAME
                if head in text:
                    _backup(a.config_path)
                    lines, keep, skipping = text.splitlines(), [], False
                    for ln in lines:
                        if ln.strip() == head:
                            skipping = True
                            continue
                        if skipping and ln.startswith("[") and not ln.startswith("[[") and ln.strip() != head:
                            skipping = False
                        if not skipping:
                            keep.append(ln)
                    _atomic_write(a.config_path, "\n".join(keep).rstrip("\n") + "\n")
                    out.append({"agent": a.key, "action": "removed"})
                else:
                    out.append({"agent": a.key, "action": "absent"})
        except Exception as exc:  # noqa: BLE001
            out.append({"agent": a.key, "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


def as_dict(a: Agent) -> Dict[str, Any]:
    return {
        "key": a.key, "name": a.name, "found": a.found,
        "path": str(a.config_path) if a.config_path else "",
        "writable": a.writable, "configured": a.configured,
        "manual": a.fmt == "manual", "note": a.note,
    }
