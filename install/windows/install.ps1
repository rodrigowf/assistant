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

      - Path mangling: the Claude / Qwen / Gemini CLIs each use slightly
        different schemes for mangling the project path into their per-project
        config dirs.  See the per-harness blocks below — the exact mangling
        is documented inline and may need a tweak the first time you actually
        run one of those harnesses.

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
    -WithOpenAI -WithoutAnthropic`.

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
# Tri-state axis resolution.  Each axis (claude / qwen / gemini / anthropic /
# openai) ends up as one of $true / $false.  Until the user has been asked
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
$AnthropicAxis = Resolve-Switch -With:$WithAnthropic -Without:$WithoutAnthropic
$OpenAIAxis    = Resolve-Switch -With:$WithOpenAI    -Without:$WithoutOpenAI

if ($QwenOnly) {
    # Only fills in blanks — explicit per-axis flags still win.
    if ($null -eq $ClaudeAxis)    { $ClaudeAxis    = $false }
    if ($null -eq $QwenAxis)      { $QwenAxis      = $true  }
    if ($null -eq $GeminiAxis)    { $GeminiAxis    = $false }
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
if ($null -eq $ClaudeAxis -and $null -eq $QwenAxis -and $null -eq $GeminiAxis) {
    Write-Host "── Session harness ──" -ForegroundColor Cyan
    Write-Host "Which agent CLI(s) should run your chats?  (You can pick more than one;"
    Write-Host "the UI's Session Provider selector switches between them at runtime.)"
    Write-Host ""
    $ClaudeAxis = Read-YesNo "Set up Claude Code (Anthropic - recommended default)?" 'Y'
    $QwenAxis   = Read-YesNo "Set up Qwen Code (Alibaba - open weights, OAuth or DashScope key)?" 'N'
    $GeminiAxis = Read-YesNo "Set up Gemini CLI (Google - OAuth or GEMINI_API_KEY)?" 'N'
    Write-Host ""
}
if ($null -eq $ClaudeAxis) { $ClaudeAxis = $false }
if ($null -eq $QwenAxis)   { $QwenAxis   = $false }
if ($null -eq $GeminiAxis) { $GeminiAxis = $false }

if (-not $ClaudeAxis -and -not $QwenAxis -and -not $GeminiAxis) {
    Write-Err "Refusing to install with no harnesses - pick at least one (-WithClaude / -WithQwen / -WithGemini)."
}

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
if ($AnthropicAxis) { Write-Info "Will install anthropic SDK (orchestrator)" }
if ($OpenAIAxis)    { Write-Info "Will install openai SDK (orchestrator + voice)" }
if (-not $AnthropicAxis -and -not $OpenAIAxis) {
    Write-Warn "No orchestrator backend selected - the orchestrator tab will be disabled."
}
Write-Host ""

# Default provider written into assistant_config.json.  Claude wins if both
# are installed (historical default); otherwise Qwen.
$DefaultProvider = if ($ClaudeAxis) { 'claude' } else { 'qwen' }

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

            # Skill / script / agent symlinks (or junctions / copies as fallback).
            # All three loops are identical except for the source dir.
            $bundles = @(
                @{ Public = 'default-skills';  Private = 'context\skills';  Kind = 'directory'; Label = 'skill'  },
                @{ Public = 'default-scripts'; Private = 'context\scripts'; Kind = 'any';       Label = 'script' },
                @{ Public = 'default-agents';  Private = 'context\agents';  Kind = 'any';       Label = 'agent'  }
            )
            foreach ($b in $bundles) {
                Write-Step "Creating $($b.Label) symlinks..."
                foreach ($item in Get-ChildItem -LiteralPath (Join-Path $ScriptDir $b.Public) -Force) {
                    if ($b.Kind -eq 'directory' -and -not $item.PSIsContainer) { continue }
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
            if ($QwenAxis)      { Enable-EnvKey 'DASHSCOPE_API_KEY' }

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
                @{ Public = 'default-skills';  Private = 'context\skills';  Kind = 'directory' },
                @{ Public = 'default-scripts'; Private = 'context\scripts'; Kind = 'any'       },
                @{ Public = 'default-agents';  Private = 'context\agents';  Kind = 'any'       }
            )
            Write-Step "Ensuring default symlinks..."
            foreach ($b in $bundles) {
                foreach ($item in Get-ChildItem -LiteralPath (Join-Path $ScriptDir $b.Public) -Force) {
                    if ($b.Kind -eq 'directory' -and -not $item.PSIsContainer) { continue }
                    $dest = Join-Path $b.Private $item.Name
                    if (Test-Path $dest) { continue }
                    New-Link -Path $dest -Target $item.FullName
                }
            }
        }
    }
}
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Path mangling — shared by Claude / Qwen / Gemini SDK config dirs.
#
# The bundled CLIs each compute a "mangled" version of the project path to
# use as the per-project subdirectory name.  On POSIX the bash installer
# replaces `/` with `-`.  On Windows the CLIs do the same with both `\` and
# `/` and replace `:` with `-` as well (so `C:\Users\you\assistant` becomes
# `C--Users-you-assistant`).
#
# This mangling is what we observe the CLIs doing in practice on Windows;
# if a CLI version uses a different mangle, re-run install.ps1 after the
# first chat to point the link at the dir the CLI actually created.
# ─────────────────────────────────────────────────────────────────────────────
$Mangled = $ScriptDir -replace '[\\/]', '-' -replace ':', '-'

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Set up Claude SDK config link (only if Claude harness enabled)
# ─────────────────────────────────────────────────────────────────────────────
if ($ClaudeAxis) {
    Write-Step "Setting up Claude SDK configuration..."

    New-Item -ItemType Directory -Path '.claude_config\projects' -Force | Out-Null

    $LinkPath = ".claude_config\projects\$Mangled"
    $existing = if (Test-Path $LinkPath) { Get-Item $LinkPath -Force } else { $null }
    if ($existing -and $existing.LinkType -in @('SymbolicLink','Junction')) {
        Write-Info "SDK link already exists"
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

    if (-not (Test-Path '.claude_config\skills')) {
        New-Link -Path '.claude_config\skills' -Target (Join-Path $ScriptDir 'context\skills')
        Write-Info "Created skills discovery link"
    }
    Write-Host ""
} else {
    Write-Info "Skipping Claude SDK setup (-WithoutClaude)"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3b: Set up Qwen Code config link (only if Qwen harness enabled)
# ─────────────────────────────────────────────────────────────────────────────
if ($QwenAxis) {
    Write-Step "Setting up Qwen Code configuration..."

    $QwenHome       = Join-Path $env:USERPROFILE '.qwen'
    $QwenProjectDir = Join-Path $QwenHome ("projects\$Mangled")
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
            Get-ChildItem -LiteralPath $chatsDir -Filter '*.runtime.json' -ErrorAction SilentlyContinue | ForEach-Object {
                $dest = Join-Path 'context\chats' $_.Name
                if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest }
            }
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

    $qwenSkills = Join-Path $QwenHome 'skills'
    if (-not (Test-Path $qwenSkills)) {
        New-Link -Path $qwenSkills -Target (Join-Path $ScriptDir 'context\skills')
        Write-Info "Created Qwen skills discovery link"
    } elseif ((Get-Item $qwenSkills -Force).LinkType -notin @('SymbolicLink','Junction')) {
        Write-Warn "$qwenSkills exists and is not a link - leaving alone"
    }
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
# Step 3d: Wire AGENTS.md as the shared project-instructions file
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Wiring context\AGENTS.md as the shared project-instructions file..."

# Migration: normalize legacy layouts to context\AGENTS.md.
if (-not (Test-Path 'context\AGENTS.md')) {
    if ((Test-Path 'AGENTS.md') -and ((Get-Item 'AGENTS.md').LinkType -notin @('SymbolicLink','Junction'))) {
        Move-Item -LiteralPath 'AGENTS.md' -Destination 'context\AGENTS.md'
        Write-Info "Moved AGENTS.md -> context\AGENTS.md"
    } elseif ((Test-Path 'CLAUDE.md') -and ((Get-Item 'CLAUDE.md').LinkType -notin @('SymbolicLink','Junction'))) {
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
    if ($item.LinkType -in @('SymbolicLink','Junction') -or $item.Length -eq 0) {
        Remove-Item -LiteralPath 'AGENTS.md' -Force
    }
}

if (Test-Path 'context\AGENTS.md') {
    foreach ($shadow in 'CLAUDE.md','QWEN.md') {
        if (Test-Path $shadow) {
            $item = Get-Item $shadow -Force
            if ($item.LinkType -in @('SymbolicLink','Junction')) {
                $t = $item.Target | Select-Object -First 1
                $r1 = (Resolve-Path -LiteralPath $t -ErrorAction SilentlyContinue).Path
                $r2 = (Resolve-Path -LiteralPath 'context\AGENTS.md' -ErrorAction SilentlyContinue).Path
                if ($r1 -and $r2 -and $r1 -eq $r2) { continue }
                Remove-Item -LiteralPath $shadow -Force
            } else {
                Write-Warn "$shadow exists and is not a link - leaving alone (delete to enable shared instructions)"
                continue
            }
        }
        New-Link -Path $shadow -Target (Join-Path $ScriptDir 'context\AGENTS.md')
        Write-Info "Created $shadow -> context\AGENTS.md link"
    }
} else {
    Write-Warn "No context\AGENTS.md found - skipping CLAUDE.md/QWEN.md links"
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

# Find a python launcher that resolves to 3.12+.  `py -3.12` is the canonical
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
                if ([int]$parts2[0] -ge 3 -and [int]$parts2[1] -ge 12) {
                    return @{ Exe = $exe; Args = $args }
                }
            }
        } catch { }
    }
    return $null
}

$Py = Get-PythonExe
if (-not $Py) {
    Write-Err "Python 3.12+ not found on PATH.  Install it via:  winget install Python.Python.3.12  (then re-open PowerShell)"
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
    & $VenvPip install -r requirements-dev.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-dev.txt failed" }
    Write-Info "Installed requirements-dev.txt (core + dev tools)"
} else {
    & $VenvPip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements.txt failed" }
    Write-Info "Installed requirements.txt (core)"
}

if ($ClaudeAxis) {
    & $VenvPip install -r requirements-claude.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-claude.txt failed" }
    Write-Info "Installed requirements-claude.txt (claude-agent-sdk)"
}
if ($AnthropicAxis) {
    & $VenvPip install -r requirements-anthropic.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-anthropic.txt failed" }
    Write-Info "Installed requirements-anthropic.txt (anthropic SDK)"
}
if ($OpenAIAxis) {
    & $VenvPip install -r requirements-openai.txt --quiet
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install requirements-openai.txt failed" }
    Write-Info "Installed requirements-openai.txt (openai SDK)"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: Install frontend dependencies
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Installing frontend dependencies..."
Push-Location 'frontend'
& npm install --silent
$npmStatus = $LASTEXITCODE
Pop-Location
if ($npmStatus -ne 0) { Write-Err "npm install (frontend) failed" }
Write-Info "Installed/updated frontend node_modules\"

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
    Write-Host "    (Or set $EnvKey in context\.env to use an API key instead.)"
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
    if ($QwenAxis) {
        [void](Install-HarnessCli 'qwen' '@qwen-code/qwen-code')
        if (Get-Command qwen -ErrorAction SilentlyContinue) {
            Read-DriverLogin 'qwen' 'qwen' {
                Test-Path (Join-Path $env:USERPROFILE '.qwen\oauth_creds.json')
            } 'DASHSCOPE_API_KEY'
        }
    }
    if ($GeminiAxis) {
        [void](Install-HarnessCli 'gemini' '@google/gemini-cli')
        if (Get-Command gemini -ErrorAction SilentlyContinue) {
            Read-DriverLogin 'gemini' 'gemini' {
                Test-Path (Join-Path $env:USERPROFILE '.gemini\oauth_creds.json')
            } 'GEMINI_API_KEY'
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
    $DefaultModel = if ($DefaultProvider -eq 'qwen') { 'qwen3.6-plus' } else { 'claude-sonnet-4-5-20250929' }
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
& $VenvPy -c "import fastapi, uvicorn, chromadb, sentence_transformers" 2>$null
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
if ($ClaudeAxis)    { Test-OptionalSdk 'claude_agent_sdk' '-WithClaude'    'requirements-claude.txt' }
if ($AnthropicAxis) { Test-OptionalSdk 'anthropic'        '-WithAnthropic' 'requirements-anthropic.txt' }
if ($OpenAIAxis)    { Test-OptionalSdk 'openai'           '-WithOpenAI'    'requirements-openai.txt' }

if (Test-Path 'frontend\package.json') {
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
    if ($QwenAxis)      { Test-EnvKey 'DASHSCOPE_API_KEY' 'Qwen harness + Qwen voice' }
} else {
    Write-Warn "No context\.env file found"
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
    if ($QwenAxis      -and -not (Test-EnvKeyPresent 'DASHSCOPE_API_KEY')) { $envMissing += 'DASHSCOPE_API_KEY' }
} else {
    if ($OpenAIAxis)    { $envMissing += 'OPENAI_API_KEY' }
    if ($AnthropicAxis) { $envMissing += 'ANTHROPIC_API_KEY' }
    if ($QwenAxis)      { $envMissing += 'DASHSCOPE_API_KEY' }
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
Write-Host "     .venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --port 8765" -ForegroundColor Blue
Write-Host ""

Write-Host "  $($step+1). " -NoNewline; Write-Host "Start the frontend (new terminal):" -ForegroundColor Green
Write-Host "     cd frontend; npm run dev" -ForegroundColor Blue
Write-Host ""

Write-Host "  $($step+2). " -NoNewline; Write-Host "Open " -ForegroundColor Green -NoNewline
Write-Host "https://localhost:5432" -ForegroundColor Blue -NoNewline
Write-Host " in your browser" -ForegroundColor Green
Write-Host ""

Write-Host "Tip: " -ForegroundColor Cyan -NoNewline
Write-Host "Use /help in the assistant to see available commands."

$harnessCount = 0
if ($ClaudeAxis) { $harnessCount++ }
if ($QwenAxis)   { $harnessCount++ }
if ($GeminiAxis) { $harnessCount++ }
if ($harnessCount -gt 1) {
    Write-Host "Tip: " -ForegroundColor Cyan -NoNewline
    Write-Host "You can switch providers anytime in Configuration -> Session provider."
}
Write-Host ""
