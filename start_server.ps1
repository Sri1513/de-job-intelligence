$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

# Activate virtual environment if it exists
if (Test-Path "venv\Scripts\Activate.ps1") {
    . "venv\Scripts\Activate.ps1"
}

if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Force -Path "logs" | Out-Null
}

Write-Host "🚀 Starting NEW platform (de-job-intelligence)..." -ForegroundColor Cyan

# Clear only its designated ports (8001 and 5002)
foreach ($port in @(8001, 5002)) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        $pidToKill = $conn.OwningProcess
        if ($pidToKill -and $pidToKill -ne 0) {
            Write-Host "🧹 Releasing port $port (PID: $pidToKill)..."
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
    }
}

# 1. MCP Server on 8001
$mcpLog = "logs\mcp.log"
$mcpPidFile = "logs\mcp.pid"
$mcpProcess = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "venv\Scripts\python.exe -m uvicorn src.protocols.app:app --host 0.0.0.0 --port 8001 > $mcpLog 2>&1" -PassThru
$mcpProcess.Id | Out-File -Encoding utf8 $mcpPidFile

# 2. Dashboard on 5002
$dashLog = "logs\dashboard.log"
$dashPidFile = "logs\dashboard.pid"
$dashProcess = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "venv\Scripts\python.exe -m uvicorn src.dashboard.app:app --host 0.0.0.0 --port 5002 > $dashLog 2>&1" -PassThru
$dashProcess.Id | Out-File -Encoding utf8 $dashPidFile

Start-Sleep -Seconds 3

# Verify MCP Server listening state
$mcpRunning = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue
if ($mcpRunning) {
    $savedMcpPid = Get-Content $mcpPidFile
    Write-Host "✅ New MCP Server running on http://localhost:8001 (PID: $savedMcpPid)" -ForegroundColor Green
} else {
    Write-Host "❌ New MCP Server failed to start. Last error from logs/mcp.log:" -ForegroundColor Red
    if (Test-Path $mcpLog) { Get-Content $mcpLog -Tail 12 }
    exit 1
}

# Verify Dashboard listening state
$dashRunning = Get-NetTCPConnection -LocalPort 5002 -ErrorAction SilentlyContinue
if ($dashRunning) {
    $savedDashPid = Get-Content $dashPidFile
    Write-Host "✅ New Dashboard running on http://localhost:5002 (PID: $savedDashPid)" -ForegroundColor Green
} else {
    Write-Host "❌ New Dashboard failed to start. Last error from logs/dashboard.log:" -ForegroundColor Red
    if (Test-Path $dashLog) { Get-Content $dashLog -Tail 12 }
    exit 1
}