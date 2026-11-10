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

function Resolve-TaskPath([string]$Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $workDir $Path))
}

$configPath = Resolve-TaskPath $Config
$logPath = Resolve-TaskPath $LogFile
foreach ($dir in @((Split-Path -Parent $configPath), (Split-Path -Parent $logPath))) {
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

$arguments = "-m service --config `"$configPath`" --log-file `"$logPath`""
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
Write-Host "  config : $configPath"
Write-Host "  log    : $logPath"
Write-Host "Start now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
