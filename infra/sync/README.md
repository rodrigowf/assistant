# context-sync

Bidirectional real-time sync for the assistant `context/` folder between two Linux machines over SSH.

## How It Works

- `inotifywait` watches the local `context/` directory for any file changes
- On change, waits 2 seconds (debounce) for writes to settle, then `rsync`s to the remote
- Both machines run the service simultaneously, each pushing their changes to the other
- If the remote is offline, the change is skipped and replayed by the reconcile when it's back (startup never runs `rsync --delete`)
- Deletes are replicated per path into the other machine's trash (`context/.sync-trash/<date>/`, kept 30 days), and *tombstoned* on both sides for 2 minutes so a deleted file isn't sent back by an in-flight push
- A *reconcile* (at startup, after an outage, every 10 min) compares both sides with the last agreed state, so deletes made while the machines were apart are replayed; if a file was deleted on one side but edited on the other, the edit wins
- Tests: `infra/sync/test/run-suite.sh` (two local instances + a fake ssh; run after any change)
- More than 50 deletions at once (or 25% of the files) are held for approval: `./context-sync.sh --status`, `--approve-deletes`, `--reject-deletes`
- If `inotifywait` can't watch a directory (out of inotify watches), the service exits and systemd restarts it with a full set of watches
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
cd ~/assistant
# config.env is gitignored (per machine), so create it first from the template
# and point it at the Jetson (REMOTE_HOST=192.168.0.200):
cp install/sync.env infra/sync/config.env
$EDITOR infra/sync/config.env
bash infra/sync/install.sh
```

### Jetson (pushes to Desktop)

```bash
# The Jetson checkout already has infra/sync/ after a git pull; otherwise copy it:
#   scp -r ~/assistant/infra/sync rodrigo@192.168.0.200:assistant/infra/

# SSH into Jetson
ssh rodrigo@192.168.0.200

# Use the Jetson config
cd assistant
cp infra/sync/config.jetson.env infra/sync/config.env
bash infra/sync/install.sh
```

## File Structure

```
infra/sync/
├── context-sync.sh          # Main sync script (same on both machines)
├── config.env               # Local machine config (gitignored)
├── config.jetson.env        # Jetson config (copy to Jetson as config.env)
├── context-sync.service     # systemd user unit (same on both machines)
├── install.sh               # Installer script
└── README.md                # This file
```

The template for `config.env` lives at the repo root as `install/sync.env` —
copy it into `infra/sync/config.env` and fill it in.

The systemd unit runs `%h/assistant/infra/sync/context-sync.sh %h/assistant/infra/sync/config.env`
(`install.sh` expands `%h` to `$HOME`). After moving the folder, re-run `bash infra/sync/install.sh`
so the installed unit in `~/.config/systemd/user/` points at the new path.

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
- **Inotify budget**: inotify watches are a per-user limit (`fs.inotify.max_user_watches`; raised to 524,288 on the laptop via `/etc/sysctl.d/60-inotify.conf`) shared with every other watcher. If the service crash-loops with "upper limit on inotify watches reached", another process is holding the budget — on 2026-10-05 it was VS Code watching `node_modules`/build dirs/the search index. The repo's `.vscode/settings.json` excludes those from VS Code's watcher. To find the culprit, count watches per process: `for p in /proc/[0-9]*; do n=$(cat $p/fdinfo/* 2>/dev/null | grep -c '^inotify'); [ "$n" -gt 0 ] && echo "$n $(cat $p/comm)"; done | sort -rn | head`.
