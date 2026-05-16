<#
.SYNOPSIS
    Personal Assistant installer — top-level entry point on Windows.

.DESCRIPTION
    Thin wrapper that dispatches to install\windows\install.ps1.  All
    parameters are forwarded verbatim.  Equivalent of `./install.sh` on
    Linux/macOS — having this at the repo root means Windows users always
    have the same entry point as POSIX users.

    See install\windows\install.ps1 for full parameter documentation.
#>
#Requires -Version 5.1

# Forward $args wholesale rather than re-declaring every parameter — the
# per-OS script owns the schema and we don't want to drift.  PowerShell
# auto-binds named parameters when the target script uses [CmdletBinding].
$installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $installerDir 'install\windows\install.ps1'
if (-not (Test-Path $target)) {
    Write-Host "Installer not found: $target" -ForegroundColor Red
    exit 1
}
& $target @args
exit $LASTEXITCODE
