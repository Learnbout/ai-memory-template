#!/usr/bin/env python3
"""L2 只读看板：本地 HTTP 服务，展示记忆库指标与历史趋势。

三层 Web 路线的第二层（L1 = 单文件报表 脚本/memory_report.py，已完成）。
L2 = 本地服务，可刷新、可历史对比。真正的成本不在前端，而在字段集是否冻结——
字段集已于 2026-09-10 冻结（schema_version=1），故 L2 此刻才有意义。

零第三方依赖：http.server + 复用 L1 的 scan/analyze/render。

用法：
    python 脚本/memory_dashboard.py                 # 起服务并自动打一次快照
    python 脚本/memory_dashboard.py --port 8765
    python 脚本/memory_dashboard.py --no-open

端点：
    /               指标看板（KPI + 目录分布 + 字段覆盖 + 历史趋势）
    /report         L1 完整体检报告
    /snapshot       打一个快照（写入历史）
    /api/snapshot   当前快照 JSON（不落盘）
    /api/history    历史快照 JSON
    /history        历史快照表
"""

from __future__ import annotations

try:  # 脚本使用计数（写入 <vault>/.workbuddy/script_usage.json，供 脚本索引.md 的「使用次数」列）
    from _usage import autotrack

    autotrack(__file__)
except Exception:  # 计数失败绝不能影响脚本本身
    pass

import argparse
import http.server
import importlib.util
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1]).resolve()
os.environ["AI_MEMORY_DIR"] = str(VAULT)
EXCLUDE_DIRS = {".archive", ".trash", ".workbuddy", ".git", ".obsidian", "__pycache__"}
TRACKED_FIELDS = ("schema_version", "type", "scope", "verified", "summary")
HISTORY_NAME = "report_history.jsonl"


