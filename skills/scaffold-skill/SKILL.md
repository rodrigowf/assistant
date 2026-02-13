---
name: scaffold-skill
description: Create a new skill from a conversation or idea. Use this when you've learned how to do something and want to turn it into a reusable skill.
argument-hint: "[skill-name]"
allowed-tools: Write, Read, Bash(mkdir *), Bash(chmod *), AskUserQuestion
---

# Create a New Skill

You are helping the user turn an ability or workflow into a reusable skill.

Skill name: **$0**
Additional context: $ARGUMENTS

## Your Job

1. **Understand what the user wants to capture** — Look at the conversation context, ask clarifying questions if needed
2. **Design the skill** — Decide what instructions and scripts are needed
3. **Create the files**:
   - Skill definition in `skills/$0/SKILL.md`
   - Any required scripts in `scripts/` (the centralized scripts folder)

## Gathering Context

Ask the user:
- What should this skill do? (if not clear from conversation)
- Any specific inputs/arguments it should accept?
- Anything else relevant?

Use the conversation history — if we just did something the user wants to capture, extract the pattern from what we did.

## Project Structure

```
assistant/
├── skills/                 # Skill definitions (declarative)
│   └── $0/
│       └── SKILL.md
├── scripts/                # Shared script library (executable)
│   └── $0.sh (or .py)
└── agents/                 # Agent definitions
```

## Architecture Principle

**Skills are declarative. Scripts are executable.**

- `skills/` contains SKILL.md files that describe *what* to do and *when*
- `scripts/` contains the shared library of executables that do the *how*
- Skills reference scripts by path: `scripts/my-script.sh`

This separation keeps skills agent-agnostic — any AI that can run shell commands can use them.

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

Your instructions here...
```

## Referencing Scripts

When a skill needs executable logic, reference the centralized scripts folder:

```markdown
To perform the action, run:
scripts/my-script.sh $ARGUMENTS
```

Or for specific arguments:
```markdown
Run the deployment script:
scripts/deploy.sh $0 $1
```

## Script Guidelines

When creating scripts in `scripts/`:

1. **Naming**: Use the skill name or a descriptive name (e.g., `deploy.sh`, `backup-db.py`)
2. **Language**: Bash for simple tasks, Python for complex logic
3. **Arguments**: Accept inputs via command-line arguments
4. **Output**: Print results to stdout for the agent to read
5. **Exit codes**: Use 0 for success, non-zero for errors
6. **Documentation**: Include a usage comment at the top

Bash script header:
```bash
#!/usr/bin/env bash
# Usage: scripts/example.sh <arg1> <arg2>
# Description: What this script does
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
```

Python script header:
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

## Variable Substitution

Available in skill instructions:
- `$ARGUMENTS` — All arguments as string
- `$0`, `$1`, `$2` — Individual arguments by index
- `${CLAUDE_SESSION_ID}` — Current session ID
- Dynamic context syntax (exclamation + backticks) — Runs shell command, injects output before skill loads

## After Creation

Tell the user:
- Skill created at: `skills/$0/SKILL.md`
- Scripts created at: `scripts/` (if any)
- How to invoke: `/$0 [arguments]`
- They may need to restart for it to appear in autocomplete

## Important: Avoid Permission Triggers

When writing skill instructions, **do not use literal backtick command syntax** (like the dynamic context pattern). Claude Code's permission system will interpret it as an actual command attempt and block the skill from loading.

Instead of showing the actual syntax, describe it in words.

## Composing with Agents

Skills can leverage agents for isolated subtasks:

1. **Run skill in isolation** — Add `context: fork` and optionally `agent: AgentName` to run the skill in a subagent
2. **Orchestrate agents** — Skill can instruct to "use the X agent" for subtasks (requires Task tool)

Ask the user if the skill should run in isolation or orchestrate other agents.

## Reference Examples

See [templates/](templates/) for example patterns — use them as inspiration, not as strict templates.
