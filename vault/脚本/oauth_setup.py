"""生成 / 轮换 OAuth 授权口令（阶段 13）。

用法：
    python 脚本/oauth_setup.py                     # 随机生成一串口令并打印一次
    python 脚本/oauth_setup.py --password 你自己的   # 用指定的口令
    python 脚本/oauth_setup.py --ttl-days 30       # 换票有效期（默认 30 天）
    python 脚本/oauth_setup.py --scope tools:read  # 换出来的票给多大权限（默认只读）
    python 脚本/oauth_setup.py --show              # 只看当前配置（不回显口令）

写的是 `<库根>/.workbuddy/oauth.json`，只存 PBKDF2 哈希；明文只在这次输出里出现，
脚本本身不落盘、日志里也没有。轮换口令不影响已经换出去的票 —— 那些票在
`.workbuddy/tokens.json` 台账里，要收回得单独吊销。

user_id / vault_id 是**必须绑**的：库内有一条实测结论「票不绑账号等于空库」——
ACL 是 fail-closed 的，票上没有 user_id/vault_id 时任何笔记都读不到，症状是
「连上了但搜不到东西」，最难查的一类。默认取现有台账里最后一张同时带两字段的票
的值，可用 --user-id / --vault-id 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oauth_server  # noqa: E402


def vault_root() -> Path:
    return Path(os.environ.get("AI_MEMORY_DIR") or Path(__file__).resolve().parent.parent)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def guess_identity(tokens_path: Path) -> tuple[str | None, str | None]:
    """从台账里挑一张同时带 user_id / vault_id 的票，借它的身份。

    为什么不写死常量：账号体系是阶段 7 才有的，之前签的票没有这两个字段；
    写死就等于把某个时刻的数据库主键焊进脚本，换库即错。
    """
    data = read_json(tokens_path).get("tokens") or {}
    for _jti, rec in sorted(data.items(), key=lambda kv: str(kv[1].get("issued_at") or ""), reverse=True):
        if isinstance(rec, dict) and rec.get("user_id") and rec.get("vault_id"):
            return str(rec["user_id"]), str(rec["vault_id"])
    return None, None


def random_passphrase() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉易混淆的 i l o 0 1
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def main() -> int:
    ap = argparse.ArgumentParser(prog="oauth_setup.py", description="生成 / 轮换 OAuth 授权口令")
    ap.add_argument("--password", default=None, help="指定口令；不给则随机生成")
    ap.add_argument("--ttl-days", type=int, default=None, help="换出来的票有效期天数（默认 30）")
    ap.add_argument("--scope", default=None, choices=["tools:read", "tools:write"], help="票的权限")
    ap.add_argument("--user-id", default=None, help="绑定的账号 id（不绑等于空库）")
    ap.add_argument("--vault-id", default=None, help="绑定的库 id")
    ap.add_argument("--subject", default=None, help="票名（台账里显示给谁），默认 oauth-web")
    ap.add_argument("--show", action="store_true", help="只看当前配置，不改动")
    args = ap.parse_args()

    root = vault_root()
    cfg_path = root / ".workbuddy" / "oauth.json"
    cfg = read_json(cfg_path)

    if args.show:
        shown = {k: ("***" if k == "password_hash" else v) for k, v in cfg.items()}
        print(json.dumps(shown or {"（未配置）": True}, ensure_ascii=False, indent=2))
        print(f"配置文件：{cfg_path}")
        return 0

    uid, vid = args.user_id, args.vault_id
    if not uid or not vid:
        g_uid, g_vid = guess_identity(root / ".workbuddy" / "tokens.json")
        uid = uid or g_uid or cfg.get("user_id")
        vid = vid or g_vid or cfg.get("vault_id")
    if not uid or not vid:
        print("警告：拿不到 user_id / vault_id，换出来的票会读不到任何笔记（ACL fail-closed）。",
              file=sys.stderr)

    password = args.password or random_passphrase()
    cfg.update(
        {
            "password_hash": oauth_server.hash_password(password),
            "ttl_days": args.ttl_days if args.ttl_days is not None else int(cfg.get("ttl_days") or 30),
            "scope": args.scope or str(cfg.get("scope") or "tools:read"),
            "subject": args.subject or str(cfg.get("subject") or "oauth-web"),
            "user_id": uid,
            "vault_id": vid,
            "enabled": True,
        }
    )

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, cfg_path)
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass

    print(f"已写入 {cfg_path}")
    print(f"  权限 scope      : {cfg['scope']}")
    print(f"  票有效期        : {cfg['ttl_days']} 天")
    print(f"  绑定账号/库     : {cfg['user_id']} / {cfg['vault_id']}")
    print(f"\n授权口令（只显示这一次，抄走）：\n\n    {password}\n")
    print("改完要重启 MCP 才生效（进程内缓存着配置）：systemctl restart ai-memory-mcp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
