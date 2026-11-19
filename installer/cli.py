# -*- coding: utf-8 -*-
"""安装器命令行：`python -m installer.cli scan|apply|remove`。

为什么先做 CLI：安装器界面（Web + GSAP）是后面的事，但「扫描 / 写配置」这套逻辑
必须能单独验收 —— 界面只是它的外壳。CLI 就是这套逻辑的可验证入口。

用法：
    python -m installer.cli scan                      # 只扫描，打印表格
    python -m installer.cli apply --url http://127.0.0.1:8770/mcp [--token XXX]
    python -m installer.cli apply --url ... --only workbuddy,cursor
    python -m installer.cli remove
    python -m installer.cli scan --json               # 给界面用的机器可读输出
"""
from __future__ import annotations

import argparse
import json
import sys

from . import agents


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="installer", description="ai-memory 安装器（Agent 扫描与配置）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描本机已安装的 AI 客户端")
    p_scan.add_argument("--json", action="store_true", help="输出 JSON（给安装器界面用）")

    p_apply = sub.add_parser("apply", help="把 ai-memory 的 MCP 配置写进选中的客户端")
    p_apply.add_argument("--url", required=True, help="MCP 地址，如 http://127.0.0.1:8770/mcp")
    p_apply.add_argument("--token", default="", help="中心化版必填；本地版留空")
    p_apply.add_argument("--only", default="", help="只处理这些 key（逗号分隔），默认全部已安装的")
    p_apply.add_argument("--json", action="store_true")

    p_rm = sub.add_parser("remove", help="撤销：只删 ai-memory 这一条配置")
    p_rm.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    found = agents.scan()

    if args.cmd == "scan":
        if args.json:
            print(json.dumps([agents.as_dict(a) for a in found], ensure_ascii=False, indent=2))
        else:
            print("%-16s %-24s %-8s %-8s %s" % ("key", "名称", "已安装", "已配置", "配置路径"))
            for a in found:
                d = agents.as_dict(a)
                print("%-16s %-24s %-8s %-8s %s" % (
                    d["key"], d["name"], "是" if d["found"] else "否",
                    "是" if d["configured"] else "否", d["path"] or "-"))
        return 0

    if args.cmd == "apply":
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        picked = [a for a in found if a.found and (not only or a.key in only)]
        if not picked:
            print("没有可写的客户端（先 scan 看看装了什么）", file=sys.stderr)
            return 1
        results = agents.apply(picked, args.url, args.token)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                if r.get("error"):
                    print("FAIL %-14s %s" % (r["agent"], r["error"]))
                elif r.get("skipped"):
                    print("SKIP %-14s %s" % (r["agent"], r.get("reason")))
                else:
                    print("OK   %-14s %s → %s%s" % (
                        r["agent"], r.get("action"), r.get("path", ""),
                        ("（备份 %s）" % r["backup"]) if r.get("backup") else ""))
        return 0 if all(not r.get("error") for r in results) else 1

    if args.cmd == "remove":
        results = agents.remove([a for a in found if a.found])
        print(json.dumps(results, ensure_ascii=False, indent=2) if args.json
              else "\n".join("%-14s %s" % (r["agent"], r.get("action") or r.get("error")) for r in results))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
