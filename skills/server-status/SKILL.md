---
name: server-status
description: Check running applications and services on the remote server (192.168.0.200)
argument-hint: "[processes|services|all|<app-name>]"
disable-model-invocation: false
user-invocable: true
---

# Server Status Checker

Query the remote server at 192.168.0.200 to check running applications and services.

> **Related Documentation**: See `.claude_config/projects/-home-rodrigo-Projects-assistant/memory/server_hub_project.md` for complete server architecture, application details, and deployment plans.

## Purpose

This skill connects via SSH to the home server and retrieves information about:
- Running processes (agentic backend, copyparty, jellyfin, nginx)
- Systemd service status
- Memory and CPU usage
- Auto-start configuration

## Related Documentation

See `memory/server_hub_project.md` for complete server architecture and application details.

## Usage

Run the server status script with an optional query type:

```
scripts/server_status.py [query-type]
```

Query types:
- `processes` - List key running processes with resource usage
- `services` - List systemd services and their status
- `all` - Show both processes and services (default)
- `<app-name>` - Query specific application (e.g., "jellyfin", "agentic", "copyparty", "nginx")

## Examples

Check all running applications:
```
scripts/server_status.py all
```

Check only systemd services:
```
scripts/server_status.py services
```

Query specific application:
```
scripts/server_status.py jellyfin
```

## Authentication

The script uses environment variables from `.env`:
- `SERVER_USERNAME` - SSH username
- `SERVER_PASSWORD` - SSH password
- Server IP is hardcoded as 192.168.0.200

## Output Format

The script returns formatted information about:
1. Key application processes (agentic, jellyfin, copyparty, nginx)
2. Systemd service status and auto-start configuration
3. Resource usage (CPU%, memory)
4. Process IDs and uptime information
