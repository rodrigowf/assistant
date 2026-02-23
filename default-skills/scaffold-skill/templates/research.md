# Research Skill Template

Example of a skill that gathers and analyzes information in an isolated context.

```yaml
---
name: {{SKILL_NAME}}
description: {{DESCRIPTION}}
argument-hint: "{{ARGUMENT_HINT}}"
context: fork
agent: Explore
allowed-tools: Read, Grep, Glob, WebSearch, WebFetch
---

# Research: $ARGUMENTS

## Objectives

{{WHAT_TO_FIND}}

## Search Strategy

{{HOW_TO_SEARCH}}

## Output

{{WHAT_TO_REPORT}}
```

## Example: Dependency Analyzer

```yaml
---
name: analyze-deps
description: Analyze project dependencies for security and updates
context: fork
agent: Explore
allowed-tools: Read, Grep, Glob, Bash(npm outdated), Bash(npm audit)
---

# Dependency Analysis

## Gather Data

1. Read package.json
2. Run `npm outdated` for version info
3. Run `npm audit` for vulnerabilities

## Analyze

For each dependency:
- Current vs latest version
- Security advisories
- Usage in codebase

## Report

Provide:
- Summary stats (total, outdated, vulnerable)
- Critical updates needed
- Safe updates available
- Potentially unused packages
```

## Example: Architecture Explorer

```yaml
---
name: explore-arch
description: Understand and document architecture of a codebase area
argument-hint: "[directory or feature]"
context: fork
agent: Explore
allowed-tools: Read, Grep, Glob
---

# Explore: $0

## Discovery

1. Find entry points and exports
2. Map internal dependencies
3. Identify patterns used

## Document

- What this area does
- Key components and responsibilities
- How it connects to other parts
- Extension points
```

Research skills typically don't need scripts — they use built-in tools to explore and report.
