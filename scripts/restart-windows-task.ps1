# Restarts the kalshi-bot Scheduled Task without the two usual failure modes: starting the new copy while
# the old one is still shutting down (the task ignores the start), and a leftover process from an older
# install holding the lock or the dashboard port. Then it waits for the dashboard to answer and shows the
# last log lines, so a failed start is visible right here instead of as a "connection refused" page.
# Run from the repo folder in PowerShell:
#     .\scripts\restart-windows-task.ps1            restart only
#     .\scripts\restart-windows-task.ps1 -Pull      git pull + uv sync first, then restart (the normal "update" step)
# If scripts are disabled: powershell -ExecutionPolicy Bypass -File .\scripts\restart-windows-task.ps1 -Pull
param(
    [string]$TaskName = "kalshi-bot",
    [switch]$Pull,
    [int]$DashboardPort = 8787,
    [int]$WaitSec = 90
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Invoke-Native([string]$Label, [scriptblock]$Command) {
    # Native tools (git, uv) write progress to stderr; show it as plain text and only fail on the exit code.
    Write-Host "== $Label"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Command 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "$Label failed (exit $code). The bot was not restarted." }
}

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Task '$TaskName' is not registered. Run .\scripts\install-windows-task.ps1 first."
}

if ($Pull) {
    Invoke-Native "git pull" { git pull --ff-only }
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
    if (-not (Test-Path $uv)) { throw "uv not found; cannot sync dependencies" }
    Invoke-Native "uv sync" { & $uv sync }
    Write-Host ("now at: " + (git log -1 --format="%h %s"))
}

Write-Host "== stopping task '$TaskName'"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(30)
while (((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 500 }
# Older installs ran the bot behind a PowerShell wrapper that Stop-ScheduledTask orphaned; clear any leftover.
$stray = Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'python.exe', 'uv.exe') -and ($_.CommandLine -like "*kalshi_bot*") }
foreach ($p in $stray) {
    Write-Host "   stopping leftover bot process $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddSeconds(15)
while ((Get-NetTCPConnection -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 500 }

Write-Host "== starting task '$TaskName'"
Start-ScheduledTask -TaskName $TaskName
$deadline = (Get-Date).AddSeconds($WaitSec)
$status = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$DashboardPort/api/status" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
            $status = $r.Content | ConvertFrom-Json
            if ($status.bot.phase -eq "running" -or $status.bot.phase -like "*failed*") { break }
        }
    } catch { }
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") { break }
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = $task | Get-ScheduledTaskInfo
Write-Host ("task state: {0}; last run {1}; last result 0x{2:X}" -f $task.State, $info.LastRunTime, $info.LastTaskResult)
if ($status) {
    Write-Host ("dashboard: http://127.0.0.1:{0}   bot: {1}" -f $DashboardPort, $status.bot.phase)
    if ($status.bot.error) { Write-Host ("   error: " + $status.bot.error) -ForegroundColor Yellow }
} else {
    Write-Host "dashboard did not answer within $WaitSec s; the log below says why (look for 'exit' or 'crashed')." -ForegroundColor Yellow
}
$log = Join-Path $repo "data\logs\bot.demo.log"
if (Test-Path $log) {
    Write-Host "== last 15 log lines ($log)"
    Get-Content $log -Tail 15
}