def load_report_module():
    path = Path(__file__).with_name("memory_report.py")
    spec = importlib.util.spec_from_file_location("memory_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


RP = load_report_module()


def history_path() -> Path:
    return VAULT / ".workbuddy" / HISTORY_NAME


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def field_coverage() -> tuple[int, dict[str, int]]:
    n = 0
    counts = {k: 0 for k in TRACKED_FIELDS}
    for path in VAULT.rglob("*.md"):
        rel = path.relative_to(VAULT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        n += 1
        text = read_text(path)
        m = RP.FM_RE.match(text)
        fm = m.group(1) if m else ""
        for key in counts:
            if re.search(rf"^{key}:", fm, re.M):
                counts[key] += 1
    return n, counts


def build_snapshot(label: str = "") -> dict:
    entries = RP.scan(VAULT)
    a = RP.analyze(entries)
    routing = VAULT / "路由索引.md"
    n, cov = field_coverage()
    verified_true = sum(1 for e in entries if e["verified"] == "true")
    return {
        "ts": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
        "entries": a["total"],
        "chars": a["chars"],
        "tokens": a["tokens"],
        "routing_chars": len(read_text(routing)) if routing.exists() else 0,
        "by_dir_tokens": {k: v["tokens"] for k, v in a["by_dir"].items()},
        "field_coverage": cov,
        "field_total": n,
        "verified_true": verified_true,
        "zero_access": len(a["cold"]),
        "dead_links": a["dead_total"],
        "dup_pairs": len(a["pairs"]),
        "no_summary": len(a["no_summary"]),
        "long_summary": len(a["long_summary"]),
    }


def load_history() -> list[dict]:
    path = history_path()
    if not path.exists():
        return []
    out = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_history(snap: dict) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")


def seed_from_bench() -> None:
    """首次运行时，把改动前的基准导入为第一个快照，让趋势一开始就有对照点。"""
    if load_history():
        return
    bench = VAULT / ".workbuddy" / "bench_before.json"
    if not bench.exists():
        return
    try:
        data = json.loads(read_text(bench))
    except json.JSONDecodeError:
        return
    scale = data.get("scale", {})
    append_history({
        "ts": data.get("collected_at", "")[:19].replace("T", " "),
        "label": "改动前基线",
        "entries": scale.get("entries"),
        "chars": scale.get("total_chars"),
        "tokens": scale.get("est_tokens"),
        "routing_chars": data.get("index", {}).get("routing_index_chars"),
        "by_dir_tokens": {},
        "field_coverage": data.get("fields", {}).get("coverage", {}),
        "field_total": scale.get("entries"),
        "verified_true": data.get("fields", {}).get("verified_true", 0),
        "zero_access": None,
        "dead_links": None,
        "dup_pairs": None,
        "no_summary": None,
        "long_summary": None,
    })


# ─────────────────────────── 渲染 ───────────────────────────

CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#14161a; color:#e6e8eb;
       font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; font-size:14px; }
.wrap { max-width:1100px; margin:0 auto; padding:28px 20px 60px; }
h1 { font-size:20px; font-weight:600; margin:0 0 4px; }
h2 { font-size:15px; font-weight:600; margin:28px 0 12px; color:#c9ccd1;
     border-left:3px solid #4a6cf7; padding-left:10px; }
.sub { color:#8b9099; font-size:12px; margin-bottom:20px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }
.card { background:#1c1f25; border:1px solid #282c34; border-radius:8px; padding:14px 16px; }
.card .k { color:#8b9099; font-size:12px; margin-bottom:6px; }
.card .v { font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; }
.card .d { color:#6f747c; font-size:11px; margin-top:4px; }
.row { display:flex; align-items:center; gap:10px; margin:7px 0; }
.row .name { width:120px; color:#a8adb5; font-size:12px; flex:none; }
.row .track { flex:1; height:16px; background:#22262d; border-radius:3px; overflow:hidden; }
.row .fill { height:100%; background:#4a6cf7; }
.row .num { width:132px; text-align:right; color:#8b9099; font-size:12px;
            font-variant-numeric:tabular-nums; flex:none; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th,td { text-align:left; padding:7px 8px; border-bottom:1px solid #242830; }
th { color:#8b9099; font-weight:500; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.muted { color:#6f747c; font-size:12px; }
a { color:#6f9bff; text-decoration:none; }
a:hover { text-decoration:underline; }
.toolbar { display:flex; gap:10px; margin-bottom:18px; }
.btn { background:#232830; border:1px solid #303643; color:#dfe3e8; padding:7px 14px;
       border-radius:6px; font-size:12.5px; cursor:pointer; text-decoration:none; }
.btn:hover { background:#2b313b; text-decoration:none; }
.ok { color:#4ade80; } .warn { color:#fbbf24; } .bad { color:#f87171; }
.chart { background:#1c1f25; border:1px solid #282c34; border-radius:8px; padding:14px; }
.legend { color:#8b9099; font-size:11.5px; margin-top:8px; }
"""


def esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def card(key: str, value: object, detail: str = "") -> str:
    d = f'<div class="d">{esc(detail)}</div>' if detail else ""
    return f'<div class="card"><div class="k">{esc(key)}</div><div class="v">{value}</div>{d}</div>'


def bars(items: list[tuple[str, int, int]], unit: str = "") -> str:
    if not items:
        return '<p class="muted">无数据</p>'
    mx = max(v for _k, v, _r in items) or 1
    out = []
    for name, value, ratio in items:
        pct = value / mx * 100
        out.append(
            f'<div class="row"><div class="name">{esc(name)}</div>'
            f'<div class="track"><div class="fill" style="width:{pct:.1f}%"></div></div>'
            f'<div class="num">{value:,}{unit}（{ratio}%）</div></div>'
        )
    return "\n".join(out)


def trend_chart(snaps: list[dict]) -> str:
    points = [(s.get("ts", ""), s.get("chars"), s.get("tokens"), s.get("routing_chars")) for s in snaps]
    series = [
        ("总字符", [p[1] for p in points], "#4a6cf7"),
        ("估算 token", [p[2] for p in points], "#22c55e"),
        ("路由索引", [p[3] for p in points], "#f59e0b"),
    ]
    w, h, pad = 640, 170, 34
    valid = [v for _n, vals, _c in series for v in vals if isinstance(v, (int, float))]
    if len(points) < 2 or not valid:
        return ('<div class="chart"><p class="muted">快照少于 2 个，暂无法绘制趋势。'
                '点击「打快照」累积数据点。</p></div>')
    mx = max(valid)
    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">']
    for gy in range(0, 5):
        y = pad + gy * (h - 2 * pad) / 4
        svg.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{w - 8}" y2="{y:.1f}" stroke="#242830"/>')
        svg.append(f'<text x="4" y="{y + 3:.1f}" fill="#5d626b" font-size="9">'
                   f'{int(mx * (1 - gy / 4))}</text>')
    for name, vals, color in series:
        pts = []
        for i, v in enumerate(vals):
            if not isinstance(v, (int, float)):
                continue
            x = pad + i * (w - pad - 8) / max(len(vals) - 1, 1)
            y = h - pad - (v / mx) * (h - 2 * pad)
            pts.append(f"{x:.1f},{y:.1f}")
        if len(pts) >= 2:
            svg.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
                       f'stroke-width="2" stroke-linejoin="round"/>')
        for i, p in enumerate(pts):
            cx, cy = p.split(",")
            svg.append(f'<circle cx="{cx}" cy="{cy}" r="2.6" fill="{color}"/>')
    svg.append("</svg>")
    legend = " ｜ ".join(
        f'<span style="color:{c}">■</span> {n}' for n, _v, c in series
    )
    return f'<div class="chart">{"".join(svg)}<div class="legend">{legend}</div></div>'


def history_table(snaps: list[dict]) -> str:
    if not snaps:
        return '<p class="muted">暂无历史快照</p>'
    rows = []
    for s in reversed(snaps):
        rows.append(
            "<tr>"
            f'<td>{esc(s.get("ts", ""))}</td>'
            f'<td>{esc(s.get("label") or "-")}</td>'
            f'<td class="num">{s.get("entries") if s.get("entries") is not None else "-"}</td>'
            f'<td class="num">{s.get("chars", 0):,}</td>'
            f'<td class="num">{s.get("tokens", 0):,}</td>'
            f'<td class="num">{s.get("routing_chars", 0):,}</td>'
            f'<td class="num">{s.get("verified_true", 0)}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间</th><th>标签</th><th>条目</th><th>总字符</th>"
        "<th>估算 token</th><th>路由索引</th><th>verified</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


def render_page(cur: dict, snaps: list[dict]) -> str:
    total = cur["field_total"] or 1
    cov_items = [
        (name, cur["field_coverage"].get(name, 0), round(cur["field_coverage"].get(name, 0) / total * 100, 1))
        for name in TRACKED_FIELDS
    ]
    dir_items = [
        (name, tokens, round(tokens / (cur["tokens"] or 1) * 100, 1))
        for name, tokens in sorted(cur["by_dir_tokens"].items(), key=lambda kv: -kv[1])
    ]
    cards = "".join([
        card("条目数", cur["entries"]),
        card("总字符", f'{cur["chars"]:,}'),
        card("估算 token", f'{cur["tokens"]:,}'),
        card("路由索引", f'{cur["routing_chars"]:,}',
             f'占全库 {cur["routing_chars"] / (cur["chars"] or 1) * 100:.1f}%'),
        card("verified 已确认", f'{cur["verified_true"]} / {cur["entries"]}',
             f'{cur["verified_true"] / (cur["entries"] or 1) * 100:.1f}%'),
        card("零访问条目", cur["zero_access"]),
        card("死链", cur["dead_links"], "应为 0" if cur["dead_links"] == 0 else "需修复"),
        card("同主题候选", cur["dup_pairs"], "写入时已加拦截"),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ai-memory 看板</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>ai-memory 只读看板</h1>
<div class="sub">数据实时扫描自 {esc(VAULT)} ｜ 快照 {len(snaps)} 个 ｜ 刷新时间 {esc(cur['ts'])}</div>
<div class="toolbar">
  <a class="btn" href="/">刷新</a>
  <a class="btn" href="/snapshot">打快照</a>
  <a class="btn" href="/report">完整体检报告</a>
  <a class="btn" href="/api/history">历史 JSON</a>
</div>
<div class="cards">{cards}</div>

<h2>目录 token 占比</h2>
{bars(dir_items, " tok")}

<h2>frontmatter 字段覆盖</h2>
{bars([(n, v, r) for n, v, r in cov_items], " 条")}

<h2>历史趋势</h2>
{trend_chart(snaps)}

<h2>快照记录</h2>
{history_table(snaps)}

<h2>说明</h2>
<p class="muted">本页为只读看板（L2），不做任何写入。<code>/snapshot</code> 仅追加一行指标到
<code>.workbuddy/report_history.jsonl</code>，不触碰笔记。token 为估算值（CJK 约 0.7/字），
仅用于同库内相对比较。</p>
</div></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code: int = 200) -> None:
        self._send(text.encode("utf-8"), "text/html; charset=utf-8", code)

    def _json(self, data: object) -> None:
        self._send(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        try:
            if route == "/":
                self._html(render_page(build_snapshot(), load_history()))
            elif route == "/snapshot":
                append_history(build_snapshot(label="手动"))
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
            elif route == "/api/snapshot":
                self._json(build_snapshot())
            elif route == "/api/history":
                self._json(load_history())
            elif route == "/history":
                self._html("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                           f"<title>历史快照</title><style>{CSS}</style></head><body>"
                           f"<div class='wrap'><h1>历史快照</h1>{history_table(load_history())}"
                           "<p><a href='/'>&larr; 返回看板</a></p></div></body></html>")
            elif route == "/report":
                entries = RP.scan(VAULT)
                self._html(RP.render(VAULT, entries, RP.analyze(entries)))
            else:
                self._html("<h1>404</h1><p><a href='/'>返回看板</a></p>", 404)
        except Exception as e:  # 看板崩了也要给出可读原因
            self._html(f"<h1>500</h1><pre>{esc(e)}</pre>", 500)

    def log_message(self, *args) -> None:  # 静默
        return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(VAULT))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--once", action="store_true", help="只打一个快照并退出（供脚本调用）")
    args = ap.parse_args()
    seed_from_bench()

    if args.once:
        snap = build_snapshot(label="脚本")
        append_history(snap)
        print(f"快照已写入：{snap['entries']} 条 / {snap['chars']:,} 字符 / "
              f"路由索引 {snap['routing_chars']:,} / verified {snap['verified_true']}")
        return 0

    snap = build_snapshot(label="启动")
    append_history(snap)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"ai-memory 看板已启动：{url}")
    print(f"当前：{snap['entries']} 条 / {snap['chars']:,} 字符 / 估算 {snap['tokens']:,} token")
    print("按 Ctrl+C 停止")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
