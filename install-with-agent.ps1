<#
.SYNOPSIS
    Personal Assistant conversational installer — top-level entry point on Windows.

.DESCRIPTION
    Thin wrapper that dispatches to install\windows\install-with-agent.ps1.
    Equivalent of `./install-with-agent.sh` on Linux/macOS.
#>
#Requires -Version 5.1

$installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $installerDir 'install\windows\install-with-agent.ps1'
if (-not (Test-Path $target)) {
    Write-Host "Installer not found: $target" -ForegroundColor Red
    exit 1
}
& $target @args
exit $LASTEXITCODE
