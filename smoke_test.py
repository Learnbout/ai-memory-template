"""Smoke + regression checks for the ai-memory MCP server.

运行： python smoke_test.py
在临时库上执行，不接触真实记忆库。

覆盖：
1. core 模式 4 个工具全部异步注册、可调用
2. 6 路并发写/读
3. stats 返回健康度（曾因模块拆分漏搬 WIKI_LINK_RE 而 NameError）
4. 回归：外部改动后读写一致（跨进程缓存失效，2026-09-10 P0 修复点）
5. 回归：并发写同一标题 + expected_version 冲突拦截
6. 回归：锁文件残留自动恢复
7. 模块边界断言（依赖方向 + 无循环 import）
8. core 工具面齐全断言（E4）+ 多词查询的零结果兜底（E1，2026-09-14）
9. 回归：兜底的两道闸——命中词自述 + 限流 top N（C 方案，2026-09-14）
10. 回归：主路径按 updated 倒序，兑现 docstring 的 recent-first（F1，2026-09-16）
11. 回归：检索结果同主题折叠，且被折叠标题必须可见（F2，2026-09-16）
12. 回归：**二级**子目录外部改动同样可见（聚合页周拆分引入，2026-09-20）
"""

from __future__ import annotations

import asyncio
import ast
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REQUIRED_TOOLS = {"memory_search", "memory_read", "memory_write", "memory_stats"}


def _count_entries(stats_result: object) -> int:
    m = re.search(r"总条目数:\s*(\d+)", _result_text(stats_result))
    assert m, f"stats 输出缺少「总条目数」：{str(stats_result)[:200]}"
    return int(m.group(1))


def _result_text(res: object) -> str:
    """从 call_tool 结果取真实文本（2026-09-16 引入；G10 后续统一走 mcp_compat）。

    不能直接 `str(res)`：v1 的内容块列表 / v2 的 CallToolResult 走 repr 都会把
    换行渲染成字面 `\\n` —— 于是 `splitlines()` 只得到 1 行，所有
    `startswith("- [")` 的按行断言恒为空集而永远为真。第 8b 项「限流条数 ≤ N」
    就是这样空转的（`[] <= 5` 恒真），直到 8c 引入首条断言才暴露出来。
    v1/v2 的结果形态差异由 mcp_compat.extract_text 统一处理。
    """
    import mcp_compat

    return mcp_compat.extract_text(res)


def check_module_boundaries(root: Path) -> list[str]:
    """静态断言模块依赖方向：store <- runtime <- tools <- server，且无环。

    随着库变大，真正会腐化的是跨层乱引用，不是目录层数。四模块对单人库已经够。
    """
    order = ["memory_store", "memory_runtime", "memory_tools", "server"]
    graph: dict[str, set[str]] = {}
    for name in order:
        path = root / f"{name}.py"
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        graph[name] = imports & set(order)

    errors: list[str] = []
    # 依赖方向：底层不得反向依赖上层
    for low in ("memory_store",):
        for up in ("memory_runtime", "memory_tools", "server"):
            if up in graph.get(low, set()):
                errors.append(f"{low} 不应依赖 {up}（依赖方向应为 store <- runtime <- tools <- server）")
    for up in ("memory_tools", "memory_store"):
        if up in graph.get("memory_runtime", set()):
            errors.append(f"memory_runtime 不应依赖 {up}")

    # 环检测
    state: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        if state.get(node) == 1:
            errors.append("检测到循环 import：" + " -> ".join(stack + [node]))
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in sorted(graph.get(node, set())):
            visit(nxt, stack + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])
    return errors


