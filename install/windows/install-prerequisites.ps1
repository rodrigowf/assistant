<#
.SYNOPSIS
    Windows prerequisite checker / installer for the Personal Assistant.

.DESCRIPTION
    Checks that Python 3.12+, Node.js 20+, npm, and git are present.
    When something is missing, offers to install it via winget (Microsoft's
    package manager built into Windows 10 1809+ / Windows 11).

    Equivalent of install/linux/install-prerequisites.sh and
    install/apple/install-prerequisites.sh.

    Exit codes:
       0 — all prereqs satisfied
       1 — one or more still missing after install attempts

.PARAMETER NonInteractive
    Skip prompts; print install commands instead.  In a non-interactive shell
    (e.g. CI) the script behaves this way automatically.
#>
#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

function Write-Info  { param([string]$m) Write-Host "[OK]   $m" -ForegroundColor Green }
function Write-Warn  { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Err   { param([string]$m) Write-Host "[FAIL] $m" -ForegroundColor Red }
function Write-Ask   { param([string]$m) Write-Host "[?]    $m" -ForegroundColor Cyan -NoNewline }

function Test-Interactive {
    if ($NonInteractive) { return $false }
    return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

function Read-YesNo {
    param([string]$Question, [string]$Default = 'Y')
    if (-not (Test-Interactive)) { return ($Default -eq 'Y') }
    Write-Ask "$Question [$(if ($Default -eq 'Y') { 'Y/n' } else { 'y/N' })] "
    $ans = Read-Host
    if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $Default }
    return $ans -match '^[Yy]'
}

# ─────────────────────────────────────────────────────────────────────────────
# winget bootstrap.  Winget ships in Windows 10 1809+ / Windows 11 by default,
# but on a fresh box it sometimes needs an App Installer update first.  If
# winget is genuinely missing, we point the user at the Store link rather than
# trying to script the install — that's how Microsoft says to do it.
# ─────────────────────────────────────────────────────────────────────────────
function Test-Winget {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Show-WingetMissing {
    Write-Host ""
    Write-Warn "winget is not installed (or not on PATH)."
    Write-Host "    winget ships with Windows 10 1809+ and Windows 11 via the App Installer."
    Write-Host "    Install or update it from the Microsoft Store:"
    Write-Host "      https://www.microsoft.com/store/productId/9NBLGGH4NNS1"
    Write-Host "    or with: Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe"
    Write-Host ""
    Write-Host "    Once winget is available, re-run this script."
    Write-Host ""
    Write-Host "    Manual fallback (download installers):"
    Write-Host "      Python 3.12+:  https://www.python.org/downloads/"
    Write-Host "      Node.js 20+:   https://nodejs.org/  (LTS)"
    Write-Host "      Git:           https://git-scm.com/download/win"
    Write-Host ""
}

function Install-WithWinget {
    param([string]$Id, [string]$DisplayName)
    if (-not (Test-Winget)) {
        Show-WingetMissing
        return $false
    }
    if (Test-Interactive) {
        if (-not (Read-YesNo "Install ${DisplayName} via winget?" 'Y')) {
            Write-Warn "Skipped $DisplayName - install manually and re-run."
            return $false
        }
    }
    # --silent skips per-package GUI prompts where supported.
    # --accept-package-agreements / --accept-source-agreements suppress the
    # one-time license prompts winget shows on a fresh install.
    & winget install --id $Id `
        --silent --accept-package-agreements --accept-source-agreements `
        --source winget
    if ($LASTEXITCODE -eq 0) {
        Write-Info "$DisplayName installed via winget"
        # winget installs put new binaries in places that the current shell's
        # %PATH% snapshot doesn't know about.  Refresh PATH from the registry
        # so the next `Get-Command` in this same script can see them.
        Update-PathFromRegistry
        return $true
    } else {
        Write-Err "winget install $Id failed (exit $LASTEXITCODE)."
        return $false
    }
}

function Update-PathFromRegistry {
    # Pull the latest PATH from the user + machine environment scopes and
    # merge them into the current process.  Without this, a tool installed
    # by winget during this script run isn't on PATH yet.
    $user    = [Environment]::GetEnvironmentVariable('Path','User')
    $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
    $env:Path = (@($machine, $user) -join ';').TrimEnd(';')
}

# ─────────────────────────────────────────────────────────────────────────────
# Per-tool checks.  Each one prints status, and on missing/outdated returns
# the canonical winget package id so the install loop knows what to fetch.
# ─────────────────────────────────────────────────────────────────────────────
$Missing = @()

Write-Host "Checking Windows prerequisites..."
Write-Host ""

# Python — look for `py -3.12`, `py -3`, `python3`, `python` in that order.
function Get-PythonVersion {
    foreach ($cand in @('py -3.12', 'py -3', 'python3', 'python')) {
        $parts = $cand -split ' '
        $exe = $parts[0]
        $args = if ($parts.Count -gt 1) { $parts[1..($parts.Count-1)] } else { @() }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0) { return $v.Trim() }
        } catch { }
    }
    return $null
}

