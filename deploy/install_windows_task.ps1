# Register the LookCam service in Windows Task Scheduler.
# Run from PowerShell (elevated if needed).
#
# Usage:
#   .\install_windows_task.ps1 -Python "C:\Python\Python313\python.exe" `
#       -ProjectDir "F:\Projects\LookCam\lookcam_py\lookcam_deepsik" `
#       -Config "config\lookcam_service.json"

param(
    [string]$Python = "python.exe",
    [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$Config = "config\lookcam_service.json",
    [string]$TaskName = "LookCam Service",
    [string]$LogFile = "lookcam_data\service.log"
)

$ErrorActionPreference = "Stop"

$pythonPath = (Get-Command $Python -ErrorAction Stop).Source
$workDir = (Resolve-Path $ProjectDir).Path

$arguments = "-m service --config `"$Config`" --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments `
    -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "LookCam: PPPP + web + RTSP" -Force

Write-Host "Task '$TaskName' registered."
Write-Host "  python : $pythonPath"
Write-Host "  workdir: $workDir"
Write-Host "  config : $Config"
Write-Host "Start now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
