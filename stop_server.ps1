$ErrorActionPreference = "SilentlyContinue"
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

Write-Host "🛑 Stopping NEW platform (de-job-intelligence)..." -ForegroundColor Yellow

# Terminate processes using saved PID files
foreach ($pidFile in @("logs\mcp.pid", "logs\dashboard.pid")) {
    if (Test-Path $pidFile) {
        $targetPid = Get-Content $pidFile
        if ($targetPid) {
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

# Force-release ports 8001 and 5002
foreach ($port in @(8001, 5002)) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        $pidToKill = $conn.OwningProcess
        if ($pidToKill -and $pidToKill -ne 0) {
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "✅ New platform stopped." -ForegroundColor Green