$pyVersion = Get-PythonVersion
if ($pyVersion) {
    $pyParts = $pyVersion -split '\.'
    if ([int]$pyParts[0] -ge 3 -and [int]$pyParts[1] -ge 12) {
        Write-Info "Python $pyVersion"
    } else {
        Write-Err "Python $pyVersion (need 3.12+)"
        $Missing += @{ Id = 'Python.Python.3.12'; Name = 'Python 3.12' }
    }
} else {
    Write-Err "Python not found"
    $Missing += @{ Id = 'Python.Python.3.12'; Name = 'Python 3.12' }
}

# Node.js
function Get-NodeVersion {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { return $null }
    try {
        $v = & node -v 2>$null
        if ($LASTEXITCODE -eq 0) { return ($v.TrimStart('v')) }
    } catch { }
    return $null
}

$nodeVersion = Get-NodeVersion
if ($nodeVersion) {
    $nodeMajor = [int](($nodeVersion -split '\.')[0])
    if ($nodeMajor -ge 20) {
        Write-Info "Node.js $nodeVersion"
    } else {
        Write-Err "Node.js $nodeVersion (need 20+)"
        # OpenJS.NodeJS.LTS tracks the current LTS line (20.x in 2026).
        $Missing += @{ Id = 'OpenJS.NodeJS.LTS'; Name = 'Node.js LTS' }
    }
} else {
    Write-Err "Node.js not found"
    $Missing += @{ Id = 'OpenJS.NodeJS.LTS'; Name = 'Node.js LTS' }
}

# npm — bundled with Node.  Only flag separately if we have node but not npm.
if (Get-Command npm -ErrorAction SilentlyContinue) {
    $npmVer = (& npm -v 2>$null).Trim()
    Write-Info "npm $npmVer"
} else {
    Write-Err "npm not found"
    if ($nodeVersion -and ($Missing.Count -eq 0 -or ($Missing[-1].Id -ne 'OpenJS.NodeJS.LTS'))) {
        # node present but npm missing (broken install) — reinstall node.
        $Missing += @{ Id = 'OpenJS.NodeJS.LTS'; Name = 'Node.js LTS (npm re-bundle)' }
    }
}

# Git (recommended)
if (Get-Command git -ErrorAction SilentlyContinue) {
    $gv = (& git --version 2>$null) -replace '^git version ', ''
    Write-Info "Git $gv"
} else {
    Write-Warn "Git not found"
    $Missing += @{ Id = 'Git.Git'; Name = 'Git' }
}

Write-Host ""

if ($Missing.Count -eq 0) {
    Write-Info "All prerequisites satisfied!"
    Write-Host ""
    Write-Host "You can now run: install\windows\install.ps1"
    exit 0
}

Write-Host "Missing prerequisites: $(($Missing | ForEach-Object Name) -join ', ')"
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Install loop.  Non-interactive mode prints the winget commands and exits 1
# (caller can rerun after they finish manually).
# ─────────────────────────────────────────────────────────────────────────────
if (-not (Test-Interactive)) {
    Write-Host "Non-interactive shell - install the missing tools manually:"
    Write-Host ""
    foreach ($m in $Missing) {
        Write-Host "  winget install --id $($m.Id) --silent --accept-package-agreements --accept-source-agreements"
    }
    Write-Host ""
    exit 1
}

# Interactive path.  Make sure winget itself is present before we offer.
if (-not (Test-Winget)) {
    Show-WingetMissing
    exit 1
}

$installFailed = $false
foreach ($m in $Missing) {
    if (-not (Install-WithWinget -Id $m.Id -DisplayName $m.Name)) {
        $installFailed = $true
    }
}

if ($installFailed) {
    Write-Err "One or more installs failed.  Fix the errors above and re-run."
    exit 1
}

Write-Host ""
Write-Info "All prerequisites installed."
Write-Host "You may need to restart PowerShell so new entries on PATH (Python, Node)"
Write-Host "are picked up by every subsequent command.  Then run: install\windows\install.ps1"
exit 0
