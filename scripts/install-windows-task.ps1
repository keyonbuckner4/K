# Registers a Windows Scheduled Task that starts the bot when you log on and restarts it if it stops.
# Run ONCE from the repo folder in PowerShell:
#     .\scripts\install-windows-task.ps1
# Trade mode (only after the observe gate in BRIEF.md is met):
#     .\scripts\install-windows-task.ps1 -BotArgs "run --dashboard --trade"
# Stop / remove:
#     Stop-ScheduledTask -TaskName kalshi-bot
#     Unregister-ScheduledTask -TaskName kalshi-bot -Confirm:$false
# The bot refuses to start twice for the same environment, so an interactive `uv run bot run` and the
# task cannot double up; stop the interactive one (Ctrl-C) before starting the task.
param(
    [string]$TaskName = "kalshi-bot",
    [string]$BotArgs = "run --dashboard"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found. Install it first: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "task.log"

# cmd.exe does the redirection so the bot's stderr status lines land in the log as plain text
# (PowerShell would wrap them as NativeCommandError records).
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c cd /d `"$repo`" && `"$uv`" run bot --quiet $BotArgs >> `"$logFile`" 2>&1" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started: bot $BotArgs"
Write-Host "Startup log: $logFile"
Write-Host "Scan log:    $(Join-Path $logDir 'bot.demo.log')   (Get-Content data\logs\bot.demo.log -Tail 5)"
Write-Host "Dashboard: http://127.0.0.1:8787"
Write-Host "Status:    Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
