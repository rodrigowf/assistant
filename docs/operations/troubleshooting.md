---
name: troubleshooting
category: archie/operations
tags: [troubleshooting, incidents, symptoms, fixes, jetson, deploy, context-sync, ssh, voice, android, search]
created: 2026-04-20
modified: 2026-10-10
summary: Symptom → cause → fix table for every known Archie incident, each linked to the doc with the detail.
source: curated (consolidated from all feedback_* and reference_* auto-memory notes, project_jetson_crash_2026_04_20.md, project_context_sync_delete_gating.md, project_indexer_full_reembed_fix_2026_06_17.md, project_voice_ghost_state_fix_2026_06_30.md (index line), project_qwen_voice_gate_no_staleness_clear.md (index line), assistant/infrastructure/server_hub_project.md, assistant/utilities/*.md, projects/video-editing/large_assets_outside_context.md; verified against code 2026-10-06)
references:
  - debugging.md
  - working-rules.md
  - ../infrastructure/deployment.md
  - ../infrastructure/context-sync.md
  - ../infrastructure/ssh-remote-execution.md
  - ../infrastructure/jetson-server.md
  - ../infrastructure/installation.md
  - ../infrastructure/topology.md
  - ../architecture/backend.md
  - ../architecture/memory-and-search.md
  - ../architecture/orchestrator.md
  - ../voice/lifecycle.md
  - ../voice/qwen-omni.md
  - ../voice/gemini-live.md
  - ../voice/wake-word.md
  - ../clients/android.md
  - ../integrations/skills.md
---

# Troubleshooting

Find the symptom, check the cause, apply the fix, follow the link for the full story. When nothing
matches, start from [debugging.md](debugging.md) and capture real logs first.

## Deploy and the Jetson

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| An agent tab (often one the orchestrator delegated to) shows "Using tools…" / the stop button forever, but `pool/live` says the session is `idle`; its JSONL ends in `[Request interrupted by user]` | The orchestrator's runner ended the turn (timeout / cancel) — the bundled CLI writes that line for **any** interrupt — and before 2026-10-10 no frame told the tabs; Android also never re-read `pool/live` on reconnect (ST-2) | Tap Stop (the backend always answers with `status: interrupted`) or reopen the session. Check the orchestrator JSONL for a `background_notification` with `status: timeout`/`cancelled` | [orchestrator.md](../architecture/orchestrator.md#fire-and-forget-agent-turns-backendorchestratorrunnerpy) |
| A conversation (Archie or agent) closed on one device is still listed "Open now" on another, or came back after a close | Before 2026-10-10: views the user had looked at stayed as "Stopped" tabs (FOCUS-3), and a returning device's automatic `start` re-created closed conversations | Fixed by the server-owned open set (spec 12 OPEN-1..4): automatic starts carry `reattach` and get `session_closed`; a missing pool row closes the view. If it recurs: compare `curl …/api/sessions/pool/live` (the truth, also `state/open_sessions.json` on that machine) with the device, and check the client sends `"reattach":true` on reconnect | [agent-sessions.md](../architecture/agent-sessions.md#sessionpool-backendapipoolpy) |
| Voice (or any new feature) fails from the **web** app but works from the **phone**; backend logs clean, `remote_console.log` shows `websocket_error` | Stale `apps/web/dist` on the Jetson — dists are built on the laptop and rsynced; the Android app ships its own current code | Build on the laptop, rsync **both** `dist` and `dist-compat` with `--delete`; hard reload. Confirm by grepping the deployed bundle for a new string | [deployment.md](../infrastructure/deployment.md#4-verify) |
| Backend runs old code although `git log` on the Jetson shows the new commit; files show `M` | `git reset --mixed` (or `--soft`) used on the **main** repo — it moves HEAD, not the files | `git pull --ff-only`, or `fetch` + `reset --hard origin/<branch>` on a tree with nothing to keep; verify the file content | [deployment.md](../infrastructure/deployment.md#rules-and-why) |
| Remote `git …` says `fatal: not a git repository` (or silently does nothing) | Inline `cd` dropped by nested quoting; command ran in `$HOME` | Quoted heredoc `<< 'REMOTE'` with an absolute `cd … \|\| exit 1`, or `git -C /home/rodrigo/assistant` | [deployment.md](../infrastructure/deployment.md#rules-and-why) |
| `git branch --show-current` fails on the Jetson | Old git | `git rev-parse --abbrev-ref HEAD` | [jetson-server.md](../infrastructure/jetson-server.md#hardware-and-os) |
| `git pull` on the Jetson: "local changes would be overwritten" after scp'ing a file | Out-of-band copy before the matching commit | Stash → pull → drop the stash (bytes match the incoming HEAD) | [deployment.md](../infrastructure/deployment.md#rules-and-why) |
| Sessions fail with 401 "Invalid authentication credentials", "OAuth session expired and could not be refreshed", or "Not logged in" — but `claude -p` from a terminal works | The backend's `.claude_config/.credentials.json` is stale/corrupt (a separate, unsynced store); copies of one grant on two machines kill each other's refresh token | Ensure `CLAUDE_CODE_OAUTH_TOKEN` in `context/.env` is valid; verify with `CLAUDE_CONFIG_DIR=…/.claude_config` forced; restart the backend. Copying fresh creds is only a stopgap | [deployment.md](../infrastructure/deployment.md#claude-code-authentication-on-the-jetson) |
| Jetson unreachable, `/` mounted read-only, I/O errors on basic binaries | Runaway systemd user-session spawn overwhelmed the SD card (2026-04-20) | Rebuild/restore from the 2026-05-15 image; keep `StartLimitBurst`, watchdog, per-host SSH lock and path cache in place | [jetson-server.md](../infrastructure/jetson-server.md#the-2026-04-20-crash-and-its-guardrails) |
| Fan loud / SoC warm hours after boot with an idle backend; CPUs stuck at 1224 MHz | HDMI hotplug false trigger woke the display controller (IRQ 74 storm) | fb0 blanking layers; force-blank `echo 4 > /sys/class/graphics/fb0/blank`; keep `nvargus-daemon` disabled | [jetson-server.md](../infrastructure/jetson-server.md#headless-display-blanking-fb0) |
| uvicorn pinned at ~100 % of one core for hours, no workload | anyio busy-loop cancelling an uncancellable `watchfiles` thread (2026-05-23) | Fixed in `be8514e`; for a recurrence `py-spy dump`, look for `_deliver_cancellation`, signal the thread in `finally` | [jetson-server.md](../infrastructure/jetson-server.md#history) |
| `systemctl is-enabled nginx.service` says disabled | Expected — the active unit is `nginx-server.service` | Check `nginx-server.service` | [jetson-server.md](../infrastructure/jetson-server.md#services-at-boot) |
| Peripheral can't open a link to `:8765` | Backend binds `127.0.0.1` on the Jetson | Use `https://192.168.0.200/<path>` (nginx) | [jetson-server.md](../infrastructure/jetson-server.md#security-notes) |
| `curl … \| sudo -S tee` doesn't work | `sudo -S` consumes stdin for the password | Download to a temp file, then `sudo -S cp` | [jetson-server.md](../infrastructure/jetson-server.md#access) |
| Restart chain dies with **exit 144**, no new process | `pkill -f <pattern>` matched the Bash tool's own shell (SIGPIPE) | `pgrep -f` + `kill <PID>`; on the Jetson only `systemctl restart agentic-backend.service` | [working-rules.md](working-rules.md#processes-and-services) |
| SSH session dies with exit 255 after `pkill -f "claude setup-token"` | Pattern matched the SSH command itself | Kill by PID | [deployment.md](../infrastructure/deployment.md#claude-code-authentication-on-the-jetson) |

## context-sync and files

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `context-sync` crash-loops: `Initial sync complete.` then `status=1/FAILURE` every ~15 s; `inotifywait: … upper limit on inotify watches reached` | Per-user inotify budget used up by another watcher (VS Code on 2026-10-05) | Keep `.vscode/settings.json` `files.watcherExclude` and reload VS Code; count watches per process; or raise `fs.inotify.max_user_watches` | [context-sync.md](../infrastructure/context-sync.md#pitfalls) |
| A file deleted on one machine came back from the other | Before 2026-10-09: a missed delete event (unwatched dir), the other side's whole-tree push, or an echo race | Fixed: tombstones, manifest + reconcile, changed-paths-only pushes. If it recurs: `journalctl --user -t context-sync` names pushed/deleted paths; run `infra/sync/test/run-suite.sh` | [context-sync.md](../infrastructure/context-sync.md#how-it-works) |
| Files missing on one machine but not deleted on the other; journal `ERROR: Reconcile: holding N deletion(s) for approval` | The brake: more than 50 deletions (or 25 %) at once, live or inferred after an outage | `infra/sync/context-sync.sh --status`, then `--approve-deletes` or `--reject-deletes` (restores them) | [context-sync.md](../infrastructure/context-sync.md#how-it-works) |
| A file disappeared and you didn't delete it | It was deleted on the other machine and replicated | Look in `context/.sync-trash/<date>/<path>` on this machine (kept 30 days); move it back | [context-sync.md](../infrastructure/context-sync.md#how-it-works) |
| `inotifywait: Couldn't watch new directory …: No space left on device`, then "A directory is not being watched; restarting" | The per-user inotify budget ran out (another app, typically VS Code on a big JS project) | The service restarts itself; raise `fs.inotify.max_user_watches` (laptop: 524288 in `/etc/sysctl.d/60-inotify.conf`) and keep `files.watcherExclude` | [context-sync.md](../infrastructure/context-sync.md#pitfalls) |
| Newly created files in `context/` vanish | `rsync --delete` on incremental syncs raced with the other side's not-yet-pushed creates | Fixed: incremental pushes never use `--delete`; deletes are per observed path. Don't reintroduce `--delete` | [context-sync.md](../infrastructure/context-sync.md#why-deletes-are-per-path-never-rsync---delete-on-incremental-syncs) |
| Unit fails after the 2026-10-05 move / `config.env` not found | `infra/sync/config.env` is gitignored; installed unit still points at the old `sync/` path | Copy `config.env` into `infra/sync/`, re-run `bash infra/sync/install.sh` on each machine | [context-sync.md](../infrastructure/context-sync.md#install) |
| `git pull` refuses in the Jetson's `context/`, or pulls cause rsync echo | rsync keeps the working tree ahead of `.git/` | `git fetch && git reset --mixed origin/main` (context repo only) | [context-sync.md](../infrastructure/context-sync.md#git-on-the-jetsons-context-repo) |
| Jetson disk fills with rsync `.partial` files; ffmpeg `+faststart` encode aborts at 100 % ("Unable to re-open output file"); Remotion renders 404 on `public/` assets | Multi-GB media project inside `context/` being mirrored live | Move heavy projects to repo-root `projects/<name>/`; or stop context-sync during renders | [context-sync.md](../infrastructure/context-sync.md#pitfalls) |
| Rodrigo / the orchestrator can't see a file you produced | It is in laptop-only `projects/` (or another non-synced path) | Copy review artifacts under `context/memory/<project>/`; link via `https://192.168.0.200/memory/…` | [working-rules.md](working-rules.md#changing-things) |
| `assistant_config.json` (or another gitignored file) suddenly overwritten | A `git stash pop` / checkout by an agent applied unrelated changes; git treats ignored files as expendable | No git writes from subagents; restore from backup; reconfigure working dirs | [working-rules.md](working-rules.md#git-and-parallel-work) |
| A session started in a terminal is missing from Archie's history | Bare `claude` writes to `~/.claude/projects/…`, not `context/` | Start sessions through the wrapper (or `run.sh`, which sets `CLAUDE_CONFIG_DIR`) | [topology.md](../infrastructure/topology.md#what-is-per-machine-and-does-not-travel) |

## SSH remote sessions

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| Remote session exits **127** with `qwen: command not found` (or claude/gemini) | Non-interactive shell never loads nvm; `which` finds nothing | nvm glob in `default_cli_search_paths`, `ls -t`; restart the backend to clear the cached path | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#the-nvm-exit-127-trap) |
| Exit **127** with `/usr/bin/env: 'node': No such file or directory` | Absolute CLI path found but `node` (same nvm dir) not on `PATH` | `RemoteCommand.render_shell()` prepends the CLI dir to `PATH`; restart the backend | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#the-nvm-exit-127-trap) |
| Remote session runs but in `/home/rodrigo`: wrong project key, no skills, JSONL in the wrong bucket | SSH space-joins arguments; naive `bash -c … "$@"` loses `cd` and args | Wrapper quotes args locally, sends one remote command string (current code) | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#the-ssh-quoting-bug-and-the-fix) |
| JSON stream corrupted by a `declare -x …` dump | `export` used in the remote command | Inline `VAR=val exec cmd` (current code) | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#shared-primitives-_sshpy) |
| `RemoteHostUnreachableError` when opening a session | ICMP pre-probe failed — remote asleep/offline | Wake the remote; nothing is cached, the next start re-probes | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#shared-primitives-_sshpy) |
| Testing the probe laptop → laptop gives `Permission denied (publickey)` | No self-key; wrong direction | Test Jetson → laptop | [ssh-remote-execution.md](../infrastructure/ssh-remote-execution.md#debugging) |

## Search and indexing

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| Search server at 200 %+ CPU for long periods, slow chat/voice on the Jetson, no queries running | History indexer re-embedding the whole corpus every pass (2026-06-17: hash of all JSONLs changed whenever any session was active) | Fixed (`6e98e2b`); the SQLite index skips sessions whose size/mtime/chunker version are unchanged. If it recurs, check that the per-pass `unchanged` count is not always 0 | [memory-and-search.md](../architecture/memory-and-search.md) |
| First search takes ~70–100 s on the Jetson | Cold load of PyTorch + sentence-transformers | The warm `shared/scripts/search-server.py` subprocess, pre-warmed at API startup, answers in ~1–3 s; check it is running | [memory-and-search.md](../architecture/memory-and-search.md) |
| `ImportError: … cannot allocate memory in static TLS block` on aarch64 | libgomp loaded too late (sklearn via sentence-transformers) | Run Python through `run.sh`, which sets `LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1` | [installation.md](../infrastructure/installation.md#the-symlink-model) |
| A grep for a memory fact returns nothing | ripgrep from the repo root skips gitignored `context/` | Grep with an explicit `context/memory` path; read `MEMORY.md` | [working-rules.md](working-rules.md#before-acting) |

## Voice, clients and providers

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| No Android notification when an agent session finishes | Switch off (Settings → This device → Notifications, off by default), permission/channel blocked, you were looking at that session, or Archie wasn't running when a turn started elsewhere | `adb logcat -s ArchieNotify` says `posted`/`suppressed … reason=…`; turn the switch on; for turns started elsewhere enable "Stay connected in background" | [android.md](../clients/android.md#agent-notifications-main-app) |
| Accounts: a CLI sign-in was started and the existing login is gone | Older build: `codex login` / `claude auth login` clear credentials when they start | Fixed (temp home + snapshot/restore). Sign in again; backups of credential files are kept next to them | [authentication.md](../harnesses/authentication.md) |
| A page or tool gets **403 "Cross-site request rejected"** (WebSocket: handshake refused / close 1008); backend log `Rejected POST /api/… (origin=… host=…)` | The browser-origin guard: the request came from a page served by another origin (other port or machine), with `Origin: null`, or reached the server under a `Host` it does not trust | Open the page from the server itself, or add its origin (`scheme://host[:port]`) to `ARCHIE_TRUSTED_ORIGINS` (a public DNS name for the server: `ARCHIE_TRUSTED_HOSTS`) — via Settings → Accounts it applies at once, in `context/.env` after a restart | [backend.md](../architecture/backend.md#auth-and-the-browser-origin-guard) |
| Android chat freezes mid-session after the phone slept; no error | okhttp closed the WS (`1011 keepalive ping timeout`) and the reconnect didn't re-send `start`, so the socket wasn't subscribed | Clients re-send `start` + resume checkpoint on every reconnect (fixed `b9646ee` in the old app; built into the new protocol) | [debugging.md](debugging.md#device-playbook-android) |
| Every new tab shows "Preparing conversation…" without anyone starting voice | Backend ghost voice state (`_voice=True` with lifecycle idle) | Fixed `f5b339a`; diagnose with the WS probe, clear with `POST /api/sessions/{local_id}/close` | [debugging.md](debugging.md#direct-websocket-probe), [lifecycle.md](../voice/lifecycle.md) |
| Voice "stopped suddenly mid-call" | Relay closed | Read `voice_session_closed` and its reason in the backend log before anything else | [debugging.md](debugging.md#voice-debugging-signals) |
| Voice commands never reach the provider: many `voice_command_deferred`, zero `voice_command_drain` | A parked command whose only drain trigger is an event it would itself cause | Give every queue a drain path independent of its own output (fixed for OpenAI and Qwen) | [debugging.md](debugging.md#voice-debugging-signals) |
| Voice suddenly fails everywhere with quota/`insufficient_quota` | OpenAI credits exhausted (2026-10-06 by paid evals) | Top up; estimate cost and confirm before paid runs | [working-rules.md](working-rules.md#changing-things) |
| Gemini Live WS closed with 1008 "The operation was aborted" ~150 s after setup | A dead resumption handle re-presented after a relay rebuild | Clear opaque provider handles in the lowest-level teardown (only goAway reconnect keeps them) | [gemini-live.md](../voice/gemini-live.md) |
| Gemini Live 1008 policy violation at setup | Stale/renamed model id | Auto-corrected since `6a4712f`; update the model id | [gemini-live.md](../voice/gemini-live.md) |
| Qwen voice ends after ~10.5 min: "Too many audios. The maximum allowed is 320." | DashScope's per-conversation audio cap (~630 s committed, silence included) | Relay silence gating + audio budget (`QWEN_AUDIO_HISTORY_BUDGET_S`) delete the oldest user audio items | [qwen-omni.md](../voice/qwen-omni.md#the-320-audio-cap-silence-gating-and-the-audio-budget) |
| Qwen voice: WS 1011 "Parse RealtimeEvent error: Common error!" / user cut off after ~30–40 s of speech | DashScope validator strictness / server VAD force-commit | Tool-schema sanitizing; client-side Silero VAD (`QWEN_MANUAL_VAD`) | [qwen-omni.md](../voice/qwen-omni.md) |
| Audio chat turns 404 `model_not_found` | `gpt-4o-audio-preview` retired; the family is `gpt-audio` / `gpt-audio-mini` | Use `gpt-audio` (`backend/orchestrator/config.py`); if it 404s again, list `/v1/models` | [orchestrator.md](../architecture/orchestrator.md) |
| Wake word became much harder to trigger after a "small improvement" | Tuned constants changed (2026-06-06 RMS threshold) | Revert; constants are read-only without approval | [wake-word.md](../voice/wake-word.md) |
| Native crash on a phone during voice | Unknown until logged (2026-04-27 turned out to be a cleanup race after `insufficient_quota`) | Capture logcat/tombstone first | [working-rules.md](working-rules.md#diagnosing) |

## Agent tooling

| Symptom | Cause | Fix | Detail |
|---|---|---|---|
| `Skill(<name>)` → "Unknown skill" for a project skill | Project skills under `context/skills/` don't always register | Read `context/skills/<name>/SKILL.md` and follow it; delegate with the path | [working-rules.md](working-rules.md#before-acting), [skills.md](../integrations/skills.md) |
| `flock` doesn't serialize builds after a reboot | `/tmp/archie-locks/*.lock` gone | `mkdir -p /tmp/archie-locks && touch …/{gradle,npm,testenv}.lock` | [working-rules.md](working-rules.md#git-and-parallel-work) |
| Parallel subagents all stop mid-work | API usage limit (six agents, 2026-10-03) | ≤ 4 long agents; resume with `SendMessage` | [working-rules.md](working-rules.md#git-and-parallel-work) |
| Laptop disk at 98–100 % | AVD userdata + Gradle build outputs | Delete AVD overlays, `gradle clean` finished modules | [working-rules.md](working-rules.md#git-and-parallel-work) |
| Local server dies when the tool's shell exits | Not detached | `setsid … > logs/… 2>&1 &` | [working-rules.md](working-rules.md#processes-and-services) |
