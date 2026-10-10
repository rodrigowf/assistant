---
name: context-sync
category: archie/infrastructure
tags: [context-sync, rsync, inotifywait, systemd, git, delete-gating, tombstones, inotify, vscode, large-files]
created: 2026-04-17
modified: 2026-10-09
summary: The context-sync service that mirrors context/ between laptop and Jetson — design, install, delete gating, git rule, pitfalls.
source: curated (consolidated from memory notes assistant/infrastructure/server_hub_project.md, project_context_sync_delete_gating.md, feedback_jetson_main_repo_normal_git.md, assistant/utilities/review_artifacts_location.md, projects/video-editing/large_assets_outside_context.md, assistant/infrastructure/repo_layout_cutover_2026_10.md; verified against infra/sync/ 2026-10-06)
references:
  - topology.md
  - deployment.md
  - jetson-server.md
  - installation.md
  - ../operations/troubleshooting.md
  - ../operations/working-rules.md
---

# context-sync

`context-sync` keeps the private `context/` folder (conversations, memory, skills, scripts, `.env`,
secrets, `public/`) identical on the laptop and the Jetson in near real time. It is a bash script
run as a **systemd user service on both machines**; each side watches its own `context/` and pushes
changes to the other. Code is not synced this way — see [topology.md](topology.md).

| File | Role |
|---|---|
| `infra/sync/context-sync.sh` | The service: initial sync, then inotify watch loop |
| `infra/sync/context-sync.service` | User unit, the same on both machines (`ExecStart=%h/assistant/infra/sync/context-sync.sh %h/assistant/infra/sync/config.env`, `Restart=on-failure`, `RestartSec=10s`, `ExecStartPre=/bin/sleep 5`) |
| `infra/sync/install.sh` | Checks deps, requires `config.env`, copies the unit to `~/.config/systemd/user/` with `%h` expanded, enables and starts it |
| `install/sync.env` | Template for `config.env` |
| `infra/sync/config.env` | **Gitignored**, per machine: which machine is the remote |
| `infra/sync/config.jetson.env` | Tracked Jetson-direction config (copy to `config.env` on the Jetson) |
| `infra/sync/README.md` | Operator README |

## Configuration

`config.env` variables (all required except the last two):

| Variable | Meaning |
|---|---|
| `LOCAL_DIR` | Local `context/` (absolute, no trailing slash) |
| `REMOTE_HOST`, `REMOTE_USER` | The other machine (laptop config → `192.168.0.200`; Jetson config → `192.168.0.28`) |
| `REMOTE_DIR` | Remote `context/` — same path on both machines |
| `SSH_KEY` | Passphrase-less private key authorized on the other side |
| `DEBOUNCE_SECONDS` | Default `2` |
| `RETRY_INTERVAL` | Default `30` — wait between reachability checks at startup |
| `TOMBSTONE_SECONDS` | Default `120` — how long a deleted path stays tombstoned (see step 5) |
| `RECONCILE_SECONDS`, `TICK_SECONDS` | Default `600` / `30` — periodic reconcile; how often decisions are checked |
| `MAX_DELETES`, `MAX_DELETE_PERCENT` | Default `50` / `25` — the brake: more deletions than this wait for approval |
| `TRASH_DAYS` | Default `30` — `.sync-trash/<date>/` folders older than this are purged |
| `REMOTE_SCRIPT` | Default `assistant/infra/sync/context-sync.sh` (relative to the remote `$HOME`) |
| `WATCH_FAIL_DELAY` | Default `30` — pause before exiting when a directory can't be watched |
| `STATE_DIR`, `REMOTE_STATE_DIR` | Tombstones, manifest, held deletions; default `~/.local/state/context-sync` on both sides |
| `LOG_TAG` | Journal tag, default `context-sync` (tests use another tag) |

## How it works

