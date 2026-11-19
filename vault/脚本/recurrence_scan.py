#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recurrence_scan.py — 重复踩坑扫描 / 经验晋升候选（2026-10-08 新增）。

为什么需要它
------------
本库的「自我改进」闭环缺一段：踩坑 → 记下来 → **量化重复次数** → 到阈值强制晋升为
铁律 / 技能。前两段已经做实（笔记 + 规范铁律），第三段此前纯靠人肉回忆
（规范里两处明文写着「一天内连踩两次」「同型事故已是第三次」），第四段完全没有触发机制。

这个脚本补的正是中间那段：把已有的「铁律」当判据，量化它们各自被验证过多少次。

**判据选型的两次翻车（2026-10-08，都记在这里以免后人重走）**
------------------------------------------------------------
本脚本首版与二版分别试过两条路，**都失败**，最终落到第三版：

| 版本 | 判据 | 为什么失败 |
| --- | --- | --- |
| v1 | 扫**笔记正文**关键词 | **把「文档写得详细」误判成「问题严重」**。实测「死链」命中 26 篇，但事故实际只发生 3 次 —— 其余 23 篇是规范复述与事后记录。11/13 条全判候选，方向反了 |
| v2 | 扫**会话流水**的工具名 | **流水里没有脚本名**。实测 221 回合里只有 `Bash` 1374 次 / `Edit` 537 次这类**工具名**，脚本经 `Bash` 执行、命令内容不落流水。故所有候选复现数恒为 0，**另一头失效** |
| **v3（现行）** | 扫笔记里的**「踩坑自述句式」** | 库里真实踩坑记录有稳定特征：显式标注「日期 + 又踩了」 |

**v3 的核心洞察**：AI 写踩坑记录时，会留下带时间与复发语义的句子 ——
「2026-09-15 实测踩到」「已复犯两次」「同型事故已是第三次」「一天内连踩两次」。
**这些句子是现场报告，不是规范复述**，因此天然区分了「记录」与「复现」。
判据 = `(时间锚点 + 复发语义)` 同句共现，加**不同日期去重**，日期数 = 复现次数。

设计要点（对应本库已踩过的教训）
--------------------------------
1. **判据来自已有铁律，不新增标签体系**。上一版方案想在笔记正文塞 LRN 区块 + 手写
   pattern_key，问题是「没人写就没有数据」→ 脚本上线即空转（本库「仪表依赖人肉喂数据」
   的老毛病，见 hook 三次反向结论）。本版复用 AI 已经在写的踩坑自述句式，**零额外负担**。
2. **不写任何文件，纯只读**。避免成为第二个「聚合页整篇覆盖」事故源（铁律 10）。
3. **主判据必须能区分「记录」与「复现」**（v1 踩坑的直接产物，见上表）。

判据表（PATTERNS）怎么加
------------------------
每条 = (key, 名字, 主题正则, 对应铁律)。主题正则限定「在讲哪个坑」，
脚本再叠加通用复发句式与时间锚点判定。新踩一个坑并写进铁律后，**在这里补一条**。

用法
----
    python 脚本/recurrence_scan.py                 # 全库扫描，打印候选清单
    python 脚本/recurrence_scan.py --min-hits 2    # 自定义复现阈值
    python 脚本/recurrence_scan.py --json out.json # 输出机器可读结果
    python 脚本/recurrence_scan.py --detail         # 附命中明细

