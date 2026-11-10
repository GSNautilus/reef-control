# Starts Reef Control in the notification area each time you sign in to Windows,
# after a short delay, and adds "Reef Control" to the Start menu.
#   Install / update:  powershell -ExecutionPolicy Bypass -File <project folder>\install-startup.ps1
#   Other delay:       ... install-startup.ps1 -DelaySeconds 120
#   Remove:            ... install-startup.ps1 -Remove
param(
    [int]$DelaySeconds = 60,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TaskName = "Reef Control"
# Uses the "codex" conda environment if it exists, otherwise pythonw on PATH.
$Pythonw = Join-Path $env:USERPROFILE "miniconda3\envs\codex\pythonw.exe"
if (-not (Test-Path $Pythonw)) { $Pythonw = (Get-Command pythonw.exe).Source }
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Reef Control.lnk"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Shortcut -ErrorAction SilentlyContinue
    Write-Host "Removed the sign-in task and the Start menu shortcut. A copy that's already running keeps running until you quit it from its icon."
    return
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m app.tray" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT$($DelaySeconds)S"
# No time limit (the default stops a task after 3 days), normal priority (the default is below normal).
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
# Interactive = runs in your session, which the notification-area icon needs.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Starts the Reef Control dashboard in the notification area $DelaySeconds s after sign-in." | Out-Null

# Start menu shortcut: starts Reef Control if it isn't running, otherwise just opens the page.
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $Pythonw
$lnk.Arguments = "-m app.tray"
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.IconLocation = Join-Path $PSScriptRoot "web\reef.ico"
$lnk.Description = "Reef Control dashboard"
$lnk.Save()

Write-Host "Reef Control will start in the notification area $DelaySeconds seconds after you sign in."
Write-Host "Start menu shortcut: $Shortcut"