1. **Startup: reconcile.** Waits until the remote answers (`ssh … true`, retrying every
   `RETRY_INTERVAL`), then runs a full **reconcile** (below). Never `rsync --delete`: the other
   machine is live (the Jetson's backend writes all the time), so a blind `--delete` would remove
   whatever it created that this side hadn't received yet.
2. **Watch.** `inotifywait --monitor --recursive` on `LOCAL_DIR` for
   `close_write,moved_to,moved_from,delete,create`, excluding `/.git/`, `/.sync-trash/`,
   sync-conflict and Syncthing files, `.stfolder` and `*.tmp`. inotifywait honours only its last
   `--exclude`, so all exclusions are one regex alternation.
3. **Debounce.** After the first event it keeps draining events until `DEBOUNCE_SECONDS` pass with
   none, so a streaming JSONL write becomes one sync.
4. **Push what changed.** `rsync -az --update --prune-empty-dirs --files-from=<this batch's paths> -r`
   **without** `--delete` (rsync excludes: `.git/`, `/.sync-trash/`, Syncthing artifacts, `*.tmp`,
   `.DS_Store`, active tombstones and held deletions). Only the paths from this batch's events are
   sent (a new directory with its contents) — **never the whole tree**: a file this machine has and
   the other lacks may have been deleted there while this side wasn't looking (its service stopped),
   and a whole-tree push from the busy Jetson silently undid exactly such deletes. Copying anything
   else that's missing on one side is the reconcile's job, which can tell new from deleted.
   `--update` skips files that are newer on the receiver (last-write-wins). The log line names what
   was pushed.
5. **Apply deletions per path, with tombstones, into the trash.** Every `DELETE` / `MOVED_FROM` path
   seen during the window is collected; after the window, only paths that are **really gone
   locally** are kept (an atomic rename fires `MOVED_FROM` for a file that reappears under the same
   name), minus rsync's own temp files (`.<name>.XXXXXX` whose `<name>` now exists). Each kept path is
   written as a *tombstone* (`<epoch>\t<path>`) on **both** machines, then **moved to the remote's
   trash** (`context/.sync-trash/<date>/<path>`; never `rm`). For `TOMBSTONE_SECONDS`:
   - pushes on either side skip tombstoned paths, so neither machine sends a deleted file back;
   - a tombstoned file that reappears with an mtime **older** than its delete is a stale copy from a
     push that was already in flight; it goes to the trash again;
   - a local delete of a path the other side already tombstoned (it deleted it here) is not echoed
     back (this ended a delete ping-pong between the machines);
   - a path re-created on purpose (mtime at or after the delete) is a new file and syncs normally.
   More than `MAX_DELETES` live deletes at once (or `MAX_DELETE_PERCENT` of the files) are **not**
   replicated: they are held for approval by the reconcile (step 8).
6. **Manifest.** After each successful sync the *manifest* (`~/.local/state/context-sync/manifest`,
   `path\tsize\tmtime`) is updated: what this batch changed now exists on both sides; deletions the
   remote applied are dropped. It is the "last agreed" state the reconcile compares against.
7. **Remote offline.** The batch is skipped and logged; deletes stay in the manifest; the reconcile
   after the remote is back replays them.
8. **Reconcile** — at startup, after an outage, when an approve/reject decision is waiting, and every
   `RECONCILE_SECONDS` (checked every `TICK_SECONDS` even when busy). It lists both sides
   (`context-sync.sh --list` over ssh; the remote runs the same script) and compares them with the
   manifest:

   | In the manifest, and now… | Meaning | Action |
   |---|---|---|
   | gone here, **unchanged** there | deleted here while apart (remote off, service down) | delete there (to its trash) |
   | gone there, **unchanged** here | deleted there | delete here (to our trash) |
   | gone on one side, **changed** on the other | delete vs. edit | **the edit wins**: copied back, logged |
   | not in the manifest | new on one side | copied, never deleted |

   Then both directions are copied (`rsync --update`, no `--delete`) and the manifest is rewritten.
   With no manifest yet (first run) nothing is deleted, only copied both ways.
   **Brake:** if the inferred deletions exceed `MAX_DELETES` (50) or `MAX_DELETE_PERCENT` (25 %) of
   the manifest, none is applied. They are written to `pending-deletes`, logged once as an `ERROR`
   ("holding N deletion(s) for approval"), sent to the other machine (`remote-held`) so neither side's
   pushes undo the hold, and left alone (not copied either way) until a decision:
   - `infra/sync/context-sync.sh --status` — what is held, what's in the trash;
   - `--approve-deletes` — apply them (to the trash of the machine they're removed from);
   - `--reject-deletes` — keep the files: they are restored on both sides.
   Decisions are per path and picked up by the running service within `TICK_SECONDS`, even if more
   deletions were held meanwhile. This is what protects against a wiped or half-restored
   `context/`: everything missing shows up as a held mass deletion, not as deletes on the other side.
9. **Trash.** Every removal the service makes (replicated deletes, reconcile deletes, stale copies)
   moves the file to `context/.sync-trash/<date>/<path>` on that machine — not synced, not in git
   (`.gitignore`), purged after `TRASH_DAYS` (30). Restore = move it back.
10. **Watch failures restart the service.** `inotifywait` never retries a directory it couldn't
    watch (out of inotify watches, permissions), so that directory stayed blind. Any `Couldn't watch
    …` / `upper limit on inotify watches` line is logged with "A directory is not being watched;
    restarting in 30s", and the script exits with a failure (SIGUSR1) so systemd
    (`Restart=on-failure`) starts it again with a complete set of watches.

`inotifywait`'s own stderr (other than the "Setting up watches" banners and the harmless "remove
watch" messages for deleted directories) goes to the journal as `inotifywait: …` errors, so a
watch-limit failure is visible in `journalctl -t context-sync`. A reconcile with nothing to do logs
nothing.

**Testing.** `infra/sync/test/run-suite.sh` (≈5 min, exit status = failures; run it after any change
to the script, on the laptop and on the Jetson). Every behaviour above was checked with two instances syncing two local folders through
a fake `ssh` (which runs the remote command locally and can simulate the remote being offline), on
the laptop and on the Jetson (mawk 1.3.3, bash 4.4, rsync 3.1.2): live create/delete, delete and
create while offline, delete while the service is stopped, edit beats delete, brake + approve, brake
+ reject, a wiped `context/`, a live mass delete while the other side keeps pushing, a delete while
this side's service is stopped and the other side keeps writing, a folder moved in with its contents,
and no log noise when idle.

### Why deletes are per-path (never `rsync --delete` on incremental syncs)

With `--delete` on every event both sides race: A creates `foo.md` and starts pushing; meanwhile any
event on B triggers B → A with `--delete`, which removes `foo.md` on A before B ever received it —
new files vanished. Gating `--delete` on seeing a delete event did not help, because Claude Code's
JSONL writer and most editors write via `tmp → rename`, so almost every burst contains a
`MOVED_FROM`. The fix (2026-05-19) dropped `--delete` semantics entirely: a path is deleted remotely
only because *this* machine saw it deleted, so a file the other side just created can never be on
the list. Deletes made while the machines were apart are replayed by the reconcile, which infers them
from the manifest (2026-10-09) instead of trusting a `--delete`. **Don't "simplify" this back to
`rsync --delete`.**

## Install

On each machine (`sudo apt install inotify-tools rsync openssh-client` first; passwordless SSH must
work in both directions):

```bash
cd ~/assistant
cp install/sync.env infra/sync/config.env      # laptop: fill in; Jetson: cp infra/sync/config.jetson.env infra/sync/config.env
bash infra/sync/install.sh
```

User units only start at boot with lingering enabled (`sudo loginctl enable-linger rodrigo`). After
moving `infra/sync/` (as in the 2026-10-05 reorganization), copy `config.env` into the new folder
and re-run `install.sh` on both machines so the installed unit points at the new path — `git pull`
does not bring `config.env`.

## Operating it

```bash
systemctl --user status context-sync
journalctl --user -u context-sync -f          # "Synced after change (rm N path(s) on remote)."
systemctl --user restart context-sync
systemctl --user stop context-sync            # e.g. during a large render inside context/
```

On the Jetson, run the same commands over SSH (`ssh rodrigo@192.168.0.200 "systemctl --user status context-sync"`).

## Git on the Jetson's context repo

`context/` is also a git repo (`assistant-context`, branch `main`) for versioning. Commit and push
from the laptop. **On the Jetson, never `git pull` the context repo**: rsync keeps its working tree
current but not its `.git/`, so the tree always looks dirty to git; `pull` refuses, and
stash/checkout/pull-rebase rewrite files that rsync then echoes back to the laptop. Use a
metadata-only reset:

```bash
sshpass -p "$SERVER_PASSWORD" ssh -o StrictHostKeyChecking=no rodrigo@192.168.0.200 << 'REMOTE'
cd /home/rodrigo/assistant/context || { echo "NO DIR"; exit 1; }
git fetch -q origin && git reset --mixed origin/main
git status --short
REMOTE
```

`reset --mixed` writes only `.git/HEAD` and `.git/index`; `.git/` is excluded from both inotify and
rsync, so the reset causes no sync traffic, and the working tree (already matching via rsync) is
untouched. After a long offline gap, first hash-compare each dirty file against `origin/main`
(`sha256sum "$f"` vs `git show origin/main:"$f" | sha256sum`); any difference means rsync is still
propagating or the Jetson has real local edits — investigate before resetting.

**This rule is for `context/` only.** The main `~/assistant` repo is not rsynced and uses normal git
([deployment.md](deployment.md#rules-and-why)); `reset --mixed` there leaves stale code on disk.

## Pitfalls

- **inotify budget.** Watches are a per-user limit (`fs.inotify.max_user_watches`) shared by every
  watcher. The laptop's is raised to 524,288 in `/etc/sysctl.d/60-inotify.conf` (2026-10-09; it
  was 65,536 and ran out again when a VS Code instance with a large JS project open held ~51k
  watches — five `context/` directories created meanwhile went unwatched; global
  `files.watcherExclude` for `node_modules`/build dirs is set in the user's VS Code settings).
  Earlier: On 2026-10-05 VS Code, open on the repo, used almost all of it
  (node_modules, Gradle build dirs, the index, `legacy/`), so `inotifywait --recursive` could not
  start and the service crash-looped for ~3.5 hours (`Initial sync complete.` then
  `status=1/FAILURE` every ~15 s). The checked-in `.vscode/settings.json` sets
  `files.watcherExclude` for those dirs and the bulky `context/` subtrees (`*.jsonl`, `chats/`,
  `trash/`, `recordings/`, `projects/`); reload the VS Code window after changing it. Raising the
  sysctl is the alternative. Count watches per process:
  `for p in /proc/[0-9]*; do n=$(cat $p/fdinfo/* 2>/dev/null | grep -c '^inotify'); [ "$n" -gt 0 ] && echo "$n $(cat $p/comm)"; done | sort -rn | head`
  — a healthy `inotifywait` on `context/` holds a few hundred.
- **Large binaries.** Everything under `context/` is mirrored, including paths the context repo
  gitignores (`recordings/`, `/projects/`, media subfolders of `memory/projects/<name>/`,
  `public/memory/`). Gitignoring a folder keeps it out of git, not out of rsync. Multi-GB asset
  projects belong in the repo-root `projects/<name>/` (laptop only, not synced): on 2026-06-12 a
  12 GB video project under `context/projects/` filled the Jetson disk with rsync partials
  (contributing to a crash), aborted ffmpeg `+faststart` encodes mid-rename, and replaced hardlinks
  with symlinks in a Remotion `public/` folder. If you must work on large files inside `context/`,
  stop the service for the duration. Review artifacts (renders, previews) should be small —
  downscale to tens of MB before copying into `context/`.
- **Hardlinks don't survive.** rsync without `-H` turns a hardlinked pair into two copies on the
  other machine (disk doubles there) — fine for small files only.
- **Conflicts.** Last write wins; there is no merge. Avoid editing the same file on both machines
  within a couple of seconds. In practice only one machine writes a given session at a time.
- **The JSONL symlink.** On both machines `.claude_config/projects/-home-rodrigo-assistant` is a
  symlink to `../../context`, which is what makes wrapper session JSONLs land in the synced tree.

## History

- 2026-04: `inotifywait` + `rsync` service introduced (replacing Syncthing), running on both machines.
- 2026-05-17: first delete-gating attempt (gate `--delete` on delete events) — did not work.
- 2026-05-19: per-path observed deletes; incremental pushes never use `--delete`.
- 2026-06-06: `reset --mixed` procedure for the Jetson's context repo confirmed (9- and 1-commit gaps,
  zero rsync echo).
- 2026-10-05: moved from `sync/` to `infra/sync/`; VS Code inotify crash-loop and the
  `.vscode/settings.json` excludes; inotifywait errors now reach the journal.
- 2026-10-07: removed the unused duplicate `context-sync.jetson.service` (both machines install
  `context-sync.service`); the README no longer claims `config.env` ships configured.
- 2026-10-09: no `--delete` at startup; tombstones (no resurrected deletes, no delete ping-pong);
  restart on unwatched directories; rsync temp names ignored; pushed/deleted paths logged; laptop
  `max_user_watches` raised to 524,288. Same day: manifest + reconcile (deletes made while apart are
  replayed; edit beats delete), trash instead of `rm`, mass-delete brake with
  `--status` / `--approve-deletes` / `--reject-deletes`.
