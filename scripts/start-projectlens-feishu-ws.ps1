#Requires -Version 5.1
<#
.SYNOPSIS
  Start ProjectLens with Feishu long-connection ingress (no cloudflared).

.DESCRIPTION
  Context-manager path:
    Feishu @ / card click
      -> ProjectLens Feishu WS (long connection)
      -> POST localhost /api/v1/feishu/events
      -> preview card / card actions
      -> 「直接回答」 runs Hermes inside ProjectLens

  Do NOT run any other Feishu long-connection client for the same app
  (only one WS per app). Docs: docs/feishu-test-quickstart.md

  Feishu open platform:
    事件 + 回调 both use 「长连接」
    Subscribe im.message.receive_v1 and card.action.trigger
    App must have im:message.group_msg published for group history fine-select

.PARAMETER SkipApiStart
  Assume uvicorn already listens on :8000.
#>
[CmdletBinding()]
param(
    [string]$ProjectLensRoot = "",
    [string]$PythonExe = "",
    [switch]$SkipApiStart
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExe {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path $Preferred)) {
        return (Resolve-Path $Preferred).Path
    }
    if ($env:PYTHON_EXE -and (Test-Path $env:PYTHON_EXE)) {
        return (Resolve-Path $env:PYTHON_EXE).Path
    }
    if (Test-Path "D:\anaconda\envs\project-lens\python.exe") {
        return "D:\anaconda\envs\project-lens\python.exe"
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $resolved = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved.Trim() }
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { return $pythonCmd.Source }
    throw "Python not found. Pass -PythonExe or set PYTHON_EXE."
}

if (-not $ProjectLensRoot) {
    $ProjectLensRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
Set-Location $ProjectLensRoot
$PythonExe = Resolve-PythonExe -Preferred $PythonExe

# Stop conflicting processes: Hermes Feishu gateway + cloudflared tunnels.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'hermes_cli|gateway run' } |
    ForEach-Object {
        Write-Host "Stopping Hermes gateway pid=$($_.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'cloudflared.exe' -or ($_.CommandLine -match 'cloudflared') } |
    ForEach-Object {
        Write-Host "Stopping cloudflared pid=$($_.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'project_lens\.integrations\.feishu\.ws_ingress' } |
    ForEach-Object {
        Write-Host "Stopping previous WS ingress pid=$($_.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

$apiReady = $false
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health" -UseBasicParsing -TimeoutSec 2
    $apiReady = $true
} catch { }

if (-not $apiReady) {
    if ($SkipApiStart) {
        throw "ProjectLens API not ready on :8000 and -SkipApiStart was set."
    }
    Write-Host "Starting ProjectLens API on :8000 ..." -ForegroundColor Cyan
    Start-Process -FilePath $PythonExe -ArgumentList @(
        "-m", "uvicorn", "project_lens.main:app",
        "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $ProjectLensRoot -WindowStyle Minimized
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health" -UseBasicParsing -TimeoutSec 2
            $apiReady = $true
            break
        } catch { }
    }
    if (-not $apiReady) { throw "ProjectLens API did not become ready on :8000" }
}

$status = (Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/integrations/feishu/status" -UseBasicParsing -TimeoutSec 5).Content
Write-Host "Feishu status: $status" -ForegroundColor DarkGray

# Ensure lark-oapi is importable.
& $PythonExe -c "import lark_oapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing lark-oapi into this Python..." -ForegroundColor Yellow
    & $PythonExe -m pip install "lark-oapi>=1.4.0"
}

Write-Host ""
Write-Host "ProjectLens Feishu LONG-CONNECTION mode (preview cards)" -ForegroundColor Cyan
Write-Host "  API:     http://127.0.0.1:8000"
Write-Host "  Ingress: python -m project_lens.integrations.feishu.ws_ingress"
Write-Host "  Hermes:  inside ProjectLens on 「直接回答」 only"
Write-Host ""
Write-Host "Feishu console must use 长连接 for 事件 + 回调 (not HTTP request URL)." -ForegroundColor Yellow
Write-Host "Do not run Hermes Feishu gateway / cloudflared alongside this." -ForegroundColor Yellow
Write-Host ""
Write-Host "Starting WS ingress (Ctrl+C stops ingress only)..." -ForegroundColor Green

& $PythonExe -m project_lens.integrations.feishu.ws_ingress
