#!/usr/bin/env bash
# install.sh — Install context-sync as a systemd user service.
#
# Run this on EACH machine that should participate in the sync.
# Before running: edit config.env with the correct paths and SSH key.
#
# Usage: bash sync/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="context-sync"

echo "=== Installing context-sync service ==="

# Check dependencies
for cmd in inotifywait rsync ssh; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' not found. Install it first:"
    echo "  sudo apt install inotify-tools rsync openssh-client"
    exit 1
  fi
done

# Check config exists
if [[ ! -f "${SCRIPT_DIR}/config.env" ]]; then
  echo "ERROR: ${SCRIPT_DIR}/config.env not found."
  echo "Copy config.env.example to config.env and fill in your values."
  exit 1
fi

# Make sync script executable
chmod +x "${SCRIPT_DIR}/context-sync.sh"

# Install systemd user service
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

# Use the right service file (jetson or desktop)
if [[ -f "${SCRIPT_DIR}/context-sync.service" ]]; then
  SERVICE_FILE="${SCRIPT_DIR}/context-sync.service"
else
  echo "ERROR: Service file not found."
  exit 1
fi

# Patch ExecStart to use absolute paths (in case the project lives elsewhere)
sed "s|%h|$HOME|g" "$SERVICE_FILE" > "${SYSTEMD_DIR}/${SERVICE_NAME}.service"

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}.service"
systemctl --user start "${SERVICE_NAME}.service"

echo ""
echo "=== Done! Service status: ==="
systemctl --user status "${SERVICE_NAME}.service" --no-pager -l
echo ""
echo "To view live logs: journalctl --user -u ${SERVICE_NAME} -f"
echo "To stop:           systemctl --user stop ${SERVICE_NAME}"
echo "To uninstall:      systemctl --user disable --now ${SERVICE_NAME} && rm ~/.config/systemd/user/${SERVICE_NAME}.service"
