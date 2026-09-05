# Stops the Docker-based infra (Redis, RAG MongoDB deployment). Does NOT
# stop the native mongod/Kafka processes (no clean "stop" hook for those in
# this setup - close their windows or kill by port if you need them down).
#
# Run:
#   powershell -ExecutionPolicy Bypass -File scripts/stop_infra.ps1

docker stop revenue-recovery-redis revenue-recovery-atlas-local 2>$null
Write-Host "Redis and the RAG MongoDB deployment stopped (containers preserved - 'docker start' brings them back with data intact)." -ForegroundColor Cyan
Write-Host "mongod and Kafka are native processes - stop them manually (they don't have a script-managed pid file)." -ForegroundColor Yellow
