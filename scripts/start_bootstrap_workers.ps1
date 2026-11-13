# Starts the per-game bootstrap as three detached background workers, one per API host,
# so each host's rate limit is used in parallel. Safe to re-run: workers resume from
# ops.ingestion_queue. Logs go to logs\worker_<host>.log.
#
#   .\scripts\start_bootstrap_workers.ps1            # enqueue scope + start workers
#   .\.venv\Scripts\steam-intel status               # check progress any time
#   Get-Process powershell | Where-Object CommandLine -like '*steam-intel*'   # see workers
#
# To stop: close the processes (Stop-Process); in-flight items are returned to the queue
# automatically on the next run (stale in_progress items are reset after 60 minutes).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = "$root\.venv\Scripts\steam-intel.exe"
New-Item -ItemType Directory -Force "$root\logs" | Out-Null

& $exe enqueue

$chains = @{
    "store"    = @("appdetails", "histogram")
    "webapi"   = @("reviews", "players", "review_sample", "news")
    "steamspy" = @("steamspy")
}
foreach ($hostName in $chains.Keys) {
    $cmds = ($chains[$hostName] | ForEach-Object { "& '$exe' work $_" }) -join "; "
    $log = "$root\logs\worker_$hostName.log"
    $script = "Set-Location '$root'; $cmds *>> '$log'"
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", $script
    Write-Host "started $hostName worker -> $log"
}
