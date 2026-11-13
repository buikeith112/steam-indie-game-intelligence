# Runs the daily incremental pipeline. Invoked by Windows Task Scheduler (see register_daily_task.ps1).
# Reads DATABASE_URL etc. from the project's .env file.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
$log = "$root\logs\daily_$(Get-Date -Format 'yyyyMMdd').log"
& "$root\.venv\Scripts\steam-intel.exe" daily --minutes-per-source 45 *>> $log
exit $LASTEXITCODE
