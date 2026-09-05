# Starts the application layer: the FastAPI backend, the Main Orchestrator,
# the watchdog poller, and all 4 A2A sub-agents (each of which spawns its own
# MCP server subprocesses on first request - nothing extra to start for those).
# Run scripts/start_infra.ps1 first. Idempotent: skips anything already up.
#
# Run:
#   powershell -ExecutionPolicy Bypass -File scripts/start_services.ps1

$ErrorActionPreference = "Stop"
$CodesRoot = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $CodesRoot ".run"
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

function Test-PortListening($Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conn
}

function Start-HttpService($Name, $ScriptPath, $Port) {
    if (Test-PortListening $Port) {
        Write-Host "$Name`: already running on port $Port"
        return
    }
    $proc = Start-Process -FilePath "uv" -ArgumentList "run", "python", $ScriptPath `
        -WorkingDirectory $CodesRoot -WindowStyle Hidden -PassThru
    $proc.Id | Out-File (Join-Path $PidDir "$Name.pid")
    Write-Host "$Name`: starting (pid $($proc.Id), port $Port)..."
}

function Start-LoopService($Name, $ScriptPath) {
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (Test-Path $pidFile) {
        $existingPid = Get-Content $pidFile
        if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
            Write-Host "$Name`: already running (pid $existingPid)"
            return
        }
    }
    $proc = Start-Process -FilePath "uv" -ArgumentList "run", "python", $ScriptPath `
        -WorkingDirectory $CodesRoot -WindowStyle Hidden -PassThru
    $proc.Id | Out-File $pidFile
    Write-Host "$Name`: starting (pid $($proc.Id))..."
}

Write-Host "=== FastAPI backend ===" -ForegroundColor Cyan
Start-HttpService "backend" "services/backend/main.py" 8000

Write-Host "`n=== A2A sub-agents ===" -ForegroundColor Cyan
Start-HttpService "checkout-salvage-agent" "services/agents/checkout_salvage_agent/main.py" 9002
Start-HttpService "recurring-revenue-agent" "services/agents/recurring_revenue_agent/main.py" 9003
Start-HttpService "conversational-nlp-agent" "services/agents/conversational_nlp_agent/main.py" 9004
Start-HttpService "b2b-receivables-agent" "services/agents/b2b_receivables_agent/main.py" 9005

Write-Host "`n=== Background loops (Kafka consumer, watchdog poller) ===" -ForegroundColor Cyan
Start-LoopService "orchestrator" "services/orchestrator/main.py"
Start-LoopService "watchdog-poller" "services/watchdog_poller/main.py"

Write-Host "`nGiving services a few seconds to bind..." -ForegroundColor Cyan
Start-Sleep -Seconds 6

Write-Host "`n=== Health check ===" -ForegroundColor Cyan
foreach ($svc in @(
    @{Name="Backend"; Url="http://localhost:8000/health"},
    @{Name="Checkout Salvage Agent"; Url="http://localhost:9002/.well-known/agent-card.json"},
    @{Name="Recurring Revenue Agent"; Url="http://localhost:9003/.well-known/agent-card.json"},
    @{Name="Conversational NLP Agent"; Url="http://localhost:9004/.well-known/agent-card.json"},
    @{Name="B2B Receivables Agent"; Url="http://localhost:9005/.well-known/agent-card.json"}
)) {
    try {
        $resp = Invoke-WebRequest -Uri $svc.Url -TimeoutSec 5 -UseBasicParsing
        Write-Host "$($svc.Name): HTTP $($resp.StatusCode)" -ForegroundColor Green
    } catch {
        Write-Warning "$($svc.Name): not responding yet - check its own log or give it a few more seconds"
    }
}

Write-Host "`nDone. Orchestrator and watchdog-poller have no HTTP port - check .run/*.pid and Get-Process to confirm they're alive." -ForegroundColor Cyan
Write-Host "To expose the backend publicly for real Razorpay/Meta webhooks: see README.md's 'Public webhooks' section." -ForegroundColor Cyan
