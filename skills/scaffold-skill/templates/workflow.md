# Workflow Skill Template

Example of a multi-step process that uses tools and scripts.

```yaml
---
name: {{SKILL_NAME}}
description: {{DESCRIPTION}}
argument-hint: "{{ARGUMENT_HINT}}"
allowed-tools: {{TOOLS}}
---

# {{TITLE}}

Target: $ARGUMENTS

## Step 1: {{STEP_1_NAME}}

{{STEP_1_INSTRUCTIONS}}

## Step 2: {{STEP_2_NAME}}

{{STEP_2_INSTRUCTIONS}}

## Step 3: {{STEP_3_NAME}}

{{STEP_3_INSTRUCTIONS}}

## Completion

When finished:
- {{COMPLETION_CHECKLIST}}
```

## Example: Deploy Workflow

This example shows a skill that references a centralized script.

```yaml
---
name: deploy
description: Deploy the application to a target environment
argument-hint: "[environment]"
allowed-tools: Read, Bash(scripts/*)
---

# Deploy to: $0

## Pre-flight Checks

1. Verify git status is clean
2. Ensure all tests pass
3. Confirm target environment: $0

## Deploy

Run the deployment script:
scripts/deploy.sh $0

## Post-deploy

1. Verify deployment succeeded
2. Run smoke tests
3. Report results
```

The actual deployment logic lives in `scripts/deploy.sh`, keeping the skill declarative.
