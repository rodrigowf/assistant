<#
.SYNOPSIS
    Personal Assistant installer for Windows.

.DESCRIPTION
    Windows PowerShell port of install/linux/install.sh — same two-axis
    decision model (session harness + orchestrator backends), same per-axis
    install steps, same end state.

    Differences vs. the Linux/macOS scripts:

      - Symlinks: we try POSIX-style symlinks first (require Windows Developer
        Mode OR running as Administrator).  If those fail, we fall back to NTFS
        junctions for directory targets and plain file copies for file targets.
        The script tells you which path it took and what's needed to upgrade.

      - Prereqs: we bootstrap missing tools via winget (Microsoft's package
        manager that ships with Windows 10 1809+ / Windows 11).  Chocolatey is
        not used here — winget is the modern, built-in option.

      - Python venv layout: Windows uses `.venv\Scripts\python.exe` and
        `.venv\Scripts\pip.exe` (vs. `.venv/bin/...` on POSIX).

      - Codex: ~/.codex-archie is %USERPROFILE%\.codex-archie; its sessions\
        link falls back to a junction like the other directory links, and the
        POSIX `chmod 700` on that home has no equivalent here (the profile
        directory's ACLs already keep it private).

      - Path mangling: Claude Code and Qwen Code replace every character of
        the project path that is not a letter or digit with '-'; Qwen
        lower-cases the path first on Windows.

      - Memory/history wiring (Gemini's projects.json label + ownership
        marker, Codex's [features] memories = false, Qwen's memory keys) runs
        after the venv exists, with the venv's Python and the same install\
        helpers as Linux/macOS.  Gemini's projects.json key is the lower-cased
        path on Windows (the CLI's own rule).

      - Not mirrored from Linux: the no-Node backend-only mode (--no-node,
        static Codex binary) and the ~/.gemini/.env offer for SSH remotes.
        install/doctor.sh is bash-only; on Windows the installer's own
        verification step is the check.

    CLI version pins come from install/harness-versions.env (shared with the
    Linux / macOS installers and install/doctor.sh).

    Run from PowerShell 7+ (recommended) or Windows PowerShell 5.1.

.PARAMETER Dev
    Install development dependencies (ruff, mypy).

.PARAMETER SkipPrereqs
    Skip prerequisite checks.

.PARAMETER SkipAuth
    Skip the agent-CLI install/login step (npm i + first run).

.PARAMETER NewContext
    Create a fresh context (non-interactive).

.PARAMETER ImportContext
    Import an existing context repository from the given URL.

.PARAMETER WithClaude
    Set up the Claude Code (Anthropic) session harness.

.PARAMETER WithoutClaude
    Skip Claude Code setup.

.PARAMETER WithQwen
    Set up the Qwen Code (Alibaba) session harness.

.PARAMETER WithoutQwen
    Skip Qwen Code setup.

.PARAMETER WithGemini
    Set up the Gemini CLI (Google) session harness.

.PARAMETER WithoutGemini
    Skip Gemini CLI setup.

.PARAMETER WithCodex
    Set up the Codex CLI (OpenAI - ChatGPT login) session harness.

.PARAMETER WithoutCodex
    Skip Codex CLI setup.

.PARAMETER WithModelStudio
    Set up Claude Code - Model Studio (GLM / DeepSeek / Kimi / Qwen through
    Claude Code's agent loop).  No extra CLI: needs the claude-agent-sdk
    (installed with it) and DASHSCOPE_API_KEY.

.PARAMETER WithoutModelStudio
    Skip Model Studio setup.

.PARAMETER WithAnthropic
    Install the `anthropic` Python SDK (Claude models in the orchestrator).

.PARAMETER WithoutAnthropic
    Skip the `anthropic` SDK.

.PARAMETER WithOpenAI
    Install the `openai` Python SDK (GPT, Qwen, Gemini via OpenAI-compat
    endpoint, plus OpenAI Realtime voice).

.PARAMETER WithoutOpenAI
    Skip the `openai` SDK.

.PARAMETER QwenOnly
    Shortcut equivalent to `-WithQwen -WithoutClaude -WithoutGemini
    -WithoutCodex -WithOpenAI -WithoutAnthropic`.

.EXAMPLE
    .\install\windows\install.ps1
    Interactive install (asks both axis questions).

.EXAMPLE
    .\install\windows\install.ps1 -QwenOnly
    Fully Qwen-backed setup, no Anthropic.

.EXAMPLE
    .\install\windows\install.ps1 -WithClaude -WithAnthropic -WithOpenAI
    Default power-user setup, no prompts.
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$SkipPrereqs,
    [switch]$SkipAuth,
    [switch]$NewContext,
    [string]$ImportContext = "",
    [switch]$WithClaude,
    [switch]$WithoutClaude,
    [switch]$WithQwen,
    [switch]$WithoutQwen,
    [switch]$WithGemini,
    [switch]$WithoutGemini,
    [switch]$WithCodex,
    [switch]$WithoutCodex,
    [switch]$WithModelStudio,
    [switch]$WithoutModelStudio,
    [switch]$WithAnthropic,
    [switch]$WithoutAnthropic,
    [switch]$WithOpenAI,
    [switch]$WithoutOpenAI,
    [switch]$QwenOnly
)

$ErrorActionPreference = 'Stop'

# ─────────────────────────────────────────────────────────────────────────────
# Resolve project root.  This script lives at install/windows/install.ps1 —
# project root is two dirs up.  All shared templates live in install/.
# ─────────────────────────────────────────────────────────────────────────────
$InstallerDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallTemplates = Resolve-Path (Join-Path $InstallerDir '..') | ForEach-Object Path
$ScriptDir        = Resolve-Path (Join-Path $InstallTemplates '..') | ForEach-Object Path
Set-Location $ScriptDir

# Pinned harness CLI versions — install/harness-versions.env is the single
# source of truth (plain KEY=VALUE lines, shared with the bash installers).
$HarnessVersions = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $InstallTemplates 'harness-versions.env')) {
    if ($line -match '^\s*([A-Z0-9_]+)=(\S+)\s*$') { $HarnessVersions[$Matches[1]] = $Matches[2] }
}
$QwenCliPin       = $HarnessVersions['QWEN_CLI_VERSION']
$GeminiCliVersion = $HarnessVersions['GEMINI_CLI_VERSION']
$CodexCliVersion  = $HarnessVersions['CODEX_CLI_VERSION']
$NodeMinMajor     = [int]$HarnessVersions['NODE_MIN_MAJOR']

