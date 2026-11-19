"""记忆库体检报告生成器（只读，零第三方依赖）。

扫描记忆库全部 .md，生成单文件 HTML 体检报告，用于把记忆库的
token 成本、结构健康度与决策仪表可视化。

只读：不修改任何记忆文件，不写 frontmatter，不触发索引重建。

用法：
    python 脚本/memory_report.py                    # 输出 <库根>/记忆库体检报告.html
    python 脚本/memory_report.py -o D:/out.html     # 指定输出
    python 脚本/memory_report.py --vault D:/vault   # 指定记忆库

环境变量 AI_MEMORY_DIR 可覆盖默认记忆库路径。
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1])

EXCLUDE_DIRS = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__"}

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
LINK_RE = re.compile(r"\[\[([^\]\|#]+)")
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
FENCE_RE = re.compile(r"```.*?```", re.S)
CODE_RE = re.compile(r"`[^`\n]*`")
PREFIX_RE = re.compile(r"^(项目|知识|规则|想法|修复|日志|任务|工作动态)[_ ]")
DATE_RE = re.compile(r"[（(]\s*\d{4}[-–]\d{2}([-–]\d{2})?\s*[)）]\s*$")
BARE_DATE_RE = re.compile(r"[_ ]?\d{4}[-–]\d{2}([-–]\d{2})?$")

TOP_N = 12
DUP_RATIO = 0.6
PREFIX_HIT = 4
FM_FIELD_LINE = 300
BODY_LONG_LINE = 2000


def strip_code(text: str) -> str:
    """去掉围栏代码块与行内代码，避免把文档里演示用的 [[双链]] 当成真死链。"""
    return CODE_RE.sub("", FENCE_RE.sub("", text))


def core_of(stem: str) -> str:
    """取标题的「主题核心」：剥掉领域前缀与日期后缀，用于同主题判定。"""
    s = PREFIX_RE.sub("", stem)
    s = DATE_RE.sub("", s)
    s = BARE_DATE_RE.sub("", s)
    return s.strip(" _-")



def est_tokens(text: str) -> int:
    """粗略估算 token：CJK 字符约 0.7 token/字，其余约 4 字符/token。仅作相对比较用。"""
    cjk = len(CJK_RE.findall(text))
    return int(cjk * 0.7 + (len(text) - cjk) / 4)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    key = None
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.lstrip()[2:].strip().strip("\"'"))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip("\"'")
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip("\"'") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        elif not val:
            meta[key] = []
        else:
            meta[key] = val
    return meta, text[m.end():]


def scan(vault: Path) -> list[dict]:
    entries = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        title = str(meta.get("title") or path.stem)
        fm_match = FM_RE.match(text)
        fm_lines = fm_match.group(1).splitlines() if fm_match else []
        body_lines = text[fm_match.end():].splitlines() if fm_match else text.splitlines()
        fm_max = max((len(x) for x in fm_lines), default=0)
        body_max = max((len(x) for x in body_lines), default=0)
        try:
            access = int(meta.get("access_count") or 0)
        except (TypeError, ValueError):
            access = 0
        try:
            version = int(meta.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        entries.append({
            "title": title,
            "stem": path.stem,
            "core": core_of(path.stem),
            "rel": str(rel),
            "dir": str(rel.parent) if rel.parent != Path(".") else "（库根）",
            "body": body,
            "chars": len(text),
            "tokens": est_tokens(text),
            "body_tokens": est_tokens(body),
            "tier": str(meta.get("tier") or "-"),
            "source": str(meta.get("source") or "-"),
            "access": access,
            "version": version,
            "tags": tags,
            "has_summary": bool(meta.get("summary")),
            "summary_len": len(str(meta.get("summary") or "")),
            "verified": str(meta.get("verified") or "").lower(),
            "fm_max_line": fm_max,
            "body_max_line": body_max,
            "links": [x.strip() for x in LINK_RE.findall(strip_code(body))],
        })
    return entries


def find_pairs(entries: list[dict]) -> list[tuple[str, str, float]]:
    """按「主题核心」比对，避免领域前缀与日期后缀造成的高相似误报。

    两条命中规则取并集：核心相似度达标，或两核心共享 ≥4 字的前缀
    （如「关系链优化方案文档 / 关系链优化落地」这类同项目多个交付物）。
    """
    pairs = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i]["core"], entries[j]["core"]
            if not a or not b or a == b:
                continue
            r = SequenceMatcher(None, a, b).ratio()
            pref = len(os.path.commonprefix([a, b]))
            near_prefix = pref >= PREFIX_HIT and pref >= 0.5 * min(len(a), len(b))
            if r >= DUP_RATIO or near_prefix:
                pairs.append((entries[i]["stem"], entries[j]["stem"], r))
    return sorted(pairs, key=lambda x: -x[2])


def norm_title(s: str) -> str:
    return re.sub(r"[\s_]+", "", s).replace("（", "(").replace("）", ")")


def analyze(entries: list[dict]) -> dict:
    titles = {e["stem"] for e in entries} | {e["title"] for e in entries}
    norm_index = {norm_title(t): t for t in titles}
    for e in entries:
        missing = sorted({l for l in e["links"] if l not in titles})
        e["dead"] = [l for l in missing if norm_title(l) not in norm_index]
        e["near"] = sorted((l, norm_index[norm_title(l)]) for l in missing
                           if norm_title(l) in norm_index)

    by_dir: dict[str, dict] = defaultdict(lambda: {"n": 0, "chars": 0, "tokens": 0})
    for e in entries:
        d = by_dir[e["dir"]]
        d["n"] += 1
        d["chars"] += e["chars"]
        d["tokens"] += e["tokens"]

    linked = set()
    for e in entries:
        for l in e["links"]:
            linked.add(l)
    orphans = [e for e in entries if not e["links"] and not any(
        e["stem"] in o["links"] or e["title"] in o["links"] for o in entries)]
    dead_total = sum(len(e["dead"]) for e in entries)

    return {
        "total": len(entries),
        "chars": sum(e["chars"] for e in entries),
        "tokens": sum(e["tokens"] for e in entries),
        "by_dir": dict(sorted(by_dir.items(), key=lambda kv: -kv[1]["tokens"])),
        "by_tier": Counter(e["tier"] for e in entries),
        "by_source": Counter(e["source"] for e in entries),
        "no_summary": [e for e in entries if not e["has_summary"]],
        "cold": sorted([e for e in entries if e["access"] == 0 and e["tier"] != "archive"],
                       key=lambda e: -e["tokens"]),
        "unverified": [e for e in entries if e["verified"] not in ("true", "false")],
        "verified_false": [e for e in entries if e["verified"] == "false"],
        "orphans": orphans,
        "dead_total": dead_total,
        "dead_entries": [e for e in entries if e["dead"]],
        "near_entries": [e for e in entries if e.get("near")],
        "long_lines": sorted([e for e in entries
                              if e["fm_max_line"] > FM_FIELD_LINE or e["body_max_line"] > BODY_LONG_LINE],
                             key=lambda e: -max(e["fm_max_line"], e["body_max_line"])),
        "long_summary": sorted([e for e in entries if e["summary_len"] > 400],
                               key=lambda e: -e["summary_len"]),
        "pairs": find_pairs(entries),
        "top": sorted(entries, key=lambda e: -e["tokens"])[:TOP_N],
        "hot": sorted(entries, key=lambda e: -e["access"])[:TOP_N],
    }


def bar(value: int, maximum: int, color: str) -> str:
    pct = (value / maximum * 100) if maximum else 0
    return (f'<div class="bar"><div class="fill" style="width:{pct:.1f}%;background:{color}"></div></div>')


def esc(s) -> str:
    return html.escape(str(s))


def collect_recurrence(vault: Path) -> dict | None:
    """调用 recurrence_scan.py 取经验晋升候选（只读子进程，失败不影响体检）。

    **为什么用子进程而不是 import**：recurrence_scan.py 是独立可执行脚本，
    直接 import 会把它的 argparse/退出码语义耦合进体检器；子进程走 `--json`
    这个**正式机器接口**，接口稳定、失败可降级（返回 None 时报表照常出）。
    """
    script = vault / "脚本" / "recurrence_scan.py"
    if not script.exists():
        return None
    out = Path(tempfile.mktemp(suffix=".json"))
    try:
        r = subprocess.run([sys.executable, str(script), "--root", str(vault),
                            "--json", str(out)],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        if not out.exists():
            return None
        data = json.loads(out.read_text(encoding="utf-8"))
        data["_rc"] = r.returncode
        return data
    except Exception:
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def render(vault: Path, entries: list[dict], a: dict) -> str:
    C = {"cost": "#E24B4A", "size": "#378ADD", "cool": "#5DCAA5", "warn": "#EF9F27", "mute": "#888780"}
    max_tok = max((e["tokens"] for e in a["top"]), default=1)

    def rows_top():
        out = []
        for e in a["top"]:
            out.append(
                f'<tr><td class="mono">{esc(e["rel"])}</td><td class="num">{e["chars"]:,}</td>'
                f'<td class="num">{e["tokens"]:,}</td><td class="w">{bar(e["tokens"], max_tok, C["cost"])}</td></tr>')
        return "".join(out)

    max_dir = max((v["tokens"] for v in a["by_dir"].values()), default=1)
    def rows_dir() -> str:
        return "".join(
            f'<tr><td>{esc(k)}</td><td class="num">{v["n"]}</td><td class="num">{v["chars"]:,}</td>'
            f'<td class="num">{v["tokens"]:,}</td><td class="w">{bar(v["tokens"], max_dir, C["size"])}</td></tr>'
            for k, v in a["by_dir"].items())

    def chip_rows(counter: Counter) -> str:
        tot = sum(counter.values()) or 1
        return "".join(
            f'<div class="chip"><span class="k">{esc(k)}</span><span class="v">{v}</span>'
            f'<span class="p">{v / tot * 100:.0f}%</span></div>'
            for k, v in counter.most_common())

    rows_cold = "".join(
        f'<tr><td class="mono">{esc(e["rel"])}</td><td class="num">{e["tokens"]:,}</td>'
        f'<td>{esc(e["tier"])}</td><td>{esc(e["dir"])}</td></tr>' for e in a["cold"][:TOP_N]) or \
        '<tr><td colspan="4" class="ok">无零访问条目</td></tr>'

    rows_hot = "".join(
        f'<tr><td class="mono">{esc(e["rel"])}</td><td class="num">{e["access"]}</td>'
        f'<td class="num">{e["tokens"]:,}</td></tr>' for e in a["hot"][:TOP_N])

    rows_dup = "".join(
        f'<tr><td class="mono">{esc(x)}</td><td class="mono">{esc(y)}</td>'
        f'<td class="num">{r * 100:.0f}%</td></tr>' for x, y, r in a["pairs"][:TOP_N]) or \
        '<tr><td colspan="3" class="ok">未发现高相似主题对</td></tr>'

    rows_dead = "".join(
        f'<tr><td class="mono">{esc(e["rel"])}</td><td class="mono">{esc("、".join(e["dead"]))}</td></tr>'
        for e in a["dead_entries"][:TOP_N]) or '<tr><td colspan="2" class="ok">无真死链</td></tr>'

    rows_near = "".join(
        f'<tr><td class="mono">{esc(e["rel"])}</td><td class="mono">{esc(l)}</td>'
        f'<td class="mono ok">{esc(t)}</td></tr>'
        for e in a["near_entries"] for l, t in e["near"][:TOP_N]) or \
        '<tr><td colspan="3" class="ok">无双链笔误</td></tr>'

    rows_long = "".join(
        f'<tr><td class="mono">{esc(e["rel"])}</td><td class="num flag">{e["fm_max_line"]:,}</td>'
        f'<td class="num">{e["body_max_line"]:,}</td><td class="num">{e["summary_len"]:,}</td>'
        f'<td class="num">{e["chars"]:,}</td></tr>'
        for e in a["long_lines"][:TOP_N]) or \
        '<tr><td colspan="5" class="ok">无异常超长字段</td></tr>'

    routing = next((e for e in entries if e["stem"] == "路由索引"), None)
    routing_share = (routing["tokens"] / a["tokens"] * 100) if routing and a["tokens"] else 0
    verify_state = ("字段已落地" if not a["unverified"] else
                    f'字段未落地（{len(entries)} 条全部缺失）')

    # ── 经验晋升候选（复现阈值装置）──────────────────────────────
    rec = collect_recurrence(vault)
    rec_block = ""
    rec_candidates = 0
    if rec is None:
        rec_block = ('<div class="hint">未取到扫描结果（<code>脚本/recurrence_scan.py</code> '
                     '缺失或执行失败），本节跳过。</div>')
    else:
        cands = [r for r in rec.get("results", []) if r.get("promote")]
        rec_candidates = len(cands)
        rows_rec = "".join(
            f'<tr class="hot-row"><td class="flag">{esc(r["name"])}</td>'
            f'<td class="mono">{esc(r["key"])}</td>'
            f'<td class="num flag">{r["occurs"]}</td>'
            f'<td class="num">{len(r.get("sources", []))}</td>'
            f'<td class="mono">{esc(r.get("law") or "-")}</td>'
            f'<td class="mono">{esc("、".join(r.get("dates", [])[-4:]))}</td></tr>'
            for r in cands) or \
            '<tr><td colspan="6" class="ok">无晋升候选（当前无判据同时满足复现 ≥3、跨来源 ≥2、30 天内）</td></tr>'
        rec_color = "#E24B4A" if rec_candidates else "#5DCAA5"
        # 阈值字段来自 JSON 的 thresholds 子对象（键名 min_sources，非 min_projects）
        th = rec.get("thresholds", {})
        th_hits = th.get("min_hits", 3)
        th_src = th.get("min_sources", 2)
        th_win = th.get("window_days", 30)
        rec_block = f"""<div class="hint">判据：<b>复现 ≥{th_hits} 次（按不同日期去重）</b> 且 <b>跨 ≥{th_src} 个来源目录</b> 且 <b>最近命中在 {th_win} 天内</b>，三者是 AND。达标即「入场券」——应据此<b>改铁律或萃 Skill</b>，不是自动改文件。判据选型与边界见规范「经验晋升阈值」节。</div>
