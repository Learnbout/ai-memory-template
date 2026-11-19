#!/usr/bin/env python3
"""票务工具：生成密钥、签票、列票、吊销（阶段 2，2026-10-22）。

为什么单独一个脚本：签票是**只在需要时做一次**的动作，不该让服务进程常驻一份
签发能力 —— 服务端持密钥是为了「验」，能「签」是顺带的。把签发放在独立脚本里，
服务进程的代码路径上就少一条「可以造票」的入口。

用法（在库根跑，或任意目录带 AI_MEMORY_DIR）：
    init   --resource http://<server-ip>:8770/mcp       # 生成密钥 + 记下 aud（只做一次）
    new    --name 主人的笔记本 --scope readwrite          # 签一张读写票（默认 90 天）
    new    --name 给老王看看  --scope read               # 签一张只读票（默认 7 天）
    list                                                 # 看台账：谁、什么权限、几号过期、有没有吊销
    revoke --jti 3f2a9c1b8d7e6f50                        # 吊销（立刻失效）
    revoke --name 给老王看看                             # 按票名吊销

安全约定（为阶段 14 开源兜底）：
  · 密钥只落 `<库根>/.workbuddy/token.key`，权限 0600，绝不进 git；
  · 本脚本不把 token 写进任何文件，只打印到标准输出 —— 由你自己粘到客户端配置里；
  · 台账 `.workbuddy/tokens.json` **不含 token，也不含 token 的哈希**，只有票号与元信息。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(VAULT))

import mcp_auth  # noqa: E402


def _ttl(text: str) -> int:
    """把 `7d` / `12h` / `30m` / `90` 解析成秒。纯数字按天算（人写票期限不会想写秒）。"""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError(f"看不懂的期限：{text}（示例：7d / 12h / 30m / 90）")
    n = int(m.group(1))
    unit = m.group(2) or "d"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def _secret_ledger(args) -> tuple[bytes, mcp_auth.Ledger]:
    secret_path = Path(args.secret_file) if args.secret_file else mcp_auth.default_secret_path()
    ledger_path = Path(args.token_ledger) if args.token_ledger else mcp_auth.default_ledger_path()
    return mcp_auth.load_secret(secret_path), mcp_auth.Ledger(ledger_path)


def cmd_init(args) -> int:
    secret_path = mcp_auth.default_secret_path()
    if secret_path.exists():
        print(f"密钥已存在，跳过生成：{secret_path}", file=sys.stderr)
    else:
        mcp_auth.generate_secret(secret_path)
        print(f"已生成 32 字节密钥：{secret_path}（权限 0600，勿入 git）")
    if args.resource:
        path = mcp_auth.save_resource(args.resource)
        print(f"已记录 aud（资源指示器）：{args.resource}  ->  {path}")
    else:
        print(
            "提示：没给 --resource，服务端与签发端就得各自保证 aud 一致。"
            f"建议补一条：init --resource http://<地址>:<端口>/mcp",
            file=sys.stderr,
        )
    return 0


def cmd_new(args) -> int:
    secret, ledger = _secret_ledger(args)
    scope = mcp_auth.SCOPE_READ if args.scope == "read" else f"{mcp_auth.SCOPE_READ} {mcp_auth.SCOPE_WRITE}"
    resource = mcp_auth.resolve_resource(args.resource)
    ttl = args.ttl if args.ttl is not None else mcp_auth.ttl_for_scope(scope)
    token, jti, rec = mcp_auth.issue(
        secret,
        ledger,
        name=args.name,
        scope=scope,
        resource=resource,
        ttl_seconds=ttl,
        note=args.note,
        user_id=args.user_id,
        vault_id=args.vault_id,
    )
    print(f"票名      {rec['name']}")
    print(f"权限      {rec['scope']}  ({'只读' if args.scope == 'read' else '读写'})")
    print(f"到期      {'永久' if rec.get('permanent') else rec['expires_at']}")
    print(f"票号      {jti}")
    print(f"aud       {resource}")
    if args.user_id or args.vault_id:
        print(f"身份      user_id={args.user_id or '-'} vault_id={args.vault_id or '-'}")
    print()
    print("令牌（只显示这一次，请立刻存进客户端配置，不要贴进聊天或提交到仓库）：")
    print(token)
    return 0


def cmd_list(args) -> int:
    _, ledger = _secret_ledger(args)
    rows = ledger.rows()
    if not rows:
        print("台账为空：一张票都没签发过。")
        return 0
    print(f"{'票号':<18}{'票名':<20}{'权限':<22}{'到期':<22}{'状态'}")
    print("-" * 100)
    for jti, rec in rows:
        scopes = str(rec.get("scope", ""))
        label = "读写" if mcp_auth.SCOPE_WRITE in scopes.split() else "只读"
        state = "已吊销" if rec.get("revoked") else "有效"
        print(
            f"{jti:<18}{str(rec.get('name',''))[:18]:<20}"
            f"{label:<22}{str(rec.get('expires_at','')):<22}{state}"
        )
    return 0


def cmd_revoke(args) -> int:
    _, ledger = _secret_ledger(args)
    if args.jti:
        ok = ledger.revoke(args.jti)
        print(f"{'已吊销' if ok else '未找到这张票'}：{args.jti}")
        return 0 if ok else 1
    hits = [jti for jti, rec in ledger.rows() if rec.get("name") == args.name and not rec.get("revoked")]
    if not hits:
        print(f"没有名为 {args.name} 的有效票", file=sys.stderr)
        return 1
    for jti in hits:
        ledger.revoke(jti)
    print(f"已吊销 {len(hits)} 张：{'、'.join(hits)}")
    return 0


def cmd_show_resource(args) -> int:
    try:
        print(mcp_auth.resolve_resource(args.resource))
    except mcp_auth.TokenError as e:
        print(f"没有可用的 aud：{e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ai-memory 票务工具（阶段 2）")
    ap.add_argument("--secret-file", default=None, help="密钥路径，默认 <库根>/.workbuddy/token.key")
    ap.add_argument("--token-ledger", default=None, help="台账路径，默认 <库根>/.workbuddy/tokens.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成密钥并记录 aud（只做一次）")
    p.add_argument("--resource", default=None, help="aud，例如 http://<server-ip>:8770/mcp")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="签一张票")
    p.add_argument("--name", required=True, help="票名（给谁用），会写进台账与审计日志")
    p.add_argument("--scope", choices=["read", "readwrite"], default="read", help="权限档")
    p.add_argument("--ttl", type=_ttl, default=None, help="有效期，如 7d / 12h / 30。默认只读 7 天、读写 90 天")
    p.add_argument("--note", default="", help="备注，写进台账")
    p.add_argument("--user-id", default=None, help="绑定用户 ID（阶段 5）")
    p.add_argument("--vault-id", default=None, help="绑定知识库 ID（阶段 5）")
    p.add_argument("--resource", default=None, help="aud，默认读 auth.json / 环境变量")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("list", help="列出所有票")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("revoke", help="吊销（立刻失效）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--jti", default=None, help="按票号吊销")
    g.add_argument("--name", default=None, help="按票名吊销（同名多张则全部吊销）")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("resource", help="打印当前生效的 aud")
    p.add_argument("--resource", default=None)
    p.set_defaults(func=cmd_show_resource)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (mcp_auth.TokenError, FileNotFoundError, ValueError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
