# Personal Assistant Architecture

This document captures the design decisions and architecture for an agent-agnostic personal assistant framework.

## Vision

Build a **skill-centered, multi-layered architecture** that is:
- **Agent-agnostic** — Works with Claude, Codex, Copilot, or any future AI assistant
- **Reusable** — Skills, scripts, and agents live at project root, not hidden in vendor-specific folders
- **Self-bootstrapping** — Meta-skills that create other skills and agents
- **Evolvable** — Can grow to automate routines and build nested systems

## Project Structure

```
assistant/
├── skills/                 # Skill definitions (declarative)
│   ├── scaffold-skill/     # Meta-skill for creating skills
│   │   ├── SKILL.md
│   │   └── templates/
│   └── scaffold-agent/     # Meta-skill for creating agents
│       └── SKILL.md
├── agents/                 # Agent definitions
├── scripts/                # Centralized executable library
├── docs/                   # Documentation
└── .claude/                # Claude Code wiring (symlinks)
    ├── skills -> ../skills
    ├── agents -> ../agents
    └── settings.local.json
```

## Core Principles

### 1. Skills are Declarative, Scripts are Executable

- `skills/` contains SKILL.md files that describe *what* to do and *when*
- `scripts/` contains the shared library of executables that do the *how*
- Skills reference scripts by path: `scripts/my-script.sh`
- This separation keeps skills agent-agnostic — any AI that can run shell commands can use them

### 2. Centralized Scripts Folder

All executable logic lives in `scripts/`, not inside individual skills. This enables:
- Reusability across multiple skills
- Independent testing
- Clear separation of concerns
- Language flexibility (bash, Python, etc.)

### 3. Symlinks for Vendor Integration

Claude Code expects skills in `.claude/skills/` and agents in `.claude/agents/`. We use symlinks to point these to our root-level folders:

```bash
ln -s ../skills .claude/skills
ln -s ../agents .claude/agents
```

This keeps our structure agent-agnostic while satisfying Claude Code's discovery mechanism.

## Skills vs Agents

| Aspect | Skills | Agents |
|--------|--------|--------|
| **What they are** | Instruction sets (prompts) | Isolated AI instances |
| **Context** | Run in **main conversation** | Run in **isolated context** |
| **Invocation** | `/skill-name` command | Automatic delegation or explicit request |
| **Can spawn subagents** | Yes (via Task tool) | No (cannot nest) |
| **Best for** | Reusable workflows, prompts | Specialized isolated tasks |

### Composition

**Skills can leverage agents:**
1. `context: fork` — Run skill in isolated subagent
2. Orchestrate agents — Skill instructs to "use the X agent" for subtasks

**Agents can leverage skills:**
- `skills:` field in agent frontmatter preloads skill content at startup

## SKILL.md Format

```yaml
---
name: skill-name                    # Lowercase, hyphens only
description: What this skill does   # Important: used for auto-invocation
argument-hint: "[arg1] [arg2]"      # Optional: shown in autocomplete
disable-model-invocation: false     # Set true for user-only triggers
user-invocable: true                # Set false to hide from /menu
allowed-tools: Read, Write, Bash(*) # Optional: tools without prompts
context: fork                       # Optional: run in isolated subagent
agent: Explore                      # Optional: subagent type when forked
---

Instructions here...
Reference scripts like: scripts/my-script.sh $ARGUMENTS
```

### Variable Substitution

- `$ARGUMENTS` — All arguments as string
- `$0`, `$1`, `$2` — Individual arguments by index
- `${CLAUDE_SESSION_ID}` — Current session ID
- Dynamic context syntax (exclamation + backticks) — Runs shell command, injects output before skill loads

### Important: Avoid Permission Triggers

When writing skill instructions, **do not use literal backtick command syntax**. Claude Code's permission system will interpret it as an actual command attempt and block the skill from loading. Instead, describe the syntax in words.

## Agent Definition Format

```yaml
---
name: agent-name                    # Lowercase, hyphens only
description: When to use this agent # Guides auto-delegation
tools: Read, Grep, Glob, Bash       # Tools the agent can use
model: inherit                      # sonnet, opus, haiku, or inherit
permissionMode: default             # default, acceptEdits, dontAsk, plan
skills:                             # Optional: preload skills
  - skill-name
---

System prompt / instructions here...
```

### Common Tool Sets

**Read-only (research/review):**
```yaml
tools: Read, Grep, Glob, Bash
disallowedTools: Write, Edit
```

**Full access (implementation):**
```yaml
tools: Read, Write, Edit, Grep, Glob, Bash
```

## Script Guidelines

When creating scripts in `scripts/`:

1. **Naming**: Use descriptive names (e.g., `deploy.sh`, `backup-db.py`)
2. **Language**: Bash for simple tasks, Python for complex logic
3. **Arguments**: Accept inputs via command-line arguments
4. **Output**: Print results to stdout for the agent to read
5. **Exit codes**: Use 0 for success, non-zero for errors
6. **Documentation**: Include a usage comment at the top

**Bash script header:**
```bash
#!/usr/bin/env bash
# Usage: scripts/example.sh <arg1> <arg2>
# Description: What this script does
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
```

**Python script header:**
```python
#!/usr/bin/env python3
"""
Usage: scripts/example.py <arg1> <arg2>
Description: What this script does
"""
import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).parent.resolve()
```

## Meta-Skills

### /scaffold-skill

Creates new skills from conversations or ideas. When invoked:
1. Gathers context from conversation and user
2. Designs skill structure
3. Creates `skills/<name>/SKILL.md`
4. Creates scripts in `scripts/` if needed

### /scaffold-agent

Creates new agent definitions. When invoked:
1. Gathers context about the specialized task
2. Designs agent configuration (tools, model, permissions)
3. Creates `agents/<name>.md`
4. Optionally preloads skills

## Next Steps

- [ ] **Memory mechanism** — Persistent context across conversations
- [ ] **UI layer** — Easy access, multi-threading, visual feedback

## Lessons Learned

1. **Permission triggers**: Backtick command syntax in SKILL.md files triggers Claude Code's permission system. Describe syntax in words instead of using literals.

2. **Symlinks work**: Claude Code successfully discovers skills and agents through symlinks, enabling our root-level structure.

3. **Keep meta-skills concise**: Large prompts in meta-skills make them harder to maintain. Keep instructions actionable and brief.