# ─────────────────────────────────────────────────────────────────────────────
# Output helpers.  Match the visual style of install.sh as closely as the
# Windows console allows.  PowerShell 7 supports ANSI escapes; 5.1 generally
# does on Windows 10/11 unless ConEmu/legacy host.  Write-Host with -Foreground
# is the safe fallback that works everywhere.
# ─────────────────────────────────────────────────────────────────────────────
function Write-Info  { param([string]$m) Write-Host "[OK]   $m" -ForegroundColor Green }
function Write-Step  { param([string]$m) Write-Host "[STEP] $m" -ForegroundColor Blue }
function Write-Warn  { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Ask   { param([string]$m) Write-Host "[?]    $m" -ForegroundColor Cyan -NoNewline }
function Write-Err   { param([string]$m) Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

function Test-Interactive {
    # Returns $true if stdin is attached to a console (user can type answers).
    # In CI / piped installs this is $false and we take the non-interactive
    # path everywhere prompts would otherwise appear.
    return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

function Read-YesNo {
    param(
        [string]$Question,
        [string]$Default = 'Y'
    )
    if (-not (Test-Interactive)) {
        return ($Default -eq 'Y')
    }
    Write-Ask "$Question [$(if ($Default -eq 'Y') { 'Y/n' } else { 'y/N' })] "
    $ans = Read-Host
    if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $Default }
    return $ans -match '^[Yy]'
}

# ─────────────────────────────────────────────────────────────────────────────
# Tri-state axis resolution.  Each axis (claude / qwen / gemini / codex /
# anthropic / openai) ends up as one of $true / $false.  Until the user has been asked
# (or a flag has been passed), the state is $null — that's what the
# interactive prompts later look for to decide whether to ask.
# ─────────────────────────────────────────────────────────────────────────────
function Resolve-Switch {
    param([switch]$With, [switch]$Without)
    if ($With)    { return $true }
    if ($Without) { return $false }
    return $null
}

$ClaudeAxis    = Resolve-Switch -With:$WithClaude    -Without:$WithoutClaude
$QwenAxis      = Resolve-Switch -With:$WithQwen      -Without:$WithoutQwen
$GeminiAxis    = Resolve-Switch -With:$WithGemini    -Without:$WithoutGemini
$CodexAxis     = Resolve-Switch -With:$WithCodex     -Without:$WithoutCodex
$ModelStudioAxis = Resolve-Switch -With:$WithModelStudio -Without:$WithoutModelStudio
$AnthropicAxis = Resolve-Switch -With:$WithAnthropic -Without:$WithoutAnthropic
$OpenAIAxis    = Resolve-Switch -With:$WithOpenAI    -Without:$WithoutOpenAI

if ($QwenOnly) {
    # Only fills in blanks — explicit per-axis flags still win.
    if ($null -eq $ClaudeAxis)    { $ClaudeAxis    = $false }
    if ($null -eq $QwenAxis)      { $QwenAxis      = $true  }
    if ($null -eq $GeminiAxis)    { $GeminiAxis    = $false }
    if ($null -eq $CodexAxis)     { $CodexAxis     = $false }
    if ($null -eq $ModelStudioAxis) { $ModelStudioAxis = $false }
    if ($null -eq $AnthropicAxis) { $AnthropicAxis = $false }
    if ($null -eq $OpenAIAxis)    { $OpenAIAxis    = $true  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "           Personal Assistant Installer (Windows)"        -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""
Write-Host "A transparent, hackable AI assistant that evolves with you."
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 0a: Session harness — which agent CLI(s) to set up
# ─────────────────────────────────────────────────────────────────────────────
if ($null -eq $ClaudeAxis -and $null -eq $QwenAxis -and $null -eq $GeminiAxis -and $null -eq $CodexAxis -and $null -eq $ModelStudioAxis) {
    Write-Host "── Session harness ──" -ForegroundColor Cyan
    Write-Host "Which agent CLI(s) should run your chats?  (You can pick more than one;"
    Write-Host "the UI's Session Provider selector switches between them at runtime.)"
    Write-Host ""
    $ClaudeAxis = Read-YesNo "Set up Claude Code (Anthropic - recommended default)?" 'Y'
    $QwenAxis   = Read-YesNo "Set up Qwen Code (Alibaba - open weights, OAuth or DashScope key)?" 'N'
    $GeminiAxis = Read-YesNo "Set up Gemini CLI (Google - needs GEMINI_API_KEY)?" 'N'
    $CodexAxis  = Read-YesNo "Set up Codex CLI (OpenAI - ChatGPT login)?" 'N'
    $ModelStudioAxis = Read-YesNo "Set up Claude Code - Model Studio (GLM / DeepSeek / Kimi via DASHSCOPE_API_KEY, no extra CLI)?" 'N'
    Write-Host ""
}
if ($null -eq $ClaudeAxis) { $ClaudeAxis = $false }
if ($null -eq $QwenAxis)   { $QwenAxis   = $false }
if ($null -eq $GeminiAxis) { $GeminiAxis = $false }
if ($null -eq $CodexAxis)  { $CodexAxis  = $false }
if ($null -eq $ModelStudioAxis) { $ModelStudioAxis = $false }

if (-not $ClaudeAxis -and -not $QwenAxis -and -not $GeminiAxis -and -not $CodexAxis -and -not $ModelStudioAxis) {
    Write-Err "Refusing to install with no harnesses - pick at least one (-WithClaude / -WithQwen / -WithGemini / -WithCodex / -WithModelStudio)."
}
# Model Studio runs the Claude Code CLI bundled with claude-agent-sdk, with the
# same .claude_config\ wiring - set that up for either harness.
$ClaudeRuntime = ($ClaudeAxis -or $ModelStudioAxis)

# ─────────────────────────────────────────────────────────────────────────────
# Step 0b: Orchestrator backends — which API SDK(s) to install
# ─────────────────────────────────────────────────────────────────────────────
if ($null -eq $AnthropicAxis -and $null -eq $OpenAIAxis) {
    Write-Host "── Orchestrator backends ──" -ForegroundColor Cyan
    Write-Host "Which API SDK(s) should the orchestrator use?"
    Write-Host ""
    Write-Host "  1) OpenAI only (GPT models, Qwen, Gemini, voice mode - recommended default for Qwen-only setups)"
    Write-Host "  2) Anthropic only (Claude models in the orchestrator picker)"
    Write-Host "  3) Both"
    Write-Host "  4) Neither (orchestrator disabled - chats only)"
    Write-Host ""
    Write-Ask "Choice [1/2/3/4] (default 3): "
    $orchChoice = Read-Host
    if ([string]::IsNullOrWhiteSpace($orchChoice)) { $orchChoice = '3' }
    switch ($orchChoice) {
        '1' { $AnthropicAxis = $false; $OpenAIAxis = $true  }
        '2' { $AnthropicAxis = $true;  $OpenAIAxis = $false }
        '3' { $AnthropicAxis = $true;  $OpenAIAxis = $true  }
        '4' { $AnthropicAxis = $false; $OpenAIAxis = $false }
        default { Write-Err "Invalid choice: $orchChoice. Expected 1, 2, 3, or 4." }
    }
    Write-Host ""
}
if ($null -eq $AnthropicAxis) { $AnthropicAxis = $false }
if ($null -eq $OpenAIAxis)    { $OpenAIAxis    = $false }

if ($ClaudeAxis)    { Write-Info "Will set up Claude Code harness" }
if ($QwenAxis)      { Write-Info "Will set up Qwen Code harness" }
if ($GeminiAxis)    { Write-Info "Will set up Gemini CLI harness" }
if ($CodexAxis)     { Write-Info "Will set up Codex CLI harness" }
if ($ModelStudioAxis) { Write-Info "Will set up Claude Code - Model Studio harness" }
if ($AnthropicAxis) { Write-Info "Will install anthropic SDK (orchestrator)" }
if ($OpenAIAxis)    { Write-Info "Will install openai SDK (orchestrator + voice)" }
if (-not $AnthropicAxis -and -not $OpenAIAxis) {
    Write-Warn "No orchestrator backend selected - the orchestrator tab will be disabled."
}
Write-Host ""

# Default provider written into assistant_config.json: the first installed
# harness in the order Claude, Qwen, Gemini, Codex, Model Studio (Claude is
# the historical default).  At least one is installed - checked above.
$DefaultProvider = if ($ClaudeAxis) { 'claude' } elseif ($QwenAxis) { 'qwen' } elseif ($GeminiAxis) { 'gemini' } elseif ($CodexAxis) { 'codex' } else { 'modelstudio' }

# ─────────────────────────────────────────────────────────────────────────────
# Symlink strategy.  Windows symbolic links require either:
#   - Developer Mode enabled (Settings -> Update & Security -> For Developers)
#   - The shell running as Administrator
#   - The SeCreateSymbolicLinkPrivilege user right
#
# We try a real symlink first; on failure we fall back to:
#   - Directory targets: NTFS junction (no privileges needed; same drive only)
#   - File targets:      plain copy (no privileges; not auto-updating)
#
# Test once at the top so we can warn the user early instead of failing
# halfway through the install.
# ─────────────────────────────────────────────────────────────────────────────
$script:SymlinksWork = $null

function Test-Symlinks {
    if ($null -ne $script:SymlinksWork) { return $script:SymlinksWork }

    $probeBase = Join-Path $env:TEMP ("assistant-symlink-probe-" + [Guid]::NewGuid().ToString('N'))
    $target = Join-Path $probeBase "real"
    $link   = Join-Path $probeBase "link"
    try {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        try {
            New-Item -ItemType SymbolicLink -Path $link -Target $target -ErrorAction Stop | Out-Null
            $script:SymlinksWork = $true
        } catch {
            $script:SymlinksWork = $false
        }
    } finally {
        if (Test-Path $probeBase) {
            Remove-Item -Path $probeBase -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    return $script:SymlinksWork
}

function New-Link {
    <#
    Create a link (or fallback) from $Path → $Target.

    Behavior:
      - If symlinks are available (Dev Mode or admin), creates a real symlink.
        Works for both files and directories.
      - Otherwise:
          - If $Target is a directory, creates an NTFS junction at $Path.
          - If $Target is a file, copies the file to $Path (one-time; future
            edits to $Target won't propagate unless install.ps1 is re-run).

    Always idempotent: if $Path already exists and points at $Target, no-op.
    #>
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Target
    )
    # Already exists with correct target?
    if (Test-Path $Path) {
        $existing = Get-Item $Path -Force
        if ($existing.LinkType -in @('SymbolicLink', 'Junction')) {
            try {
                $existingTarget = $existing.Target | Select-Object -First 1
                if ($existingTarget) {
                    $resolvedExisting = (Resolve-Path -LiteralPath $existingTarget -ErrorAction SilentlyContinue).Path
                    $resolvedTarget   = (Resolve-Path -LiteralPath $Target          -ErrorAction SilentlyContinue).Path
                    if ($resolvedExisting -and $resolvedTarget -and ($resolvedExisting -eq $resolvedTarget)) {
                        return  # already correctly linked
                    }
                }
            } catch { }
        }
    }

    if (Test-Symlinks) {
        New-Item -ItemType SymbolicLink -Path $Path -Target $Target -Force | Out-Null
        return
    }

    # Fallback path.
    $targetItem = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    if ($null -eq $targetItem) {
        throw "New-Link: target '$Target' does not exist"
    }
    if ($targetItem.PSIsContainer) {
        # Directory → junction (no privileges needed, same drive only).
        if (Test-Path $Path) { Remove-Item -Path $Path -Recurse -Force }
        New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
    } else {
        # File → copy.  Re-running install.ps1 re-copies if the source changed.
        Copy-Item -LiteralPath $Target -Destination $Path -Force
    }
}

function Show-SymlinkFallbackBanner {
    Write-Host ""
    Write-Warn "Real symlinks aren't available — falling back to junctions (directories) and copies (files)."
    Write-Host "    On Windows 10/11, enable Developer Mode for full symlink support:"
    Write-Host "      Settings -> Update & Security -> For Developers -> Developer Mode"
    Write-Host "    Or re-run this installer from an elevated (Administrator) PowerShell prompt."
    Write-Host "    Without symlinks: file links become one-time copies — you'll need to re-run"
    Write-Host "    install.ps1 if any of the install/ templates change.  Directory junctions"
    Write-Host "    work as well as symlinks for the SDK config dirs (.claude_config etc.)."
    Write-Host ""
}

if (-not (Test-Symlinks)) {
    Show-SymlinkFallbackBanner
}

function Set-DirLink {
    <#
    Idempotent directory link (symlink, or junction without symlink rights):
      correct link -> left alone; wrong link -> warn, never clobbered;
      empty real directory -> replaced; non-empty directory or file -> warn.
    Get-Item -Force sees broken links that Test-Path reports as missing.
    #>
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Target,
        [Parameter(Mandatory)] [string]$Label
    )
    $existing = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        $t  = $existing.Target | Select-Object -First 1
        $r1 = if ($t) { (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path } else { $null }
        $r2 = (Resolve-Path -LiteralPath $Target -ErrorAction SilentlyContinue).Path
        if ($r1 -and $r2 -and $r1 -eq $r2) {
            Write-Info "$Label link already points to $Target"
        } else {
            Write-Warn "$Path points to $t (expected $Target) - leaving alone"
        }
    } elseif ($existing -and $existing.PSIsContainer) {
        if (-not (Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $Path -Force
            New-Link -Path $Path -Target $Target
            Write-Info "Replaced empty directory $Path with the $Label link"
        } else {
            Write-Warn "$Path is a non-empty directory - leaving alone (merge it into $Target, remove it, re-run)"
        }
    } elseif ($existing) {
        Write-Warn "$Path exists and is not a link - leaving alone"
    } else {
        $parent = Split-Path -Parent $Path
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        New-Link -Path $Path -Target $Target
        Write-Info "Created $Label link -> $Target"
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Check prerequisites
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipPrereqs) {
    Write-Step "Checking prerequisites..."
    Write-Host ""
    $prereqScript = Join-Path $InstallerDir 'install-prerequisites.ps1'
    & $prereqScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Err "Please install missing prerequisites and try again."
    }
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Context setup
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Setting up context..."
Write-Host ""

$ContextSetupNeeded = $false

if ((Test-Path 'context') -and (Test-Path 'context\memory\MEMORY.md')) {
    Write-Info "Context folder already exists and is configured"
    Write-Host ""
    if (-not (Read-YesNo "Do you want to keep the existing context?" 'Y')) {
        Write-Warn "Backing up existing context to context.bak\"
        if (Test-Path 'context.bak') { Remove-Item -Recurse -Force 'context.bak' }
        Move-Item 'context' 'context.bak'
        $ContextSetupNeeded = $true
    }
} elseif (Test-Path 'context') {
    if (-not (Get-ChildItem 'context' -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -Path 'context' -Force -ErrorAction SilentlyContinue
        $ContextSetupNeeded = $true
    } else {
        Write-Warn "Context folder exists but may be incomplete"
        $ContextSetupNeeded = $true
    }
} else {
    $ContextSetupNeeded = $true
}

if ($ContextSetupNeeded) {
    if ($ImportContext) {
        $ContextMode = 'import'
        $ContextUrl  = $ImportContext
    } elseif ($NewContext) {
        $ContextMode = 'new'
    } else {
        Write-Host ""
        Write-Host "The context folder stores your personal data:"
        Write-Host "  - Conversation history"
        Write-Host "  - Memory files"
        Write-Host "  - Custom skills and scripts"
        Write-Host "  - API credentials"
        Write-Host ""
        Write-Host "Choose how to set up your context:"
        Write-Host ""
        Write-Host "  1) " -NoNewline; Write-Host "New installation"   -ForegroundColor Green -NoNewline; Write-Host " - Start fresh with an empty context"
        Write-Host "  2) " -NoNewline; Write-Host "Import existing"    -ForegroundColor Blue  -NoNewline; Write-Host " - Clone your existing context repository"
        Write-Host ""
        Write-Ask "Enter choice [1/2]: "
        $choice = Read-Host
        switch ($choice) {
            '1' { $ContextMode = 'new' }
            '2' {
                $ContextMode = 'import'
                Write-Ask "Enter your context repository URL (e.g. git@github.com:user/assistant-context.git): "
                $ContextUrl = Read-Host
            }
            default { Write-Err "Invalid choice. Please run the installer again." }
        }
    }
    Write-Host ""

    switch ($ContextMode) {
        'new' {
            Write-Step "Creating fresh context..."

            foreach ($sub in 'memory','skills','scripts','agents','secrets','certs') {
                New-Item -ItemType Directory -Path "context\$sub" -Force | Out-Null
            }

            Copy-Item -LiteralPath (Join-Path $InstallTemplates 'MEMORY.md') -Destination 'context\memory\MEMORY.md' -Force
            Write-Info "Seeded context/memory/MEMORY.md from install/MEMORY.md"
            if (-not (Test-Path 'context\AGENTS.md')) {
                Copy-Item -LiteralPath (Join-Path $InstallTemplates 'AGENTS.md') -Destination 'context\AGENTS.md' -Force
                Write-Info "Seeded context/AGENTS.md from install/AGENTS.md"
            }
            # The orchestrator's private memory (its identity, and how it gets to
            # know a new user) and its empty run_script allowlist.
            foreach ($seed in 'ORCHESTRATOR_MEMORY.md','ORCHESTRATOR_SCRIPTS.md') {
                Copy-Item -LiteralPath (Join-Path $InstallTemplates $seed) -Destination "context\memory\$seed" -Force
                Write-Info "Seeded context/memory/$seed from install/$seed"
            }

            # Skill / script / agent symlinks (or junctions / copies as fallback).
            # All three loops are identical except for the source dir.
            $bundles = @(
                @{ Public = 'shared\skills';  Private = 'context\skills';  Kind = 'directory'; Label = 'skill'  },
                @{ Public = 'shared\scripts'; Private = 'context\scripts'; Kind = 'any';       Label = 'script' },
                @{ Public = 'shared\agents';  Private = 'context\agents';  Kind = 'any';       Label = 'agent'  }
            )
            foreach ($b in $bundles) {
                Write-Step "Creating $($b.Label) symlinks..."
                foreach ($item in Get-ChildItem -LiteralPath (Join-Path $ScriptDir $b.Public) -Force) {
                    if ($b.Kind -eq 'directory' -and -not $item.PSIsContainer) { continue }
                    if ($item.Name -eq '__pycache__') { continue }  # bytecode cache, not a script
                    $dest = Join-Path $b.Private $item.Name
                    if (Test-Path $dest) { continue }
                    New-Link -Path $dest -Target $item.FullName
                }
            }

            Copy-Item -LiteralPath (Join-Path $InstallTemplates 'context.env') -Destination 'context\.env' -Force
            Write-Info "Seeded context/.env from install/context.env"

            function Enable-EnvKey {
                # Uncomment a `# KEY=` line in context/.env so it shows up as
                # required (vs. just a commented-out hint).  Idempotent.
                param([string]$Key)
                $envPath = 'context\.env'
                $content = Get-Content -LiteralPath $envPath -Raw
                $pattern = "(?m)^# *${Key}="
                if ($content -match $pattern) {
                    $content = [regex]::Replace($content, $pattern, "${Key}=")
                    Set-Content -LiteralPath $envPath -Value $content -NoNewline
                }
            }
            if ($OpenAIAxis)    { Enable-EnvKey 'OPENAI_API_KEY'    }
            if ($AnthropicAxis) { Enable-EnvKey 'ANTHROPIC_API_KEY' }
            if ($QwenAxis -or $ModelStudioAxis) { Enable-EnvKey 'DASHSCOPE_API_KEY' }
            if ($GeminiAxis)    { Enable-EnvKey 'GEMINI_API_KEY'    }

            Write-Info "Created fresh context with default structure"
            Write-Host ""
            Write-Warn "Remember to:"
            Write-Host "    1. Edit context\.env with your API keys"
            Write-Host "    2. (Optional) Initialize as a git repo for backup:"
            Write-Host "       cd context; git init; git add .; git commit -m 'Initial context'"
        }

        'import' {
            Write-Step "Importing context from: $ContextUrl"
            & git clone $ContextUrl context
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Failed to clone context repository. Check the URL and your access."
            }
            Write-Info "Successfully cloned context repository"

            foreach ($sub in 'memory','skills','scripts','agents') {
                if (-not (Test-Path "context\$sub")) {
                    Write-Warn "Creating missing $sub\ folder"
                    New-Item -ItemType Directory -Path "context\$sub" -Force | Out-Null
                }
            }

            $bundles = @(
                @{ Public = 'shared\skills';  Private = 'context\skills';  Kind = 'directory' },
                @{ Public = 'shared\scripts'; Private = 'context\scripts'; Kind = 'any'       },
                @{ Public = 'shared\agents';  Private = 'context\agents';  Kind = 'any'       }
            )
            Write-Step "Ensuring default symlinks..."
            foreach ($b in $bundles) {
                foreach ($item in Get-ChildItem -LiteralPath (Join-Path $ScriptDir $b.Public) -Force) {
                    if ($b.Kind -eq 'directory' -and -not $item.PSIsContainer) { continue }
                    if ($item.Name -eq '__pycache__') { continue }  # bytecode cache, not a script
                    $dest = Join-Path $b.Private $item.Name
                    if (Test-Path $dest) { continue }
                    New-Link -Path $dest -Target $item.FullName
                }
            }
        }
    }
}
# Archie's documentation (docs\, versioned with the code) is part of the memory
# wiki as context\memory\archie - new, imported and kept contexts all need it.
New-Item -ItemType Directory -Path 'context\memory' -Force | Out-Null
Set-DirLink -Path 'context\memory\archie' -Target (Join-Path $ScriptDir 'docs') -Label 'Docs (memory\archie)'

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Path mangling — shared by Claude / Qwen / Gemini SDK config dirs.
#
# Claude Code and Qwen Code replace every character of the project path that
# is not a letter or digit with '-' (so `C:\Users\you\assistant` becomes
# `C--Users-you-assistant`).  Qwen lower-cases the path first on Windows
# (its sanitizeCwd), so its key differs only in case.
# ─────────────────────────────────────────────────────────────────────────────
$Mangled     = $ScriptDir -replace '[^A-Za-z0-9]', '-'
$QwenMangled = $ScriptDir.ToLowerInvariant() -replace '[^a-z0-9]', '-'

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Set up Claude SDK config link (Claude or Model Studio harness)
# ─────────────────────────────────────────────────────────────────────────────
if ($ClaudeRuntime) {
    Write-Step "Setting up Claude SDK configuration..."

    New-Item -ItemType Directory -Path '.claude_config\projects' -Force | Out-Null

    $LinkPath = ".claude_config\projects\$Mangled"
    $existing = if (Test-Path $LinkPath) { Get-Item $LinkPath -Force } else { $null }
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        $t  = $existing.Target | Select-Object -First 1
        $r1 = if ($t) { (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path } else { $null }
        $r2 = (Resolve-Path -LiteralPath (Join-Path $ScriptDir 'context') -ErrorAction SilentlyContinue).Path
        if ($r1 -and $r2 -and $r1 -eq $r2) { Write-Info "SDK link already points to context\" }
        else { Write-Warn "$LinkPath points to $t (not context\) - leaving alone" }
    } elseif ($existing -and $existing.PSIsContainer) {
        Write-Warn "Found real directory at $LinkPath - migrating to link"
        # Migrate any .jsonl session files into context\ before replacing.
        Get-ChildItem -LiteralPath $LinkPath -Filter '*.jsonl' -ErrorAction SilentlyContinue | ForEach-Object {
            $dest = Join-Path 'context' $_.Name
            if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest }
        }
        Remove-Item -LiteralPath $LinkPath -Recurse -Force
        New-Link -Path $LinkPath -Target (Join-Path $ScriptDir 'context')
        Write-Info "Replaced directory with SDK link"
    } else {
        New-Link -Path $LinkPath -Target (Join-Path $ScriptDir 'context')
        Write-Info "Created SDK link"
    }

    Set-DirLink -Path '.claude_config\skills' -Target (Join-Path $ScriptDir 'context\skills') -Label 'Claude skills'
    # And agents: the bundled CLI loads user agents from $CLAUDE_CONFIG_DIR\agents.
    Set-DirLink -Path '.claude_config\agents' -Target (Join-Path $ScriptDir 'context\agents') -Label 'Claude agents'

    Write-Host ""
} else {
    Write-Info "Skipping Claude SDK setup (neither -WithClaude nor -WithModelStudio)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3b: Set up Qwen Code config link (only if Qwen harness enabled)
# ─────────────────────────────────────────────────────────────────────────────
if ($QwenAxis) {
    Write-Step "Setting up Qwen Code configuration..."

    $QwenHome       = Join-Path $env:USERPROFILE '.qwen'
    $QwenProjectDir = Join-Path $QwenHome ("projects\$QwenMangled")
    $ExpectedTarget = Join-Path $ScriptDir 'context'

    New-Item -ItemType Directory -Path (Join-Path $QwenHome 'projects') -Force | Out-Null

    $existing = if (Test-Path $QwenProjectDir) { Get-Item $QwenProjectDir -Force } else { $null }
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        $currentTarget = $existing.Target | Select-Object -First 1
        if ($currentTarget) {
            $r1 = (Resolve-Path -LiteralPath $currentTarget -ErrorAction SilentlyContinue).Path
            $r2 = (Resolve-Path -LiteralPath $ExpectedTarget -ErrorAction SilentlyContinue).Path
            if ($r1 -and $r2 -and $r1 -eq $r2) {
                Write-Info "Qwen project link already points to context/"
            } else {
                Write-Warn "Qwen project link points to $currentTarget (not this project) - leaving alone"
            }
        }
    } elseif ($existing -and $existing.PSIsContainer) {
        Write-Warn "Found real directory at $QwenProjectDir - migrating to link"
        New-Item -ItemType Directory -Path 'context\chats' -Force | Out-Null
        $backup = "context\qwen-backup-$(Get-Date -Format yyyyMMddTHHmmss)"
        Copy-Item -LiteralPath $QwenProjectDir -Destination $backup -Recurse -ErrorAction SilentlyContinue
        if (Test-Path $backup) { Write-Info "Backed up original Qwen project dir -> $backup" }

        $chatsDir = Join-Path $QwenProjectDir 'chats'
        if (Test-Path $chatsDir) {
            Get-ChildItem -LiteralPath $chatsDir -Filter '*.jsonl' -ErrorAction SilentlyContinue | ForEach-Object {
                $dest = Join-Path 'context\chats' $_.Name
                if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest }
            }
            # (No *.runtime.json: resume only needs the JSONL; since 0.25 the
            # runtime file is a short-lived liveness marker, not session data.)
            Write-Info "Migrated Qwen chats into context\chats\"
        }
        Remove-Item -LiteralPath $QwenProjectDir -Recurse -Force
        New-Link -Path $QwenProjectDir -Target $ExpectedTarget
        Write-Info "Replaced directory with Qwen project link -> context/"
    } else {
        New-Link -Path $QwenProjectDir -Target $ExpectedTarget
        Write-Info "Created Qwen project link -> context/"
    }

    New-Item -ItemType Directory -Path 'context\chats' -Force | Out-Null

    Set-DirLink -Path (Join-Path $QwenHome 'skills') -Target (Join-Path $ScriptDir 'context\skills') -Label 'Qwen skills'
    Write-Host ""
} else {
    Write-Info "Skipping Qwen Code setup (-WithoutQwen)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3c: Set up Gemini CLI config link (only if Gemini harness enabled)
# ─────────────────────────────────────────────────────────────────────────────
if ($GeminiAxis) {
    Write-Step "Setting up Gemini CLI configuration..."

    $GeminiHome    = Join-Path $env:USERPROFILE '.gemini'
    $GeminiProjects = Join-Path $GeminiHome 'projects.json'

    # Gemini computes a label for the cwd on first run and stores it in
    # projects.json.  If that file exists already, read the label; otherwise
    # fall back to the cwd basename (matches the CLI's own first-run logic).
    $GeminiLabel = ''
    if (Test-Path $GeminiProjects) {
        try {
            $data = Get-Content -LiteralPath $GeminiProjects -Raw | ConvertFrom-Json
            if ($data.projects) {
                # Try the absolute path Gemini sees (with backslashes), then a
                # normalized version, since either form may be in projects.json.
                foreach ($k in $data.projects.PSObject.Properties.Name) {
                    if ($k -eq $ScriptDir -or $k -eq ($ScriptDir -replace '\\','/')) {
                        $GeminiLabel = $data.projects.$k
                        break
                    }
                }
            }
        } catch {
            # malformed projects.json — fall through to basename fallback
        }
    }
    if ([string]::IsNullOrEmpty($GeminiLabel)) {
        $GeminiLabel = Split-Path -Leaf $ScriptDir
    }

    $GeminiProjectDir = Join-Path $GeminiHome "tmp\$GeminiLabel"
    $ExpectedTarget   = Join-Path $ScriptDir 'context'

    New-Item -ItemType Directory -Path (Join-Path $GeminiHome 'tmp') -Force | Out-Null

    $existing = if (Test-Path $GeminiProjectDir) { Get-Item $GeminiProjectDir -Force } else { $null }
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        $currentTarget = $existing.Target | Select-Object -First 1
        if ($currentTarget) {
            $r1 = (Resolve-Path -LiteralPath $currentTarget -ErrorAction SilentlyContinue).Path
            $r2 = (Resolve-Path -LiteralPath $ExpectedTarget -ErrorAction SilentlyContinue).Path
            if ($r1 -and $r2 -and $r1 -eq $r2) {
                Write-Info "Gemini project link already points to context/"
            } else {
                Write-Warn "Gemini project link points to $currentTarget (not this project) - leaving alone"
            }
        }
    } elseif ($existing -and $existing.PSIsContainer) {
        Write-Warn "Found real directory at $GeminiProjectDir - migrating to link"
        New-Item -ItemType Directory -Path 'context\chats' -Force | Out-Null
        $backup = "context\gemini-backup-$(Get-Date -Format yyyyMMddTHHmmss)"
        Copy-Item -LiteralPath $GeminiProjectDir -Destination $backup -Recurse -ErrorAction SilentlyContinue
        if (Test-Path $backup) { Write-Info "Backed up original Gemini project dir -> $backup" }

        $chatsDir = Join-Path $GeminiProjectDir 'chats'
        if (Test-Path $chatsDir) {
            Get-ChildItem -LiteralPath $chatsDir -Filter 'session-*.jsonl' -ErrorAction SilentlyContinue | ForEach-Object {
                $dest = Join-Path 'context\chats' $_.Name
                if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest }
            }
            Write-Info "Migrated Gemini chats into context\chats\"
        }
        Remove-Item -LiteralPath $GeminiProjectDir -Recurse -Force
        New-Link -Path $GeminiProjectDir -Target $ExpectedTarget
        Write-Info "Replaced directory with Gemini project link -> context/"
    } else {
        New-Link -Path $GeminiProjectDir -Target $ExpectedTarget
        Write-Info "Created Gemini project link -> context/"
    }
    New-Item -ItemType Directory -Path 'context\chats' -Force | Out-Null
    Write-Host ""
} else {
    Write-Info "Skipping Gemini CLI setup (-WithoutGemini)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3c2: Set up Archie's Codex home (only if Codex harness enabled)
# ─────────────────────────────────────────────────────────────────────────────
# Archie runs Codex with CODEX_HOME=~/.codex-archie (%USERPROFILE%\.codex-archie)
# once that home holds its own login; until then it falls back to the shared
# ~/.codex.  Here we seed that home's config.toml (never overwritten) and link
# its sessions\ to context\codex\sessions so rollouts travel with the rest of
# context\.  We never copy auth.json: ChatGPT refresh tokens rotate, and two
# homes holding one token family break each other.
if ($CodexAxis) {
    Write-Step "Setting up Codex CLI configuration..."

    $CodexArchieHome     = Join-Path $env:USERPROFILE '.codex-archie'
    $CodexSessionsTarget = Join-Path $ScriptDir 'context\codex\sessions'
    New-Item -ItemType Directory -Path $CodexArchieHome     -Force | Out-Null
    New-Item -ItemType Directory -Path $CodexSessionsTarget -Force | Out-Null

    $CodexConfig = Join-Path $CodexArchieHome 'config.toml'
    if (-not (Test-Path $CodexConfig)) {
        # Template (install\cli-runtime\codex-home\config.toml): project_doc_max_bytes
        # = 131072 (context\AGENTS.md is ~51 KB, the default 32 KiB would truncate
        # it); plugins/apps/memories features off.  Copied byte for byte (UTF-8, no BOM).
        # An existing file gets only [features] memories = false added, after
        # the venv exists (Step 4, below).
        Copy-Item -LiteralPath (Join-Path $InstallTemplates 'cli-runtime\codex-home\config.toml') -Destination $CodexConfig
        Write-Info "Seeded $CodexConfig"
    } else {
        Write-Info "$CodexConfig already exists - leaving it alone"
    }

    $CodexSessionsLink = Join-Path $CodexArchieHome 'sessions'
    $existing = if (Test-Path $CodexSessionsLink) { Get-Item $CodexSessionsLink -Force } else { $null }
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        $currentTarget = $existing.Target | Select-Object -First 1
        $r1 = if ($currentTarget) { (Resolve-Path -LiteralPath $currentTarget -ErrorAction SilentlyContinue).Path } else { $null }
        $r2 = (Resolve-Path -LiteralPath $CodexSessionsTarget -ErrorAction SilentlyContinue).Path
        if ($r1 -and $r2 -and $r1 -eq $r2) {
            Write-Info "Codex sessions link already points to context\codex\sessions"
        } else {
            Write-Warn "$CodexSessionsLink points to $currentTarget - leaving alone"
        }
    } elseif ($existing -and $existing.PSIsContainer) {
        Write-Warn "Found real directory at $CodexSessionsLink - migrating to link"
        # Copy every rollout that isn't already in context\codex\sessions
        # (keeps the YYYY\MM\DD layout), then move the original aside.
        $srcRoot = $existing.FullName
        Get-ChildItem -LiteralPath $srcRoot -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
            $rel  = $_.FullName.Substring($srcRoot.Length).TrimStart('\','/')
            $dest = Join-Path $CodexSessionsTarget $rel
            if (-not (Test-Path $dest)) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $dest
            }
        }
        Move-Item -LiteralPath $srcRoot -Destination "$srcRoot.bak-$(Get-Date -Format yyyyMMddTHHmmss)"
        New-Link -Path $CodexSessionsLink -Target $CodexSessionsTarget
        Write-Info "Moved rollouts into context\codex\sessions and linked $CodexSessionsLink"
    } else {
        New-Link -Path $CodexSessionsLink -Target $CodexSessionsTarget
        Write-Info "Created Codex sessions link -> context\codex\sessions"
    }
    Write-Host ""
} else {
    Write-Info "Skipping Codex CLI setup (-WithoutCodex)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3c3: Repo-level skills dir (.agents\skills -> context\skills)
# ─────────────────────────────────────────────────────────────────────────────
# Codex (verified on 0.161), Gemini CLI (workspace skills alias) and Qwen Code
# read skills from <repo>\.agents\skills.
if ($CodexAxis -or $GeminiAxis -or $QwenAxis) {
    Write-Step "Linking .agents\skills -> context\skills (Codex / Gemini / Qwen skill discovery)..."
    Set-DirLink -Path '.agents\skills' -Target (Join-Path $ScriptDir 'context\skills') -Label 'repo skills'
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3d: Wire AGENTS.md as the shared project-instructions file
# ─────────────────────────────────────────────────────────────────────────────
# Claude Code reads CLAUDE.md, Qwen Code reads QWEN.md, Gemini CLI reads
# GEMINI.md, Codex reads AGENTS.md — all at the project root, all links ->
# context\AGENTS.md.
Write-Step "Wiring context\AGENTS.md as the shared project-instructions file..."

# The repo commits these root files as git symlinks.  A Windows checkout
# without symlink support (core.symlinks=false) turns each into a tiny text
# file holding just the target path — a stub, not real instructions.
function Test-GitSymlinkStub {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -in @('SymbolicLink','Junction') -or $item.Length -gt 64) { return $false }
    return ((Get-Content -LiteralPath $Path -Raw).Trim() -eq 'context/AGENTS.md')
}

# Migration: normalize legacy layouts to context\AGENTS.md.
if (-not (Test-Path 'context\AGENTS.md')) {
    if ((Test-Path 'AGENTS.md') -and ((Get-Item 'AGENTS.md').LinkType -notin @('SymbolicLink','Junction')) -and -not (Test-GitSymlinkStub 'AGENTS.md')) {
        Move-Item -LiteralPath 'AGENTS.md' -Destination 'context\AGENTS.md'
        Write-Info "Moved AGENTS.md -> context\AGENTS.md"
    } elseif ((Test-Path 'CLAUDE.md') -and ((Get-Item 'CLAUDE.md').LinkType -notin @('SymbolicLink','Junction')) -and -not (Test-GitSymlinkStub 'CLAUDE.md')) {
        Move-Item -LiteralPath 'CLAUDE.md' -Destination 'context\AGENTS.md'
        Write-Info "Promoted CLAUDE.md -> context\AGENTS.md"
    } elseif (Test-Path (Join-Path $InstallTemplates 'AGENTS.md')) {
        Copy-Item -LiteralPath (Join-Path $InstallTemplates 'AGENTS.md') -Destination 'context\AGENTS.md'
        Write-Info "Seeded context\AGENTS.md from install/AGENTS.md"
    }
}

# Clean up stale root-level AGENTS.md from intermediate layout.
if (Test-Path 'AGENTS.md') {
    $item = Get-Item 'AGENTS.md' -Force
    if ($item.LinkType -in @('SymbolicLink','Junction') -or $item.Length -eq 0 -or (Test-GitSymlinkStub 'AGENTS.md')) {
        Remove-Item -LiteralPath 'AGENTS.md' -Force
    }
}

if (Test-Path 'context\AGENTS.md') {
    foreach ($shadow in 'CLAUDE.md','QWEN.md','GEMINI.md','AGENTS.md') {
        if (Test-Path $shadow) {
            $item = Get-Item $shadow -Force
            if ($item.LinkType -in @('SymbolicLink','Junction')) {
                $t = $item.Target | Select-Object -First 1
                $r1 = (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path
                $r2 = (Resolve-Path -LiteralPath 'context\AGENTS.md' -ErrorAction SilentlyContinue).Path
                if ($r1 -and $r2 -and $r1 -eq $r2) { continue }
                Remove-Item -LiteralPath $shadow -Force
            } elseif (Test-GitSymlinkStub $shadow) {
                Remove-Item -LiteralPath $shadow -Force   # git symlink checked out as text
            } else {
                Write-Warn "$shadow exists and is not a link - leaving alone (delete to enable shared instructions)"
                continue
            }
        }
        New-Link -Path $shadow -Target (Join-Path $ScriptDir 'context\AGENTS.md')
        Write-Info "Created $shadow -> context\AGENTS.md link"
    }
} else {
    Write-Warn "No context\AGENTS.md found - skipping CLAUDE.md/QWEN.md/GEMINI.md/AGENTS.md links"
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 3e: Seed per-CLI runtime dirs (.claude\, .qwen\, .gemini\)
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Seeding local CLI runtime dirs..."

function Initialize-CliRuntime {
    param([string]$Cli)  # claude | qwen | gemini
    $dst = ".$Cli"
    $src = Join-Path $InstallTemplates "cli-runtime\$Cli"
    if (-not (Test-Path $src)) {
        Write-Warn "No template at $src - skipping $Cli seed"
        return
    }
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    foreach ($f in Get-ChildItem -LiteralPath $src -Force) {
        $dest = Join-Path $dst $f.Name
        if (Test-Path $dest) { continue }  # never clobber
        Copy-Item -LiteralPath $f.FullName -Destination $dest
        Write-Info "Seeded $dest"
    }
}

if ($ClaudeAxis) { Initialize-CliRuntime 'claude' }
if ($QwenAxis)   { Initialize-CliRuntime 'qwen'   }
if ($GeminiAxis) { Initialize-CliRuntime 'gemini' }
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Create Python virtual environment
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Setting up Python virtual environment..."

# Find a python launcher that resolves to 3.11+.  `py -3.12` is the canonical
# Windows entry point; `python` and `python3` are common fallbacks.
function Get-PythonExe {
    foreach ($cand in 'py -3.12', 'py -3', 'python3', 'python') {
        $parts = $cand -split ' '
        $exe = $parts[0]
        $args = if ($parts.Count -gt 1) { $parts[1..($parts.Count-1)] } else { @() }
        try {
            $v = & $exe @args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $parts2 = $v.Trim() -split '\.'
                if ([int]$parts2[0] -ge 3 -and [int]$parts2[1] -ge 11) {
                    return @{ Exe = $exe; Args = $args }
                }
            }
        } catch { }
    }
    return $null
}

$Py = Get-PythonExe
if (-not $Py) {
    Write-Err "Python 3.11+ not found on PATH.  Install it via:  winget install Python.Python.3.12  (then re-open PowerShell)"
}

if (-not (Test-Path '.venv')) {
    & $Py.Exe @($Py.Args + @('-m','venv','.venv'))
    if ($LASTEXITCODE -ne 0) { Write-Err "Failed to create .venv" }
    Write-Info "Created .venv\"
} else {
    Write-Info ".venv\ already exists"
}

$VenvPy  = Join-Path $ScriptDir '.venv\Scripts\python.exe'
$VenvPip = Join-Path $ScriptDir '.venv\Scripts\pip.exe'

# Step 3e (continued): an existing .gemini\settings.json is never overwritten
# by the seed, so merge Archie's keys into it: session retention OFF (the
# CLI's sweep would delete old sessions in context\chats\),
# context.fileFiltering, API-key auth and the thinking overrides.  The Linux /
# macOS installers run this before the venv exists with the system python3;
# here it waits for the venv so we reuse the interpreter found above
# (the script is stdlib-only).
if ($GeminiAxis) {
    & $VenvPy 'backend\manager\gemini\workspace_settings.py' $ScriptDir
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Could not merge Archie's keys into .gemini\settings.json - fix the file; the backend refuses Gemini turns until then"
    }
}

# Step 3c (continued): pin Gemini's label.  Register the repo in
# ~\.gemini\projects.json and make the ownership marker (context\.project_root
# through the link) name this repo — otherwise Gemini can claim a new label on
# its next run, and its chats (history) and its memory index
# ~\.gemini\tmp\<label>\memory\MEMORY.md (through the link: the wiki's
# context\memory\MEMORY.md) would leave context\.  Same helper as Linux/macOS.
if ($GeminiAxis) {
    $gemOut = @(& $VenvPy 'install\gemini-project.py' $ScriptDir 'apply' '--label' $GeminiLabel 2>$null | ForEach-Object { "$_" })
    $gemOk  = ($LASTEXITCODE -eq 0)
    foreach ($l in $gemOut) {
        if ($l.StartsWith('note: '))        { Write-Host ("    " + $l.Substring(6)) }
        elseif ($l.StartsWith('problem: ')) { Write-Warn $l.Substring(9) }
    }
    if ($gemOk) { Write-Info "Gemini project '$GeminiLabel' registered for $ScriptDir" }
    if ($gemOut -contains 'memory_index=ok') {
        Write-Info "Gemini's memory index resolves to context\memory\MEMORY.md"
    } else {
        Write-Warn "Gemini's memory index (~\.gemini\tmp\$GeminiLabel\memory\MEMORY.md) does not resolve to context\memory\MEMORY.md - check the Gemini project link"
    }
    # Gemini's built-in prompt offers a "global personal memory" at
    # ~\.gemini\GEMINI.md - outside context\, not synced, not indexed.
    if (Test-Path (Join-Path $env:USERPROFILE '.gemini\GEMINI.md')) {
        Write-Warn "$env:USERPROFILE\.gemini\GEMINI.md exists (Gemini's global memory, outside context\) - move its facts into context\memory\ and delete it"
    }
}

# Step 3c2 (continued): Archie's memory is the wiki (the backend passes
# context\memory\MEMORY.md per session); keep Codex's own memory store off.
# Adds or replaces only `memories` under [features] in an existing file.
if ($CodexAxis) {
    $CodexConfig = Join-Path $env:USERPROFILE '.codex-archie\config.toml'
    $codexRes = "$(& $VenvPy 'install\codex-home-config.py' $CodexConfig 'apply' 2>$null | Select-Object -Last 1)"
    switch -Regex ($codexRes) {
        '^ok$'                         { Write-Info "Codex memories feature already off ([features] memories = false)" }
        '^(added|appended|replaced)$'  { Write-Info "Set [features] memories = false in $CodexConfig" }
        default                        { Write-Warn "Could not set [features] memories = false in $CodexConfig ($codexRes) - add it by hand" }
    }
}

# Step 3e (continued): an existing .qwen\settings.json is never overwritten
# either, so make sure its memory keys are off (Qwen's project dir is context\,
# so its managed auto-memory / auto-dream / auto-skill would write into the
# memory wiki in their own format; the backend also turns them off per run).
if ($QwenAxis -and (Test-Path '.qwen\settings.json')) {
    $qwenPy = "import json,sys;p=sys.argv[1];d=json.load(open(p,encoding='utf-8'));m=d.setdefault('memory',{});k=('enableManagedAutoMemory','enableManagedAutoDream','enableAutoSkill');b=[x for x in k if m.get(x) is not False];[m.__setitem__(x,False) for x in k];b and open(p,'w',encoding='utf-8').write(json.dumps(d,indent=2)+chr(10));print(' '.join(b))"
    $qwenRes = "$(& $VenvPy -c $qwenPy '.qwen\settings.json' 2>$null)"
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Could not check .qwen\settings.json (not valid JSON?) - set memory.enableManagedAutoMemory/AutoDream/AutoSkill to false by hand"
    } elseif ([string]::IsNullOrWhiteSpace($qwenRes)) {
        Write-Info "Qwen managed auto-memory/auto-dream/auto-skill already off in .qwen\settings.json"
    } else {
        Write-Info "Turned off in .qwen\settings.json: $qwenRes"
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Upgrade pip
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Upgrading pip..."
& $VenvPy -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Err "pip upgrade failed" }
Write-Info "pip upgraded"

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Install Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Installing Python dependencies..."
if ($Dev) {
    & $VenvPip install -r backend\requirements-dev.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-dev.txt failed" }
    Write-Info "Installed requirements-dev.txt (core + dev tools)"
} else {
    & $VenvPip install -r backend\requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements.txt failed" }
    Write-Info "Installed requirements.txt (core)"
}

if ($ClaudeRuntime) {
    # claude-agent-sdk bundles the Claude Code CLI the claude and modelstudio harnesses run.
    & $VenvPip install -r backend\requirements-claude.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-claude.txt failed" }
    Write-Info "Installed requirements-claude.txt (claude-agent-sdk)"
}
if ($AnthropicAxis) {
    & $VenvPip install -r backend\requirements-anthropic.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-anthropic.txt failed" }
    Write-Info "Installed requirements-anthropic.txt (anthropic SDK)"
}
if ($OpenAIAxis) {
    & $VenvPip install -r backend\requirements-openai.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-openai.txt failed" }
    Write-Info "Installed requirements-openai.txt (openai SDK)"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: Install frontend dependencies
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Installing frontend dependencies..."
Push-Location 'apps\web'
& npm install --silent
$npmStatus = $LASTEXITCODE
Pop-Location
if ($npmStatus -ne 0) { Write-Err "npm install (frontend) failed" }
Write-Info "Installed/updated frontend node_modules\"
# The web build checks the design tokens (apps\design-tokens), which has its own dependency.
Push-Location 'apps\design-tokens'
& npm install --silent
$npmStatus = $LASTEXITCODE
Pop-Location
if ($npmStatus -ne 0) { Write-Err "npm install (design tokens) failed" }

# ─────────────────────────────────────────────────────────────────────────────
# Step 7b: Install + authenticate agent CLIs
# ─────────────────────────────────────────────────────────────────────────────
function Install-HarnessCli {
    param([string]$Cli, [string]$Pkg)
    $existing = Get-Command $Cli -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Info "$Cli CLI already installed ($($existing.Source))"
        return $true
    }
    if (Test-Interactive) {
        if (-not (Read-YesNo "$Cli CLI not found.  Install globally via npm?" 'Y')) {
            Write-Warn "Skipped $Cli install - run 'npm install -g $Pkg' manually before first use."
            return $false
        }
    } else {
        Write-Info "$Cli CLI not found - installing (non-interactive mode)"
    }
    Write-Step "Installing $Cli via npm..."
    & npm install -g $Pkg
    if ($LASTEXITCODE -eq 0) {
        Write-Info "$Cli installed"
        return $true
    } else {
        Write-Warn "$Cli install failed - install manually: npm install -g $Pkg"
        return $false
    }
}

function Test-EnvKeyPresent {
    param([string]$Key)
    if (-not (Test-Path 'context\.env')) { return $false }
    $rx = "(?m)^${Key}=.+"
    return ((Get-Content -LiteralPath 'context\.env' -Raw) -match $rx)
}

function Read-DriverLogin {
    # Pause for the user to run the login command in a separate shell, then
    # re-check the auth state.  Silent (no prompt) in non-interactive or
    # --SkipAuth mode.
    param(
        [string]$Cli,
        [string]$LoginCmd,
        [scriptblock]$CheckAuth,
        [string]$EnvKey
    )
    if ($EnvKey -and (Test-EnvKeyPresent $EnvKey)) {
        Write-Info "${Cli}: $EnvKey set in context\.env - no interactive login needed"
        return
    }
    if (& $CheckAuth) {
        Write-Info "$Cli already authenticated"
        return
    }
    if ($SkipAuth) {
        Write-Warn "$Cli not authenticated, -SkipAuth set - log in manually before first use"
        return
    }
    if (-not (Test-Interactive)) {
        Write-Warn "$Cli not authenticated (non-interactive install) - run '$LoginCmd' manually before first use"
        return
    }
    Write-Host ""
    Write-Warn "$Cli is not authenticated."
    Write-Host "    Open a separate PowerShell in this directory and run:"
    Write-Host "      $LoginCmd" -ForegroundColor Blue
    if ($EnvKey) {
        Write-Host "    (Or set $EnvKey in context\.env to use an API key instead.)"
    }
    Write-Ask "Press Enter once login completes (or just press Enter to finish setup later): "
    [void](Read-Host)
    if (& $CheckAuth) {
        Write-Info "$Cli authenticated"
    } else {
        Write-Warn "$Cli still not authenticated - finish login before your first chat."
    }
}

if (-not $SkipAuth) {
    Write-Step "Installing and authenticating agent CLIs..."
    Write-Host ""

    if ($ClaudeAxis) {
        [void](Install-HarnessCli 'claude' '@anthropic-ai/claude-code')
        if (Get-Command claude -ErrorAction SilentlyContinue) {
            Read-DriverLogin 'claude' 'claude auth login' {
                $out = & claude auth status 2>$null
                $out -match '"loggedIn":\s*true'
            } 'ANTHROPIC_API_KEY'
        }
    }
    if ($ModelStudioAxis) {
        # No CLI to install: Model Studio runs the bundled Claude Code CLI
        # against DashScope's Anthropic-compatible endpoint.
        if (Test-EnvKeyPresent 'DASHSCOPE_API_KEY') {
            Write-Info "modelstudio: DASHSCOPE_API_KEY set in context\.env"
        } else {
            Write-Warn "modelstudio: set DASHSCOPE_API_KEY in context\.env (Alibaba Model Studio console) - sessions fail to start without it"
        }
    }
    if ($QwenAxis) {
        # Pinned ($QwenCliPin from install\harness-versions.env, =
        # backend/manager/qwen/adapter.py QWEN_CLI_VERSION).  Needs Node 22+.
        [void](Install-HarnessCli 'qwen' "@qwen-code/qwen-code@$QwenCliPin")
        if (Get-Command qwen -ErrorAction SilentlyContinue) {
            $qwenHave = (& qwen --version 2>$null | Select-Object -First 1)
            if ("$qwenHave".Trim() -ne $QwenCliPin) {
                Write-Warn "qwen $qwenHave installed; Archie expects $QwenCliPin - run: npm install -g @qwen-code/qwen-code@$QwenCliPin"
            }
            $nodeV = (& node -v 2>$null)
            if ($nodeV -and [int](("$nodeV".TrimStart('v') -split '\.')[0]) -lt $NodeMinMajor) {
                Write-Warn "qwen-code $QwenCliPin needs Node.js $NodeMinMajor+ (found $nodeV); upgrade Node before using the Qwen harness."
            }
            # Qwen has no `auth status` subcommand and stores OAuth state in
            # ~\.qwen\oauth_creds.json when used in OAuth mode.  API-key mode
            # (DashScope) is detected via context\.env.
            Read-DriverLogin 'qwen' 'qwen' {
                Test-Path (Join-Path $env:USERPROFILE '.qwen\oauth_creds.json')
            } 'DASHSCOPE_API_KEY'
        }
    }
    if ($GeminiAxis) {
        # Pinned ($GeminiCliVersion from install\harness-versions.env).
        [void](Install-HarnessCli 'gemini' "@google/gemini-cli@$GeminiCliVersion")
        if (Get-Command gemini -ErrorAction SilentlyContinue) {
            $geminiHave = (& gemini --version 2>$null | Select-Object -Last 1)
            if ("$geminiHave".Trim() -ne $GeminiCliVersion) {
                Write-Warn "gemini CLI is $geminiHave; Archie is tested with $GeminiCliVersion - run: npm install -g @google/gemini-cli@$GeminiCliVersion"
            }
            # Google stopped serving Gemini CLI to personal Google logins
            # (oauth-personal) on 2026-06-18 - an OAuth login no longer
            # counts; only GEMINI_API_KEY (AI Studio) does.
            if (Test-EnvKeyPresent 'GEMINI_API_KEY') {
                Write-Info "gemini: GEMINI_API_KEY set in context\.env"
            } else {
                Write-Warn "gemini: set GEMINI_API_KEY in context\.env (create one at https://aistudio.google.com/apikey) - Google no longer serves Gemini CLI to personal Google-account logins"
            }
        }
    }
    if ($CodexAxis) {
        # Pinned ($CodexCliVersion from install\harness-versions.env).
        [void](Install-HarnessCli 'codex' "@openai/codex@$CodexCliVersion")
        if (Get-Command codex -ErrorAction SilentlyContinue) {
            # `codex --version` prints "codex-cli <version>".
            $codexHave = ("$(& codex --version 2>$null | Select-Object -First 1)".Trim() -split '\s+')[-1]
            if ($codexHave -ne $CodexCliVersion) {
                Write-Warn "codex CLI is $codexHave; Archie is tested with $CodexCliVersion - run: npm install -g @openai/codex@$CodexCliVersion"
            }
            # A dedicated login (its own token family) is preferred; the
            # shared ~\.codex login also works.  No API-key fallback on
            # purpose: Archie strips OPENAI_API_KEY from Codex's env.
            $codexLoginCmd = '$env:CODEX_HOME = "$env:USERPROFILE\.codex-archie"; codex login --device-auth; Remove-Item Env:CODEX_HOME'
            Read-DriverLogin 'codex' $codexLoginCmd {
                (Test-Path (Join-Path $env:USERPROFILE '.codex-archie\auth.json')) -or
                (Test-Path (Join-Path $env:USERPROFILE '.codex\auth.json'))
            } ''
            if (-not (Test-Path (Join-Path $env:USERPROFILE '.codex-archie\auth.json')) -and
                (Test-Path (Join-Path $env:USERPROFILE '.codex\auth.json'))) {
                Write-Info "codex: using the shared ~\.codex login.  For a dedicated Archie login run:"
                Write-Host "      $codexLoginCmd"
            }
        }
    }
    Write-Host ""
} else {
    Write-Info "Skipping agent CLI install/login step (-SkipAuth)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Create local directories
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Creating local directories..."
New-Item -ItemType Directory -Path 'index' -Force | Out-Null
New-Item -ItemType Directory -Path 'logs'  -Force | Out-Null
Write-Info "Created index\, logs\"

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Link Claude Code credentials into .claude_config\
# ─────────────────────────────────────────────────────────────────────────────
# On Windows the Claude CLI stores its OAuth credentials at
# %USERPROFILE%\.claude\.credentials.json.  Symlink (or copy as fallback)
# into .claude_config\ so the SDK in this project picks up the same token.
$ClaudeCreds = Join-Path $env:USERPROFILE '.claude\.credentials.json'
if ($ClaudeAxis -and (Test-Path $ClaudeCreds)) {
    $dest = '.claude_config\.credentials.json'
    $existing = if (Test-Path $dest) { Get-Item $dest -Force } else { $null }
    if ($existing -and $existing.LinkType -eq 'SymbolicLink') {
        Write-Info "Claude Code credentials link already present"
    } else {
        if ($existing) { Remove-Item -LiteralPath $dest -Force }
        New-Link -Path $dest -Target $ClaudeCreds
        Write-Info "Linked Claude Code credentials into .claude_config\"
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 10: Create default assistant_config.json
# ─────────────────────────────────────────────────────────────────────────────
if (-not (Test-Path 'assistant_config.json')) {
    Write-Step "Creating default assistant_config.json..."
    $DefaultModel = if ($DefaultProvider -in @('qwen','modelstudio')) { 'qwen3.6-plus' } else { 'claude-sonnet-4-5-20250929' }
    # JSON gets the project path embedded; on Windows that's a backslash path.
    # The wrapper code consumes it as a generic path string, but to keep the
    # JSON encoded form simple we escape backslashes via JSON-encoding.
    $tpl = Get-Content -LiteralPath (Join-Path $InstallTemplates 'assistant_config.json') -Raw
    $jsonScriptDir = ($ScriptDir | ConvertTo-Json).Trim('"')   # safe escaping
    $tpl = $tpl.Replace('@@SCRIPT_DIR@@',       $jsonScriptDir)
    $tpl = $tpl.Replace('@@DEFAULT_PROVIDER@@', $DefaultProvider)
    $tpl = $tpl.Replace('@@DEFAULT_MODEL@@',    $DefaultModel)
    Set-Content -LiteralPath 'assistant_config.json' -Value $tpl -NoNewline
    Write-Info "Created assistant_config.json (provider=$DefaultProvider)"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 11: Create default manager config
# ─────────────────────────────────────────────────────────────────────────────
if (-not (Test-Path '.manager.json')) {
    Write-Step "Creating default configuration..."
    Copy-Item -LiteralPath (Join-Path $InstallTemplates 'manager.json') -Destination '.manager.json'
    Write-Info "Created .manager.json"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 12: Verify installation
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Step "Verifying installation..."

$VerificationFailed = $false

# Core packages.
& $VenvPy -c "import fastapi, uvicorn, numpy, sentence_transformers" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Info "Core Python packages OK"
} else {
    Write-Host "[FAIL] Core Python package verification failed" -ForegroundColor Red
    $VerificationFailed = $true
}

function Test-OptionalSdk {
    param([string]$Sdk, [string]$Axis, [string]$ReqFile)
    & $VenvPy -c "import $Sdk" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "$Sdk SDK present"
    } else {
        Write-Warn "$Sdk SDK not importable despite $Axis being selected (try: pip install -r $ReqFile)"
    }
}
if ($ClaudeRuntime) { Test-OptionalSdk 'claude_agent_sdk' '-WithClaude / -WithModelStudio' 'backend\requirements-claude.txt' }
if ($AnthropicAxis) { Test-OptionalSdk 'anthropic'        '-WithAnthropic' 'backend\requirements-anthropic.txt' }
if ($OpenAIAxis)    { Test-OptionalSdk 'openai'           '-WithOpenAI'    'backend\requirements-openai.txt' }

if (Test-Path 'apps\web\package.json') {
    Write-Info "Frontend package.json OK"
} else {
    Write-Warn "Frontend package.json not found"
}

if ((Test-Path 'context\memory') -and (Test-Path 'context\skills')) {
    Write-Info "Context structure OK"
} else {
    Write-Warn "Context structure incomplete"
}

function Test-EnvKey {
    param([string]$Key, [string]$Feature)
    if (Test-EnvKeyPresent $Key) {
        Write-Info "$Key set in context\.env"
    } else {
        Write-Warn "$Key not set in context\.env ($Feature)"
    }
}
if (Test-Path 'context\.env') {
    if ($OpenAIAxis)    { Test-EnvKey 'OPENAI_API_KEY'    'OpenAI orchestrator text + Realtime voice' }
    if ($AnthropicAxis) { Test-EnvKey 'ANTHROPIC_API_KEY' 'Anthropic Claude models in orchestrator' }
    if ($QwenAxis -or $ModelStudioAxis) { Test-EnvKey 'DASHSCOPE_API_KEY' 'Qwen / Model Studio harnesses + Qwen voice' }
    if ($GeminiAxis)    { Test-EnvKey 'GEMINI_API_KEY'    'Gemini CLI harness - the only auth Google still serves it' }
} else {
    Write-Warn "No context\.env file found"
}

# Harness wiring (the checks install/doctor.sh runs on Linux / macOS): every
# link this installer manages, reported in one place.
function Test-LinkTarget {
    param([string]$Path, [string]$Target, [string]$Label)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { Write-Warn "$Label missing: $Path"; return }
    if ($item.LinkType -notin @('SymbolicLink','Junction')) {
        if ($item.PSIsContainer) { Write-Warn "$Label is a real directory: $Path" }
        else { Write-Info "$Label is a copy (no symlink rights): $Path" }
        return
    }
    $t  = $item.Target | Select-Object -First 1
    $r1 = if ($t) { (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path } else { $null }
    $r2 = (Resolve-Path -LiteralPath $Target -ErrorAction SilentlyContinue).Path
    if ($r1 -and $r2 -and $r1 -eq $r2) { Write-Info "$Label -> $Target" }
    else { Write-Warn "$Label points to $t (expected $Target)" }
}
$ctxDir = Join-Path $ScriptDir 'context'
foreach ($md in 'CLAUDE.md','QWEN.md','GEMINI.md','AGENTS.md') { Test-LinkTarget $md (Join-Path $ctxDir 'AGENTS.md') $md }
Test-LinkTarget 'context\memory\archie' (Join-Path $ScriptDir 'docs') 'Docs link (context\memory\archie)'
if ($ClaudeRuntime) {
    Test-LinkTarget ".claude_config\projects\$Mangled" $ctxDir 'Claude projects link'
    Test-LinkTarget '.claude_config\skills' (Join-Path $ctxDir 'skills') 'Claude skills link'
    Test-LinkTarget '.claude_config\agents' (Join-Path $ctxDir 'agents') 'Claude agents link'
}
if ($QwenAxis) {
    Test-LinkTarget (Join-Path $env:USERPROFILE ".qwen\projects\$QwenMangled") $ctxDir 'Qwen projects link'
    Test-LinkTarget (Join-Path $env:USERPROFILE '.qwen\skills') (Join-Path $ctxDir 'skills') 'Qwen skills link'
}
if ($GeminiAxis) {
    Test-LinkTarget $GeminiProjectDir $ctxDir 'Gemini project link'
    $gemChk = @(& $VenvPy 'install\gemini-project.py' $ScriptDir 'check' '--label' $GeminiLabel 2>$null | ForEach-Object { "$_" })
    if ($gemChk -contains 'registered=yes') { Write-Info "Gemini projects.json maps this repo to '$GeminiLabel'" }
    else { Write-Warn "Gemini projects.json does not map this repo to '$GeminiLabel' - Gemini may pick another label (re-run the installer)" }
    if ($gemChk -contains 'memory_index=ok') { Write-Info "Gemini memory index -> context\memory\MEMORY.md" }
    else { Write-Warn "Gemini memory index does not resolve to context\memory\MEMORY.md" }
    foreach ($l in $gemChk) {
        if ($l -like 'marker_*=foreign:*') { Write-Warn "Gemini ownership marker names another path ($l) - Gemini would claim a new label" }
    }
}
if ($CodexAxis)  {
    Test-LinkTarget (Join-Path $env:USERPROFILE '.codex-archie\sessions') (Join-Path $ctxDir 'codex\sessions') 'Codex sessions link'
    $codexChk = "$(& $VenvPy 'install\codex-home-config.py' (Join-Path $env:USERPROFILE '.codex-archie\config.toml') 'check' 2>$null)"
    if ($codexChk -eq 'ok') { Write-Info "Codex [features] memories = false" }
    else { Write-Warn "Codex config.toml: memories feature is '$codexChk' (want [features] memories = false)" }
}
if (Test-Path 'context\memory\MEMORY.md') { Write-Info "context\memory\MEMORY.md present (every harness's memory index)" }
else { Write-Warn "context\memory\MEMORY.md missing - no session gets a memory index" }
if ($CodexAxis -or $GeminiAxis -or $QwenAxis) { Test-LinkTarget '.agents\skills' (Join-Path $ctxDir 'skills') 'Repo skills link (.agents\skills)' }

# ─────────────────────────────────────────────────────────────────────────────
# Step 12b: Probe Gemini Live voice backends
# ─────────────────────────────────────────────────────────────────────────────
#
# Same logic as the Linux / macOS installers — see those for the
# rationale.  PowerShell doesn't have ``eval``, so we read the probe's
# JSON output directly instead of going through the shell-quoted
# wrapper.
$venvPython = Join-Path '.venv' 'Scripts\python.exe'
if (Test-Path $venvPython) {
    $hasWebsockets = $false
    try {
        & $venvPython -c "import websockets" 2>$null
        if ($LASTEXITCODE -eq 0) { $hasWebsockets = $true }
    } catch {}
    if ($hasWebsockets) {
        Write-Host ""
        Write-Info "Probing Gemini Live backends (AI Studio + Vertex AI)..."
        $probeRaw = & $venvPython 'install\probe-gemini-voice.py' 2>$null
        if ($probeRaw) {
            try { $probe = $probeRaw | Out-String | ConvertFrom-Json } catch { $probe = $null }
        }
        if ($probe) {
            if ($probe.vertex.status -eq 'ok') {
                Write-Info "Gemini Live (Vertex AI) - reachable [OK]"
            } else {
                Write-Warn "Gemini Live (Vertex AI) - $($probe.vertex.status): $($probe.vertex.reason)"
            }
            if ($probe.aistudio.status -eq 'ok') {
                Write-Info "Gemini Live (AI Studio) - reachable [OK]"
            } else {
                Write-Warn "Gemini Live (AI Studio) - $($probe.aistudio.status): $($probe.aistudio.reason)"
            }
            $recommended = $probe.recommended_default
            if ($recommended) {
                Write-Info "Recommended default voice endpoint: $recommended"
                if (Test-Path 'assistant_config.json') {
                    try {
                        $cfg = Get-Content 'assistant_config.json' -Raw | ConvertFrom-Json
                    } catch { $cfg = $null }
                    if ($cfg) {
                        $current = $null
                        if ($cfg.PSObject.Properties.Match('default_voice_endpoint').Count -gt 0) {
                            $current = $cfg.default_voice_endpoint
                        }
                        if (-not $current -or $current -eq 'vertex') {
                            if ($cfg.PSObject.Properties.Match('default_voice_endpoint').Count -gt 0) {
                                $cfg.default_voice_endpoint = $recommended
                            } else {
                                $cfg | Add-Member -NotePropertyName default_voice_endpoint -NotePropertyValue $recommended
                            }
                            $cfg | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath 'assistant_config.json' -Encoding UTF8
                        }
                    }
                }
            } else {
                Write-Warn "Neither Gemini Live backend is reachable - voice in the Google provider won't work until one is configured."
                Write-Host "       See https://aistudio.google.com/apikey (AI Studio) or" -ForegroundColor DarkGray
                Write-Host "       https://console.cloud.google.com/apis/library/aiplatform.googleapis.com (Vertex AI)." -ForegroundColor DarkGray
            }
        }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Completion
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
if (-not $VerificationFailed) {
    Write-Host "Installation complete!" -ForegroundColor Green
} else {
    Write-Host "Installation completed with warnings" -ForegroundColor Yellow
}
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

Write-Host "Next steps:" -ForegroundColor White
Write-Host ""

$step = 1

$envMissing = @()
if (Test-Path 'context\.env') {
    if ($OpenAIAxis    -and -not (Test-EnvKeyPresent 'OPENAI_API_KEY'))    { $envMissing += 'OPENAI_API_KEY' }
    if ($AnthropicAxis -and -not (Test-EnvKeyPresent 'ANTHROPIC_API_KEY')) { $envMissing += 'ANTHROPIC_API_KEY' }
    if (($QwenAxis -or $ModelStudioAxis) -and -not (Test-EnvKeyPresent 'DASHSCOPE_API_KEY')) { $envMissing += 'DASHSCOPE_API_KEY' }
    if ($GeminiAxis    -and -not (Test-EnvKeyPresent 'GEMINI_API_KEY'))    { $envMissing += 'GEMINI_API_KEY' }
} else {
    if ($OpenAIAxis)    { $envMissing += 'OPENAI_API_KEY' }
    if ($AnthropicAxis) { $envMissing += 'ANTHROPIC_API_KEY' }
    if ($QwenAxis -or $ModelStudioAxis) { $envMissing += 'DASHSCOPE_API_KEY' }
    if ($GeminiAxis)    { $envMissing += 'GEMINI_API_KEY' }
}
if ($envMissing.Count -gt 0) {
    Write-Host "  $step. " -NoNewline; Write-Host "Configure your API keys:" -ForegroundColor Red
    Write-Host "     Edit context\.env" -ForegroundColor Blue -NoNewline
    Write-Host "   ($($envMissing -join ', '))" -ForegroundColor Cyan
    Write-Host ""
    $step++
}

# On Windows the backend is launched via the venv's uvicorn (no run.sh equivalent).
Write-Host "  $step. " -NoNewline; Write-Host "Start the backend:" -ForegroundColor Green
Write-Host "     .venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --app-dir backend --port 8765" -ForegroundColor Blue
Write-Host ""

Write-Host "  $($step+1). " -NoNewline; Write-Host "Start the frontend (new terminal):" -ForegroundColor Green
Write-Host "     cd apps\web; npm run dev" -ForegroundColor Blue
Write-Host ""

Write-Host "  $($step+2). " -NoNewline; Write-Host "Open " -ForegroundColor Green -NoNewline
Write-Host "https://localhost:5450" -ForegroundColor Blue -NoNewline
Write-Host " in your browser" -ForegroundColor Green
Write-Host ""

Write-Host "Tip: " -ForegroundColor Cyan -NoNewline
Write-Host "Use /help in the assistant to see available commands."

$harnessCount = 0
if ($ClaudeAxis) { $harnessCount++ }
if ($QwenAxis)   { $harnessCount++ }
if ($GeminiAxis) { $harnessCount++ }
if ($CodexAxis)  { $harnessCount++ }
if ($ModelStudioAxis) { $harnessCount++ }
if ($harnessCount -gt 1) {
    Write-Host "Tip: " -ForegroundColor Cyan -NoNewline
    Write-Host "You can switch providers anytime in Configuration -> Session provider."
}
Write-Host ""
