# setup-firewall.ps1 - let this PC's phone remote be reached.
#
# Run by the installer, elevated, once. It adds ONE inbound allow rule for
# this app and nothing else.
#
# What it deliberately does NOT do: turn the firewall on or off, or change
# any profile's default action. Those are the machine owner's settings and an
# installer has no business touching them.
#
# Safe to run repeatedly - the rule is replaced, not duplicated.
[CmdletBinding()]
param(
    # Where the app is installed. Defaults to this script's own folder.
    [string]$Root = $PSScriptRoot,
    # Overrides the port found in config.json.
    [int]$Port = 0,
    # Remove the rule instead of adding it (used by the uninstaller).
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$NAME = "Aether Remote"
$LOG = Join-Path $env:LOCALAPPDATA "Aether Remote\setup-firewall.log"

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
    Say "not elevated - a firewall rule needs admin. Aborting."
    exit 1
}

# The port the app actually uses, so a changed port still gets a rule.
if ($Port -le 0) {
    $Port = 8787
    $cfg = Join-Path $env:LOCALAPPDATA "Aether Remote\config.json"
    if (-not (Test-Path $cfg)) { $cfg = Join-Path $Root "config.json" }
    if (Test-Path $cfg) {
        try {
            $p = (Get-Content $cfg -Raw | ConvertFrom-Json).port
            if ($p -gt 0) { $Port = [int]$p }
        } catch { Say "could not read the port from config.json - using 8787" }
    }
}

$rule = "$NAME (TCP $Port)"

# Clear any previous version of our rule, whatever port it named.
Get-NetFirewallRule -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like "$NAME (TCP *" } |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

if ($Remove) {
    Say "removed the firewall rule"
    exit 0
}

# Scoped to the program where we can, so the hole is this app's and not
# "anything that grabs the port".
$exe = Join-Path $Root "AetherRemote.exe"
# Not $args - that is an automatic variable in PowerShell.
$ruleArgs = @{
    DisplayName = $rule
    Direction   = "Inbound"
    Action      = "Allow"
    Protocol    = "TCP"
    LocalPort   = $Port
    Profile     = "Any"
    Enabled     = "True"
    Description = "Lets your phone reach this PC's remote. Added by the Aether Remote installer."
}
if (Test-Path $exe) { $ruleArgs["Program"] = $exe }

try {
    New-NetFirewallRule @ruleArgs | Out-Null
    if ($ruleArgs.ContainsKey("Program")) {
        Say ("allowed TCP {0} for {1}" -f $Port, $exe)
    } else {
        Say ("allowed TCP {0} (no exe found at {1} - rule is port-only)" -f $Port, $Root)
    }
} catch {
    Say ("FAILED to add the rule: {0}" -f $_.Exception.Message)
    exit 2
}

# Say plainly whether the firewall is even on, because a rule added while it
# is off explains nothing later.
try {
    Get-NetFirewallProfile | ForEach-Object {
        Say ("  profile {0}: firewall {1}" -f $_.Name,
             $(if ($_.Enabled) { "on" } else { "off" }))
    }
} catch { }

Say "done"
exit 0