退出码：0 = 无晋升候选；1 = 有候选（便于接入自动化 / 报表）。
"""
from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 与 memory_store.SYSTEM_DIR_NAMES 保持一致：这些目录下的 md 不参与扫描
SKIP_DIRS = {".git", "__pycache__", ".trash", ".obsidian", ".workbuddy",
             ".archive", ".trash_backup", "备份归档"}

FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
CODE_RE = re.compile(r"`[^`\n]*`")
DATE_IN_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# 晋升阈值（对应 规则_记忆写入格式规范「经验晋升阈值」节）
PROMOTE_MIN_HITS = 3       # 命中次数
PROMOTE_MIN_PROJECTS = 2   # 跨项目数
PROMOTE_WINDOW_DAYS = 30   # 时间窗口

# ---------------------------------------------------------------------------
# 复发句式判据（v3 核心）
# ---------------------------------------------------------------------------
# 时间锚点：显式日期，或「实测 / 踩到 / 发生」类现场词
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 复发语义（必须与时间锚点**同句共现**才计数）
_RECUR_RE = re.compile(
    r"又踩|再踩|已踩|踩到|复犯|复发|重犯|重现|再次出现|再次发生|第二次|第三次|第四次"
    r"|连踩|同型|同源|同类事故|同一个坑|重复出现|反复|屡次|多次出现|又一次|再度")
# 序号型复发（「第二次踩坑」「已修两次」），可**不带日期**
_ORDINAL_RE = re.compile(r"(第[二三四五六七八九十]+次|两[次回]|三[次回]|多次|屡次)")

# 判据表：key / 中文名 / 主题正则（限定在讲哪个坑）/ 对应铁律
PATTERNS: list[tuple[str, str, list[str], str]] = [
    ("frontmatter_field",
     "frontmatter 字段 / summary 越界",
     [r"frontmatter", r"_clamp_summary", r"summary.{0,12}(400|超长|上限)", r"重复字段"],
     "铁律 4/8"),
    ("backtick_shell",
     "反引号经 shell 传参被吃掉",
     [r"反引号", r"命令替换", r"\$\(\)"],
     "铁律 16"),
    ("crlf_mismatch",
     "CRLF / LF 混用致正则失配",
     [r"crlf", r"换行风格", r"\\r.{0,8}失配", r"行尾"],
     "铁律 6/7"),
    ("obsidian_padding",
     "Obsidian 表格 padding 膨胀",
     [r"padding", r"列宽对齐", r"表格.{0,8}(膨胀|变胖)"],
     "铁律 12"),
    ("hook_registration",
     "hook 登记位置错 / 未生效",
     [r"hooks\.json", r"settings\.json.{0,10}hooks", r"接错插座", r"hook"],
     "hook 登记位置节"),
    ("transcript_format",
     "桌面端 transcript 格式与 CLI 不同",
     [r"transcript", r"function_call", r"延迟通道|DeferExecuteTool|延迟派发"],
     "桌面端 transcript 节"),
    ("cache_mtime",
     "缓存失效判据 / 目录 mtime",
     [r"缓存失效", r"目录 mtime", r"mtime.{0,10}之和", r"_tree_stamp"],
     "铁律 20"),
    ("aggregate_overwrite",
     "聚合页被整篇覆盖 / 丢条目",
     [r"整篇覆盖", r"upsert", r"聚合页.{0,8}(覆盖|丢)", r"丢.{0,6}条"],
     "铁律 10"),
    ("table_structure",
     "表格结构写坏（分隔线 / 空行）",
     [r"分隔线", r"lazy continuation", r"表格.{0,10}纯文本"],
     "铁律 11/12"),
    ("tool_silent_fallback",
     "工具参数非法值静默回落",
     [r"静默回落", r"静默失败", r"非法值", r"_norm_int"],
     "铁律 18/22"),
    ("dead_link",
     "双链死链 / 空壳文件",
     [r"死链", r"0 字节", r"空壳"],
     "索引双链节"),
    ("sandbox_path",
     "本机 bash PATH 残缺",
     [r"command not found", r"portablegit", r"path.{0,8}(残缺|注入|自愈)"],
     "铁律 3"),
    ("mcp_reload",
     "热重载只换 worker，宿主不刷新",
     [r"热重载", r"supervisor", r"worker pid"],
     "改了工具描述节"),
    ("archive_split",
     "聚合页归档结构 / 周拆分",
     [r"周拆分", r"归档结构", r"split_weekly", r"月索引"],
     "铁律 24"),
]


def strip_noise(text: str) -> str:
    """剥掉代码块 / 行内代码，避免把语法示例重复计数。"""
    return CODE_RE.sub("", FENCE_RE.sub("", text))


def split_fm(text: str) -> str:
    """剥掉开头 YAML frontmatter，只留正文。

    **2026-10-08 踩坑**：首版正文扫描保留 frontmatter，导致 summary 里的日期口径被当命中，
    `--detail` 样本里抓出整段 YAML。故正文扫描前必须剥掉。
    """
    return FRONTMATTER_RE.sub("", text, count=1)


def collect(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".md"):
                files.append(Path(dirpath) / fn)
    return files


def file_date(path: Path, text: str) -> datetime | None:
    """推断笔记的时间：优先文件名里的日期，其次 frontmatter updated/created，最后 mtime。"""
    m = DATE_IN_NAME_RE.search(path.stem)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except ValueError:
            pass
    head = text[:2000]
    for key in ("updated", "created"):
        mm = re.search(rf'^{key}:\s*"?([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})', head, re.M)
        if mm:
            try:
                return datetime.strptime(mm.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def project_of(rel: Path) -> str:
    """取「来源」粒度用于跨来源判定。

    **2026-10-08 修正**：初版取「一级目录名」，库根文件一律归为「（库根）」——
    结果是**只要两个库根系统页提到同一个坑，就算「跨 2 个项目」**，跨项目门槛被轻易灌满，
    属假信号（实测：11/13 条全判候选，其中多数只靠库根文件满足）。
    现行口径：一级目录内的文件按目录名算；**库根文件按文件名各自独立**，不再合并成一个「（库根）」。
    """
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def read_flow(root: Path) -> list[dict]:
    """读会话流水。每行是一条回合事实，字段见 ai_memory_stop.sh 的输出。

    关键字段：
      - `tools`：本回合调用的工具名列表（延迟通道的是 ``DeferExecuteTool``，
        hook 已解一层 `arguments.toolName` 把真名计入，见规范「延迟通道」节）。
      - `cwd`：本回合工作目录，用于跨来源判定。
      - `ts`：ISO 时间戳（带时区）。
    """
    p = root / ".workbuddy" / "会话流水.jsonl"
    rows: list[dict] = []
    if not p.exists():
        return rows
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows


def flow_tool_names(row: dict) -> list[str]:
    """把一条流水里的 tools 归一成小写工具名列表（兼容 str / list 两种存储形态）。"""
    t = row.get("tools")
    if isinstance(t, str):
        # hook 早期可能写成 Python repr 形态 "[a, b]"；剥掉方括号与引号再切分
        t = t.strip().strip("[]")
        items = [x.strip().strip("'\"") for x in t.split(",")]
    elif isinstance(t, list):
        items = [str(x) for x in t]
    else:
        return []
    return [x.lower() for x in items if x]


def parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def extract_incidents(body: str, topic_regs: list[re.Pattern]) -> list[dict]:
    """从笔记正文里抽出「踩坑自述」句子。

    判据（同一句内）：
      ① 主题词（在讲哪个坑）
      ② 复发语义
      ③ 时间锚点 —— 两种路径任一即可：
         - **显式日期**（`2026-09-15 实测踩到`）→ 用该日期
         - **序号型**（`第二次踩坑` / `已修两次`）→ 记 `date=None`，由调用方按笔记自身日期兜底
    返回每次事件，含它归属的日期（用于**按日期去重** —— 同一天踩两次算一次「事件」，
    因为规范口径是「同型事故第 N 次」按天计）。
    """
    events: list[dict] = []
    # 按句切：中文句号 / 分号 / 换行 / 竖线（表格）
    for sent in re.split(r"[。；\n|]", body):
        s = sent.strip()
        if len(s) < 6:
            continue
        if not _RECUR_RE.search(s):
            continue
        hit_topics = [reg.pattern for reg in topic_regs if reg.search(s)]
        if not hit_topics:
            continue
        dm = _DATE_RE.search(s)
        events.append({"date": dm.group(0) if dm else None,
                       "text": s[:160],
                       "topic": hit_topics[0],
                       "ordinal": bool(_ORDINAL_RE.search(s))})
    return events


def scan(root: Path, min_hits: int) -> tuple[list[dict], dict]:
    files = collect(root)
    rows: dict[str, dict] = {}

    for key, name, topic_pats, law in PATTERNS:
        topic_regs = [re.compile(p, re.I) for p in topic_pats]
        all_events: list[dict] = []
        note_files: list[str] = []
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.strip():
                continue
            rel = path.relative_to(root)
            body = strip_noise(split_fm(raw))
            evs = extract_incidents(body, topic_regs)
            if not evs:
                continue
            note_files.append(str(rel))
            fallback = file_date(path, raw)
            fallback_s = fallback.strftime("%Y-%m-%d") if fallback else None
            for e in evs:
                e["rel"] = str(rel)
                e["date"] = e["date"] or fallback_s
                all_events.append(e)

        # 复现次数 = **不同日期**的个数（同日多次算一次）
        date_set = sorted({e["date"] for e in all_events if e["date"]})
        # 跨来源 = 不同一级目录数
        srcs = sorted({project_of(Path(f)) for f in note_files})
        latest = date_set[-1] if date_set else None
        recent = bool(latest and datetime.strptime(latest, "%Y-%m-%d").replace(
            tzinfo=timezone.utc) >= datetime.now(timezone.utc)
            - timedelta(days=PROMOTE_WINDOW_DAYS))
        promote = (len(date_set) >= min_hits and len(srcs) >= PROMOTE_MIN_PROJECTS
                   and recent)

        rows[key] = {
            "key": key, "name": name, "law": law,
            "occurs": len(date_set),
            "dates": date_set,
            "sources": srcs,
            "latest": latest or "-",
            "recent": recent,
            "note_files": len(note_files),
            "recorded": len(all_events),
            "promote": promote,
            "events": all_events,
        }

    results = list(rows.values())
    results.sort(key=lambda r: (not r["promote"], -r["occurs"], -r["note_files"]))
    return results, {"files": len(files), "patterns": len(PATTERNS)}


def render(results: list[dict], summary: dict, detail: bool, min_hits: int = PROMOTE_MIN_HITS) -> int:
    cand = [r for r in results if r["promote"]]
    print(f"扫描 .md 文件 {summary['files']} 篇 ｜ 判据 {summary['patterns']} 条 "
          f"｜ 晋升候选 {len(cand)} 条")
    # 阈值行必须打印**本次实际生效**的 min_hits，不能用常量默认值 ——
    # 否则 `--min-hits 2` 时表头显示 ≥3 而候选数按 2 算，自相矛盾（实测踩到）。
    note = "" if min_hits == PROMOTE_MIN_HITS else f"（含 --min-hits {min_hits} 覆盖）"
    print(f"晋升阈值：复现日期数 ≥ {min_hits}（同日算一次） 且 跨来源 ≥ "
          f"{PROMOTE_MIN_PROJECTS} 且 {PROMOTE_WINDOW_DAYS} 天内有复现{note}")
    print()

    if cand:
        print("★ 晋升候选（建议：改铁律 或 SkillManage 萃技能）")
        for r in cand:
            print(f"   [{r['law']}] {r['name']}")
            print(f"      复现 {r['occurs']} 次 ｜ 跨来源 {len(r['sources'])} "
                  f"（{'、'.join(r['sources'][:5])}）｜ 最近 {r['latest']} "
                  f"｜ 自述 {r['recorded']} 处 / {r['note_files']} 篇")
        print()

    rest = [r for r in results if not r["promote"]]
    if rest:
        print("· 未达阈值（继续观察）")
        for r in rest:
            why = []
            if r["occurs"] < PROMOTE_MIN_HITS:
                why.append(f"复现不足")
            if len(r["sources"]) < PROMOTE_MIN_PROJECTS:
                why.append(f"跨来源不足")
            if not r["recent"]:
                why.append("时效过期")
            # 阈值与实测值一并给出，避免「未达行」把关键数字藏起来
            print(f"   [{r['law']}] {r['name']} — 未达（{'、'.join(why)}）")
            print(f"      复现 {r['occurs']}/{PROMOTE_MIN_HITS} 次 ｜ 跨来源 "
                  f"{len(r['sources'])}/{PROMOTE_MIN_PROJECTS} ｜ 最近 {r['latest']} "
                  f"｜ 自述 {r['recorded']} 处 / {r['note_files']} 篇")
        print()

    if detail:
        print("明细（自述句＝显式提到「又踩了」的现场报告）：")
        for r in results:
            print(f"\n[{r['law']}] {r['name']}  —  复现日期：{'、'.join(r['dates']) or '无'}")
            seen = set()
            for e in r["events"][:12]:
                sig = (e["date"], e["rel"])
                if sig in seen:
                    continue
                seen.add(sig)
                print(f"   {e['date'] or '-'}  {e['rel']}")
                print(f"       … {e['text'][:120]}")

    if cand:
        print("提示：候选条目请走两条路径之一 ——")
        print("  ① 值得复用 → SkillManage 萃成技能；② 值得记牢 → 更新 规则_记忆写入格式规范 铁律。")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆库重复踩坑扫描 / 经验晋升候选（只读）")
    ap.add_argument("--root", default=os.environ.get(
        "AI_MEMORY_DIR") or str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("--detail", action="store_true", help="打印命中明细")
    ap.add_argument("--min-hits", type=int, default=PROMOTE_MIN_HITS,
                    help=f"复现次数阈值（默认 {PROMOTE_MIN_HITS}）")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"找不到记忆库目录: {root}", file=sys.stderr)
        return 2

    if args.min_hits < 1:
        print(f"--min-hits 必须 ≥ 1，收到 {args.min_hits}", file=sys.stderr)
        return 2

    results, summary = scan(root, args.min_hits)
    if args.json:
        payload = dict(summary=summary, thresholds=dict(
            min_hits=args.min_hits, min_sources=PROMOTE_MIN_PROJECTS,
            window_days=PROMOTE_WINDOW_DAYS), results=results)
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"已写出 JSON: {args.json}")
    return render(results, summary, args.detail, args.min_hits)


if __name__ == "__main__":
    sys.exit(main())
