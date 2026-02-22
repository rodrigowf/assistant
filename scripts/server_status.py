#!/usr/bin/env python3
"""
Usage: scripts/server_status.py [processes|services|all|<app-name>]
Description: Query remote server (192.168.0.200) for running applications and services

Connects via SSH using credentials from environment variables:
- SERVER_USERNAME
- SERVER_PASSWORD
"""

import sys
import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
SERVER_IP = "192.168.0.200"

# Key applications to monitor
KEY_APPS = {
    "agentic": ["uvicorn", "agentic"],
    "jellyfin": ["jellyfin"],
    "copyparty": ["copyparty"],
    "nginx": ["nginx"],
}


def run_ssh_command(command):
    """Execute SSH command on remote server."""
    username = os.environ.get("SERVER_USERNAME")
    password = os.environ.get("SERVER_PASSWORD")

    if not username or not password:
        print("ERROR: SERVER_USERNAME and SERVER_PASSWORD must be set in environment")
        sys.exit(1)

    ssh_cmd = [
        "sshpass", "-p", password,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{username}@{SERVER_IP}",
        command
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "SSH command timed out", 1
    except Exception as e:
        return "", f"SSH error: {str(e)}", 1


def query_processes():
    """Query running processes for key applications."""
    print("\n=== Running Processes ===\n")

    # Get process list for key applications
    patterns = "|".join([f"({pattern})" for patterns in KEY_APPS.values() for pattern in patterns])
    cmd = f"ps aux | grep -E '({patterns})' | grep -v grep"

    stdout, stderr, code = run_ssh_command(cmd)

    if code != 0 and not stdout:
        print("No key application processes found running.")
        return

    # Parse and format output
    lines = stdout.strip().split('\n')
    if lines and lines[0]:
        # Print header
        print(f"{'APP':<12} {'USER':<10} {'PID':<8} {'CPU%':<6} {'MEM%':<6} {'COMMAND':<50}")
        print("-" * 98)

        for line in lines:
            if not line.strip():
                continue
            parts = line.split(None, 10)
            if len(parts) >= 11:
                user, pid, cpu, mem = parts[0], parts[1], parts[2], parts[3]
                command = parts[10]

                # Identify application
                app = "unknown"
                for app_name, patterns in KEY_APPS.items():
                    if any(pattern in command for pattern in patterns):
                        app = app_name
                        break

                # Truncate command if too long
                if len(command) > 50:
                    command = command[:47] + "..."

                print(f"{app:<12} {user:<10} {pid:<8} {cpu:<6} {mem:<6} {command:<50}")


def query_services():
    """Query systemd service status for key applications."""
    print("\n=== Systemd Services ===\n")

    services = [
        "agentic-backend.service",
        "jellyfin.service",
        "nginx-server.service",
        "mongodb.service",
        "postgresql@10-main.service",
        "tailscaled.service"
    ]

    for service in services:
        cmd = f"systemctl is-active {service} 2>/dev/null && systemctl is-enabled {service} 2>/dev/null"
        stdout, stderr, code = run_ssh_command(cmd)

        lines = stdout.strip().split('\n')
        active = lines[0] if len(lines) > 0 else "unknown"
        enabled = lines[1] if len(lines) > 1 else "unknown"

        # Color coding for status
        status_symbol = "✓" if active == "active" else "✗"
        autostart = "auto-start" if enabled == "enabled" else "manual"

        print(f"{status_symbol} {service:<35} [{active:<8}] [{autostart}]")

    # Check for copyparty in crontab
    print("\nAdditional startup methods:")
    cmd = "crontab -l | grep copyparty"
    stdout, stderr, code = run_ssh_command(cmd)
    if code == 0 and stdout.strip():
        print("✓ copyparty                            [crontab ] [auto-start]")


def query_specific_app(app_name):
    """Query details for a specific application."""
    app_lower = app_name.lower()

    print(f"\n=== {app_name.upper()} Status ===\n")

    # Map app names to service names
    service_map = {
        "agentic": "agentic-backend.service",
        "jellyfin": "jellyfin.service",
        "nginx": "nginx-server.service",
        "copyparty": None,  # Uses crontab
        "mongodb": "mongodb.service",
        "postgres": "postgresql@10-main.service",
        "postgresql": "postgresql@10-main.service",
        "tailscale": "tailscaled.service",
    }

    service = service_map.get(app_lower)

    if service:
        # Get detailed service status
        cmd = f"systemctl status {service}"
        stdout, stderr, code = run_ssh_command(cmd)
        print(stdout)
    elif app_lower == "copyparty":
        # Get copyparty process info
        cmd = "ps aux | grep copyparty | grep -v grep"
        stdout, stderr, code = run_ssh_command(cmd)
        if stdout:
            print("Process info:")
            print(stdout)

        cmd = "crontab -l | grep copyparty"
        stdout2, stderr2, code2 = run_ssh_command(cmd)
        if stdout2:
            print("\nStartup configuration:")
            print(stdout2)
    else:
        # Try to find by process name
        cmd = f"ps aux | grep -i {app_name} | grep -v grep"
        stdout, stderr, code = run_ssh_command(cmd)
        if stdout:
            print(stdout)
        else:
            print(f"No process or service found matching '{app_name}'")


def main():
    query_type = sys.argv[1] if len(sys.argv) > 1 else "all"

    # Check if sshpass is available
    try:
        subprocess.run(["which", "sshpass"], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        print("ERROR: sshpass is not installed. Install with: sudo apt-get install sshpass")
        sys.exit(1)

    print(f"Querying server at {SERVER_IP}...")

    if query_type == "processes":
        query_processes()
    elif query_type == "services":
        query_services()
    elif query_type == "all":
        query_processes()
        query_services()
    else:
        # Assume it's a specific app name
        query_specific_app(query_type)

    print()  # Final newline


if __name__ == "__main__":
    main()
