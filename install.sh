#!/usr/bin/env bash
# ai-memory 一键安装（Linux / macOS）—— 本地版或中心化版
#
# 用法：
#   ./install.sh                       本地版（默认）：只给本机用，不用登录
#   ./install.sh --central             中心化版：装在这台机器上给多人用，要登录
#   ./install.sh --dir /opt/ai-memory  指定安装目录（默认：脚本所在目录）
#   ./install.sh --no-start            只装，不启动
#
# 它做什么（每步都会打印出来，能看懂）：
#   1. 查 Python（要 3.10+）
#   2. 建独立 .venv（不污染系统 Python）
#   3. 装**两份**依赖：网页工作台一份 + MCP 服务端一份
#   4. 建空库（只建表结构，不动任何笔记）
#   5. 写 backend/data/backend.json，把形态钉死
#   6. 启动工作台（8787）和 MCP 服务（8770）
#
# 为什么必须装两份依赖：给 AI 客户端用的 MCP 服务（vault/server.py）是**另一个程序**，
# 只装网页依赖会让 8770 起不来，表现就是「装了 AI 却连不上」。2026-11-19 踩过。
#
# 它**不做**什么：不装系统服务、不动防火墙、不碰系统 Python。
# 要开机自启请用同目录的 deploy-server.sh（它会生成 systemd 单元，需要 sudo）。

set -euo pipefail

MODE="local"
DO_START=1
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --central) MODE="central"; shift ;;
    --local)   MODE="local"; shift ;;
    --dir)     INSTALL_DIR="$2"; shift 2 ;;
    --no-start) DO_START=0; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()  { printf '\033[32m%s\033[0m\n' "$*"; }
warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── 目录布局兼容：仓库里的树是 graphite/backend + vault；安装后要 backend + frontend/dist
GRAPHITE="$INSTALL_DIR/graphite"
[[ -d "$GRAPHITE" ]] || GRAPHITE="$INSTALL_DIR"

