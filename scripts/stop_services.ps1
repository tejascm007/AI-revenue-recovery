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

function Stop-ByPidFile($Name, $ScriptPath) {
    # Real bug found live (2026-09-05): Start-Process launches these via
    # "uv run python <ScriptPath>", and the PID recorded in the .pid file is
    # uv's own wrapper PID, not the actual Python process - killing just that
    # PID can leave the real interpreter (a different PID, holding the Kafka
    # consumer group) running orphaned, invisible to this script from then on
    # since the next start_services.ps1 run just writes a fresh .pid file over
    # it. Verified this had actually happened: 4 separate orchestrator.py
    # processes and 4 watchdog_poller.py processes were all found still alive
    # via `Get-CimInstance Win32_Process`, accumulated silently across past
    # sessions. Matching by command line and killing every match, rather than
    # trusting one recorded PID, is the only reliable way to actually stop
    # this - mirrors what Stop-ByPort already does correctly for the 5 HTTP
    # services (whatever process really holds the port gets killed, not
    # whatever PID happened to get recorded).
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$ScriptPath*" }
    if ($procs) {
        foreach ($proc in $procs) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Write-Host "$Name`: stopped ($($procs.Count) matching process(es): $($procs.ProcessId -join ', '))"
    } else {
        Write-Host "$Name`: not running"
    }
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
}

Stop-ByPort "Backend" 8000
Stop-ByPort "Checkout Salvage Agent" 9002
Stop-ByPort "Recurring Revenue Agent" 9003
Stop-ByPort "Conversational NLP Agent" 9004
Stop-ByPort "B2B Receivables Agent" 9005
Stop-ByPidFile "orchestrator" "services/orchestrator/main.py"
Stop-ByPidFile "watchdog-poller" "services/watchdog_poller/main.py"

Write-Host "`nDone. Infra (Mongo/Kafka/Docker) left running - use stop_infra.ps1 if you want that down too." -ForegroundColor Cyan
