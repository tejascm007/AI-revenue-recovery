# Stops everything scripts/start_services.ps1 started. Does NOT touch infra
# (MongoDB/Kafka/Docker containers) - those are meant to stay up across dev
# sessions; use stop_infra.ps1 for that separately if you actually want them
# down too.
#
# Run:
#   powershell -ExecutionPolicy Bypass -File scripts/stop_services.ps1

$CodesRoot = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $CodesRoot ".run"

function Stop-ByPort($Name, $Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "$Name`: stopped (was pid $($conn.OwningProcess))"
    } else {
        Write-Host "$Name`: not running"
    }
}

function Stop-ByPidFile($Name) {
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (Test-Path $pidFile) {
        $targetPid = Get-Content $pidFile
        if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {
            Stop-Process -Id $targetPid -Force
            Write-Host "$Name`: stopped (was pid $targetPid)"
        } else {
            Write-Host "$Name`: pid file present but process already gone"
        }
        Remove-Item $pidFile -Force
    } else {
        Write-Host "$Name`: no pid file, assuming not running"
    }
}

Stop-ByPort "Backend" 8000
Stop-ByPort "Checkout Salvage Agent" 9002
Stop-ByPort "Recurring Revenue Agent" 9003
Stop-ByPort "Conversational NLP Agent" 9004
Stop-ByPort "B2B Receivables Agent" 9005
Stop-ByPidFile "orchestrator"
Stop-ByPidFile "watchdog-poller"

Write-Host "`nDone. Infra (Mongo/Kafka/Docker) left running - use stop_infra.ps1 if you want that down too." -ForegroundColor Cyan
