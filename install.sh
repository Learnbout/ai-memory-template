#!/usr/bin/env bash
# ai-memory-template installer for Linux/macOS.
# Usage: bash install.sh [target_dir]   (default: ~/ai-memory)

set -euo pipefail

TARGET_DIR="${1:-$HOME/ai-memory}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/template"

echo "=== ai-memory-template 安装 ==="

# 1. Check Python
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[FAIL] 未找到 Python 3，请先安装 Python 3.10+"
  exit 1
fi
echo "[OK] Python: $($PY --version)"

# 2. Install MCP SDK
echo "[...] 安装 mcp 包..."
$PY -m pip install -q mcp
echo "[OK] mcp 已安装"

# 3. Create the vault
mkdir -p "$TARGET_DIR"
cp -R "$TEMPLATE_DIR/." "$TARGET_DIR/"
cp "$SCRIPT_DIR/server.py" "$TARGET_DIR/server.py"
echo "[OK] 知识库已创建: $TARGET_DIR"

# 4. Print MCP config snippets
echo ""
echo "=== 下一步：把下面的配置加入你的 AI 工具 ==="
echo ""
echo "Codex CLI (~/.codex/config.toml):"
echo "[mcp_servers]"
echo "[mcp_servers.ai-memory]"
echo "command = \"$PY\""
echo "args = [\"$TARGET_DIR/server.py\"]"
echo "env = { AI_MEMORY_DIR = \"$TARGET_DIR\" }"
echo ""
echo "Claude Desktop:"
echo '{ "mcpServers": { "ai-memory": {'
echo "  \"command\": \"$PY\","
echo "  \"args\": [\"$TARGET_DIR/server.py\"],"
echo "  \"env\": { \"AI_MEMORY_DIR\": \"$TARGET_DIR\" }"
echo '} } }'
echo ""
echo "教程: https://github.com/Learnbout/ai-memory-template/blob/master/docs/%E4%BD%BF%E7%94%A8%E6%95%99%E7%A8%8B.md"
