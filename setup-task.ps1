# setup-task.ps1 - register the scheduled task that keeps the remote running.
#
# This is what makes the app work the way someone expects an app to work:
# on whenever the PC is on, with nothing to click. Run by the installer,
# elevated, once.
#
#   at logon        -> start the supervisor
#   every 5 minutes -> start it again if it died
#
# The repeat is unconditional and that is fine: the supervisor holds a named
# mutex, so a second copy exits immediately.
[CmdletBinding()]
param(
    [string]$Root = $PSScriptRoot,
    [string]$TaskName = "Aether Remote",
    # The account the task runs as. Must be an interactive user (see below).
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$LOG = Join-Path $env:LOCALAPPDATA "Aether Remote\setup-task.log"

function Say($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    try {
        $dir = Split-Path $LOG -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LOG -Value $line
    } catch { }
    Write-Output $line
}

Say ("=== {0} ===" -f (Get-Date))

$pr = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say "not elevated - registering a task needs admin. Aborting."
    exit 1
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
        -ErrorAction SilentlyContinue
    Say "task removed"
    exit 0
}

# Installed, the app is one exe with a mode flag. From source it is a .vbs
# that finds Python. Prefer the exe; fall back so this script works in both.
$exe = Join-Path $Root "AetherRemote.exe"
$vbs = Join-Path $Root "watchdog.vbs"
if (Test-Path $exe) {
    $execute = $exe
    $argument = "--supervise"
} elseif (Test-Path $vbs) {
    $execute = "wscript.exe"
    $argument = '"' + $vbs + '"'
} else {
    Say ("found neither AetherRemote.exe nor watchdog.vbs in {0}" -f $Root)
    exit 3
}
Say ("action: {0} {1}" -f $execute, $argument)
Say ("user  : {0}" -f $User)

try {
    $action = New-ScheduledTaskAction -Execute $execute -Argument $argument `
              -WorkingDirectory $Root

    $tLogon  = New-ScheduledTaskTrigger -AtLogOn -User $User
    $tRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
               -RepetitionInterval (New-TimeSpan -Minutes 5)

    # Interactive, and NOT SYSTEM or "run whether logged on or not". A task in
    # session 0 cannot see the desktop, so screen capture and input injection
    # - the whole point of the remote - would silently do nothing.
    # RunLevel Limited because the app needs no admin rights to do its job.
    $principal = New-ScheduledTaskPrincipal -UserId $User `
                 -LogonType Interactive -RunLevel Limited

    $set = New-ScheduledTaskSettingsSet `
           -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
           -StartWhenAvailable -MultipleInstances IgnoreNew `
           -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
           -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($tLogon, $tRepeat) -Principal $principal -Settings $set `
        -Description ("Keeps Aether Remote running so your phone can reach " +
                      "this PC. Runs in your session so screen sharing works.") `
        -Force -ErrorAction Stop | Out-Null

    Say "task registered"
} catch {
    Say ("REGISTER FAILED: {0}" -f $_.Exception.Message)
    exit 2
}

# Start it now so the user does not have to log out and back in, and so any
# failure shows up here rather than at 2am on someone else's PC.
Say "starting it once to prove it works"
try { Start-ScheduledTask -TaskName $TaskName } catch { Say $_.Exception.Message }
Start-Sleep -Seconds 10

$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
if ($info) {
    Say ("  last run: {0}  result: {1}" -f $info.LastRunTime, $info.LastTaskResult)
}

$port = 8787
$cfg = Join-Path $env:LOCALAPPDATA "Aether Remote\config.json"
if (Test-Path $cfg) {
    try { $port = [int](Get-Content $cfg -Raw | ConvertFrom-Json).port } catch { }
}

$listen = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($listen) {
    Say ("  listening on {0}:{1}" -f $listen[0].LocalAddress, $port)
    Say "done"
    exit 0
}

# Not listening yet is not necessarily failure - the supervisor may still be
# starting, or the network mode may be waiting on Tailscale. Say so plainly
# instead of claiming success.
Say ("  nothing listening on {0} yet - it may still be starting, or the " +
     "network mode is waiting for Tailscale" -f $port)
Say "done"
exit 0
