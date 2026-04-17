# context-sync

Bidirectional real-time sync for the assistant `context/` folder between two Linux machines over SSH.

## How It Works

- `inotifywait` watches the local `context/` directory for any file changes
- On change, waits 2 seconds (debounce) for writes to settle, then `rsync`s to the remote
- Both machines run the service simultaneously, each pushing their changes to the other
- If the remote is offline, the change is skipped (the service will catch up on next restart via an initial full sync)
- Last-write-wins conflict resolution (no locks, no versioning — simple and predictable)

## Prerequisites

On **both** machines:

```bash
sudo apt install inotify-tools rsync openssh-client
```

SSH key-based auth must work between both machines without a passphrase:

```bash
# Generate key if you don't have one
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""

# Copy to remote (run once per direction)
ssh-copy-id -i ~/.ssh/id_ed25519 rodrigo@192.168.0.200   # Desktop → Jetson
ssh-copy-id -i ~/.ssh/id_ed25519 rodrigo@192.168.0.28    # Jetson → Desktop
```

## Installation

### Desktop (pushes to Jetson)

```bash
cd ~/Projects/assistant/sync
# config.env is already configured for the Desktop → Jetson direction
bash install.sh
```

### Jetson (pushes to Desktop)

```bash
# Copy sync/ folder to Jetson
scp -r ~/Projects/assistant/sync rodrigo@192.168.0.200:assistant/sync

# SSH into Jetson
ssh rodrigo@192.168.0.200

# Use the Jetson config
cd assistant/sync
cp config.jetson.env config.env
bash install.sh
```

## File Structure

```
sync/
├── context-sync.sh          # Main sync script (same on both machines)
├── config.env               # Local machine config (gitignored)
├── config.env.example       # Template — copy and edit
├── config.jetson.env        # Jetson config (copy to Jetson as config.env)
├── context-sync.service     # systemd unit for Desktop
├── context-sync.jetson.service  # systemd unit for Jetson
├── install.sh               # Installer script
└── README.md                # This file
```

## Managing the Service

```bash
# View live logs
journalctl --user -u context-sync -f

# Check status
systemctl --user status context-sync

# Restart
systemctl --user restart context-sync

# Stop
systemctl --user stop context-sync

# Uninstall
systemctl --user disable --now context-sync
rm ~/.config/systemd/user/context-sync.service
```

## What Gets Synced

Everything in `context/` except:
- `.git/` — git internal files
- `*.sync-conflict-*` — old Syncthing conflict files
- `.stfolder`, `.syncthing.*`, `.stversions/` — Syncthing artifacts
- `*.tmp` — temp files

## Notes

- **Conflict resolution**: Last write wins. Since only one machine writes Claude sessions at a time (Desktop when SSH-remote is active, Jetson for local sessions), conflicts are rare.
- **Offline handling**: If the remote is down, the current change is skipped. When the service restarts (e.g., after reboot), it does a full rsync to catch up.
- **Performance**: inotifywait is event-driven with zero CPU when idle. The 2-second debounce prevents excessive rsync calls during Claude's incremental JSONL writes.
