# -*- coding: utf-8 -*-
"""命令行安装入口（给脚本、自动化、以及沙盒验收用）。

## 为什么要单独一个入口

参考文档里那条决策是对的：**一个 exe 硬凑两种形态会翻车**（`--noconsole` 想弹控制台
在受限环境直接挂死）。所以分两个产物：

- `ai-memory-installer-<形态>.exe` —— 双击用的 GUI 向导（无控制台）
- `ai-memory-setup-cli.exe` —— 脚本用的这个（有控制台）

## 用法

```
ai-memory-setup-cli.exe --mode local --path D:\\ai-memory --json
ai-memory-setup-cli.exe --mode local --dry-run            # 只预演，不落盘
ai-memory-setup-cli.exe --check                            # 只体检
```

`--json` 输出一行机器可读结果（自动化读它，别去解析人类日志）。
退出码：0 成功 / 1 失败 / 2 参数错。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import agents, core, steps
from .wizard import package_version


def _print(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai-memory-setup-cli", description="ai-memory 安装（命令行）")
    ap.add_argument("--mode", choices=["local", "central"], default="")
    ap.add_argument("--path", default="", help="装到哪（默认 %%LOCALAPPDATA%%\\ai-memory）")
    ap.add_argument("--runtime-mode", choices=["portable", "system"], default="portable")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不改任何文件")
    ap.add_argument("--check", action="store_true", help="只体检，不安装")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    mode = args.mode or "local"
    target = Path(args.path) if args.path else steps.default_target()
    version = package_version()

    if args.check:
        found = agents.scan()
        disk_ok, disk_msg = steps.disk_status(target)
        out = {
            "ok": True,
            "package_version": version,
            "portable": steps.bundle_runtime() is not None,
            "system_python": steps.system_python() or "",
            "disk_ok": disk_ok, "disk_msg": disk_msg,
            "agents_found": [agents.as_dict(a) for a in found if a.found],
            "default_path": str(steps.default_target()),
            "existing": steps.detect_existing(),
        }
        if args.json:
            _print(out)
        else:
            print("版本 %s｜自带运行环境 %s｜默认装到 %s"
                  % (version, "有" if out["portable"] else "没有", out["default_path"]))
            print("磁盘：%s｜找到 AI 工具 %d 个" % (disk_msg, len(out["agents_found"])))
        return 0

    plan_obj = core.plan(target, mode=mode, runtime_mode=args.runtime_mode,
                         package_version=version)
    summary = core.preview(plan_obj)
    if args.dry_run:
        payload = {"ok": True, "dry_run": True, "target": str(target),
                   "state": plan_obj["state"]["state"], "counts": plan_obj["counts"],
                   "summary": summary, "problems": plan_obj["problems"]}
        if args.json:
            _print(payload)
        else:
            print("预演（什么都没改）：%s" % summary)
            for p in plan_obj["problems"]:
                print("  ! " + p)
        return 0 if not plan_obj["problems"] else 1

    logs: list[str] = []

    def progress(msg: str, pct: float) -> None:
        logs.append(msg)
        if not args.json:
            print("[%3d%%] %s" % (int(pct * 100), msg), flush=True)

    res = core.execute(plan_obj, progress=progress, package_version=version, mode=mode,
                       runtime_mode=args.runtime_mode, with_deps=not args.skip_deps)
    if not res.get("ok"):
        payload = {"ok": False, "target": str(target), "errors": res.get("errors"),
                   "log": logs}
        if args.json:
            _print(payload)
        else:
            print("装失败了：")
            for e in res.get("errors") or []:
                print("  - " + str(e))
        return 1

    # 接 AI 工具（默认接上本机找到的，可用 --skip-deps 之类控制不了，这里全接）
    mcp = steps.start_mcp(target, log=lambda m: logs.append(m))
    url = mcp.get("url") or "http://127.0.0.1:8770/mcp"
    found = [a for a in agents.scan() if a.found]
    applied = agents.apply(found, url, "")
    launched = steps.start_app(target, log=lambda m: logs.append(m))
    steps.create_shortcuts(target, mode=mode)

    payload = {
        "ok": True, "target": str(target), "version": version, "mode": mode,
        "runtime_mode": args.runtime_mode, "summary": summary,
        "web_ok": bool(launched.get("ok")), "web_url": launched.get("url") or "",
        "mcp_ok": bool(mcp.get("ok")), "mcp_url": url,
        "mcp_reason": mcp.get("reason") or "",
        "web_reason": launched.get("reason") or "",
        "agents": [{"agent": r.get("agent"), "action": r.get("action"),
                    "error": r.get("error")} for r in applied],
        "log": logs,
    }
    if args.json:
        _print(payload)
    else:
        print("装好了：%s" % target)
        print("工作台：%s" % (payload["web_url"] or ("没起来，" + payload["web_reason"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
