"""MCP tool definitions for ai-memory."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
import os
import time
import logging
import sys
import threading
import functools

from pydantic import Field

from memory_runtime import _tool, mcp
from memory_store import *
from mcp_compat import ToolAnnotations  # G10 后续：SDK 依赖收口，不直连 mcp.types

# G2（2026-10-09）：tool annotations。MCP 协议允许工具声明行为提示，宿主可据此
# 优化（readOnlyHint=True 的工具可安全并行/缓存，destructiveHint=True 的会弹确认）。
# 这里只声明有把握的四条只读 + 一条写 + 一条删；提示错了比没有更糟，不确定的
# （如 memory_archive）宁可不标。
_ANN_RO = ToolAnnotations(readOnlyHint=True)
_ANN_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_ANN_DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

# ── 同主题拦截（2026-09-10）────────────────────────────────────────────────
# 替代被否决的 supersedes/conflicts 字段方案：字段的问题是「没人会填」——
# AI 写入时根本不知道自己正在覆盖谁。把校验放在唯一必然发生的时刻（写入入口），
# 用现成的标题集合做纯内存比对，零字段成本。
_TOPIC_PREFIXES = ("项目_", "知识_", "规则_", "工具_", "修复_", "运维_", "模板_", "日志_", "工作动态_")
_TOPIC_DATE_RE = re.compile(r"[（(]\s*\d{4}-\d{2}-\d{2}\s*[)）]|\d{4}-\d{2}-\d{2}")
_TOPIC_MIN_COMMON = 4  # 主题核心的最短公共前缀（字）
# 库名出现在绝大多数标题里，不携带任何区分度——不剥掉会大面积误报。
# 2026-09-10 实测：写入「ai-memory优化清单十六项与验收标准」被误判为与
# 「ai-memory改进建议评审与采纳清单」「ai-memory-template更新」「ai-memory_GitHub_私人备份」
# 同主题，仅因共享 "ai-memory" 前缀。剥掉后仍保留真实主题比对能力。
_TOPIC_STOP_PREFIXES = ("ai-memory", "ai_memory", "aimemory")


def _topic_core(title: str) -> str:
    """剥离领域前缀、库名与日期后缀，得到「主题核心」，用于同主题比对。"""
    t = (title or "").strip()
    for prefix in _TOPIC_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    t = _TOPIC_DATE_RE.sub("", t)
    for stop in _TOPIC_STOP_PREFIXES:
        if t.lower().startswith(stop):
            t = t[len(stop):]
            break
    return re.sub(r"[（()）\s_\-—·]+", "", t)


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _find_similar_titles(title: str, limit: int = 6) -> list[str]:
    """在现有条目里找同主题候选（公共前缀 ≥ _TOPIC_MIN_COMMON 字）。

    只对含中文的标题生效：纯 ASCII 标题（冒烟测试的 smoke_N、临时探针
    _bench_probe 之类）共用英文词根属于正常，不该被拦。
    """
    core = _topic_core(title)
    if len(core) < _TOPIC_MIN_COMMON or not re.search(r"[\u4e00-\u9fff]", core):
        return []
    hits: list[tuple[int, str]] = []
    for path, meta, _body in _iter_entries():
        other_title = _entry_title(meta, path)
        if other_title == title:
            continue
        other = _topic_core(other_title)
        if not other:
            continue
        n = _common_prefix_len(core, other)
        if n >= _TOPIC_MIN_COMMON or core == other:
            hits.append((n, other_title))
    hits.sort(key=lambda item: (-item[0], item[1]))
    return [t for _n, t in hits[:limit]]


# ── verified 与待核实结论（2026-09-10，第 4 项）────────────────────────────
# 库里凡带这些措辞又未标 verified 的结论，跨会话最容易被后来的 AI 当既成事实读走。
_UNVERIFIED_HINTS = ("待重启", "待验证", "待确认", "尚未验证", "未经", "预期", "预计")
# 索引/时间线/状态页天然充满这类措辞（它们记录的是过程），不参与待核实统计。
_VERIFIED_SKIP_TITLES = {"记忆索引", "近期工作动态", "项目状态", "路由索引", "脚本索引"}


# ── 检索日志（2026-09-10，第 12 项）────────────────────────────────────────
# 目的：让「要不要上 FTS5/别名/向量」由数据决定，而不是凭感觉。
# 落盘位置选 .workbuddy/ 而非 日志/：前者在 SYSTEM_DIR_NAMES 里，不参与
# _tree_stamp 缓存判据，否则每次搜索都会打穿全库缓存；且 .jsonl 不是 .md，
# 不会进笔记索引、不产生 token 成本。
SEARCH_LOG_NAME = "检索日志.jsonl"

# 基准样本标记（E2，2026-09-14）：脚本/bench_memory.py 会调用 memory_search 采样，
# 不打标就会把合成查询混进「零结果率」这个唯一判据里（2026-09-11 实测：87 条样本
# 中 73 条是合成查询）。bench 脚本在采样前置 True，日志写 bench=true，统计时排除。
LOG_AS_BENCH = False


def _search_log_path() -> Path:
    return MEMORY_DIR / ".workbuddy" / SEARCH_LOG_NAME


def _log_search(query: str, hits: int, top1: str, tag: str | None = None,
                tool: str = "search") -> None:
    try:
        path = _search_log_path()
        # 读路径不得建库（E5a，2026-09-14）：此前这里 mkdir(parents=True) 会在
        # MEMORY_DIR 不存在时把整个库目录建出来——正是「检索即建空库」那一半 footgun。
        if not path.parent.parent.is_dir():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "tag": tag,
            "hits": hits,
            "top1": top1,
            # E2（2026-09-14）：记录是哪条检索路径产生的（search / smart /
            # search→smart 兜底），并标出基准合成样本，否则零结果率口径不全。
            "tool": tool,
            "bench": bool(LOG_AS_BENCH),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # 日志失败绝不能影响检索本身
        logger.debug("_log_search failed: %s", e)


def _maybe_auto_archive(title: str, path: Path) -> None:
    """Auto-archive entries that are cold enough."""
    if title in CORE_PAGES:
        return
    try:
        meta, body = _load_memory(path)
        if meta.get("tier") != "cold":
            return
        # Only archive if tier is already cold
        memory_archive(title)
    except Exception as e:
        logger.warning("_maybe_auto_archive(%s): %s", title, e)

# ---------------------------------------------------------------------------
# 聚合页覆盖护栏（2026-09-15 事故后新增，P0）
#
# 背景：memory_write 是 upsert（整篇替换），没有「追加」语义。2026-09-15 11:10，
# 某会话把「项目状态」的 content 写成了 463 字符的叙述文字（实际是它自己的
# summary 值），整张 36 行项目表被静默抹掉；同一分钟「近期工作动态」也被写成
# 1146 字符短版，丢掉 09-05~09-14 共 59 条。文件顶部写的「历史按月归档」只是
# 描述、没有代码执行，所以内容既不在近期文件也没进归档。
# 详见记忆库：项目_记忆库聚合页覆盖事故与恢复（2026-09-15）
#
# 判据用「结构指纹」而不是字数：聚合页允许正常的条目精简与滚动瘦身，
# 但日期区块 / 表格行 / 条目 / 双链 这四项任一变小，都说明内容被削。
# ---------------------------------------------------------------------------
_GUARDED_TITLES = {
    "近期工作动态", "项目状态", "记忆索引", "路由索引", "脚本索引",
    "用户画像", "AI人格", "AI 人格",
}


def _structure_metrics(body: str) -> dict[str, int]:
    """聚合页结构指纹：四项计数任一变小即视为缩水。"""
    return {
        "日期区块": len(re.findall(r"(?m)^##\s*\d{4}-\d{2}-\d{2}", body)),
        "表格行": len(re.findall(r"(?m)^\|\s*\[\[", body)),
        "条目": len(re.findall(r"(?m)^-\s*\[", body)),
        "双链": len(re.findall(r"\[\[[^\]]+\]\]", body)),
    }


# ---------------------------------------------------------------------------
# 表格结构护栏（2026-09-17 事故后新增，P0）
#
# 背景：09-15 修的是「引用块后缺空行」（lazy continuation 吞表），09-17 又以
# 另一种姿势复现 —— 数据行被插到「表头」与「分隔线」之间，CommonMark 判定
# 表头 + 分隔线结构不成立，整张 44 行项目表退化为纯文本。
#
# 教训：只防「内容缩水」不够，还要防「结构被写坏」。两者是不同的破坏方式，
# 前者丢内容，后者丢渲染。本条护栏补的是后者。
# ---------------------------------------------------------------------------
_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def _is_table_row(ln: str) -> bool:
    return bool(_ROW_RE.match(ln))


def _is_table_sep(ln: str) -> bool:
    return bool(_SEP_RE.match(ln)) and "-" in ln


def _table_structure_issues(body: str) -> list[str]:
    """检查表格结构是否合法：表头下一行必须是分隔线。

    返回问题描述列表（空列表 = 结构合法）。
    """
    issues: list[str] = []
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        if not _is_table_row(ln) or i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        if not _is_table_row(nxt) or _is_table_sep(nxt):
            continue
        # 表头后紧跟的是数据行：再看第三行是不是分隔线 → 典型错位
        if i + 2 < len(lines) and _is_table_sep(lines[i + 2]):
            issues.append(
                f"第 {i+1} 行表头与第 {i+3} 行分隔线之间夹进了数据行"
                f"（第 {i+2} 行），表格会整体退化为纯文本"
            )
    # 引用块后紧贴非空行（09-15 形态）
    prev_quote = False
    for i, ln in enumerate(lines):
        is_quote = ln.lstrip().startswith(">")
        if prev_quote and not is_quote and ln.strip():
            issues.append(f"第 {i+1} 行紧贴引用块、缺空行（lazy continuation 风险）")
        prev_quote = is_quote
    return issues


def _normalize_md_spacing(content: str) -> str:
    """写入前的 Markdown 卫生处理：引用块结束后补空行（2026-09-15 修）。

    CommonMark 的 lazy continuation 会把紧跟在 `>` 引用行之后、没有空行分隔的
    表格/列表行一并吞进引用块 —— 整张表格于是被渲染成一行行纯文本 `| a | b |`。
    本函数只在「引用块结束 + 下一行非空」处插入一个空行，其余原样透传。
    """
    if not content:
        return content
    out: list[str] = []
    prev_quote = False
    for ln in content.split("\n"):
        is_quote = ln.lstrip().startswith(">")
        if prev_quote and not is_quote and ln.strip():
            out.append("")
        out.append(ln)
        prev_quote = is_quote
    return "\n".join(out)


def _guard_aggregate_overwrite(title: str, new_content: str, existing_body: str) -> str | None:
    """聚合页禁止用 memory_write 做缩水式整篇覆盖。放行返回 None。"""
    if (title or "").strip() not in _GUARDED_TITLES:
        return None
    if not existing_body or not existing_body.strip():
        return None  # 首次创建 / 原有内容为空，放行
    old = _structure_metrics(existing_body)
    new = _structure_metrics(new_content)
    lost = {k: (old[k], new[k]) for k in old if new[k] < old[k]}

    # 表格结构校验（2026-09-17 新增）：写坏结构同样是破坏，且比丢内容更隐蔽 ——
    # 内容一行不少，但整张表渲染不出来。旧护栏只数「表格行」条数，拦不住它。
    structure_issues = _table_structure_issues(new_content)
    if structure_issues and not _table_structure_issues(existing_body):
        # 仅当「原有结构合法」而「新结构坏掉」时拦截，避免误伤历史遗留文件
        detail_lines = "\n".join(f"  · {x}" for x in structure_issues[:5])
        return (
            f"错误：已拒绝写入「{title}」——检测到表格结构被写坏：\n"
            f"{detail_lines}\n"
            f"  Markdown 规定表头下方必须紧贴分隔线（|---|），中间夹任何行都会让整张表\n"
            f"  渲染成纯文本。正确做法：数据行一律追加到分隔线之后（表格末尾或指定位置）。\n"
            f"  修复工具：python 脚本/fix_table_structure.py --fix\n"
            f"  若确实需要手工重建，请直接编辑磁盘文件（不经本护栏），改完刷新索引。"
        )

    if not lost:
        return None
    detail = "、".join(f"{k} {a}→{b}" for k, (a, b) in lost.items())
    return (
        f"错误：已拒绝写入「{title}」——检测到结构性缩水（{detail}）。\n"
        f"  memory_write 是整篇替换（upsert），content 里没写的部分会被永久删除。\n"
        f"  聚合页的正确维护方式：先 memory_read 取回全文 → 在目标位置追加/替换 → 写回完整内容；\n"
        f"  或用 Edit 直接改磁盘文件（不经本护栏），改完刷新索引。\n"
        f"  若确实是主人明确要求整篇重建，请直接编辑文件，并在完成后跑一次索引刷新。"
    )


@_tool(annotations=_ANN_WRITE)
def memory_write(title: str, content: str, tags: list[str] | None = None, source: str | None = None,
                 summary: str | None = None, tier: str | None = None,
                 expected_version: int | None = None, verified: bool | None = None,
                 allow_duplicate: bool = False) -> str:
    """[写入] 写入记忆条目：同标题已存在则更新，不存在则新建 —— 本库唯一的写入口。

    写入前先 memory_search 查重，能更新就不新建。聚合页（近期工作动态 / 项目状态）
    不要用本工具整篇覆盖：护栏 _guard_aggregate_overwrite 会拦下，正确做法是
    Read + Edit 追加（2026-09-15 出过覆盖丢 59 条动态的事故）。

    expected_version (optional): optimistic concurrency check. If provided and the
    entry's current on-disk version does not match, the write is rejected to prevent
    one client silently overwriting another's concurrent edit. Omit for normal upsert.

    verified (optional): 可信度标记。True = 主人明确确认或本机实测通过；
    False = 明确未验证；不传 = 沿用该条既有值（新建时默认 false）。
    库里凡带「待重启生效」「预期」「预计」类措辞而未验证的结论，跨会话会被
    后来的 AI 当作既成事实读取，本字段就是给这种情况留的出口。

    allow_duplicate (optional): 新建时若检测到同主题已有条目，默认返回提示而不落盘；
    确认要建独立条目时传 true。
    """
    tags = tags or []

    # 第三轮 P0-6：拒绝空壳与空标题
    if not title or not title.strip():
        return "错误：标题为空，已拒绝写入。"
    if not content or not content.strip():
        return "错误：内容为空，已拒绝创建空壳记忆（改进#5）。如为更新且需保留正文，请勿传空 content。"

    # auto-generate summary from first 100 chars of content if not provided
    if not summary:
        clean = content.replace("\n", " ").strip()
        summary = clean[:100] + ("..." if len(clean) > 100 else "")

    tags = _auto_suggest_tags(content, tags)

    existing_path: Path | None = None
    existing_meta: dict[str, Any] = {}
    existing_body: str = ""

    # 写入只匹配活动笔记（递归，不含 .archive/.trash 等系统目录）。
    # 若活动区无此标题而归档中有，视为"归档后重建"——新建条目，
    # 归档保持原样。要修改归档内容请先 memory_restore。
    for f in _active_md_files():
        meta, body = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            existing_path = f
            existing_meta = meta
            existing_body = body
            break

    # 同主题拦截（2026-09-10）：新建前比对已有条目的「主题核心」，命中则先提示确认。
    if existing_path is None and not allow_duplicate:
        similar = _find_similar_titles(title)
        if similar:
            return ("检测到同主题已有条目，本次未写入（避免重复新建）：\n- "
                    + "\n- ".join(similar)
                    + "\n\n若本意是更新其中一条，请用它的标题原样重写（走 upsert）；"
                    + "\n若确认这是一条独立的新笔记，请带上 allow_duplicate=true 重新调用。")

    _invalidate_cache()
    locked = _acquire_lock()
    try:
        # 第三轮 P0-2：乐观锁必须在锁内校验，且基于锁内重新读取的磁盘状态，
        # 避免"检查与写入非原子"的 TOCTOU 竞态（上一轮修复不彻底）。
        if existing_path is not None:
            fresh_meta, fresh_body = _load_memory(existing_path)
            existing_meta, existing_body = fresh_meta, fresh_body
            if expected_version is not None:
                current_version = existing_meta.get("version", 0)
                if isinstance(current_version, int) and current_version != expected_version:
                    return (f"错误：版本冲突（期望 version={expected_version}，"
                            f"实际 version={current_version}）。记忆可能已被其他端修改，"
                            f"请重新读取后再写入。")
        elif expected_version is not None:
            return f"错误：版本冲突（记忆 '{title}' 不存在，期望 version={expected_version}）。"

        # Markdown 卫生：引用块后补空行，防止表格被 lazy continuation 吞成纯文本
        content = _normalize_md_spacing(content)

        # 聚合页覆盖护栏（2026-09-15 事故后新增）：整篇替换 + 结构缩水 = 拒绝
        guard_msg = _guard_aggregate_overwrite(title, content, existing_body)
        if guard_msg:
            logger.warning("GUARD-BLOCK aggregate overwrite: title=%s", title)
            return guard_msg

        # 改进#6：source 默认值；改进#10：version 递增（基于锁内最新值）
        effective_source = source or (existing_meta.get("source") if existing_path else None) or "unknown"
        old_version = existing_meta.get("version", 0)
        new_version = (old_version + 1) if isinstance(old_version, int) else 1

        if existing_path:
            old_count = existing_meta.get("access_count", 0)
            old_tier = tier or existing_meta.get("tier", "warm")
            _write_memory(
                existing_path,
                title=title,
                tags=tags,
                source=effective_source,
                content=content,
                created=str(existing_meta.get("created")) if existing_meta.get("created") else None,
                summary=summary,
                tier=old_tier,
                access_count=old_count if isinstance(old_count, int) else 0,
                version=new_version,
                verified=verified,
            )
            # Auto-archive: if tier is cold or very-low-heat, move to archive
            _maybe_auto_archive(title, existing_path)
            logger.info("UPDATE  title=%s tags=%s source=%s", title, tags, source)
            result_msg = f"已更新记忆: {title} ({existing_path.name})"
        else:
            folder_name = _folder_for_title(title)
            target_dir = (MEMORY_DIR / folder_name) if folder_name else MEMORY_DIR
            if folder_name:
                target_dir.mkdir(parents=True, exist_ok=True)
            filepath = _resolve_unique_path(target_dir, title)
            _write_memory(filepath, title=title, tags=tags, source=effective_source, content=content,
                          summary=summary, tier=tier or "warm", version=new_version,
                          verified=verified)
            logger.info("CREATE  title=%s tags=%s source=%s (new)", title, tags, source)
            rel = filepath.relative_to(MEMORY_DIR)
            result_msg = f"已创建记忆: {title} ({rel})"
        # 改进#7：写入后自动维护索引（跳过索引自身，直接写文件防递归）
        if title != "记忆索引":
            _refresh_index()
        if title != ROUTING_INDEX_TITLE:
            _refresh_routing_index()
        return result_msg
    finally:
        _flush_access_counts()
        _release_lock()

# ── 骨架压缩：六类型分块（2026-09-19 重构，mode=skeleton）──────────────────
# 动机：memory_read 是全库最重的读路径（实测单篇最高 36,094 tok）。但绝大多数
# 调用只需要判断「有没有这件事、什么时候的」，不需要「具体怎么做的」。本模块
# 提供一种可选的窄粒度返回：保留结构骨架与索引信息，裁掉细节正文。
#
# 关键设计：**不存原文副本**。md 文件本身就在磁盘上，mode="full" 随时能再读，
# 因此压缩版是纯粹的「目录页」，不存在「原文丢了」的风险。这也是它比
# headroom 那套 CCR（压缩—缓存—回取）更轻的原因——本库的原文天然持久，
# 不需要第二份缓存副本。
#
# 演进史（两版，第二版是当前实现）：
#   v1（同日上午）：只分「表格型/长文型」两类。实测九篇省 70.5%，但有两个缺陷：
#     ① **代码块被整个丢弃** —— 全库 29 个代码块装的是命令/路径/配置/SQL
#        （`git pull`、`skip-name-resolve`、`CREATE TABLE facts(...)`、python
#        绝对路径），是硬资产，丢了下次自己都跑不起来；
#     ② 段落被整个丢弃，而《规则规范》《系统卡顿》这类文章段落是主干。
#   v2（当日 17:00）：改为**六类型分块**（列表/表格/段落/标题/引用/代码），
#     按「内容的冗余来源」分别处理，并对**代码与引用永久保留**（体量小
#     1.7%+1.7%、价值高）。总体省 66.3%，略低于 v1 的 70.5%，但保真度显著更高：
#     代码不再丢、单篇压缩率更均衡（脚本索引 19.4%→59.6%）。
#     **这是有意的取舍**：省 token 是手段，保住可操作性才是目的。参考规范铁律 16。
#
# 库内真实类型分布（2026-09-19 全库扫描，按字符数）：
#   列表 46.3% ｜ 表格 35.2% ｜ 段落 12.2% ｜ 标题 2.8% ｜ 引用 1.7% ｜ 代码 1.7%
#
# 各类型的「冗余来源」与对应手法（这是本模块的设计原理）：
#   标题  冗余≈0      → 全留（占比小、是骨架，价值最高）
#   引用  冗余≈0      → 全留（体量小，常含事故现场原话）
#   代码  冗余在行数  → 完整保留到 cap；超出才截并标注总行数（命令/路径多在头部）
#   列表  冗余在展开  → 每条留首句到 cap（列表是叙述主力，压太狠会丢条文）
#   表格  冗余在单格  → **只裁最长的那个单元格**（通常是「备注」列），其余原样
#   段落  冗余在展开  → 留首句到 cap（散文多为铺垫，核心通常在标题/列表里）
#
# 设计要点：表格从 v1 的「留前 N 列」改为 v2 的「只裁最长格」。原因：列数固定时
# 砍掉后面几列会**盲砍**——若某表的关键信息恰在第 4 列就丢了；而「只裁最长格」
# 精准命中冗余（备注列往往比其余所有列加起来还长），且不会误伤短列。
_SKELETON_LINE_CAP = 90        # 列表：每条保留首句到此字符数
_SKELETON_CELL_CAP = 60        # 表格：最长单元格裁到此字符数
_SKELETON_PARA_CAP = 100       # 段落：留首句到此字符数
_SKELETON_QUOTE_CAP = 120      # 引用：裁到此字符数
_SKELETON_CODE_CAP = 240       # 代码：完整保留到此字符数，超出才截

# 块类型标识
_B_HEAD, _B_LIST, _B_TABLE, _B_PARA, _B_QUOTE, _B_CODE, _B_RULE = (
    "heading", "list", "table", "para", "quote", "code", "rule")
# 永久保留的类型（冗余≈0 或价值极高）：标题、引用
_B_KEEP_FULL = {_B_HEAD, _B_QUOTE}

_LIST_RE = re.compile(r"^\s*[-*+]\s+|^\s*\d+\.\s+")
_RULE_RE = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$")


def _split_blocks(body: str) -> list[tuple[str, str]]:
    """把正文切成 (类型, 文本) 块序列。代码块用围栏识别，内部不再分型。"""
    blocks: list[tuple[str, str]] = []
    cur: list[str] = []
    kind: str | None = None
    in_code = False

    def flush() -> None:
        nonlocal cur
        if cur:
            blocks.append((kind or _B_PARA, "\n".join(cur)))
            cur = []

    for line in body.splitlines():
        s = line.rstrip()
        # 围栏判定必须先去前导空格：库里存在「列表项内嵌代码块」的写法
        # （如 `  ```ini` / `   ```），用 startswith("```") 会漏判，导致这类
        # 代码块被当成普通段落压掉。2026-09-19 全库验证时踩到（2/17 篇）。
        if s.strip().startswith("```"):
            if not in_code:          # 进入代码块：先冲刷前面的普通块
                flush()
                in_code = True
                kind = _B_CODE
            else:                    # 离开代码块
                flush()
                in_code = False
                kind = None
            continue
        if in_code:
            cur.append(s)
            continue
        if not s.strip():
            flush()
            kind = None
            continue
        if s.startswith("|") and s.count("|") >= 2:
            k = _B_TABLE
        elif s.startswith("#"):
            k = _B_HEAD
        elif _LIST_RE.match(s):
            k = _B_LIST
        elif s.startswith(">"):
            k = _B_QUOTE
        elif _RULE_RE.match(s):
            k = _B_RULE
        else:
            k = _B_PARA
        if k != kind:
            flush()
            kind = k
        cur.append(s)
    flush()
    return blocks


def _c_list(text: str, cap: int = _SKELETON_LINE_CAP) -> str:
    """列表：每条留首句到 cap。空项丢弃。"""
    out = []
    for line in text.splitlines():
        x = _LIST_RE.sub("", line)
        x = re.sub(r"^\[[ xX]\]\s*", "", x).strip()   # 去掉待办复选框标记
        if not x:
            continue
        out.append("- " + x[:cap] + ("…" if len(x) > cap else ""))
    return "\n".join(out)


def _c_table(text: str, cap: int = _SKELETON_CELL_CAP) -> str:
    """表格：只裁最长的那个单元格，其余原样保留；分隔线压成固定宽度。"""
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        # 分隔线（全是 - / : / 空格）：原样保留会把几千字符的长破折号全量带进
        # 上下文（2026-09-19 实测踩到），压成固定宽度
        if not "".join(cells).replace("-", "").replace(":", "").strip():
            out.append("|" + "---|" * max(1, len(cells)))
            continue
        idx = max(range(len(cells)), key=lambda i: len(cells[i]))
        cut = list(cells)
        if len(cut[idx]) > cap:
            cut[idx] = cut[idx][:cap] + "…"
        out.append("| " + " | ".join(cut) + " |")
    return "\n".join(out)


def _c_para(text: str, cap: int = _SKELETON_PARA_CAP) -> str:
    """段落：折叠空白后留首句到 cap。"""
    x = re.sub(r"\s+", " ", text).strip()
    if not x:
        return ""
    return x[:cap] + ("…" if len(x) > cap else "")


def _c_quote(text: str, cap: int = _SKELETON_QUOTE_CAP) -> str:
    """引用：折叠空白后裁到 cap。"""
    x = re.sub(r"\s+", " ", re.sub(r"^>\s?", "", text, flags=re.M)).strip()
    if not x:
        return ""
    return "> " + x[:cap] + ("…" if len(x) > cap else "")


def _c_code(text: str, cap: int = _SKELETON_CODE_CAP) -> str:
    """代码：完整保留到 cap（命令/路径/配置是资产）；超出才截并标注总行数。"""
    if len(text) <= cap:
        return text
    lines = text.splitlines()
    keep = max(2, len(lines) // 2)
    return "\n".join(lines[:keep]) + f"\n… (共 {len(lines)} 行)"


def _compress_body(body: str) -> str:
    """六类型分块压缩：按各类型的冗余来源分别处理。"""
    if not body.strip():
        return body
    out = []
    for kind, text in _split_blocks(body):
        if kind in _B_KEEP_FULL:
            piece = text
        elif kind == _B_LIST:
            piece = _c_list(text)
        elif kind == _B_TABLE:
            piece = _c_table(text)
        elif kind == _B_CODE:
            piece = "```\n" + _c_code(text) + "\n```"
        elif kind == _B_QUOTE:
            piece = _c_quote(text)
        elif kind == _B_RULE:
            piece = "---"
        else:
            piece = _c_para(text)
        if piece.strip():
            out.append(piece)
    return "\n\n".join(out)


# ── 工具参数校验（2026-09-19 审计后补）─────────────────────────────────────
# 起因：审计实测出三类「静默失败」，共同点是**入参不对时不报错、直接走默认路径
# 或抛裸 TypeError**：
#   ① memory_read(mode="skel") 等拼错值 → 静默回落 full，实测 8,592 字符 vs
#      113,305 字符（13 倍差），调用方以为省了 token 其实付了全文；
#   ② memory_search(limit="3") → 裸 TypeError，模型收到 Python 内部错误信息，
#      无法自我纠正；
#   ③ memory_search(limit=-1) → 切片 `[:−1]` 静默变成「几乎不限」，还输出
#      「显示前 -1 组」这种荒谬提示。
# 统一原则：**非法入参一律显式报错，且错误消息给出合法取值范围**，让调用方
# （AI）拿到消息就能改。对本库这种「省 token 依赖参数精确传递」的架构，静默
# 回落等于让调用方付全额代价却误以为省了 —— 比直接报错更坏。
def _norm_int(value: Any, name: str, *, default: int, lo: int, hi: int) -> int:
    """把入参归一为 [lo, hi] 区间内的整数；非法值抛 ValueError（绝不静默回落）。

    None 与缺省视为「用默认值」——JSON 里显式传 null 等同于不传，属宽容项；
    其余任何非整数、越界值一律报错并在消息里写明合法范围。
    """
    if value is None:
        return default
    # 浮点只接受整数值：int(1.5) 会静默丢精度成 1，属同一类「静默改变语义」，
    # 与 ①③ 同源，故显式拒绝（MCP 通道下 pydantic 也会先拦一道）。
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} 必须是整数，收到 {value!r}（小数会被截断，故显式拒绝）。")
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数，收到 {value!r}（布尔值不当作 0/1 使用）。")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} 必须是整数，收到 {value!r}（{type(value).__name__}）。"
        ) from None
    if n < lo:
        raise ValueError(f"{name} 不能小于 {lo}，收到 {n}。")
    if n > hi:
        raise ValueError(f"{name} 不能大于 {hi}，收到 {n}（超出上限会被静默截断，故显式拒绝）。")
    return n


_READ_MODES = ("full", "skeleton")


@_tool(annotations=_ANN_RO)
def memory_read(
    title: str,
    mode: Annotated[
        Literal["full", "skeleton"],
        Field(description="full=整篇正文(默认，行为不变)；skeleton=骨架(标题+列表首句+表格最长格)，九篇实测省 64.4%"),
    ] = "full",
) -> str:
    """[读正文] 按精确标题读取笔记。mode="full" 返回整篇（默认，行为不变）；
    mode="skeleton" 返回骨架：标题与引用全留、代码保留到 240 字、列表每条留首句
    90 字、段落留首句 100 字、表格只裁最长格。九篇实测 100,913 → 35,931 tok
    （省 64.4%），《项目状态》单篇 36,553 → 3,666（省 90.0%）。

    何时换别的：只要 summary、计数或某一列 → memory_sql；标题不确定 → 先用
    memory_search 定位。mode="full" 会把整篇正文灌进上下文（实测最重约
    36,600 tok/篇），是五条里最重的一条；不确定是否需要细节时，先用
    mode="skeleton" 判断相关性，确需细节再调 mode="full" 读原文。
    非法 mode 会显式报错，不会静默回落全文。

    标题匹配支持文件名回退（legacy 文件）。"""
    read_mode = str(mode).strip().lower()
    if read_mode not in _READ_MODES:
        raise ValueError(
            f'mode 只接受 "full" 或 "skeleton"，收到 {mode!r}。'
            f'（拼错会静默拉全文，代价差约 13 倍，故此处显式报错）'
        )
    for f in _active_md_files() + list((MEMORY_DIR / ".archive").glob("*.md")):
        meta, body = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            # increment access_count in memory cache (lazy write-back)
            with _ACCESS_LOCK:
                _ACCESS_CACHE[title] = _ACCESS_CACHE.get(title, 0) + 1
                flush_access = len(_ACCESS_CACHE) >= _ACCESS_FLUSH_THRESHOLD
            if flush_access:
                _flush_access_counts()

            core_keys = ["title", "tags", "summary", "created", "updated", "tier",
                         "access_count", "source", "version",
                         "type", "scope", "verified", "schema_version"]
            meta_str = " | ".join(
                f"{k}={str(meta[k]).replace(chr(10), ' ')}"
                for k in core_keys
                if k in meta
            )
            if read_mode == "skeleton":
                skeleton = _compress_body(body)
                notice = (
                    f"[mode=skeleton] 以下为骨架，已省略细节正文。"
                    f"需要全文请重新调用 memory_read(title=\"{title}\", mode=\"full\")。"
                )
                return f"[{meta_str}]\n\n{notice}\n\n{skeleton}"
            return f"[{meta_str}]\n\n{body}"
    return f"未找到标题为 '{title}' 的记忆"


# ── G4（2026-10-09，对标 Basic Memory 的 build_context）：上下文包 ─────────────
# 「讲讲 X，顺带把相关笔记也带上」此前要 1 次 search + N 次 read，每篇全文动辄
# 上万 token。memory_context 一次调用沿 [[wikilink]] 拉 1-hop 邻居并全部走
# skeleton 压缩，把「主题 + 关联」的成本压到一次调用。

def _title_index() -> dict[str, Path]:
    """标题（含文件名回退）→ 路径。active + .archive，与 memory_read 同口径。"""
    idx: dict[str, Path] = {}
    files = _active_md_files()
    archive = MEMORY_DIR / ".archive"
    if archive.exists():
        files = files + sorted(archive.glob("*.md"))
    for f in files:
        try:
            meta, _ = _load_memory(f)
        except Exception:
            continue
        idx.setdefault(_entry_title(meta, f), f)
        idx.setdefault(f.stem, f)
    return idx


@_tool(annotations=_ANN_RO)
def memory_context(
    title: str,
    max_neighbors: Annotated[
        int,
        Field(ge=1, le=10, description="最多拉取的 1-hop 邻居数，默认 5，上限 10"),
    ] = 5,
) -> str:
    """[上下文包] 拉一篇笔记的骨架 + 沿 [[wikilink]] 的一跳邻居骨架 —— 一次调用拿到「主题 + 关联」。

    对标 Basic Memory 的 build_context：中心笔记与邻居全部走 skeleton 压缩
    （九篇实测省 64.4%），邻居按中心笔记正文里 wikilink 的出现顺序取前
    max_neighbors 个。确需某邻居的细节时，再单独 memory_read(mode="full")。
    何时换别的：不需要关联笔记 → 直接 memory_read；标题还没定 → 先 memory_search。
    标题不存在时返回未找到提示，不会静默返回空包。
    """
    idx = _title_index()
    f = idx.get(title)
    if f is None:
        return f"未找到标题为 '{title}' 的记忆"
    meta, body = _load_memory(f)

    links = _extract_wiki_links(body)
    seen: set[str] = set()
    neighbors: list[tuple[str, str, str]] = []  # (title, meta_str, skeleton)
    for link in links:
        if len(neighbors) >= max_neighbors:
            break
        if link in seen or link == title:
            continue
        nf = idx.get(link)
        if nf is None:
            continue  # 死链：check_dead_links 的职责，这里只跳过
        seen.add(link)
        nmeta, nbody = _load_memory(nf)
        nmeta_str = " | ".join(
            f"{k}={str(nmeta[k]).replace(chr(10), ' ')}"
            for k in ("title", "summary", "updated", "tier")
            if k in nmeta
        )
        neighbors.append((link, nmeta_str, _compress_body(nbody)))

    meta_str = " | ".join(
        f"{k}={str(meta[k]).replace(chr(10), ' ')}"
        for k in ("title", "summary", "updated", "tier", "tags")
        if k in meta
    )
    out = [
        f'[context-pack] 中心：{title}（skeleton；全文用 memory_read(title="{title}", mode="full")）',
        f"[{meta_str}]",
        "",
        _compress_body(body),
    ]
    if neighbors:
        out.append("")
        out.append(
            f"── 1-hop 邻居（中心共 {len(set(links))} 条 wikilink，实拉 {len(neighbors)} 条）──"
        )
        for nt, nmeta_s, nskel in neighbors:
            out.append("")
            out.append(f"### [[{nt}]]")
            out.append(f"[{nmeta_s}]")
            out.append(nskel)
    else:
        out.append("")
        out.append("（中心笔记无有效 wikilink，无邻居可拉）")
    return "\n".join(out)


@_tool
def memory_rebuild_links() -> str:
    """Validate link consistency without writing frontmatter links.

    Since 2026-09-06 links are derived from explicit [[wikilinks]] in note
    bodies only and are never persisted in frontmatter. This tool only scans
    bodies and reports dead links; it does NOT rewrite files.
    """
    _invalidate_cache()
    all_titles: set[str] = set()
    for f in _active_md_files():
        try:
            m, _ = _load_memory(f)
            all_titles.add(_entry_title(m, f))
            all_titles.add(f.stem)
        except Exception:
            continue
    dead: list[str] = []
    checked = 0
    for f in _active_md_files():
        m, body = _load_memory(f)
        for link in _extract_wiki_links(body):
            if link not in all_titles:
                dead.append(f"{_entry_title(m, f)} -> [[{link}]]")
        checked += 1
    if dead:
        return f"发现 {len(dead)} 条死链：\n- " + "\n- ".join(sorted(set(dead)))
    return f"链接校验通过：扫描 {checked} 条笔记，无死链（links 字段已不再持久化）"


# ── 检索结果排序 + 同主题折叠（F 档，2026-09-16，源自 ruflo 对标）───────────────
# F1：memory_search 此前是「命中即收集 → results[:limit] 截断」，结果顺序完全等于
#     _active_md_files() 的路径倒序（memory_store.py:80）——库根文件恒排在子目录笔记
#     之前，高频词查询被截掉的那批是「路径序靠后」的，最新更新的笔记可能整个不出现；
#     而 docstring 第一行写着 recent-first。这里把承诺兑现。
# F2：同一件事常常同时命中叶子笔记与聚合页，5 条里 3~4 条是同主题的不同层级。
_NAV_TITLES = {"记忆索引", "路由索引", "脚本索引", "项目状态", "近期工作动态"}
_TOPIC_COLLAPSE_CHARS = 4   # 主题核心取前 N 字做分桶
_COLLAPSE_LIST_MAX = 4      # 折叠说明里最多列几个标题，其余收成「等 N 条」


def _hit_ts(meta: dict[str, Any], path: Path) -> float:
    """命中条目的排序时间戳：优先 frontmatter updated，回落文件 mtime（F1）。"""
    dt = _parse_dt_utc(str(meta.get("updated") or ""))
    if dt is not None:
        return dt.timestamp()
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# G5（2026-10-09，源自对标 Basic Memory 的 since 过滤）：检索时间窗。
# 「最近一周改过哪些」这类问题此前只能先检索再人工翻 updated，窗口过滤
# 直接砍掉过期命中，省的是折叠后仍要展示的行数。
_SINCE_RE = re.compile(r"(\d+)\s*d(?:ays?)?")


def _parse_since(since: str | None) -> float | None:
    """解析时间窗参数：返回 Unix 秒时间戳下界，None 表示不限。

    接受 "Nd"（N 天内，如 "7d"）或 "YYYY-MM-DD"（当地时间该日 00:00 起）。
    非法值显式报错而不是静默当「不限」——静默忽略会把「带过滤的查询」
    变成「全库返回」，结果更糟糕且极难排查。
    """
    if since is None:
        return None
    s = str(since).strip()
    if not s:
        return None
    m = _SINCE_RE.fullmatch(s.lower())
    if m:
        return time.time() - int(m.group(1)) * 86400
    dt = _parse_dt_utc(s)
    if dt is not None:
        return dt.timestamp()
    try:
        return datetime.strptime(s, "%Y-%m-%d").timestamp()
    except ValueError:
        raise ValueError(
            f'since 只接受 "Nd"（如 "7d"）或 "YYYY-MM-DD"，收到 {since!r}。'
        ) from None


def _collapse_key(title: str) -> str:
    """同主题折叠键（F2）。

    导航 / 聚合页独占一桶：它们收录全库标题与摘要，几乎命中任何查询，但「查
    `记忆索引`」这类需求必须仍能一眼看到它本身，故不参与折叠。其余按「主题核心」
    前 4 字分桶 —— `_topic_core` 已剥掉 `项目_` / `知识_` / 日期 / 库名，前 4 字足以把
    「记忆库结构调研 / 记忆库结构升级」并成一桶，而「记忆库根目录归档整理」不会误并
    （核心自第 3 字起就分叉）。与 `_find_similar_titles` 同口径：核心不含中文的
    纯 ASCII 标题（smoke_N、探针样本）一律不折叠，避免英文词根造成误并。
    """
    if title in _NAV_TITLES:
        return "nav:" + title
    core = _topic_core(title)
    if len(core) >= _TOPIC_COLLAPSE_CHARS and re.search(r"[\u4e00-\u9fff]", core):
        return "core:" + core[:_TOPIC_COLLAPSE_CHARS]
    return "self:" + title


def _group_by_topic(ordered: list[tuple[bool, float, str, str]]) -> list[list[tuple[bool, float, str, str]]]:
    """按折叠键分组，组序沿用首次出现顺序（此时已是 updated 倒序）。"""
    groups: list[list[tuple[bool, float, str, str]]] = []
    index: dict[str, int] = {}
    for hit in ordered:
        key = _collapse_key(hit[2])
        if key in index:
            groups[index[key]].append(hit)
        else:
            index[key] = len(groups)
            groups.append([hit])
    return groups


def _render_groups(groups: list[list[tuple[bool, float, str, str]]]) -> list[str]:
    """分组 → 展示行。

    硬约束：被折叠条目的标题必须出现在输出里 —— 静默吞掉会让人误判「库里没有」，
    那比冗余本身更糟（这是 F2 方案里唯一不可让步的一条）。
    """
    lines: list[str] = []
    for group in groups:
        lines.append(group[0][3])
        extra = [h[2] for h in group[1:]]
        if extra:
            head = "、".join("`%s`" % t for t in extra[:_COLLAPSE_LIST_MAX])
            tail = "、等 %d 条" % len(extra) if len(extra) > _COLLAPSE_LIST_MAX else ""
            lines[-1] += "\n  ↳ 另 %d 条同主题：%s%s" % (len(extra), head, tail)
    return lines


@_tool(annotations=_ANN_RO)
def memory_search(
    keyword: str,
    tag: str | None = None,
    limit: Annotated[
        int,
        Field(ge=1, le=200, description="返回的最大组数（同主题会折叠，故实际行数可能少于该值），默认 20，上限 200"),
    ] = 20,
    since: Annotated[
        str | None,
        Field(description='时间窗（G5）：如 "7d"=最近7天、"2026-09-01"=从该日起；按 updated 过滤，不传=不限'),
    ] = None,
) -> str:
    """[检索] 按关键词在标题/标签/正文里找笔记（recent-first）—— 回答「库里有没有、叫什么」。

    何时换别的：标题已确定、要看整篇正文 → memory_read；只要 summary / 计数 /
    某一列 → memory_sql（约 200 tok，比读整篇省 99%）。本工具只给命中行与定位，
    不返回全文，是「先探路」的首选。

    结果按 frontmatter `updated` 倒序（活动区在前、`.archive` 在后），同主题条目
    折叠为一组、被折叠的标题仍会列出。故 `limit` 限制的是**组数**（默认 20），
    有折叠时返回的行数会少于 limit。正文里命中的关键词用 ** 包起来便于定位。
    非法 limit（非整数 / 小于 1）会显式报错，不会静默变成「不限」。
    since 为时间窗（如 "7d"、"2026-09-01"），窗口外的命中直接不返回（并注明
    剔除多少条）；非法格式显式报错，不会静默退化成「不限」。
    """
    if not keyword or not keyword.strip():
        return "错误：搜索关键词不能为空。"
    limit = _norm_int(limit, "limit", default=20, lo=1, hi=200)
    hits: list[tuple[bool, float, str, str]] = []  # (归档?, 排序时间戳, title, 展示行)
    keyword_lower = keyword.lower()
    archive_dir = MEMORY_DIR / ".archive"

    search_files = _active_md_files()
    if archive_dir.exists():
        search_files += sorted(archive_dir.glob("*.md"), reverse=True)

    for f in search_files:
            try:
                meta, body = _load_memory(f)
            except Exception:
                continue

            entry_tags = _entry_tags(meta)
            if tag and tag.lower() not in [t.lower() for t in entry_tags]:
                continue

            title = _entry_title(meta, f)
            tags_str = " ".join(entry_tags)
            searchable = f"{title} {tags_str} {body}".lower()

            if keyword_lower in searchable:
                summary = meta.get("summary", "")
                tier = meta.get("tier", "warm")
                count = meta.get("access_count", 0)

                # extract keyword context from body (20 chars before and after)
                idx = body.lower().find(keyword_lower)
                context = ""
                if idx >= 0:
                    start = max(0, idx - 20)
                    end = min(len(body), idx + len(keyword) + 20)
                    ctx = body[start:end].replace("\n", " ")
                    ctx = re.sub(re.escape(keyword), f"**{keyword}**", ctx, flags=re.IGNORECASE)
                    if start > 0:
                        ctx = "..." + ctx
                    if end < len(body):
                        ctx = ctx + "..."
                    context = f"\n  → {ctx}"

                source = meta.get("source", "")
                source_info = f" | 来源: {source}" if source else ""
                tags_display = ", ".join(entry_tags)
                summary_display = f"\n  {summary}" if summary else ""
                # scope / verified 标注（2026-09-10）：跨项目套用规则时能一眼看出该条
                # 是否通用；未验证结论也会显式打标，不再和既成事实混在一条结果里。
                scope = str(meta.get("scope") or _derive_scope(title, f))
                verified_mark = "" if meta.get("verified") is True else " | 未验证"

                hits.append((
                    archive_dir in f.parents,
                    _hit_ts(meta, f),
                    title,
                    f"- [{title}] ({f.name}) | {tags_display} | tier={tier} | scope={scope} | reads={count}{verified_mark}{source_info}{summary_display}{context}",
                ))

    # F1（2026-09-16）：按 updated 倒序，兑现 docstring 的 recent-first 承诺。
    # 活动区优先、`.archive` 靠后 —— 两组各自排序、不混排（归档是历史层，不该顶掉现役笔记）。
    active_hits = sorted([h for h in hits if not h[0]], key=lambda x: -x[1])
    archived_hits = sorted([h for h in hits if h[0]], key=lambda x: -x[1])
    ordered = active_hits + archived_hits

    # G5（2026-10-09）：时间窗过滤放在折叠之前 —— 窗口外的命中不该占用折叠组，
    # 也不该计入「找到 N 条」。剔除数写进头部，让人知道过滤真实生效了。
    window_dropped = 0
    cutoff = _parse_since(since)
    if cutoff is not None:
        before = len(ordered)
        ordered = [h for h in ordered if h[1] >= cutoff]
        window_dropped = before - len(ordered)

    # 检索日志（第 12 项）：零结果率是「要不要上 FTS5 / 别名 / 向量」的唯一判据来源。
    top1 = ""
    if ordered:
        top1 = ordered[0][2]
        _log_search(keyword, len(ordered), top1, tag)

    if not ordered:
        # 零结果兜底（E1，2026-09-14，走法 1b）：memory_search 是单串子串匹配，
        # 多词查询必然零命中（实测 `supervisor worker 热重载` 0 命中，而同义单词
        # `原地热重载` 命中 7 条）——属于分词问题，转交分词加权打分即可解决。
        # 刻意不把 memory_smart_search 挂进 core 白名单：对外工具面保持 4 个，零常驻
        # schema 开销，只在真的零结果时才多扫一遍库。
        ranked, matched = _smart_rank(keyword, tag)
        if ranked:
            _log_search(keyword, len(ranked), ranked[0][1], tag, tool="search→smart")
            # C 方案（2026-09-14，两道闸，为兜底的 OR 语义噪声兜底）：
            #   ① 命中词自述 —— 中文 2-gram 会把短语拆成库内高频词
            #      （「不存在的组合」→ 存在/组合），把命中词摊开让人一眼看出是弱匹配；
            #   ② 限流 top N —— 尾巴上那批低分蹭词条目直接不返回。
            shown = min(limit, _FALLBACK_LIMIT)
            head = "子串匹配零结果，已转分词加权兜底（smart），共 {} 条".format(len(ranked))
            tail = []
            if len(ranked) > shown:
                tail.append("显示前 {} 条".format(shown))
            if matched:
                tail.append("命中词：" + "、".join(matched))
            if tail:
                head += "（" + "，".join(tail) + "）"
            lines = [head + "：", ""]
            lines.extend(_format_smart_lines(ranked[:shown]))
            return "\n".join(lines)
        _log_search(keyword, 0, "", tag)
        return f"未找到与 '{keyword}' 相关的记忆"

    # F2（2026-09-16）：同主题折叠后再截断。header 的「找到 N 条记忆」保持原口径
    # （总命中数，不受折叠影响），只额外注明折叠掉多少 —— 便于一眼判断这是不是弱匹配扎堆。
    rendered = _render_groups(_group_by_topic(ordered))
    shown = rendered[:limit]
    total = len(ordered)
    collapsed = total - len(rendered)
    header_parts = [f"找到 {total} 条记忆"]
    if collapsed:
        header_parts.append(f"{collapsed} 条同主题已折叠")
    if window_dropped:
        header_parts.append(f"时间窗外剔除 {window_dropped} 条")
    if len(rendered) > limit:
        header_parts.append(f"显示前 {limit} 组")
    header = "，".join(header_parts) + "：\n\n"
    return header + "\n\n".join(shown)

@_tool
def memory_list(tag: str | None = None, limit: int = 20, tier: str | None = None) -> str:
    """List memory entries with a small preview."""
    entries: list[str] = []

    for f in _active_md_files():
        try:
            meta, body = _load_memory(f)
        except Exception:
            continue

        entry_tags = _entry_tags(meta)
        if tag and tag.lower() not in [t.lower() for t in entry_tags]:
            continue
        if tier and meta.get("tier", "warm") != tier:
            continue

        title = _entry_title(meta, f)
        tags_display = ", ".join(entry_tags)
        created = str(meta.get("created", ""))[:10]
        source = meta.get("source", "")
        tier_val = meta.get("tier", "warm")
        reads = meta.get("access_count", 0)
        source_info = f" | {source}" if source else ""

        # prefer summary field, fallback to body[:40]
        summary = meta.get("summary", "")
        if summary:
            preview = summary[:80].replace("\n", " ").strip()
        else:
            preview = body[:40].replace("\n", " ").strip()
        preview += "..." if len(preview) >= (40 if not summary else 80) else ""

        entries.append(f"- [{created}] {title} [{tier_val}|{reads}]{source_info} | {tags_display}\n  {preview}")

    if not entries:
        return "记忆库为空"

    total = len(entries)
    entries = entries[:limit]
    result = "\n".join(entries)
    if total > limit:
        result += f"\n\n... 共 {total} 条，显示前 {limit} 条"
    return result

@_tool(annotations=_ANN_DELETE)
@_locked_write
def memory_delete(title: str, trash: bool = True, purge: bool = False) -> str:
    """Delete a memory entry by exact title, with filename fallback for legacy files.

    trash=True (default) moves the file to .trash/ instead of unlinking, so a
    mistaken delete can be recovered. Pass purge=True (or trash=False) to
    permanently remove it (including a previously soft-deleted copy in .trash/).
    Archive entries (.archive/) are also matched.
    """
    search_dirs = [MEMORY_DIR]
    archive_dir = MEMORY_DIR / ".archive"
    if archive_dir.exists():
        search_dirs.append(archive_dir)
    if not trash:
        trash_dir = MEMORY_DIR / ".trash"
        if trash_dir.exists():
            search_dirs.append(trash_dir)
    search_files = list(_active_md_files())
    for d in search_dirs:
        if d == MEMORY_DIR:
            continue
        search_files += sorted(d.glob("*.md"))
    for f in search_files:
        meta, _ = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            if trash and not purge:
                trash_dir = MEMORY_DIR / ".trash"
                trash_dir.mkdir(parents=True, exist_ok=True)
                dest = trash_dir / f.name
                n = 2
                while dest.exists():
                    dest = trash_dir / f"{f.stem}_{n}{f.suffix}"
                    n += 1
                f.replace(dest)
                logger.info("DELETE(soft) title=%s -> %s", title, dest.name)
                _invalidate_cache()
                if title != "记忆索引":
                    _refresh_index()
                if title != ROUTING_INDEX_TITLE:
                    _refresh_routing_index()
                return f"已软删除记忆: {title} -> .trash/{dest.name}（如需彻底删除，用 purge=true）"
            f.unlink()
            logger.info("DELETE(purge) title=%s", title)
            _invalidate_cache()
            if title != "记忆索引":
                _refresh_index()
            if title != ROUTING_INDEX_TITLE:
                _refresh_routing_index()
            return f"已永久删除记忆: {title} ({f.name})"
    return f"未找到标题为 '{title}' 的记忆"

@_tool
@_locked_write
def memory_update_metadata(title: str, tier: str | None = None, tags: list[str] | None = None,
                           summary: str | None = None, source: str | None = None,
                           verified: bool | None = None) -> str:
    """Update only the frontmatter metadata of an entry without rewriting its body.

    Useful for changing tier/tags/summary/source of an existing note while keeping
    its full content intact (avoids the full rewrite that memory_write requires).
    Pass None to leave a field unchanged; pass an empty list to clear tags.

    verified (2026-09-10): 回填/纠正可信度标记的唯一入口——不必重写正文即可把
    一条已实测通过的结论标为 true，或把过时结论退回 false。
    """
    for f in _active_md_files() + list((MEMORY_DIR / ".archive").glob("*.md")):
        meta, body = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            if tier is not None:
                meta["tier"] = tier
            if tags is not None:
                meta["tags"] = tags
            if summary is not None:
                meta["summary"] = summary
            if source is not None:
                meta["source"] = source
            if verified is not None:
                meta["verified"] = bool(verified)
            old_version = meta.get("version", 0)
            meta["version"] = (old_version + 1) if isinstance(old_version, int) else 1
            meta["updated"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_text(f, f"---\n{_dump_yaml_frontmatter(meta)}\n---\n\n{body}")
            _invalidate_cache()
            logger.info("UPDATE_META  title=%s", title)
            _refresh_index()
            _refresh_routing_index()
            return f"已更新元数据: {title}"
    return f"未找到标题为 '{title}' 的记忆"

@_tool
def memory_audit() -> str:
    """Scan the vault and summarize what should be indexed or cleaned up manually.

    Now also covers filesystem-level issues that frontmatter-only stats miss
    (improvements #8/#9): empty shells, dead links, naming drift.
    """
    entries = _iter_entries()
    if not entries:
        return "记忆库为空"

    # 建立全量 title 集合（含 .archive）用于死链检测
    all_titles: set[str] = set()
    for f in _active_md_files():
        try:
            m, _ = _load_memory(f)
            all_titles.add(_entry_title(m, f))
            all_titles.add(f.stem)
        except Exception:
            pass
    archive_dir = MEMORY_DIR / ".archive"
    if archive_dir.exists():
        for f in archive_dir.glob("*.md"):
            try:
                m, _ = _load_memory(f)
                all_titles.add(_entry_title(m, f))
                all_titles.add(f.stem)
            except Exception:
                pass

    bucket_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    attention: list[str] = []
    suggestions: list[str] = []
    dead_links: set[str] = set()
    empty_shells: list[str] = []
    verified_count = 0
    unverified_claims: list[str] = []

    for path, meta, body in entries:
        title = _entry_title(meta, path)
        bucket, reason = _entry_bucket(meta, path)
        bucket_counts[bucket] += 1

        source = str(meta.get("source", "")).strip()
        if source:
            source_counts[source] += 1
        else:
            attention.append(f"- {title}：缺少 source")

        tags = _entry_tags(meta)
        if not tags:
            attention.append(f"- {title}：缺少 tags")

        # 改进#9：空壳检测
        if not body or not body.strip():
            empty_shells.append(title)
            attention.append(f"- {title}：空壳（正文为空）")

        # verified 统计（2026-09-10 第 4 项）：字段已落地，这里给出覆盖率与
        # 仍带未验证措辞、需复核的条目。
        if meta.get("verified") is True:
            verified_count += 1
        elif title not in _VERIFIED_SKIP_TITLES and any(
            hint in body for hint in _UNVERIFIED_HINTS
        ):
            unverified_claims.append(title)

        # 改进#9/#10：死链检测（仅正文 wikilink；frontmatter links 已取消）
        # 跳过代码块（```...``` 与 ~~~...~~~）中的示例链接，避免误报噪点
        body_no_code = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
        body_no_code = re.sub(r"~~~.*?~~~", "", body_no_code, flags=re.DOTALL)
        PLACEHOLDER_LINKS = {"X", "XX", "XXX", "笔记名", "示例", "example", "placeholder", "Placeholder"}
        for link in _extract_wiki_links(body_no_code):
            if link in PLACEHOLDER_LINKS:
                continue
            if link != title and link not in all_titles:
                dead_links.add(f"- {title} → [[{link}]]（正文指向不存在的笔记）")

        # 改进#8：命名建议（游离的时间戳前缀）
        if re.match(r"^\d{4}-\d{2}-\d{2}_", title) and bucket == "其他":
            suggestions.append(f"- {title} -> 建议加分类前缀（如 项目_/修复_/工具_）")

        if bucket in {"项目", "工具", "运维", "修复", "规则", "身份与配置"}:
            suggestions.append(f"- {title} -> {bucket}（{reason}）")

    lines = ["# 记忆库审计报告", ""]
    lines.append("## 分类统计")
    for name, count in bucket_counts.most_common():
        lines.append(f"- {name}: {count}")

    lines.append("")
    lines.append("## 来源统计")
    for name, count in source_counts.most_common():
        lines.append(f"- {name}: {count}")

    lines.append("")
    lines.append("## 建议纳入索引")
    lines.extend(suggestions[:60] if suggestions else ["- 暂无"])

    lines.append("")
    lines.append("## 死链（指向不存在的笔记）")
    lines.extend(sorted(dead_links)[:60] if dead_links else ["- 暂无"])

    lines.append("")
    lines.append("## 空壳笔记")
    lines.extend([f"- {t}" for t in empty_shells] if empty_shells else ["- 暂无"])

    lines.append("")
    lines.append("## 需要人工补齐")
    lines.extend(attention[:60] if attention else ["- 暂无"])

    lines.append("")
    lines.append("## verified 覆盖")
    lines.append(f"- 已确认（verified=true）: {verified_count} / {len(entries)} 条")

    lines.append("")
    lines.append("## 待核实结论（含未验证措辞且未标 verified）")
    lines.extend([f"- {t}" for t in unverified_claims] if unverified_claims else ["- 暂无"])

    return "\n".join(lines)

@_tool
def memory_index_draft() -> str:
    """Generate a draft of the main index without writing it back automatically."""
    entries = _iter_entries()
    if not entries:
        return "记忆库为空"

    groups: dict[str, list[tuple[str, str]]] = {
        "索引": [],
        "规则": [],
        "项目": [],
        "工具": [],
        "运维": [],
        "修复": [],
        "时间线": [],
        "身份与配置": [],
        "模板": [],
        "AI相关": [],
        "其他": [],
    }

    for path, meta, _body in entries:
        title = _entry_title(meta, path)
        bucket, _reason = _entry_bucket(meta, path)
        display = title if title == path.stem else f"{title}|{path.stem}"
        groups.setdefault(bucket, []).append((title, display))

    order = ["索引", "规则", "身份与配置", "项目", "工具", "运维", "修复", "时间线", "模板", "AI相关", "其他"]
    lines = ["# 记忆索引（半自动草稿）", "", "> 这个草稿由 MCP 自动扫描生成，建议人工确认后再回写到 `记忆索引.md`。", ""]

    for bucket in order:
        items = groups.get(bucket, [])
        if not items:
            continue
        lines.append(f"## {bucket}")
        for title, _display in sorted(items, key=lambda item: item[0].lower()):
            lines.append(f"- [[{title}]]")
        lines.append("")

    return "\n".join(lines).rstrip()

@_tool
@_locked_write
def memory_archive(title: str) -> str:
    """Archive a memory entry: move full content to .archive/, leave a summary stub."""
    archive_dir = MEMORY_DIR / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    target_path: Path | None = None
    target_meta: dict[str, Any] = {}
    target_body = ""

    for f in _active_md_files():
        meta, body = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            target_path = f
            target_meta = meta
            target_body = body
            break

    if not target_path:
        return f"未找到标题为 '{title}' 的记忆"

    if target_meta.get("tier") == "cold" and target_meta.get("archived_to"):
        return f"记忆 '{title}' 已在归档中"

    archive_path = archive_dir / target_path.name
    if archive_path.exists():
        # 第三轮 P1-10：同名归档不覆盖，加时间戳唯一化
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_path = archive_dir / f"{target_path.stem}_{ts}{target_path.suffix}"
    meta_copy = dict(target_meta)
    meta_copy["tier"] = "cold"
    meta_copy["updated"] = datetime.now(timezone.utc).isoformat()
    archive_fm = _dump_yaml_frontmatter(meta_copy)
    _atomic_write_text(archive_path, f"---\n{archive_fm}\n---\n\n{target_body}")

    summary = target_meta.get("summary", "")
    if not summary:
        clean = target_body.replace("\n", " ").strip()
        summary = clean[:100] + ("..." if len(clean) > 100 else "")

    stub_meta = {
        "title": target_meta.get("title", title),
        "tags": target_meta.get("tags", []),
        "summary": summary,
        "tier": "cold",
        "access_count": target_meta.get("access_count", 0),
        "archived_to": ".archive/{0}".format(archive_path.name),
        "created": str(target_meta.get("created", "")),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    if target_meta.get("source"):
        stub_meta["source"] = target_meta["source"]

    stub_fm = _dump_yaml_frontmatter(stub_meta)
    stub_body = "> \u26a1 本条记忆已归档。完整内容在 `.archive/{0}`\n> 需要恢复的话跟我说一声就行。\n\n{1}".format(target_path.name, summary)
    _atomic_write_text(target_path, "---\n{0}\n---\n\n{1}".format(stub_fm, stub_body))

    logger.info("ARCHIVE title=%s", title)
    _invalidate_cache()
    _refresh_index()
    _refresh_routing_index()
    return "已归档: {0} -> .archive/{1}".format(title, archive_path.name)

@_tool
@_locked_write
def memory_restore(title: str) -> str:
    """Restore an archived entry back to the vault root (P1-7).

    Moves the full content from .archive/ back to MEMORY_DIR, replacing the stub
    left behind by memory_archive, and bumps tier back to warm.
    """
    archive_dir = MEMORY_DIR / ".archive"
    if not archive_dir.exists():
        return "归档目录不存在，无内容可恢复"

    target_path: Path | None = None
    target_meta: dict[str, Any] = {}
    target_body = ""
    for f in archive_dir.glob("*.md"):
        meta, body = _load_memory(f)
        if _entry_title(meta, f) == title or f.stem == title:
            target_path = f
            target_meta = meta
            target_body = body
            break
    if not target_path:
        return f"归档中未找到标题为 '{title}' 的记忆"

    target_title = str(target_meta.get("title") or title)
    if target_title in CORE_PAGES:
        return f"错误：'{target_title}' 是核心页面，不允许恢复覆盖。"

    # 定位活动笔记中的 stub（若有），否则用安全文件名新建
    stub = MEMORY_DIR / _safe_filename(target_title)
    for f in _active_md_files():
        m, _ = _load_memory(f)
        if _entry_title(m, f) == target_title or f.stem == target_title:
            stub = f
            break

    restored_meta = dict(target_meta)
    restored_meta["tier"] = "warm"
    restored_meta.pop("archived_to", None)
    restored_meta["updated"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(stub, f"---\n{_dump_yaml_frontmatter(restored_meta)}\n---\n\n{target_body}")
    target_path.unlink(missing_ok=True)
    _invalidate_cache()
    if target_title != "记忆索引":
        _refresh_index()
    if target_title != ROUTING_INDEX_TITLE:
        _refresh_routing_index()
    logger.info("RESTORE title=%s", target_title)
    return f"已恢复记忆: {target_title}（从 .archive/{target_path.name}）"

@_tool
def memory_heat_suggest() -> str:
    """Scan entries and suggest tier changes based on access_count and staleness."""
    now = datetime.now(timezone.utc)
    suggestions: list[str] = []
    stats: list[tuple[str, str, str, int, str, str]] = []

    for f in _active_md_files():
        try:
            meta, body = _load_memory(f)
        except Exception:
            continue
        title = _entry_title(meta, f)
        tier_val = meta.get("tier", "warm")
        reads = meta.get("access_count", 0)
        if not isinstance(reads, int):
            reads = 0
        updated_str = str(meta.get("updated", ""))[:10]
        created_str = str(meta.get("created", ""))[:10]
        source = str(meta.get("source", "")) or "?"
        stats.append((title, tier_val, source, reads, updated_str, created_str))

        updated_raw = meta.get("updated", "")
        days_since_update = _days_since_utc(updated_raw)

        score = _heat_score(reads, str(meta.get("updated", "")))
        if score < 0.1 and tier_val != "cold":
            suggestions.append("- [{0}] {1}->cold | heat_score={2:.2f}, {3}d 未更新".format(title, tier_val, score, days_since_update))
        elif score < 0.5 and tier_val == "hot":
            suggestions.append("- [{0}] hot->warm | heat_score={1:.2f}, {2}d 未更新".format(title, score, days_since_update))
        elif score > 3.0 and tier_val != "hot":
            suggestions.append("- [{0}] {1}->hot | heat_score={2:.2f}, 访问频繁".format(title, tier_val, score))

    lines = ["# 热度分析建议", ""]
    lines.append("共 {0} 条记忆\n".format(len(stats)))

    lines.append("## 按访问次数排序")
    for title, tier_val, source, reads, updated_str, created_str in sorted(stats, key=lambda x: -x[3]):
        score = _heat_score(reads, updated_str)
        lines.append("- [{0}] {1} | {2} | score={3:.1f}, {4} 次读取 | {5}".format(created_str, title, tier_val, score, reads, source))

    lines.append("")
    if suggestions:
        lines.append("## 建议调整")
        lines.extend(suggestions[:30])
    else:
        lines.append("## 建议调整")
        lines.append("- 暂无需要调整的条目")

    return "\n".join(lines)

@_tool
def memory_graph(title: str, limit: int = 10, include_all: bool = False) -> str:
    """Show which pages this entry links to and which pages link to it (backlinks).

    Backlinks are computed dynamically from note bodies; frontmatter `links`
    is no longer used. Default output is truncated to `limit`; pass
    include_all=true to print every link.
    """
    if include_all:
        limit = 10**9
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()

    target_path: Path | None = None
    target_meta: dict[str, Any] = {}
    target_body = ""
    for f, meta, body in all_entries:
        if _entry_title(meta, f) == title or f.stem == title:
            target_meta = meta
            target_path = f
            target_body = body
            break

    if not target_path:
        return '未找到标题为 "{}" 的记忆'.format(title)

    outlinks = _extract_wiki_links(target_body)
    outlinks = [link for link in outlinks if link != title]

    backlinks: list[str] = []
    for f, _meta, body in all_entries:
        if f == target_path:
            continue
        entry_title = _entry_title(_meta, f)
        if title in _extract_wiki_links(body):
            if entry_title not in backlinks:
                backlinks.append(entry_title)

    lines = ["# {} 的链接图谱".format(title), ""]
    lines.append("## 出链 ({} 条)".format(len(outlinks)))
    if not include_all:
        lines.append("（默认显示前 {} 条；传 include_all=true 查看全部）".format(limit))
    for link in sorted(outlinks)[:limit]:
        lines.append("- [[{}]]".format(link))
    lines.append("")
    lines.append("## 反向链接 ({} 条)".format(len(backlinks)))
    if not include_all:
        lines.append("（默认显示前 {} 条；传 include_all=true 查看全部）".format(limit))
    for link in sorted(backlinks)[:limit]:
        lines.append("- [[{}]]".format(link))

    return "\n".join(lines)

@_tool
def memory_orphans() -> str:
    """Find entries with zero backlinks (isolated notes)."""
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()

    backlink_index: dict[str, list[str]] = {}
    title_map: dict[str, tuple[Path, dict[str, Any], str]] = {}

    for f, meta, body in all_entries:
        t = _entry_title(meta, f)
        title_map[t] = (f, meta, body)
        if t not in backlink_index:
            backlink_index[t] = []
        body_links = _extract_wiki_links(body)
        for link in body_links:
            if link != t:
                backlink_index.setdefault(link, [])
                if t not in backlink_index[link]:
                    backlink_index[link].append(t)

    core_pages = {"记忆索引", "近期工作动态", "用户画像", "AI身份档案",
                  "Codex 身份档案", "记忆库总规范", "共享记忆库规则"}
    orphans: list[str] = []
    for t in sorted(title_map.keys()):
        if t in core_pages:
            continue
        if not backlink_index.get(t):
            orphans.append(t)

    lines = ["# 孤立笔记 ({} 条)".format(len(orphans)), "",
             "以下笔记没有其他笔记引用它们：", ""]
    if orphans:
        for title in orphans:
            lines.append("- [[{}]]".format(title))
    else:
        lines.append("- (无孤立笔记)")

    return "\n".join(lines)

@_tool
@_locked_write
def memory_batch_tag(old_tag: str, new_tag: str) -> str:
    """Rename a tag across all entries."""
    _invalidate_cache()
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()
    updated: list[str] = []

    for f, meta, body in all_entries:
        tags = _entry_tags(meta)
        if old_tag not in tags:
            continue
        new_tags = [new_tag if t == old_tag else t for t in tags]
        title = _entry_title(meta, f)
        _write_memory(
            f,
            title=title,
            tags=new_tags,
            source=meta.get("source"),
            content=body,
            created=str(meta.get("created", "")),
            summary=meta.get("summary"),
            tier=meta.get("tier", "warm"),
            access_count=meta.get("access_count", 0),
        )
        updated.append(title)

    if updated:
        logger.info("BATCH_TAG %s->%s (%d entries)", old_tag, new_tag, len(updated))
        return "已更新 {} 条记忆的标签: {} -> {}\n- ".format(len(updated), old_tag, new_tag) + "\n- ".join(updated)
    return '未找到包含标签 "{}" 的记忆'.format(old_tag)

@_tool
@_locked_write
def memory_batch_tier(target_tier: str, min_score: float = 0.0, max_score: float | None = None) -> str:
    """Batch-set tier based on heat score range."""
    _invalidate_cache()
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()
    updated: list[str] = []

    for f, meta, body in all_entries:
        reads = meta.get("access_count", 0)
        if not isinstance(reads, int):
            reads = 0
        updated_str = str(meta.get("updated", ""))
        score = _heat_score(reads, updated_str)
        if min_score is not None and score < min_score:
            continue
        if max_score is not None and score > max_score:
            continue
        title = _entry_title(meta, f)
        _write_memory(
            f,
            title=title,
            tags=_entry_tags(meta),
            source=meta.get("source"),
            content=body,
            created=str(meta.get("created", "")),
            summary=meta.get("summary"),
            tier=target_tier,
            access_count=meta.get("access_count", 0),
        )
        updated.append("{} (score={:.1f})".format(title, score))

    if updated:
        logger.info("BATCH_TIER target=%s count=%d", target_tier, len(updated))
        return "已调整 {} 条记忆为 tier={}:\n- ".format(len(updated), target_tier) + "\n- ".join(updated)
    return "没有符合筛选条件的记忆"

@_tool
@_locked_write
def memory_archive_old(days: int = 90) -> str:
    """Archive entries not updated in N days."""
    _invalidate_cache()
    core_pages = {"记忆索引", "近期工作动态", "用户画像", "AI身份档案", "Codex 身份档案",
                  "Claude Code 身份档案", "记忆库总规范", "共享记忆库规则", "记忆半自动整理流程",
                  "AI交互配置", "AI 对话自动归档提示词"}
    now = datetime.now(timezone.utc)
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()
    archived: list[str] = []

    for f, meta, body in all_entries:
        title = _entry_title(meta, f)
        if title in core_pages:
            continue
        if meta.get("tier") == "cold":
            continue
        updated_raw = meta.get("updated", "")
        updated_dt = _parse_dt_utc(updated_raw)
        if updated_dt is None:
            continue
        if (datetime.now(timezone.utc) - updated_dt).days < days:
            continue
        result = memory_archive(title)
        archived.append("{} -> {}".format(title, result))

    if archived:
        logger.info("ARCHIVE_OLD days=%d count=%d", days, len(archived))
        return "已归档以下记忆:\n- " + "\n- ".join(archived)
    return "没有超过 {} 天未更新的条目需要归档".format(days)

# C 方案（2026-09-14）：零结果兜底最多返回多少条。兜底是 OR 语义，尾巴上那批
# 4.0 分（纯正文蹭到 2 个 token）的条目基本是噪声，不值得占用户的注意力。
_FALLBACK_LIMIT = 5


def _tokenize_query(q: str) -> list[str]:
    """检索分词：英文/数字整词保留，连续 CJK 切 2-gram（P2-11）。"""
    # English/number words stay whole; contiguous CJK runs are split into
    # 2-grams so a whole Chinese phrase matches relevant fragments.
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", (q or "").lower()):
        if len(tok) > 2 and re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok)
    return out


def _smart_rank(query: str, tag: str | None = None
                ) -> tuple[list[tuple[float, str, str, str, str]], list[str]]:
    """分词 + 四字段加权 + 时间衰减的排序核心，返回 (全部命中, 命中词表)。

    2026-09-14（E1）从 memory_smart_search 提升为模块级：memory_search 的「零结果
    兜底」要复用同一套打分，且检索日志需要真实命中数（此前只能解析格式化后的字符串）。

    2026-09-14（C 方案）改为返回二元组：第二个值是**最终保留条目上真正命中的 query
    token**。兜底的 OR 语义在中文 2-gram 下会假阳性（「不存在的组合」拆出「存在」
    「组合」这两个库内高频词），光靠分数挡不住——六组阈值实验都无法同时保住真查询、
    清掉噪声（噪声最高 20.0 与真查询的 12.0/15.0 区间重叠）。把命中词摊开给用户看，
    让人一眼判断这是弱匹配，比调阈值可靠。
    """
    keywords = _tokenize_query(query)
    if not keywords:
        return [], []

    scored: list[tuple[float, str, str, str, str]] = []
    matched: set[str] = set()

    for f, meta, body in _iter_entries():
        title = _entry_title(meta, f)
        tags = _entry_tags(meta)
        summary = str(meta.get("summary", ""))
        tier = str(meta.get("tier", "warm"))
        source = str(meta.get("source", ""))
        updated_str = str(meta.get("updated", ""))

        if tag and tag not in tags:
            continue

        title_lower = title.lower()
        summary_lower = summary.lower()
        body_lower = body.lower()[:2000]
        tags_lower = {t.lower() for t in tags}

        score = 0.0
        strong_hits = 0  # 命中在 title/tags/summary 上的「不同 token」数（强信号）
        body_tokens = 0  # 命中在正文里的「不同 token」数（弱信号）

        row_hits: set[str] = set()

        for kw in keywords:
            strong = False
            if kw == title_lower:
                score += 10.0
                strong = True
            elif kw in title_lower:
                score += 5.0
                strong = True

            if kw in tags_lower:
                score += 4.0
                strong = True

            if kw in summary_lower:
                score += 3.0
                strong = True

            body_count = body_lower.count(kw)
            if body_count:
                body_tokens += 1
            score += min(body_count, 5) * 1.0

            if strong:
                strong_hits += 1
            if strong or body_count:
                row_hits.add(kw)

        # 精确性下限（2026-09-14 实测加）：兜底是「任一 token 命中即算」的 OR 语义，
        # 而 body 匹配是 [:2000] 子串计数——于是「完全不存在 热重载」这类查询会把
        # 仅在正文出现过一次「存在」的长笔记全捞回来（实测顶满 20 条上限）。下限规则：
        # 要么有标题/标签/摘要级的强命中，要么正文命中的**不同 token ≥ 2**；
        # 只在正文里蹭到一个 token 的条目不算命中。
        if strong_hits == 0 and body_tokens < 2:
            continue

        # 必须先有词面命中，再谈时间加分（2026-09-14 实测修复）：此前时间加分无条件
        # 叠加，于是任何 updated 是今天的笔记都白拿 +2.0、score 必然 > 0、被当成命中返回。
        # 该 bug 一直不可见——smart 检索在 core 模式下从未注册，没人调用；一旦被
        # memory_search 的零结果兜底接上，就会出现「查一个根本不存在的词，返回 12 条
        # 不相干笔记」（smoke 反向用例当场抓到）。改为时间只做已命中条目的排序加权。
        if score <= 0:
            continue

        if updated_str:
            updated_dt = _parse_dt_utc(updated_str)
            if updated_dt is not None:
                days_since = (datetime.now(timezone.utc) - updated_dt).days
                score += max(0, 2.0 - days_since * 0.02)

        # 只有通过精度下限、真的会被返回的条目，才算「命中了这个 token」——
        # 否则被丢掉的弱命中会把命中词表撑大，反而误导用户。
        matched |= row_hits
        scored.append((score, title, tier, summary[:80], source))

    scored.sort(key=lambda x: -x[0])
    return scored, sorted(matched)


def _format_smart_lines(scored: list[tuple[float, str, str, str, str]]) -> list[str]:
    """打分结果 → 展示行（与 memory_smart_search 原输出格式保持一致）。"""
    lines: list[str] = []
    for score, title, tier, summary, source in scored:
        lines.append("- [{}] **{}** (score={:.1f}, {})".format(tier, title, score, source))
        if summary:
            lines.append("  {}".format(summary))
    return lines


@_tool
def memory_smart_search(query: str, tag: str | None = None, limit: int = 10) -> str:
    """Multi-field scored search across titles, tags, summaries, and bodies."""
    if not _tokenize_query(query):
        return "查询词无效"
    scored, matched = _smart_rank(query, tag)
    # 检索日志（E2，2026-09-14）：smart 路径此前完全没有日志，导致「零结果率」这个
    # 唯一判据口径不全（只能在 memory_search 里被间接记到）。
    _log_search(query, len(scored), scored[0][1] if scored else "", tag, tool="smart")
    if not scored:
        return '未找到与 "{}" 相关的结果'.format(query)
    head = "共找到 {} 条相关记忆".format(len(scored))
    if matched:
        head += "（命中词：" + "、".join(matched) + "）"
    lines = ["# 搜索结果: {}".format(query), head, ""]
    lines.extend(_format_smart_lines(scored[:limit]))
    return "\n".join(lines)

@_tool
def memory_recent(days: int = 7, limit: int = 20) -> str:
    """List entries updated within the last N days, most recent first."""
    now = datetime.now(timezone.utc)
    recent: list[tuple[str, str, str]] = []
    for f in _active_md_files():
        try:
            meta, _ = _load_memory(f)
        except Exception:
            continue
        updated_str = str(meta.get("updated", ""))
        updated_dt = _parse_dt_utc(updated_str)
        if updated_dt is None:
            continue
        if (now - updated_dt).days <= days:
            title = _entry_title(meta, f)
            summary = str(meta.get("summary", ""))
            recent.append((updated_str, title, summary[:80]))
    if not recent:
        return f"近 {days} 天无更新"
    recent.sort(reverse=True)
    lines = [f"# 近 {days} 天更新（{len(recent)} 条）", ""]
    for updated_str, title, summary in recent[:limit]:
        lines.append(f"- {updated_str[:10]} [[{title}]]")
        if summary:
            lines.append(f"  {summary}")
    if len(recent) > limit:
        lines.append(f"\n... 共 {len(recent)} 条，显示前 {limit} 条")
    return "\n".join(lines)

# ────────────────────────── 只读查库（2026-09-17 新增）──────────────────────────
# 为什么加这个工具：
#   实测「数据库本身不省 token」—— 读全文 18,717 vs 从库读 18,391，仅差 2%，
#   因为正文原样存在库里。**真正省的是「按需取数的粒度」**：
#   只要摘要 201 tok（-99%）、只要计数 6 tok（-99%）。
#   而 md 文件读不出这种粒度 —— 要读就得整篇读。所以需要一个能只取
#   一列 / 一行 / 一个计数的入口。
#
#   与 脚本/qdb.py **共用同一套护栏实现**（后者是给运维与排错用的 CLI）：
#   两处各写一遍必然漂移，而护栏漂移一次就是安全洞，所以单一来源。
#
# 三道护栏：
#   ① mode=ro 引擎级只读 —— 实测绕过上层 SQL 检查直接写仍被拒
#      （attempt to write a readonly database）。比正则过滤关键字可靠。
#   ② 只允许单条 SELECT / WITH（双保险 + 防多语句）
#   ③ 超时 + 行数上限（防全表扫卡死、防一次拉回十万行）

_SQL_ALLOWED_HEAD = re.compile(r"^\s*(select|with)\b", re.I)
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.I,
)
_SQL_DEFAULT_MAX_ROWS = 200
_SQL_DEFAULT_TIMEOUT = 5.0
_SQL_CELL_WIDTH = 80


def sql_guard(sql: str) -> str:
    """校验 SQL 只读且单条，返回去掉尾分号的语句；不合法抛 ValueError。

    与 脚本/qdb.py 共用。改这里必须同步那边（反之亦然）。
    """
    s = (sql or "").strip()
    if not s:
        raise ValueError("SQL 为空")
    if not _SQL_ALLOWED_HEAD.match(s):
        raise ValueError("只允许 SELECT / WITH 开头的查询")
    m = _SQL_FORBIDDEN.search(s)
    if m:
        raise ValueError(f"含被禁止的关键字 {m.group(0)!r}（只读入口不接受写操作）")
    body = s.rstrip(";").strip()
    if ";" in body:
        raise ValueError("检测到多条语句（只允许单条查询）")
    return body


def index_db_path() -> "Path | None":
    """索引库路径；不存在返回 None。与 脚本/qdb.py 共用。"""
    p = MEMORY_DIR / ".workbuddy" / "index.sqlite3"
    return p if p.exists() else None


def _sql_cell(v: Any, width: int) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<BLOB {len(v)}B>"
    s = str(v).replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= width else s[: width - 1] + "…"


def run_readonly_sql(sql: str, max_rows: int = _SQL_DEFAULT_MAX_ROWS,
                     timeout: float = _SQL_DEFAULT_TIMEOUT) -> str:
    """执行只读查询并返回紧凑文本结果。护栏见本段开头注释。"""
    import sqlite3 as _sqlite3

    db = index_db_path()
    if db is None:
        return ("索引库不存在，无法查库。先跑 `脚本/sync_all.py` 重建索引，"
                "或改用 memory_search / memory_read 走 md 路径。")

    body = sql_guard(sql)
    if not re.search(r"\blimit\b", body, re.I):
        body = f"{body} LIMIT {_norm_int(max_rows, 'max_rows', default=_SQL_DEFAULT_MAX_ROWS, lo=1, hi=1000)}"

    # mode=ro：引擎级只读，写操作物理不可达
    conn = _sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        t0 = time.perf_counter()

        def _handler() -> int:
            return 1 if (time.perf_counter() - t0) > timeout else 0

        conn.set_progress_handler(_handler, 2000)
        cur = conn.execute(body)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    except _sqlite3.OperationalError as e:
        return f"查询失败：{e}"
    finally:
        conn.close()

    if not cols:
        return "（无结果集）"

    cells = [[_sql_cell(r[c], _SQL_CELL_WIDTH) for c in cols] for r in rows]
    widths = [len(c) for c in cols]
    for row in cells:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    widths = [min(w, _SQL_CELL_WIDTH) for w in widths]

    def _line(vals: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(vals)).rstrip()

    out = [_line(cols), "-+-".join("-" * w for w in widths)]
    out += [_line(r) for r in cells]
    elapsed = round((time.perf_counter() - t0) * 1000, 2)
    out.append("")
    out.append(f"（{len(rows)} 行 ｜ {elapsed} ms ｜ 只读连接）")
    return "\n".join(out)


@_tool(annotations=_ANN_RO)
def memory_sql(
    sql: str,
    max_rows: Annotated[
        int,
        Field(ge=1, le=1000, description="返回行数上限（SQL 自带 LIMIT 时不生效），默认 200，上限 1000"),
    ] = _SQL_DEFAULT_MAX_ROWS,
) -> str:
    """[检索·窄粒度] 在记忆索引上跑只读 SQL —— 只取片段，不拉整篇。

    Prefer this over memory_read when you only need a fragment: it can fetch a
    single column (summary), a count, or a compact list, instead of pulling a
    whole note. Measured: fetching one summary costs ~200 tokens versus ~36,600
    for reading the heaviest note in full.

    何时换别的：需要整篇正文 → memory_read；标题关键词都还不知道、要用自然语言
    探索 → memory_search。本工具是「只要数据不要文章」时的默认选择。

    Tables (business data):
      notes(title,stem,rel_path,dir,type,scope,tier,source,verified,tags,summary,
            created,updated,version,access_count,schema_version,chars,body_md,body_text)
      links(src,dst)  meta(k,v)

    （G3，2026-10-09：conversations / atoms / facts / schema_log / provenance 五张
    预留表实测 0 行、无写入方，已从本描述下架 —— 挂着只耗 token 还会诱导模型
    去查空表。功能启用前不再宣传；若查询这些表名仍可执行，返回 0 行。）

    Chinese full-text search uses the notes_fts table (CJK 2-gram pre-tokenised);
    query it with `SELECT rel_path FROM notes_fts WHERE notes_fts MATCH ?` where
    the term is space-joined 2-grams — usually easier to just use memory_search.

    Examples:
      SELECT summary FROM notes WHERE title='项目状态'
      SELECT COUNT(*) AS n FROM notes WHERE body_md LIKE '%热重载%'
      SELECT dir, title FROM notes ORDER BY dir, title
      SELECT name, COUNT(*) FROM links GROUP BY dst ORDER BY 2 DESC LIMIT 10

    Read-only by construction: the connection is opened with mode=ro (the engine
    rejects writes), only a single SELECT/WITH is accepted, and results are
    capped (default 200 rows, 5s timeout, cells truncated at 80 chars).
    """
    try:
        return run_readonly_sql(sql, max_rows=max_rows)
    except ValueError as e:
        return f"拒绝执行：{e}"


@_tool(annotations=_ANN_RO)
def memory_stats() -> str:
    """[体检] 库健康统计：条目数、tier 分布、零访问条目、孤儿页、未验证结论等。

    何时换别的：只想知道某一个具体数字（如条目总数、某目录下有几篇）→ 用
    memory_sql 更省。本工具是「看整体状况」，不是「查单点」。
    """
    all_entries: list[tuple[Path, dict[str, Any], str]] = _iter_entries()
    now = datetime.now(timezone.utc)

    total = len(all_entries)
    tier_count: dict[str, int] = {"hot": 0, "warm": 0, "cold": 0}
    tag_counter: Counter = Counter()
    reads_list: list[tuple[int, str]] = []
    zero_reads: list[str] = []
    orphan_count = 0
    total_links = 0
    verified_true = 0
    unverified_claims: list[str] = []
    recent_7d = 0
    recent_30d = 0
    recent_90d = 0

    for f, meta, body in all_entries:
        t = meta.get("tier", "warm")
        tier_count[t] = tier_count.get(t, 0) + 1
        tag_counter.update(_entry_tags(meta))
        reads = meta.get("access_count", 0)
        if not isinstance(reads, int):
            reads = 0
        reads_list.append((reads, _entry_title(meta, f)))
        if reads == 0:
            zero_reads.append(_entry_title(meta, f))

        # 双链口径修正（2026-09-10）：links 字段自 2026-09-06 起已不再持久化，
        # 旧代码读 meta["links"] 恒为 0——"总双向链接数: 0" 一直是假数据。
        # 改为统计正文显式 [[双链]]，与 memory_rebuild_links / memory_graph 口径一致。
        total_links += len(_extract_wiki_links(body))

        # verified 覆盖 + 待核实结论（2026-09-10）：库里凡带「待重启生效」「预期」
        # 类措辞又没标 verified 的结论，跨会话最容易被当成既成事实读走。
        title_now = _entry_title(meta, f)
        if meta.get("verified") is True:
            verified_true += 1
        elif title_now not in _VERIFIED_SKIP_TITLES and any(
            hint in body for hint in _UNVERIFIED_HINTS
        ):
            unverified_claims.append(title_now)

        updated_str = str(meta.get("updated", ""))
        updated_dt = _parse_dt_utc(updated_str)
        if updated_dt is not None:
            days = (now - updated_dt).days
            if days <= 7:
                recent_7d += 1
            if days <= 30:
                recent_30d += 1
            if days <= 90:
                recent_90d += 1

    # Build backlink index for orphan count
    backlinks: dict[str, set[str]] = {}
    for f, meta, body in all_entries:
        t = _entry_title(meta, f)
        body_links = _extract_wiki_links(body)
        for link in body_links:
            if link != t:
                backlinks.setdefault(link, set()).add(t)

    for f, meta, body in all_entries:
        t = _entry_title(meta, f)
        if t not in CORE_PAGES and t not in backlinks:
            orphan_count += 1

    reads_list.sort(key=lambda x: -x[0])
    top_tags = tag_counter.most_common(10)

    lines = ["# 记忆库健康度统计", ""]
    lines.append(f"## 概览")
    lines.append(f"- 总条目数: {total}")
    lines.append(f"- 活跃 hot: {tier_count.get('hot', 0)}")
    lines.append(f"- 常温 warm: {tier_count.get('warm', 0)}")
    lines.append(f"- 已归档 cold: {tier_count.get('cold', 0)}")
    lines.append(f"- 孤立笔记: {orphan_count}（无反向链接）")
    lines.append(f"- 正文双链数: {total_links}（按正文 [[双链]] 统计，含重复指向）")
    verified_pct = round(verified_true / total * 100, 1) if total else 0.0
    lines.append(f"- verified 已确认: {verified_true} / {total} 条（{verified_pct}%）")
    lines.append("")
    lines.append(f"## 访问频率")
    lines.append(f"- 从未读取: {len(zero_reads)} 条")
    for i, (r, title) in enumerate(reads_list[:10]):
        lines.append(f"  {i+1}. [[{title}]] — {r} 次")
    lines.append("")
    lines.append(f"## 活跃度")
    lines.append(f"- 近 7 天更新: {recent_7d} 条")
    lines.append(f"- 近 30 天更新: {recent_30d} 条")
    lines.append(f"- 近 90 天更新: {recent_90d} 条")
    lines.append(f"- 超 90 天未更新: {total - recent_90d} 条")
    lines.append("")
    lines.append(f"## 热门标签 TOP 10")
    for tag, count in top_tags:
        lines.append(f"- {tag}: {count} 条")
    if zero_reads:
        lines.append(f"\n## 未读取条目（{len(zero_reads)} 条）")
        for title in zero_reads:
            lines.append(f"- [[{title}]]")
    if unverified_claims:
        lines.append(f"\n## 待核实结论（{len(unverified_claims)} 条，含未验证措辞且未标 verified）")
        for title in unverified_claims:
            lines.append(f"- [[{title}]]")

    return "\n".join(lines)

__all__ = [name for name in globals() if name.startswith('memory_')]
