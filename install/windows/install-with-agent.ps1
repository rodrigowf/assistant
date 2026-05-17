<#
.SYNOPSIS
    Conversational installer for the Personal Assistant on Windows.

.DESCRIPTION
    Windows PowerShell port of install/linux/install-with-agent.sh.  Detects
    which agent CLIs are installed, picks (or asks for) one to drive the
    install, ensures it's authenticated, then launches it with INSTALL.md as
    its boot prompt.  From that moment the agent handles the install
    conversationally; this script's job is just to get the agent running.

    The agent then reads install.ps1 as the canonical recipe (not install.sh —
    only Windows-specific paths and symlink quirks live in install.ps1) and
    executes each step itself using its file + shell tools.

.NOTES
    Runs from PowerShell 7+ or Windows PowerShell 5.1.  An interactive
    terminal is required — for non-interactive (CI) installs, use
    install.ps1 directly.
#>
#Requires -Version 5.1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# Project root = two dirs up.
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptDir    = Resolve-Path (Join-Path $InstallerDir '..\..') | ForEach-Object Path
Set-Location $ScriptDir

function Write-Info  { param([string]$m) Write-Host "[OK]   $m" -ForegroundColor Green }
function Write-Step  { param([string]$m) Write-Host "[STEP] $m" -ForegroundColor Blue }
function Write-Warn  { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Ask   { param([string]$m) Write-Host "[?]    $m" -ForegroundColor Cyan -NoNewline }
function Write-Err   { param([string]$m) Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

function Test-Interactive {
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
# Header + safety notice
# ─────────────────────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  Personal Assistant - Conversational Installer (Windows)"  -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""
Write-Host "This is the conversational install path.  It will launch an agent CLI"
Write-Host "(Claude Code, Qwen Code, or Gemini CLI) and let that agent walk you"
Write-Host "through the install - asking you questions, executing each step, and"
Write-Host "logging progress to context\install.log."
Write-Host ""
Write-Host "Heads up: " -ForegroundColor Yellow -NoNewline
Write-Host "the agent will run with broad file + shell permissions inside"
Write-Host "this directory.  It follows install\windows\install.ps1 as the recipe"
Write-Host "and writes files into the project root.  If you'd rather see every"
Write-Host "action as plain PowerShell, exit now and run install.ps1 instead."
Write-Host ""

if (-not (Test-Interactive)) {
    Write-Err "This script needs an interactive terminal.  Use install.ps1 for non-interactive installs."
}

if (-not (Read-YesNo "Proceed with the conversational install?" 'Y')) {
    Write-Info "Cancelled.  Run install.ps1 whenever you're ready."
    exit 0
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Detect installed agent CLIs
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Detecting installed agent CLIs..."

$AllClis = @('claude','qwen','gemini')
$CliPkg = @{
    'claude' = '@anthropic-ai/claude-code'
    'qwen'   = '@qwen-code/qwen-code'
    'gemini' = '@google/gemini-cli'
}
$CliLabel = @{
    'claude' = 'Claude Code (Anthropic)'
    'qwen'   = 'Qwen Code (Alibaba)'
    'gemini' = 'Gemini CLI (Google)'
}

$Installed = @()
foreach ($cli in $AllClis) {
    $cmd = Get-Command $cli -ErrorAction SilentlyContinue
    if ($cmd) {
        $Installed += $cli
        Write-Info "Found $cli at $($cmd.Source)"
    }
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: If nothing is installed, offer to install one or more
# ─────────────────────────────────────────────────────────────────────────────
if ($Installed.Count -eq 0) {
    Write-Warn "No agent CLI is installed yet - one is required to drive this install."
    Write-Host ""
    Write-Host "Available agent CLIs:"
    foreach ($cli in $AllClis) {
        Write-Host "  - $cli  ($($CliLabel[$cli])) - npm install -g $($CliPkg[$cli])"
    }
    Write-Host ""
    Write-Host "You can install one or more now.  The first one you install becomes"
    Write-Host "the install driver; the others (if any) can be added later by the"
    Write-Host "agent during the conversational install."
    Write-Host ""

    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Err "npm not found - install Node.js 20+ first, then re-run this script."
    }

    foreach ($cli in $AllClis) {
        if (Read-YesNo "Install $cli ($($CliLabel[$cli]))?" 'N') {
            Write-Step "Installing $($CliPkg[$cli]) globally..."
            & npm install -g $CliPkg[$cli]
            if ($LASTEXITCODE -eq 0) {
                Write-Info "$cli installed"
                $Installed += $cli
            } else {
                Write-Warn "$cli install failed - skipping"
            }
        }
    }
    Write-Host ""

    if ($Installed.Count -eq 0) {
        Write-Err "No CLI was installed - can't proceed.  Install one manually (npm install -g <pkg>) and re-run."
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Pick which CLI drives the install
# ─────────────────────────────────────────────────────────────────────────────
if ($Installed.Count -eq 1) {
    $Driver = $Installed[0]
    Write-Info "Using $Driver to drive the install"
} else {
    Write-Host "── Which CLI should drive the install? ──" -ForegroundColor Cyan
    Write-Host ""
    for ($i = 0; $i -lt $Installed.Count; $i++) {
        Write-Host "  $($i+1)) $($Installed[$i])  ($($CliLabel[$Installed[$i]]))"
    }
    Write-Host ""
    Write-Ask "Choice [1-$($Installed.Count)] (default 1): "
    $choice = Read-Host
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = '1' }
    if ($choice -notmatch '^\d+$') { Write-Err "Invalid choice: $choice" }
    $idx = [int]$choice
    if ($idx -lt 1 -or $idx -gt $Installed.Count) { Write-Err "Invalid choice: $choice" }
    $Driver = $Installed[$idx - 1]
    Write-Info "Using $Driver to drive the install"
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Ensure the driver CLI is authenticated
# ─────────────────────────────────────────────────────────────────────────────
function Test-DriverAuth {
    param([string]$Cli)
    switch ($Cli) {
        'claude' {
            $out = & claude auth status 2>$null
            return [bool]($out -match '"loggedIn":\s*true')
        }
        'qwen' {
            if (Test-Path (Join-Path $env:USERPROFILE '.qwen\oauth_creds.json')) { return $true }
            if ((Test-Path 'context\.env') -and ((Get-Content -LiteralPath 'context\.env' -Raw) -match '(?m)^DASHSCOPE_API_KEY=.+')) { return $true }
            if ($env:DASHSCOPE_API_KEY) { return $true }
            return $false
        }
        'gemini' {
            if (Test-Path (Join-Path $env:USERPROFILE '.gemini\oauth_creds.json')) { return $true }
            if ((Test-Path 'context\.env') -and ((Get-Content -LiteralPath 'context\.env' -Raw) -match '(?m)^GEMINI_API_KEY=.+')) { return $true }
            if ($env:GEMINI_API_KEY) { return $true }
            return $false
        }
    }
    return $false
}

function Get-LoginCmd {
    param([string]$Cli)
    switch ($Cli) {
        'claude' { return 'claude auth login' }
        'qwen'   { return 'qwen' }
        'gemini' { return 'gemini' }
    }
}

Write-Step "Checking authentication state for $Driver..."
if (Test-DriverAuth $Driver) {
    Write-Info "$Driver is authenticated"
} else {
    Write-Warn "$Driver is not authenticated yet."
    Write-Host ""
    Write-Host "    Open a separate PowerShell in this directory and run:"
    Write-Host "      $(Get-LoginCmd $Driver)" -ForegroundColor Blue
    Write-Host "    Complete the login flow, then return here."
    Write-Host ""
    Write-Ask "Press Enter once login completes: "
    [void](Read-Host)
    if (-not (Test-DriverAuth $Driver)) {
        Write-Warn "$Driver still doesn't look authenticated.  The agent may not be able to start."
        if (-not (Read-YesNo "Continue anyway?" 'N')) {
            Write-Info "Cancelled.  Finish authentication and re-run this script."
            exit 0
        }
    } else {
        Write-Info "$Driver authenticated"
    }
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Ensure context/install.log's parent dir exists.
# The agent creates the log itself as its first action.
# ─────────────────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path 'context' -Force | Out-Null
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Build the agent prompt and launch
# ─────────────────────────────────────────────────────────────────────────────
# Equivalent of the Linux version's heredoc.  We point the agent at
# install\windows\install.ps1 (not install.sh) since Windows-specific quirks
# (symlinks, PowerShell, winget) live there.
$startTs = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$prompt = @"
You are the install agent for the Personal Assistant project, launched by
install-with-agent.ps1 at $startTs.  You are running inside the project root
($ScriptDir) on Windows.  The driver CLI you are running on is: $Driver.

Your instructions are in INSTALL.md - read it now (the section marked
"For the install agent (boot prompt)") and follow it carefully.  Treat
install\windows\install.ps1 as the canonical recipe; do the steps yourself
with your file and shell tools so you can adapt to the user's answers and
recover from errors.

Begin by:
  1. Reading INSTALL.md end-to-end.
  2. Reading install\windows\install.ps1 end-to-end (NOT install.sh - we are
     on Windows; the PowerShell version has Windows-specific symlink and
     path-mangling logic).
  3. Reading install\README.md and install\windows\README.md.
  4. Creating context\install.log if it doesn't exist and appending a
     timestamped "install agent started" line.
  5. Greeting the user briefly and asking the first axis question
     (session harness - claude / qwen / gemini, any subset).

Important context for this session:
  - The user already has $Driver installed and authenticated (it's driving
    this conversation).  Do NOT re-install or re-authenticate $Driver.
  - Other harnesses may need to be installed if the user picks them.  The
    'Install + authenticate agent CLIs' block in install.ps1 has the
    detection + npm install + login-prompt logic - follow that.
  - Symlinks on Windows: install.ps1's Test-Symlinks / New-Link functions
    handle the Developer-Mode-vs-fallback decision.  If symlinks aren't
    available, the script falls back to junctions (directories) and
    file copies.  Explain this trade-off when relevant.
  - When the install is complete, ask the user if they'd like the backend
    and/or frontend started in the background, then exit cleanly.

Begin.
"@

Write-Step "Launching $Driver with the install prompt..."
Write-Host ""

# Per-CLI launch.  Each one takes the prompt slightly differently — same
# pattern as the Linux/macOS version, but `exec` doesn't exist in PowerShell
# so we just invoke the CLI and let it inherit the terminal.
switch ($Driver) {
    'claude' { & claude $prompt }
    'qwen'   { & qwen --prompt-interactive $prompt }
    'gemini' { & gemini --prompt-interactive $prompt }
    default  { Write-Err "Internal error: unknown driver $Driver" }
}
