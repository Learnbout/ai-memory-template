# ai-memory 一键安装（Windows）—— 本地版或中心化版
#
# 用法：
#   .\install.ps1                本地版（默认）
#   .\install.ps1 -Central       中心化版
#   .\install.ps1 -Dir D:\ai-memory  指定安装目录（默认：脚本所在目录）
#   .\install.ps1 -NoStart       只装，不启动
#
# 它做什么：
#   1. 查 Python（要 3.10+）
#   2. 建独立 .venv
#   3. 装**两份**依赖：网页工作台 + MCP 服务端
#   4. 建空库（只建表结构，不动笔记）
#   5. 写 backend/data/backend.json（形态钉死）
#   6. 起工作台（8787）与 MCP 服务（8770）
#
# 为什么必须两份依赖：给 AI 客户端用的 MCP 服务是**另一个程序**（vault\server.py），
# 只装网页依赖会让 8770 起不来，表现就是「装了 AI 却连不上」。2026-11-19 踩过。
#
# 想要「双击就能装、自带运行环境、不用先装 Python」的，用 dist 里的
# ai-memory-installer-local.exe（便携版，自带 Python 与全部依赖）。

param(
    [switch]$Central,
    [switch]$NoStart,
    [string]$Dir = "",
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Say($m, $c = 'Cyan') { Write-Host $m -ForegroundColor $c }
function Ok($m) { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Die($m) { Write-Host $m -ForegroundColor Red; exit 1 }

$mode = if ($Central) { 'central' } else { 'local' }
if (-not $Dir) { $Dir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Dir = (Resolve-Path $Dir).Path

# 仓库里的树是 graphite\backend + vault；安装后要 backend + frontend\dist
$graphite = Join-Path $Dir 'graphite'
if (-not (Test-Path $graphite)) { $graphite = $Dir }

Say "== 1/6 检查 Python =="
$py = $Python
if (-not $py) {
    foreach ($cand in @('python', 'python3', 'py')) {
        $g = Get-Command $cand -ErrorAction SilentlyContinue
        if ($g) { $py = $g.Source; break }
    }
}
if (-not $py) { Die "没找到 Python。先装 Python 3.10+（安装时勾 Add to PATH）。" }
$ver = & $py -c "import sys;print('%d.%d' % sys.version_info[:2])"
Ok "Python $ver ($py)"
if ([version]$ver -lt [version]'3.10') { Die "版本太低，需要 3.10 以上。" }

Say "== 2/6 建虚拟环境 .venv =="
$venvPy = Join-Path $Dir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { & $py -m venv (Join-Path $Dir '.venv') }
if (-not (Test-Path $venvPy)) { Die "虚拟环境没建成功：$venvPy" }
Ok "虚拟环境：$(Join-Path $Dir '.venv')"

Say "== 3/6 装依赖（两份，可能需要一两分钟）=="
& $venvPy -m pip install --quiet --upgrade pip
$reqWeb = Join-Path $graphite 'backend\requirements.txt'
if (-not (Test-Path $reqWeb)) { Die "找不到网页依赖清单：$reqWeb" }
& $venvPy -m pip install --quiet -r $reqWeb
Ok "网页工作台依赖：装完"
$reqMcp = Join-Path $Dir 'vault-requirements.txt'
if (Test-Path $reqMcp) {
    & $venvPy -m pip install --quiet -r $reqMcp
    Ok "MCP 服务端依赖：装完"
} else {
    Warn "没有 vault-requirements.txt，跳过 MCP 依赖 —— AI 客户端会连不上 8770"
}

Say "== 4/6 建空库（只建表结构，不动笔记）=="
$initPy = Join-Path $Dir 'vault\脚本\db_init.py'
if (Test-Path $initPy) {
    Push-Location (Join-Path $Dir 'vault')
    try { & $venvPy $initPy *> $null; Ok "空库结构已建好" } catch { Warn "建库跳过（首次启动会再试）" }
    finally { Pop-Location }
} else { Warn "没有 db_init.py，跳过" }

Say "== 5/6 写配置（形态：$mode）=="
$dataDir = Join-Path $Dir 'backend\data'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$cfg = Join-Path $dataDir 'backend.json'
if (Test-Path $cfg) { Copy-Item $cfg "$cfg.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"; Warn "已有配置，已备份" }
@{ mode = $mode; vault_root = (Join-Path $Dir 'vault') } | ConvertTo-Json | Set-Content $cfg -Encoding UTF8
Get-Content $cfg

if ($NoStart) {
    Say "== 6/6 按要求不启动 =="
    Ok "装好了，自己启动："
    Write-Host "  工作台：$venvPy $(Join-Path $Dir 'backend\run.py')"
    Write-Host "  MCP   ：$venvPy -m memory_runtime --transport http --port 8770   （工作目录 $Dir\vault）"
    exit 0
}

Say "== 6/6 启动服务 =="
function Test-Port($port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $port)
        $c.Close(); return $true
    } catch { return $false }
}
if (-not (Test-Port 8787)) {
    Start-Process -FilePath $venvPy -ArgumentList (Join-Path $Dir 'backend\run.py') -WorkingDirectory $Dir -WindowStyle Hidden
    Ok "工作台已启动"
} else { Ok "工作台已经在 8787 上跑着" }
if (-not (Test-Port 8770)) {
    Start-Process -FilePath $venvPy -ArgumentList '-m','memory_runtime','--transport','http','--port','8770' -WorkingDirectory (Join-Path $Dir 'vault') -WindowStyle Hidden
    Ok "MCP 服务已启动"
} else { Ok "MCP 服务已经在 8770 上跑着" }
Start-Sleep -Seconds 6

Write-Host ""
Write-Host "────────────────────────────────────────────"
Ok "装好了。"
Write-Host "  工作台： http://127.0.0.1:8787/"
Write-Host "  MCP   ： http://127.0.0.1:8770/mcp"
Write-Host "  形态  ： $mode"
Write-Host ("  MCP 状态：" + $(if (Test-Port 8770) { '就绪' } else { '未就绪，看 backend\data\mcp.log' }))
Write-Host ""
Write-Host "接给 AI 客户端：在它的 MCP 配置里加 remote/http → http://127.0.0.1:8770/mcp"
if ($mode -eq 'central') {
    Write-Host ""
    Write-Host "中心化版还需要：8766（登录门禁）对同事开放；8788 只绑本机；建第一个管理员账号。" -ForegroundColor Yellow
}