# ── 交互模式：不带参数且在终端里跑时，问一句形态（回车 = 本地版）
if [[ $# -eq 0 && -t 0 ]]; then
  echo ""
  echo "ai-memory 安装：这份装给谁用？"
  echo "  1) 只有我自己用（本机，不用登录）      ← 默认"
  echo "  2) 和同事一起用（多人，要登录）"
  read -r -p "选 1 或 2 ( 1 ) " _ans
  [[ "${_ans:-1}" == "2" ]] && MODE="central"
  echo ""
fi

say "== 1/6 检查 Python =="
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "没找到 python3。先装 Python 3.10+ 再来。"
"$PY" - <<'EOF' || die "Python 版本太低，需要 3.10 以上。"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
"$PY" -V

say "== 2/6 建虚拟环境 .venv =="
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  "$PY" -m venv "$INSTALL_DIR/.venv"
fi
VENV_PY="$INSTALL_DIR/.venv/bin/python"
[[ -x "$VENV_PY" ]] || die "虚拟环境没建成功：$VENV_PY 不存在"
ok "虚拟环境：$INSTALL_DIR/.venv"

say "== 3/6 装依赖（两份，可能需要一两分钟）=="
"$VENV_PY" -m pip install --quiet --upgrade pip
REQ_WEB="$GRAPHITE/backend/requirements.txt"
REQ_MCP="$INSTALL_DIR/vault-requirements.txt"
[[ -f "$REQ_WEB" ]] || die "找不到网页依赖清单：$REQ_WEB"
"$VENV_PY" -m pip install --quiet -r "$REQ_WEB"
ok "网页工作台依赖：装完"
if [[ -f "$REQ_MCP" ]]; then
  "$VENV_PY" -m pip install --quiet -r "$REQ_MCP"
  ok "MCP 服务端依赖：装完"
else
  warn "没有 vault-requirements.txt，跳过 MCP 依赖 —— AI 客户端会连不上 8770"
fi

say "== 4/6 建空库（只建表结构，不动笔记）=="
if [[ -f "$INSTALL_DIR/vault/脚本/db_init.py" ]]; then
  ( cd "$INSTALL_DIR/vault" && "$VENV_PY" 脚本/db_init.py >/dev/null 2>&1 ) \
    && ok "空库结构已建好" || warn "建库跳过（不影响使用，首次启动会再试）"
else
  warn "没有 db_init.py，跳过"
fi

say "== 5/6 写配置（形态：$MODE）=="
mkdir -p "$INSTALL_DIR/backend/data"
CFG="$INSTALL_DIR/backend/data/backend.json"
[[ -f "$CFG" ]] && cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)" && warn "已有配置，已备份"
cat > "$CFG" <<JSON
{
  "mode": "$MODE",
  "vault_root": "$INSTALL_DIR/vault"
}
JSON
cat "$CFG"

if [[ "$DO_START" == "0" ]]; then
  say "== 6/6 按要求不启动 =="
  cat <<TIPS

装好了，自己启动：
  工作台：$VENV_PY $INSTALL_DIR/backend/run.py
  MCP   ：$VENV_PY $INSTALL_DIR/.runtime/mcp_launch.py --transport http --port 8770
          （没有 .runtime 时用它：$VENV_PY $INSTALL_DIR/vault/server.py --transport http --port 8770）
TIPS
  exit 0
fi

say "== 6/6 启动服务 =="
mkdir -p "$INSTALL_DIR/backend/data"
LOG_DIR="$INSTALL_DIR/backend/data"
# 工作台（8787）
if ! (exec 3<>/dev/tcp/127.0.0.1/8787) 2>/dev/null; then
  nohup "$VENV_PY" "$INSTALL_DIR/backend/run.py" >> "$LOG_DIR/workbench.log" 2>&1 &
  ok "工作台已启动（日志：$LOG_DIR/workbench.log）"
else
  ok "工作台已经在 8787 上跑着"
fi
# MCP（8770）
if ! (exec 3<>/dev/tcp/127.0.0.1/8770) 2>/dev/null; then
  if [[ -f "$INSTALL_DIR/.runtime/mcp_launch.py" ]]; then
    nohup "$VENV_PY" "$INSTALL_DIR/.runtime/mcp_launch.py" --transport http --port 8770 \
      >> "$LOG_DIR/mcp.log" 2>&1 &
  else
    ( cd "$INSTALL_DIR/vault" && nohup "$VENV_PY" server.py --transport http --port 8770 \
      >> "$LOG_DIR/mcp.log" 2>&1 & )
  fi
  ok "MCP 服务已启动（日志：$LOG_DIR/mcp.log）"
else
  ok "MCP 服务已经在 8770 上跑着"
fi

# 等它们真正起来再报地址
for i in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/8787) 2>/dev/null && break
  sleep 1
done
(exec 3<>/dev/tcp/127.0.0.1/8770) 2>/dev/null && MCP_OK=1 || MCP_OK=0

cat <<TIPS

────────────────────────────────────────────
装好了。

  工作台： http://127.0.0.1:8787/       （只有本机能打开）
  MCP   ： http://127.0.0.1:8770/mcp    （给 AI 客户端用）
  形态  ： $MODE
  MCP 状态： $([[ "$MCP_OK" == "1" ]] && echo 就绪 || echo 未就绪，看 $LOG_DIR/mcp.log)

想把它接给 AI 客户端（Cursor / opencode / Codex …），在 AI 里加一条 MCP：
  type = remote / url = http://127.0.0.1:8770/mcp
TIPS

if [[ "$MODE" == "central" ]]; then
  cat <<'TIPS'

中心化版还需要：
  · 8766（登录门禁）要对同事开放；8788（工作台后端）只能绑本机；
  · 建第一个管理员账号（细节见 DEPLOY.md「中心化版」一节）；
  · 开机自启用 deploy-server.sh，它会生成 systemd 单元。
TIPS
fi
