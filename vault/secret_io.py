"""密钥文件读写（纯标准库，不依赖任何 SDK）。

**为什么单独一个模块**（2026-10-23，阶段 4 起）：`mcp_auth.py` 负责 MCP 的令牌，
`脚本/web_auth.py` 负责 Web 的会话，两者要用**同一把** `<库根>/.workbuddy/token.key`。
但 `mcp_auth` import 了 `mcp_compat`（进而 import `mcp` SDK），而 Web 端根本不需要
MCP SDK —— 让一个网页服务因为「没装 mcp 包」而起不来是很荒谬的故障。

所以把「读密钥」这件事抽到这里：零依赖，谁都能用。`mcp_auth.load_secret` 保留为
薄封装，外部调用方不受影响（单一来源，不是复制粘贴）。

格式约定（历史见 mcp_auth 里那段说明，2026-10-23 的「31 字节」事故）：
  1. 64 位 hex 文本（新格式，纯 ASCII，无空白歧义）
  2. 32 字节原始二进制（旧格式，**一个字节都不能 strip**）
  3. 文本格式但带换行（手工创建的文件）先 strip 再判
"""

from __future__ import annotations

from pathlib import Path

MIN_SECRET_BYTES = 32


def load_secret(path: str | Path) -> bytes:
    """读密钥。长度不足 32 字节直接拒绝 —— 短密钥让 HMAC 失去意义。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到密钥文件：{path}（先用 脚本/issue_token.py init 生成）")

    raw = path.read_bytes()
    text = raw.strip()

    # 1. hex 格式：长度必须严格等于 2 倍，否则不进这个分支（避免与二进制混淆）
    if len(text) == MIN_SECRET_BYTES * 2 and all(
        c in b"0123456789abcdefABCDEF" for c in text
    ):
        return bytes.fromhex(text.decode("ascii"))

    # 2. 原始二进制：绝不 strip
    if len(raw) >= MIN_SECRET_BYTES:
        return raw

    # 3. 兜底：文本格式带换行
    if len(text) >= MIN_SECRET_BYTES:
        return text

    raise ValueError(
        f"密钥太短：{path} 只有 {len(raw)} 字节（去空白后 {len(text)}），"
        f"至少需要 {MIN_SECRET_BYTES} 字节"
    )
