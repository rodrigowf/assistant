# Generator Skill Template

Example of a skill that creates files, optionally using a generator script.

```yaml
---
name: {{SKILL_NAME}}
description: {{DESCRIPTION}}
argument-hint: "{{ARGUMENT_HINT}}"
allowed-tools: Write, Read, Bash(mkdir *), Bash(context/scripts/*)
---

# Generate: $0

## Output Structure

{{DESCRIBE_WHAT_WILL_BE_CREATED}}

## Generation

{{INSTRUCTIONS_OR_SCRIPT_REFERENCE}}

## Verify

{{VERIFICATION_STEPS}}
```

## Example: Simple Generator (No Script)

For simple file generation, the skill can instruct directly:

```yaml
---
name: new-module
description: Create a new Python module with tests
argument-hint: "[module-name]"
allowed-tools: Write, Bash(mkdir *)
---

# Create Module: $0

Create the following structure:

src/$0/
├── __init__.py
├── main.py
└── test_$0.py

Write each file with appropriate boilerplate.
```

## Example: Complex Generator (With Script)

For complex generation, use a centralized script:

```yaml
---
name: new-service
description: Scaffold a new microservice with all boilerplate
argument-hint: "[service-name]"
allowed-tools: Bash(context/scripts/*)
---

# Create Service: $0

Run the service generator:
context/scripts/generate-service.sh $0

This will create:
- Service directory structure
- Dockerfile
- CI/CD configuration
- Base implementation files
- Test scaffolding
```

The script `context/scripts/generate-service.sh` contains the actual generation logic.
