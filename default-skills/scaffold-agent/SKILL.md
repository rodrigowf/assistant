---
name: scaffold-agent
description: Create a new agent definition. Use this when you want to create a specialized subagent for specific tasks like code review, debugging, research, etc.
argument-hint: "[agent-name]"
allowed-tools: Write, Read, Bash(mkdir *), AskUserQuestion
---

# Create a New Agent

You are helping the user create a new specialized agent (subagent).

Agent name: **$0**
Additional context: $ARGUMENTS

## Your Job

1. **Understand what the user wants** — What specialized task should this agent handle?
2. **Design the agent** — Decide on tools, model, permissions, and system prompt
3. **Create the agent file** — Write to `context/agents/$0.md`

## Gathering Context

Ask the user if not clear from conversation:
- What should this agent specialize in?
- Should it be read-only (research) or able to make changes?
- Any specific constraints or workflows?

## Agent Location

Create at: `context/agents/$0.md`

Note: General-purpose agents live in `default-agents/` and are symlinked from `context/agents/`. Personalized agents go directly in `context/agents/`.

## Agent File Format

```yaml
---
name: agent-name                    # Lowercase, hyphens only
description: When to use this agent # Important: guides auto-delegation
tools: Read, Grep, Glob, Bash       # Tools the agent can use
model: inherit                      # sonnet, opus, haiku, or inherit
permissionMode: default             # default, acceptEdits, dontAsk, plan
skills:                             # Optional: preload skills
  - skill-name
---

System prompt / instructions here...
```

## Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (lowercase + hyphens) |
| `description` | Yes | When Claude should delegate to this agent |
| `tools` | No | Allowed tools (comma-separated). Inherits all if omitted |
| `disallowedTools` | No | Tools to explicitly deny |
| `model` | No | `sonnet`, `opus`, `haiku`, or `inherit` (default) |
| `permissionMode` | No | `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan` |
| `skills` | No | Skills to preload into agent context |
| `hooks` | No | Lifecycle hooks scoped to this agent |

## Common Tool Sets

**Read-only (research/review):**
```yaml
tools: Read, Grep, Glob, Bash
disallowedTools: Write, Edit
```

**Full access (implementation):**
```yaml
tools: Read, Write, Edit, Grep, Glob, Bash
```

**Web research:**
```yaml
tools: Read, WebSearch, WebFetch, Grep, Glob
```

## System Prompt Best Practices

The markdown body after frontmatter is the agent's system prompt:

1. **Define the role clearly** — "You are a senior code reviewer..."
2. **List specific workflows** — Step-by-step instructions
3. **Set constraints** — What the agent should NOT do
4. **Define output format** — How to present results

## Key Differences from Skills

| Aspect | Agents | Skills |
|--------|--------|--------|
| Context | Isolated (own conversation) | Main conversation |
| Invocation | Automatic delegation or explicit request | `/skill-name` command |
| Can spawn others | No (cannot nest agents) | Can use Task tool |
| Best for | Specialized isolated tasks | Reusable workflows |

## Composing with Skills

Agents can leverage skills via the `skills:` field:

```yaml
skills:
  - api-conventions
  - error-handling
```

This preloads skill content into the agent's context at startup. Ask the user if the agent should have any skills preloaded.

## After Creation

Tell the user:
- Where the agent was created
- How it gets invoked (automatic delegation based on description, or explicit "use the X agent")
- That agents run in isolated context and return summarized results
- Which skills are preloaded (if any)
