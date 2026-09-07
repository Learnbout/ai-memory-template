#!/usr/bin/env pwsh
# ai-memory-template installer for Windows.
# Usage: .\install.ps1 [-TargetDir D:\ai-memory]

param(
    [string]$TargetDir = "$HOME\ai-memory"
)

$ErrorActionPreference = "Stop"

Write-Host "=== ai-memory-template 安装 ===" -ForegroundColor Cyan

# 1. Check Python
try {
    $py = (Get-Command python -ErrorAction Stop).Source
    Write-Host "[OK] Python: $(& $py --version)" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] 未找到 Python 3，请先安装 Python 3.10+" -ForegroundColor Red
    exit 1
}

# 2. Install MCP SDK
Write-Host "[...] 安装 mcp 包..." -ForegroundColor Yellow
& $py -m pip install -q mcp
Write-Host "[OK] mcp 已安装" -ForegroundColor Green

# 3. Create the vault
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$templateDir = Join-Path $scriptDir "template"
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
Copy-Item "$templateDir\*" -Destination $TargetDir -Recurse -Force
Copy-Item (Join-Path $scriptDir "server.py") -Destination (Join-Path $TargetDir "server.py") -Force
Write-Host "[OK] 知识库已创建: $TargetDir" -ForegroundColor Green

# 4. Print MCP config snippets
Write-Host ""
Write-Host "=== 下一步：把下面的配置加入你的 AI 工具 ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "--- Codex CLI ($env:USERPROFILE\.codex\config.toml) ---"
Write-Host "[mcp_servers]"
Write-Host "[mcp_servers.ai-memory]"
Write-Host "command = ""$py"""
Write-Host "args = [""$TargetDir\server.py""]"
Write-Host "env = { AI_MEMORY_DIR = ""$TargetDir"" }"
Write-Host ""
Write-Host "--- Claude Desktop ---"
Write-Host '{'
Write-Host '  "mcpServers": {'
Write-Host '    "ai-memory": {'
Write-Host "      ""command"": ""$py"","
Write-Host "      ""args"": [""$TargetDir\server.py""],"
Write-Host "      ""env"": { ""AI_MEMORY_DIR"": ""$TargetDir"" }"
Write-Host '    }'
Write-Host '  }'
Write-Host '}'
Write-Host ""
Write-Host "教程: https://github.com/Learnbout/ai-memory-template/blob/master/docs/%E4%BD%BF%E7%94%A8%E6%95%99%E7%A8%8B.md"
