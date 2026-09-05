# Starts the infrastructure layer (MongoDB, Kafka, Redis, the RAG MongoDB
# deployment) - everything the application layer (services/) depends on.
# Idempotent: safe to re-run, skips anything already up.
#
# Native processes (not containerized, matching how this project was
# actually built and verified all session): mongod and Kafka run as plain
# Windows processes, not Docker - adjust $MongodPath / $KafkaHome below to
# match your own install locations.
#
# Run:
#   powershell -ExecutionPolicy Bypass -File scripts/start_infra.ps1

$ErrorActionPreference = "Stop"

# --- Adjust these to your own install paths -----------------------------
$MongodPath = "C:\Users\ADMIN\OneDrive\Documents\career\mongo\bin\mongod.exe"
$MongoDbPath = "C:\Users\ADMIN\OneDrive\Documents\career\Razorpay\data\db"
$KafkaHome = "C:\Users\ADMIN\OneDrive\Documents\career\kafka_2.13-4.1.0\kafka_2.13-4.1.0"
# --------------------------------------------------------------------------

function Test-PortListening($Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conn
}

Write-Host "=== MongoDB (main, port 27017) ===" -ForegroundColor Cyan
if (Test-PortListening 27017) {
    Write-Host "already running"
} else {
    Start-Process -FilePath $MongodPath -ArgumentList "--dbpath", "`"$MongoDbPath`"" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    if (Test-PortListening 27017) { Write-Host "started" -ForegroundColor Green }
    else { Write-Warning "mongod did not come up - check its own logs" }
}

Write-Host "`n=== Kafka broker (port 9092) ===" -ForegroundColor Cyan
if (Test-PortListening 9092) {
    Write-Host "already running"
} else {
    Push-Location $KafkaHome
    Start-Process -FilePath "java" -ArgumentList "-cp", "`"libs/*`"", "kafka.Kafka", "config/server.properties" -WindowStyle Hidden
    Pop-Location
    Start-Sleep -Seconds 8
    if (Test-PortListening 9092) { Write-Host "started" -ForegroundColor Green }
    else { Write-Warning "Kafka did not come up - check it's already formatted (scripts/README.md has the one-time setup)" }
}

Write-Host "`n=== Docker containers (Redis + RAG MongoDB deployment) ===" -ForegroundColor Cyan
try {
    docker ps > $null 2>&1
    if (-not $?) { throw "Docker daemon not reachable" }
} catch {
    Write-Warning "Docker Desktop doesn't appear to be running - start it, then re-run this script."
    exit 1
}

$existing = docker ps -a --format "{{.Names}}"
if ($existing -contains "revenue-recovery-redis") {
    docker start revenue-recovery-redis | Out-Null
    Write-Host "revenue-recovery-redis: started"
} else {
    docker run -d --name revenue-recovery-redis -p 6379:6379 redis:7-alpine | Out-Null
    Write-Host "revenue-recovery-redis: created and started (first run)"
}

if ($existing -contains "revenue-recovery-atlas-local") {
    docker start revenue-recovery-atlas-local | Out-Null
    Write-Host "revenue-recovery-atlas-local: started"
} else {
    docker run -d --name revenue-recovery-atlas-local -p 27018:27017 mongodb/mongodb-atlas-local:latest | Out-Null
    Write-Host "revenue-recovery-atlas-local: created and started (first run - will need scripts/rag_db_setup.py afterward)"
}

Write-Host "`nWaiting for the RAG deployment's health check..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 3
    $status = docker inspect --format "{{.State.Health.Status}}" revenue-recovery-atlas-local 2>$null
} while ($status -ne "healthy" -and (Get-Date) -lt $deadline)
if ($status -eq "healthy") { Write-Host "healthy" -ForegroundColor Green }
else { Write-Warning "RAG deployment did not report healthy within 60s - check 'docker logs revenue-recovery-atlas-local'" }

Write-Host "`n=== Infra check ===" -ForegroundColor Cyan
Write-Host "MongoDB (main): $(if (Test-PortListening 27017) {'up'} else {'DOWN'})"
Write-Host "Kafka:          $(if (Test-PortListening 9092) {'up'} else {'DOWN'})"
Write-Host "Redis:          $(if (Test-PortListening 6379) {'up'} else {'DOWN'})"
Write-Host "RAG MongoDB:    $(if (Test-PortListening 27018) {'up'} else {'DOWN'})"
Write-Host "`nDone. Run scripts/start_services.ps1 next." -ForegroundColor Cyan
