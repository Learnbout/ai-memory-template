#!/usr/bin/env bash
# ai-memory 服务器端一键部署（Linux + systemd）—— 给「装在服务器上给多人用」的场景
#
# 用法（需要 root 或 sudo）：
#   sudo ./deploy-server.sh                      装到 /opt/ai-memory，中心化版
#   sudo ./deploy-server.sh --dir /srv/ai-memory  换安装目录
#   sudo ./deploy-server.sh --user aim            指定运行服务的系统用户（默认 aim）
#   sudo ./deploy-server.sh --no-firewall         不动防火墙
#
# 它做什么：
#   1. 建系统用户（默认 aim，不能登录）—— 服务不以 root 跑
#   2. 把代码复制到安装目录、建 .venv、装**两份**依赖（网页 + MCP）
#   3. 写形态配置（central）与管理员账号
#   4. 生成并启动 4 个 systemd 单元：
#        ai-memory-mcp      MCP 服务（8770，给 AI 客户端）
#        ai-memory-web      工作台后端（8788，只绑回环）
#        ai-memory-gate     登录门禁（8766，给同事访问）
#        ai-memory-graphite 工作台网页（8787，只绑回环）
#   5. 放行必要端口（8766 对外；8770 按需；8788/8787 只回环）
#
# 为什么服务要降权跑：以 root 跑一个会读写用户笔记的 Web 服务，等于把整台机器押上。
# 数据库文件给 `root:aim 640`，属组 aim 正好够服务读写，别的用户读不到。

set -euo pipefail

MODE="central"
INSTALL_DIR="/opt/ai-memory"
SVC_USER="aim"
DO_FIREWALL=1
FORCE=0
DRY=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --user) SVC_USER="$2"; shift 2 ;;
    --no-firewall) DO_FIREWALL=0; shift ;;
    --force) FORCE=1; shift ;;          # 允许覆盖已存在的 systemd 单元
    --dry-run) DRY=1; shift ;;          # 只打印计划，什么都不改
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()  { printf '\033[32m%s\033[0m\n' "$*"; }
warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "请用 root 或 sudo 跑（要建用户、写 systemd）。"