<table><thead><tr><th>判据主题</th><th>key</th><th style="text-align:right">复现次数</th><th style="text-align:right">跨来源</th><th>对应铁律</th><th>命中日期（末 4 条）</th></tr></thead>
<tbody>{rows_rec}</tbody></table>
<div class="hint">阈值装置状态：<span style="color:{rec_color}">候选 {rec_candidates} 条</span> ｜ 扫描判据 {len(rec.get('results', []))} 条 ｜ 扫描退出码 {rec.get('_rc')}（0=无候选，1=有候选，皆属正常）</div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>记忆库体检报告</title>
<style>
:root{{--bg:#16181c;--panel:#1e2126;--line:#2c3037;--tx:#e6e6e3;--tx2:#a8a9a5;--tx3:#71726e}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:40px 28px 72px}}
h1{{font-size:22px;font-weight:500;margin:0 0 6px}}
.sub{{color:var(--tx2);font-size:13px;margin-bottom:32px}}
.mono{{font-family:Consolas,"Cascadia Mono",monospace;font-size:12.5px;color:var(--tx2)}}
h2{{font-size:15px;font-weight:500;margin:40px 0 4px;padding-bottom:8px;border-bottom:1px solid var(--line)}}
.hint{{color:var(--tx3);font-size:12.5px;margin:8px 0 14px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:8px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}}
.card .n{{font-size:22px;font-weight:500}}
.card .l{{color:var(--tx3);font-size:12px;margin-top:2px}}
table{{width:100%;border-collapse:collapse;margin-top:6px}}
th{{text-align:left;color:var(--tx3);font-weight:400;font-size:12px;
padding:6px 10px 6px 0;border-bottom:1px solid var(--line)}}
td{{padding:7px 10px 7px 0;border-bottom:1px solid #23262b;vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;color:var(--tx);width:84px}}
td.w{{width:180px}}
.bar{{background:#2a2e34;height:7px;border-radius:4px;overflow:hidden;margin-top:6px}}
.fill{{height:100%;border-radius:4px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}}
.chip{{background:var(--panel);border:1px solid var(--line);border-radius:7px;
padding:6px 11px;font-size:12.5px;display:flex;gap:8px;align-items:baseline}}
.chip .k{{color:var(--tx2)}} .chip .v{{font-weight:500}}
.chip .p{{color:var(--tx3);font-size:11.5px}}
.ok{{color:#5DCAA5;padding:10px 0}}
.flag{{color:#EF9F27}}
tr.hot-row td{{background:rgba(226,75,74,.10)}}
tr.hot-row td:first-child{{border-left:2px solid #E24B4A}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}
.lamp{{display:grid;grid-template-columns:1fr 34px 1fr;gap:8px 14px;align-items:center;margin-top:10px;font-size:13px}}
.lamp .m{{color:var(--tx2)}} .lamp .d{{color:var(--tx);text-align:right}}
.dot{{width:9px;height:9px;border-radius:50%;justify-self:center}}
.foot{{margin-top:44px;color:var(--tx3);font-size:12px}}
code{{background:#262a30;padding:1px 5px;border-radius:4px;font-size:12.5px}}
</style></head><body><div class="wrap">

<h1>记忆库体检报告</h1>
<div class="sub">库路径 {esc(vault)} ｜ 扫描 {a["total"]} 篇 .md（已排除 .archive/.trash/.workbuddy/.git/.obsidian）｜ 生成于本机</div>

<div class="cards">
<div class="card"><div class="n">{a["total"]}</div><div class="l">笔记条目</div></div>
<div class="card"><div class="n">{a["chars"]:,}</div><div class="l">总字符</div></div>
<div class="card"><div class="n">{a["tokens"]:,}</div><div class="l">估算 token</div></div>
<div class="card"><div class="n">{routing_share:.0f}%</div><div class="l">路由索引占 token 比</div></div>
</div>

<h2>一、token 成本排行</h2>
<div class="hint">读一篇笔记就吃掉对应的上下文预算。这张表回答「哪几篇最贵、值不值得拆」。token 为估算值（CJK 约 0.7/字，其余约 4 字符/token），只用于相对比较。</div>
<table><thead><tr><th>文件</th><th style="text-align:right">字符</th><th style="text-align:right">估算 token</th><th></th></tr></thead>
<tbody>{rows_top()}</tbody></table>

<h2>二、目录分布</h2>
<div class="hint">按 token 降序。用于判断「领域目录 + 路由索引」两级结构是否还成立。</div>
<table><thead><tr><th>目录</th><th style="text-align:right">条目</th><th style="text-align:right">字符</th><th style="text-align:right">估算 token</th><th></th></tr></thead>
<tbody>{rows_dir()}</tbody></table>

<div class="grid2">
<div><h2>三、tier 构成</h2><div class="chips">{chip_rows(a["by_tier"])}</div></div>
<div><h2>四、source 构成</h2><div class="chips">{chip_rows(a["by_source"])}</div></div>
</div>

<h2>五、最常被读（access_count 前 {TOP_N}）</h2>
<div class="hint">热点条目若同时占大量 token，就是最值得做「压缩 / 拆分」的候选。</div>
<table><thead><tr><th>文件</th><th style="text-align:right">访问</th><th style="text-align:right">估算 token</th></tr></thead>
<tbody>{rows_hot}</tbody></table>

<h2>六、从未被读（access_count = 0）</h2>
<div class="hint">按 token 降序。这些条目占着上下文预算却零召回，是归档 / 合并的首选。</div>
<table><thead><tr><th>文件</th><th style="text-align:right">估算 token</th><th>tier</th><th>目录</th></tr></thead>
<tbody>{rows_cold}</tbody></table>

<h2>七、字段异常与膨胀检查</h2>
<div class="hint">frontmatter 字段被异常放大属于静默事故：不报错，但会持续吃掉上下文预算。判定线＝frontmatter 单行 ≥ {FM_FIELD_LINE} 字符，或正文单行 ≥ {BODY_LONG_LINE} 字符。历史事故：一条含双引号的 summary 因转义 bug 被反复翻倍至 131,274 字符（占全库 68.6%）。</div>
<table><thead><tr><th>文件</th><th style="text-align:right">frontmatter 最长行</th><th style="text-align:right">正文最长行</th><th style="text-align:right">summary 长度</th><th style="text-align:right">文件字符</th></tr></thead>
<tbody>{rows_long}</tbody></table>

<h2>八、结构健康</h2>
<table><thead><tr><th>指标</th><th>值</th></tr></thead><tbody>
<tr><td>缺 summary 的条目</td><td class="{'ok' if not a["no_summary"] else 'flag'}">{len(a["no_summary"])}</td></tr>
<tr><td>孤立条目（无出链也无入链）</td><td class="{'ok' if not a["orphans"] else 'flag'}">{len(a["orphans"])}</td></tr>
<tr><td>真死链总数</td><td class="{'ok' if not a["dead_total"] else 'flag'}">{a["dead_total"]}</td></tr>
<tr><td>双链笔误数（标题近似但不同名）</td><td class="{'ok' if not a["near_entries"] else 'flag'}">{sum(len(e["near"]) for e in a["near_entries"])}</td></tr>
<tr><td>同主题候选对（核心相似 ≥ {int(DUP_RATIO * 100)}% 或共享前缀 ≥ {PREFIX_HIT} 字）</td><td class="{'ok' if not a["pairs"] else 'flag'}">{len(a["pairs"])}</td></tr>
</tbody></table>

<h2>九、双链笔误（改对即可）</h2>
<div class="hint">链接目标不存在，但与某个真实标题仅差空格 / 下划线 / 全半角括号——属改名后残留。</div>
<table><thead><tr><th>来源文件</th><th>写成</th><th>应为</th></tr></thead><tbody>{rows_near}</tbody></table>

<h2>十、真死链明细</h2>
<div class="hint">已排除文档里演示用的代码块内 [[双链]]。</div>
<table><thead><tr><th>来源文件</th><th>指向不存在的标题</th></tr></thead><tbody>{rows_dead}</tbody></table>

<h2>十一、同主题候选</h2>
<div class="hint">按标题的「主题核心」比对（已剥离领域前缀与日期后缀）。用于判断是否需要写入时重复拦截；同主题不等于重复，多数是同一项目的多个交付物。</div>
<table><thead><tr><th>标题 A</th><th>标题 B</th><th style="text-align:right">核心相似度</th></tr></thead><tbody>{rows_dup}</tbody></table>

<h2>十二、经验晋升候选 <span style="font-size:12px;color:{'#E24B4A' if rec_candidates else '#5DCAA5'}">{'● ' + str(rec_candidates) + ' 条达标' if rec_candidates else '○ 无'}</span></h2>
{rec_block}

<h2>十三、决策仪表</h2>
<div class="hint">把指标直接钉在它该决定的那个设计问题上——先用数据决定，再决定要不要加功能。</div>
<div class="lamp">
<span class="m">检索零结果率</span><span class="dot" style="background:#EF9F27"></span><span class="d">是否需要 FTS5 / 向量检索</span>
<span class="m">搜索平均耗时 / 命中数</span><span class="dot" style="background:#EF9F27"></span><span class="d">扫盘是否仍是瓶颈</span>
<span class="m">路由索引 token 占比（本库 {routing_share:.0f}%）</span><span class="dot" style="background:{"#E24B4A" if routing_share > 25 else "#5DCAA5"}"></span><span class="d">索引策略是否需要瘦身</span>
<span class="m">真死链 + 笔误（本库 {a["dead_total"] + sum(len(e["near"]) for e in a["near_entries"])}）</span><span class="dot" style="background:{"#E24B4A" if a["dead_total"] else "#5DCAA5"}"></span><span class="d">是否需要写入时双链校验</span>
<span class="m">同主题候选对（本库 {len(a["pairs"])}）</span><span class="dot" style="background:{"#E24B4A" if a["pairs"] else "#5DCAA5"}"></span><span class="d">是否需要冲突拦截 / supersedes</span>
<span class="m">零访问条目数（本库 {len(a["cold"])}）</span><span class="dot" style="background:{"#E24B4A" if len(a["cold"]) > 10 else "#5DCAA5"}"></span><span class="d">生命周期是否需要状态机</span>
<span class="m">超长字段文件数（本库 {len(a["long_lines"])}）</span><span class="dot" style="background:{"#E24B4A" if a["long_lines"] else "#5DCAA5"}"></span><span class="d">是否需要字段写入护栏</span>
<span class="m">verified 字段覆盖</span><span class="dot" style="background:#71726e"></span><span class="d">可信度机制是否已落地（{verify_state}）</span>
</div>

<div class="foot">由 <code>脚本/memory_report.py</code> 生成，只读扫描，未修改任何记忆文件。token 为估算值（CJK 约 0.7/字，其余约 4 字符/token），仅用于同库内相对比较，不可外报为绝对值。</div>
</div></body></html>"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="记忆库体检报告生成器（只读）")
    ap.add_argument("--vault", default=str(DEFAULT_VAULT), help="记忆库路径")
    ap.add_argument("-o", "--output", default=None, help="输出 html 路径")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"错误：记忆库路径不存在 {vault}")
        return 1

    entries = scan(vault)
    if not entries:
        print("错误：未扫描到任何 .md")
        return 1

    a = analyze(entries)
    out = Path(args.output) if args.output else vault / "记忆库体检报告.html"
    out.write_text(render(vault, entries, a), encoding="utf-8")

    print(f"已生成：{out}")
    print(f"  条目 {a['total']} ｜ 字符 {a['chars']:,} ｜ 估算 token {a['tokens']:,}")
    print(f"  零访问 {len(a['cold'])} ｜ 真死链 {a['dead_total']} "
          f"｜ 双链笔误 {sum(len(e['near']) for e in a['near_entries'])} "
          f"｜ 同主题候选 {len(a['pairs'])} ｜ 孤立 {len(a['orphans'])} "
          f"｜ 缺 summary {len(a['no_summary'])}")
    rec = collect_recurrence(vault)
    if rec is not None:
        cands = [r for r in rec.get("results", []) if r.get("promote")]
        if cands:
            names = "、".join(r["name"] for r in cands)
            print(f"  【经验晋升候选】{len(cands)} 条达标，应改铁律或萃 Skill：{names}")
        else:
            print("  经验晋升候选 0 条（无判据同时满足复现 ≥3、跨来源 ≥2、30 天内）")
    if a["long_lines"] or a["long_summary"]:
        print(f"  字段异常：超长行文件 {len(a['long_lines'])} 个，超长 summary {len(a['long_summary'])} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
