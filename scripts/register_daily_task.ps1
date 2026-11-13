# Registers a Windows scheduled task that runs the daily pipeline at 03:00.
# Run once from an elevated or normal PowerShell:  .\scripts\register_daily_task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName "SteamIndieDaily" -Confirm:$false
param([string]$At = "03:00")

$script = Join-Path $PSScriptRoot "run_daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "SteamIndieDaily" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Steam indie game intelligence: daily catalog, prices, reviews, CCU refresh" -Force