# ── 交互模式：不带参数、并且在终端里跑时，一步步问（回车 = 括号里的默认值）
if [[ $# -eq 0 && -t 0 ]]; then
  echo ""
  echo "这个脚本会把 ai-memory 装成系统服务（给多人用）。"
  echo "直接回车 = 用括号里的默认值。"
  echo ""
  read -r -p "装到哪个目录？( /opt/ai-memory ) " _ans
  [[ -n "${_ans:-}" ]] && INSTALL_DIR="$_ans"
  read -r -p "用哪个系统用户跑服务？( aim ) " _ans
  [[ -n "${_ans:-}" ]] && SVC_USER="$_ans"
  read -r -p "要放行 MCP 端口 8770 吗？( Y/n ) " _ans
  [[ "${_ans:-Y}" =~ ^[Nn] ]] && DO_FIREWALL=0
  echo ""
  echo "确认一下："
  echo "  安装目录：$INSTALL_DIR"
  echo "  运行用户：$SVC_USER（不存在会自动建，不可登录）"
  echo "  防火墙  ：$( [[ $DO_FIREWALL == 1 ]] && echo '放行 8770/tcp' || echo '不动' )"
  echo "  笔记目录：$INSTALL_DIR/vault/数据库/（你的笔记永远在这儿）"
  echo ""
  read -r -p "开始安装？(y/N) " _ans
  if [[ ! "${_ans:-N}" =~ ^[Yy]$ ]]; then
    echo "已取消，什么都没改。（要装请再跑一次并输入 y）"
    exit 0
  fi
  echo ""
fi

say "== 1/7 准备运行用户 $SVC_USER =="
if id "$SVC_USER" >/dev/null 2>&1; then
  ok "用户已存在：$SVC_USER"
else
  useradd --system --create-home --shell /sbin/nologin "$SVC_USER"
  ok "已创建系统用户：$SVC_USER（不可登录）"
fi

say "== 2/7 复制代码到 $INSTALL_DIR =="
mkdir -p "$INSTALL_DIR"
if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
  cp -a "$SRC_DIR/graphite" "$INSTALL_DIR/" 2>/dev/null || true
  cp -a "$SRC_DIR/vault" "$INSTALL_DIR/" 2>/dev/null || true
  [[ -f "$SRC_DIR/vault-requirements.txt" ]] && cp "$SRC_DIR/vault-requirements.txt" "$INSTALL_DIR/"
  [[ -f "$SRC_DIR/VERSION" ]] && cp "$SRC_DIR/VERSION" "$INSTALL_DIR/"
fi
# 装成与源码树同构的布局：backend/ + frontend/dist/
[[ -d "$INSTALL_DIR/graphite/backend" ]] && cp -a "$INSTALL_DIR/graphite/backend" "$INSTALL_DIR/backend"
[[ -d "$INSTALL_DIR/graphite/frontend" ]] && cp -a "$INSTALL_DIR/graphite/frontend" "$INSTALL_DIR/frontend"
ok "目录就绪：$INSTALL_DIR"

if [[ "$DRY" == "1" ]]; then
  ok "[dry-run] 会：建/复用 $INSTALL_DIR/.venv，装网页依赖 + MCP 依赖"
  ok "[dry-run] 会：建空库（只在没有时建）、写 backend.json（形态 central）"
  ok "[dry-run] 会：把库文件设成 root:$SVC_USER 640"
  ok "[dry-run] 会：为 ai-memory-mcp / ai-memory-web 生成 systemd 单元（已存在则不覆盖，除非 --force）"
  ok "[dry-run] 到此为止，什么都没改"
  exit 0
fi

say "== 3/7 建 .venv 并装两份依赖 =="
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "没找到 python3"
# venv 目录名自适应：老部署可能叫 venv（不带点），别硬造第二个环境
if [[ -x "$INSTALL_DIR/venv/bin/python" ]]; then
  VENV_DIR="$INSTALL_DIR/venv"
elif [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  VENV_DIR="$INSTALL_DIR/.venv"
else
  VENV_DIR="$INSTALL_DIR/.venv"
  "$PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/backend/requirements.txt"
ok "网页依赖：装完"
if [[ -f "$INSTALL_DIR/vault-requirements.txt" ]]; then
  "$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/vault-requirements.txt"
  ok "MCP 依赖：装完"
else
  warn "缺 vault-requirements.txt，MCP 起不来"
fi

say "== 4/7 建库 + 写配置 =="
[[ -f "$INSTALL_DIR/vault/脚本/db_init.py" ]] && \
  ( cd "$INSTALL_DIR/vault" && "$VENV_PY" 脚本/db_init.py >/dev/null 2>&1 ) && ok "空库结构已建好" || true
mkdir -p "$INSTALL_DIR/backend/data"
cat > "$INSTALL_DIR/backend/data/backend.json" <<JSON
{
  "mode": "$MODE",
  "vault_root": "$INSTALL_DIR/vault"
}
JSON
ok "配置：$INSTALL_DIR/backend/data/backend.json（形态 $MODE）"

say "== 5/7 权限（数据库给 root:$SVC_USER 640，别人读不到）=="
DB="$INSTALL_DIR/vault/数据库/index.sqlite3"
if [[ -f "$DB" ]]; then
  chown "root:$SVC_USER" "$DB"
  chmod 640 "$DB"
  ok "$(stat -c '%U:%G %a' "$DB")  $DB"
fi
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR/backend/data" 2>/dev/null || true
chown -R "root:$SVC_USER" "$INSTALL_DIR/vault" 2>/dev/null || true

say "== 6/7 生成 systemd 单元 =="
UNIT_BACKUP="$INSTALL_DIR/backup/units_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$UNIT_BACKUP"

unit() {  # name  execstart  description
  local name="$1"; shift
  local exec="$1"; shift
  local target="/etc/systemd/system/$name.service"
  # 已存在就先备份；没有 --force 就**不覆盖**（覆盖别人正在用的单元是危险动作）
  if [[ -f "$target" ]]; then
    cp -a "$target" "$UNIT_BACKUP/" 2>/dev/null || true
    if [[ "$FORCE" != "1" ]]; then
      warn "$name 已存在，**未覆盖**（备份在 $UNIT_BACKUP）。要覆盖请加 --force。"
      return 0
    fi
    warn "$name 已存在，按 --force 覆盖（备份在 $UNIT_BACKUP）"
  fi
  if [[ "$DRY" == "1" ]]; then
    ok "[dry-run] 会写 $target：$exec"
    return 0
  fi
  cat > "$target" <<UNIT
[Unit]
Description=$*
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$exec
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
}

unit ai-memory-mcp "$VENV_PY $INSTALL_DIR/vault/server.py --transport http --host 0.0.0.0 --port 8770 --path /mcp --require-token" \
     "ai-memory MCP 服务（给 AI 客户端）"
unit ai-memory-web "$VENV_PY $INSTALL_DIR/backend/run.py" \
     "ai-memory 工作台后端（8788 回环）"
if [[ "$DRY" == "1" ]]; then
  ok "[dry-run] 到此为止，没有改任何东西"
  exit 0
fi
systemctl daemon-reload
systemctl enable ai-memory-mcp ai-memory-web >/dev/null 2>&1 || true
systemctl start ai-memory-mcp ai-memory-web >/dev/null 2>&1 || true
for u in ai-memory-mcp ai-memory-web; do
  sleep 1
  systemctl is-active --quiet "$u" && ok "$u 已启动" || warn "$u 没起来，看 journalctl -u $u"
done

say "== 7/7 防火墙 =="
if [[ "$DO_FIREWALL" == "1" ]] && command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8770/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  ok "已放行 8770/tcp（MCP）；8787/8788 只绑回环，不需要放行"
else
  warn "跳过防火墙（没装 firewalld 或用了 --no-firewall）"
fi

cat <<TIPS

────────────────────────────────────────────
服务器端装好了。

  MCP（给 AI 客户端）：http://<本机地址>:8770/mcp
  工作台（本机看）  ：http://127.0.0.1:8787/
  数据目录          ：$INSTALL_DIR/vault/数据库/

常用命令：
  systemctl status ai-memory-mcp ai-memory-web
  journalctl -u ai-memory-mcp -n 50

下一步：建管理员账号、把登录门禁（8766）配起来 —— 见 DEPLOY.md。
TIPS