async def _main(vault: Path) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    # 2026-09-11 修复：模块拆分后 FastMCP 实例在 memory_runtime.mcp，
    # server 只剩 `main` 与 `from memory_tools import *`（后者 __all__ 只导出
    # memory_* 前缀，故 server.mcp 不存在）。此前这里一直指向 server.mcp，
    # 使 7 个回归用例整体失效（实测 AttributeError）。
    import memory_runtime
    import memory_tools  # noqa: F401  工具的注册发生在导入期（@_tool 装饰器），
    #                     只导入 memory_runtime 会得到 0 个工具（实测 AssertionError: []）

    tools = memory_runtime.mcp._tool_manager.list_tools()
    tool_names = {tool.name for tool in tools}
    # E4（2026-09-14）：此前只断言「工具数 == 4」。数量相等既挡不住「某个能力被配置
    # 悄悄关掉」（当天实测：core 白名单漏掉 memory_smart_search，AI 根本调不到它，而
    # 所有回归照样全绿），也挡不住「多注册了不该有的东西」。改为断言必需集合齐全——
    # 声明了就必须存在（对齐 MemPalace tests/_backend_conformance.py 的做法）。
    missing = REQUIRED_TOOLS - tool_names
    assert not missing, f"core 模式下缺少必需工具：{sorted(missing)}（实际注册 {sorted(tool_names)}）"
    assert len(tool_names) == len(tools), f"工具重名：{[t.name for t in tools]}"
    # G10（2026-10-09，v2 迁移）：v1 时代这里断言「全部工具 is_async」——当时同步
    # 工具靠我们的 async_tool 包装才有 async 外壳，漏包会阻塞事件循环。v2 的
    # func_metadata.call_fn 原生把同步工具丢 anyio.to_thread（func_metadata.py:164），
    # 注册对象本身就是同步函数（is_async=False），该保证移交 SDK。改为断言：
    # 每个工具要么 async，要么带可调用体（同步，SDK 托管线程池）。
    for tool in tools:
        assert tool.is_async or callable(tool.fn), f"工具 {tool.name} 既非 async 也无可调用体"

    async def call(name: str, **kwargs):
        return await memory_runtime.mcp.call_tool(name, kwargs)

    async def text_of(name: str, **kwargs) -> str:
        """调工具并取回**真实文本**（2026-09-16 修；G10 2026-10-09 适配 v2）。

        不能直接 `str(await call(...))`：v1 的元组与 v2 的 CallToolResult 走 repr
        都会把换行渲染成字面 `\\n` —— 于是 `splitlines()` 只得到 1 行，所有
        `startswith("- [")` 的按行断言恒为空集而永远为真。第 8b 项「限流条数 ≤ N」
        就是这样空转的（`[] <= 5` 恒真），直到 8c 引入首条断言才暴露出来。
        v2 走 _result_text 先抽 TextContent.text。
        """
        res = await call(name, **kwargs)
        return _result_text(res)

    async def write(index: int):
        return await call("memory_write", title=f"smoke_{index}", content=f"# smoke {index}", source="smoke_test")

    async def read(index: int):
        return await call("memory_read", title=f"smoke_{index}")

    # ── 1/2. 并发写读 ────────────────────────────────────────────────────
    writes = await asyncio.gather(*(write(index) for index in range(6)))
    reads = await asyncio.gather(*(read(index) for index in range(6)))
    assert all(writes)
    assert all(reads)
    assert len(list(vault.rglob("smoke_*.md"))) == 6

    # ── 3. stats 健康度（曾因漏搬 WIKI_LINK_RE 而 NameError）──────────────
    stats_text = await text_of("memory_stats")
    assert stats_text and "总条目数" in stats_text, stats_text[:200]

    # ── 4. 回归：外部改动后读写一致（跨进程缓存失效 P0）──────────────────
    # 关键：必须先把子目录建好并预热缓存，再往里面写文件。
    # 否则「新建目录」这一步本身就会改库根 mtime，旧实现（只比库根）也能碰巧通过，
    # 用例就测不出缺陷了（本用例第一版正是栽在这里，破坏代码后仍然全绿）。
    import memory_store as ms

    sub = vault / "项目"
    sub.mkdir(parents=True, exist_ok=True)
    await call("memory_stats")          # 让 项目/ 目录先存在
    ms._invalidate_cache()              # 清掉上一步建立的缓存
    before = _count_entries(await call("memory_stats"))  # 预热：此刻树戳成为基准

    probe = sub / "外部改动探针.md"
    ms._atomic_write_text(
        probe,
        "---\ntitle: 外部改动探针\ntags: [probe]\ntier: warm\n---\n\n由外部进程写入\n",
    )
    after = _count_entries(await call("memory_stats"))
    assert after == before + 1, (
        f"子目录外部改动未被感知（跨进程缓存失效回归）：{before} -> {after}"
    )
    got = await text_of("memory_read", title="外部改动探针")
    assert "由外部进程写入" in got, got[:200]
    ms._invalidate_cache()

    # ── 12. 回归：**二级**子目录外部改动同样可见（2026-09-20）──────────────
    # 背景：聚合页周拆分把归档挪进 `日志/2026-09/` 这类二级目录。旧判据只累加
    # **一级**子目录 mtime，二级目录改动完全不在判据内 → 新文件静默不可见
    # （实测 `_iter_entries()` 报 80 而实际 81）。
    #
    # 与上面用例 4 的关键差别：探针必须写在**二级**目录里。用例 4 只写到 `项目/`，
    # 一级目录 mtime 变化旧实现也能察觉 —— 所以它是绿的，测不出这个缺陷。
    #
    # 另：本机实测**目录 mtime 读取滞后**（写文件后立刻读父目录 mtime 值不变），
    # 故判据已从「目录 mtime」改为「全树 .md 文件 mtime 之和」。
    nested = vault / "日志" / "_smoke_nested"
    nested.mkdir(parents=True, exist_ok=True)
    await call("memory_stats")
    ms._invalidate_cache()
    n_before = _count_entries(await call("memory_stats"))

    n_probe = nested / "二级外部改动探针.md"
    ms._atomic_write_text(
        n_probe,
        "---\ntitle: 二级外部改动探针\ntags: [probe]\ntier: warm\n---\n\n二级目录外部写入\n",
    )
    n_after = _count_entries(await call("memory_stats"))
    assert n_after == n_before + 1, (
        f"二级子目录外部改动未被感知（缓存失效判据退化为只看一级）：{n_before} -> {n_after}"
    )
    n_got = await text_of("memory_read", title="二级外部改动探针")
    assert "二级目录外部写入" in n_got, n_got[:200]
    # 清理探针与临时目录（rmdir 只在空目录上成功，失败即说明有残留）
    try:
        n_probe.unlink()
    except OSError:
        pass
    try:
        nested.rmdir()
    except OSError:
        pass
    ms._invalidate_cache()

    # ── 5. 回归：并发写同一标题 + expected_version 冲突拦截 ───────────────
    await call("memory_write", title="并发基线", content="# v1", source="smoke_test")
    await asyncio.gather(
        *(call("memory_write", title="并发基线", content=f"# v{i}", source="smoke_test") for i in range(2, 6))
    )
    same_title_files = [p for p in vault.rglob("并发基线*.md")]
    assert len(same_title_files) == 1, [str(p) for p in same_title_files]
    # version 已被并发写推高，用一个过时版本号写入必须被拒
    conflict = await text_of(
        "memory_write", title="并发基线", content="# 陈旧写入", expected_version=1, source="smoke_test"
    )
    assert "版本冲突" in conflict, conflict[:300]
    # 正文不应被陈旧写入覆盖
    latest = await text_of("memory_read", title="并发基线")
    assert "陈旧写入" not in latest, "乐观锁失效：陈旧写入覆盖了较新内容"

    # ── 6. 回归：锁文件残留自动恢复 ─────────────────────────────────────
    lock = vault / ".memory.lock"
    lock.write_text('{"pid": 999999, "time": %f}' % (time.time() - 3600), encoding="utf-8")
    revived = await text_of("memory_write", title="锁残留恢复", content="# ok", source="smoke_test")
    assert "已创建记忆" in revived or "已更新记忆" in revived, revived[:300]
    assert not lock.exists(), "超时锁文件未被清理，后续写入会被永久阻塞"

    # ── 8. 回归：多词查询的零结果兜底（E1，2026-09-14）─────────────────
    # memory_search 是单串子串匹配，带空格的词串必然零命中；兜底会转交分词加权打分。
    # 这里写一条「只有分词后才命中」的笔记：标题含「重载」，正文含「探针」，
    # 而查询串「重载 探针」含空格，子串匹配注定找不到 → 必须由兜底捞回来。
    await call("memory_write", title="词形探针重载", content="# 记录\n\n用于验证分词兜底",
               source="smoke_test")
    ms._invalidate_cache()
    multi = await text_of("memory_search", keyword="重载 探针")
    assert "未找到" not in multi, f"多词查询未触发分词兜底：{multi[:300]}"
    assert "词形探针重载" in multi, multi[:300]
    # 反向用例：真零结果仍须是零结果，避免兜底把不相干条目捞上来
    nope = await text_of("memory_search", keyword="zzz绝无此词zzz")
    assert "未找到" in nope, f"兜底把不相干条目捞上来了：{nope[:300]}"
    ms._invalidate_cache()

    # ── 8b. 回归：兜底的两道闸（C 方案，2026-09-14）───────────────────────
    # 分词兜底本质是 OR 语义，必然带来噪声：实测「blorptastic 不存在的组合」这类
    # 纯编造短语会命中若干库内高频词条目。阈值实验（T1~T5 六种规则）证明切不开——
    # 噪声最高分 20.0 与真查询的 12.0/15.0 区间重叠，任何分数阈值都会同时误杀真查询
    # 或漏放噪声。所以不调阈值，改走透明化 + 限流两道闸：
    #   ① 输出必须自述「命中词」，让人一眼看出这是弱匹配（而不是被当成精确结果）；
    #   ② 输出条数必须收敛到 _FALLBACK_LIMIT，不把低分蹭词条目全倒出来。
    # 本用例构造 8 条只含「限流/探针」的样本，确保命中数必然超过限流阈值，
    # 否则闸② 永远不会被触发，用例就成了摆设（与第 4 项同一个坑）。
    # 注意必须带 allow_duplicate=True：这批标题共用中文前缀「限流探针」，会被
    # memory_write 的同主题拦截判为重复而拒绝落盘（第一版正是栽在这里——只写进
    # 第 0 条，命中数 5 条刚好压在阈值上，断言假绿/假红都会发生）。
    import memory_tools as mt

    for i in range(8):
        await call(
            "memory_write",
            title=f"限流探针{i}",
            content=f"# 记录\n\n限流验证样本 {i}",
            source="smoke_test",
            allow_duplicate=True,
        )
    ms._invalidate_cache()
    capped = await text_of("memory_search", keyword="限流 探针")
    assert "命中词" in capped, f"兜底未自述命中词（C 方案闸①失效）：{capped[:300]}"
    entry_lines = [ln for ln in capped.splitlines() if ln.startswith("- [")]
    assert len(entry_lines) <= mt._FALLBACK_LIMIT, (
        f"兜底未限流到 {mt._FALLBACK_LIMIT} 条（C 方案闸②失效）：实际 {len(entry_lines)} 条"
    )
    m_total = re.search(r"共 (\d+) 条", capped)
    assert m_total and int(m_total.group(1)) > mt._FALLBACK_LIMIT, (
        f"用例未构造出超过 {mt._FALLBACK_LIMIT} 条命中，限流分支没被真正执行：{capped[:200]}"
    )
    assert "显示前" in capped, f"限流后未提示「显示前 N 条」：{capped[:300]}"
    ms._invalidate_cache()

    # ── 8c. 回归：主路径按 updated 倒序（F1，2026-09-16）─────────────────
    # 改造前的结果顺序完全等于 _active_md_files() 的路径倒序：库根文件恒排在子目录
    # 笔记之前、被 limit 截掉的永远是「路径序靠后」的那批，最新更新的笔记可能整个
    # 不出现，而 docstring 写着 recent-first。这里刻意让「文件名字典序」与「updated
    # 倒序」相反（甲最新文件名最靠前、乙最新却排中间），使排序一旦回退必然变红 ——
    # 否则用例可能假绿（与第 4 项同一个坑）。
    for title, updated in (
        ("甲序探针更新时间", "2026-01-01T00:00:00+00:00"),
        ("乙序探针更新时间", "2026-09-01T00:00:00+00:00"),
        ("丙序探针更新时间", "2025-06-01T00:00:00+00:00"),
    ):
        ms._atomic_write_text(
            vault / f"{title}.md",
            '---\ntitle: %s\ntags: [probe]\nupdated: "%s"\ntier: warm\n---\n\n序号探针内容\n'
            % (title, updated),
        )
    ms._invalidate_cache()
    ranked = await text_of("memory_search", keyword="序探针更新时间")
    first_line = next((ln for ln in ranked.splitlines() if ln.startswith("- [")), "")
    assert "乙序探针更新时间" in first_line, (
        f"主路径未按 updated 倒序（F1 失效）：首条应为最新的「乙序探针更新时间」，实际 {first_line[:80]}"
    )
    ms._invalidate_cache()

    # ── 8d. 回归：同主题折叠，且折叠项标题必须可见（F2，2026-09-16）───────
    # 折叠的硬约束是「不许静默吞掉」——被折叠条目的标题必须出现在输出里，否则用户会
    # 误判「库里没有」，比冗余本身更糟。故本用例同时断言：主条目收敛到 1 条、
    # 折叠说明存在、**被折叠的两个标题都出现**、以及三篇文件一个都没少（没动数据）。
    for title, updated in (
        ("折叠探针甲", "2026-02-01T00:00:00+00:00"),
        ("折叠探针乙", "2026-08-01T00:00:00+00:00"),
        ("折叠探针丙", "2026-05-01T00:00:00+00:00"),
    ):
        ms._atomic_write_text(
            vault / f"{title}.md",
            '---\ntitle: %s\ntags: [probe]\nupdated: "%s"\ntier: warm\n---\n\n折叠探针内容\n'
            % (title, updated),
        )
    ms._invalidate_cache()
    folded = await text_of("memory_search", keyword="折叠探针")
    folded_lines = [ln for ln in folded.splitlines() if ln.startswith("- [")]
    assert len(folded_lines) == 1, f"同主题未折叠（F2 失效）：{folded[:300]}"
    assert "2 条同主题已折叠" in folded, f"折叠计数缺失：{folded[:300]}"
    assert "折叠探针乙" in folded_lines[0], f"折叠组未保留 updated 最新的一条：{folded_lines[0][:80]}"
    for title in ("折叠探针甲", "折叠探针丙"):
        assert f"`{title}`" in folded, f"被折叠条目标题未出现在输出里（静默吞掉）：{folded[:300]}"
    assert len(list(vault.glob("折叠探针*.md"))) == 3, "折叠不应改动任何文件"
    ms._invalidate_cache()

    # ── 8e. 回归：标题命中优先于纯时间序（2026-10-09，F1 强化）────────────
    # 加一条 updated 最新但**标题不含关键词**、仅正文命中的笔记（丁），断言它不占首条；
    # 首条仍应是标题命中里最新的一条（乙）。若排序回退成纯时间序，丁会抢首条 → 变红。
    ms._atomic_write_text(
        vault / "丁序探针正文命中.md",
        '---\ntitle: 丁序探针正文命中\ntags: [probe]\nupdated: "2026-12-01T00:00:00+00:00"\ntier: warm\n---\n\n正文里出现序探针更新时间这个词\n',
    )
    ms._invalidate_cache()
    ranked_t = await text_of("memory_search", keyword="序探针更新时间")
    first_line_t = next((ln for ln in ranked_t.splitlines() if ln.startswith("- [")), "")
    assert "乙序探针更新时间" in first_line_t and "丁序探针" not in first_line_t, (
        f"标题命中未优先于纯时间序（F1 强化失效）：首条应为标题命中的「乙序探针更新时间」，实际 {first_line_t[:80]}"
    )
    ms._invalidate_cache()

    # ── 8f. 回归：正文分流（2026-10-09，甲案）────────────────────────────
    # 「错误路径不会回来」：整串查询词不在任何标题里、但有候选的标题命中其**子 token**
    # 时，必须转分词打分，且强信号（标题命中）条目排第一 —— 而不是被"仅正文命中"的
    # 新笔记按时间序抢先。若分流回退成纯精确路径，首条会是仅正文命中的「无关条目」→ 变红。
    # 注：探针必须让目标在**标题**上命中子 token（如「备份」），否则会撞上 _smart_rank 的
    # 精度下限（strong_hits==0 且 body_tokens<2 直接丢弃），探针本身失效。
    ms._atomic_write_text(
        vault / "备份专题.md",
        '---\ntitle: 备份专题\ntags: [probe]\nupdated: "2026-06-01T00:00:00+00:00"\ntier: warm\n---\n\n备份相关说明\n',
    )
    ms._atomic_write_text(
        vault / "无关条目.md",
        '---\ntitle: 无关条目\ntags: [probe]\nupdated: "2026-11-01T00:00:00+00:00"\ntier: warm\n---\n\n这里写着备份机制四个字\n',
    )
    ms._invalidate_cache()
    split = await text_of("memory_search", keyword="备份机制")
    split_first = next((ln for ln in split.splitlines() if ln.startswith("- [")), "")
    assert "转分词" in split, f"仅正文命中未走分词分流（甲案失效）：{split[:200]}"
    assert "备份专题" in split_first, f"分流后未把标题强命中的排首位：{split_first[:100]}"
    ms._invalidate_cache()

    # ── 7. 模块边界断言 ─────────────────────────────────────────────────
    boundary_errors = check_module_boundaries(Path(__file__).parent)
    assert not boundary_errors, boundary_errors

    print(
        "smoke ok: core 工具面齐全 | 6 并发写读 | stats | 外部改动可见 | "
        "乐观锁拦截 | 锁残留恢复 | 分词兜底 | 兜底双闸（命中词+限流） | "
        "updated 倒序（F1） | 标题命中优先（F1 强化） | 正文分流（甲案） | 同主题折叠且折叠项可见（F2） | 模块边界"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ai-memory-smoke-") as temp_dir:
        os.environ["AI_MEMORY_DIR"] = temp_dir
        os.environ["AI_MEMORY_CORE_TOOLS"] = "1"
        asyncio.run(_main(Path(temp_dir)))
