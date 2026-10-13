#Requires -Version 5.1
<#
.SYNOPSIS
  Start ProjectLens with Feishu long-connection ingress (no cloudflared).

.DESCRIPTION
  Context-manager path:
    Feishu @ / card click
      -> ProjectLens API embedded FeishuLongConnection
      -> POST localhost /api/v1/feishu/events
      -> preview card / card actions
      -> 「直接回答」 runs Hermes inside ProjectLens

  This script starts only the ProjectLens API. The API owns the single
  Feishu long connection; standalone Hermes Gateway and ws_ingress are not used.
  Docs: docs/feishu-test-quickstart.md

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
    [switch]$SkipApiStart,
    [switch]$Foreground
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

# Stop conflicting processes: standalone Hermes Feishu gateway, legacy WS
# ingress, and cloudflared tunnels. ProjectLens API owns Feishu ingress.
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
    $apiProcess = Start-Process -FilePath $PythonExe -ArgumentList @(
        "-m", "uvicorn", "project_lens.main:app",
        "--host", "127.0.0.1", "--port", "8000", "--log-level", "info"
    ) -WorkingDirectory $ProjectLensRoot -WindowStyle $(if ($Foreground) { "Normal" } else { "Minimized" }) -PassThru
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if ($apiProcess.HasExited) {
            throw "ProjectLens API exited during startup (pid=$($apiProcess.Id), exit=$($apiProcess.ExitCode)). Run uvicorn manually in this directory to see the startup error."
        }
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health" -UseBasicParsing -TimeoutSec 2
            $apiReady = $true
            break
        } catch { }
    }
    if (-not $apiReady) { throw "ProjectLens API did not become ready on :8000" }
}

$status = $null
for ($i = 0; $i -lt 30; $i++) {
    $statusResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/integrations/feishu/status" -UseBasicParsing -TimeoutSec 5
    $status = $statusResponse.Content | ConvertFrom-Json
    Write-Host ("Feishu status: configured={0}, long_connection={1}, status={2}, hermes_bridge={3}" -f $status.configured, $status.long_connection_enabled, $status.long_connection_status, $status.hermes_bridge_active) -ForegroundColor DarkGray
    if (-not $status.configured) { throw "Feishu is not configured. Check PROJECT_LENS_FEISHU_APP_ID/APP_SECRET/VERIFICATION_TOKEN and project bindings." }
    if ($status.long_connection_status -eq "connected") { break }
    if ($status.long_connection_status -eq "error" -or $status.long_connection_status -eq "stopped") {
        throw "Feishu long connection failed (status=$($status.long_connection_status)). Check the API startup output and Feishu app long-connection settings."
    }
    Start-Sleep -Seconds 1
}
if ($null -eq $status -or $status.long_connection_status -ne "connected") {
    throw "Feishu long connection did not become connected within 30 seconds (last status=$($status.long_connection_status))."
}

# Ensure lark-oapi is importable.
& $PythonExe -c "import lark_oapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing lark-oapi into this Python..." -ForegroundColor Yellow
    & $PythonExe -m pip install "lark-oapi>=1.4.0"
}

Write-Host ""
Write-Host "ProjectLens Feishu LONG-CONNECTION mode (preview cards)" -ForegroundColor Cyan
Write-Host "  API:     http://127.0.0.1:8000"
Write-Host "  Ingress: ProjectLens API embedded FeishuLongConnection"
Write-Host "  Hermes:  inside ProjectLens on 「直接回答」 only"
Write-Host "  Gateway: disabled (standalone Hermes Feishu Gateway is stopped)"
Write-Host ""
Write-Host "Feishu console must use 长连接 for 事件 + 回调 (not HTTP request URL)." -ForegroundColor Yellow
Write-Host "Only this ProjectLens API may connect to the Feishu app." -ForegroundColor Green

Write-Host "Startup verified. Keep this PowerShell window open while testing." -ForegroundColor Green
if ($Foreground) {
    Write-Host "Foreground mode: API is running in its own console; press Ctrl+C there to stop it." -ForegroundColor Yellow
}
