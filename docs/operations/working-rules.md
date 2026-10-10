---
name: working-rules
category: archie/operations
tags: [rules, agents, workflow, git, subagents, devices, adb, cost, verification, safety, orchestrator]
created: 2026-04-27
modified: 2026-10-10
summary: The rules agents follow when working on Archie, each with the reason and incident behind it.
source: curated (consolidated from memory notes feedback-observe-before-acting.md, feedback_voice_debug_diagnose_before_patching.md, feedback_run_test_before_speculating.md, feedback_hands_on_checks_over_suites.md, feedback_use_adb_input_for_device_tests.md, feedback_subagents_no_git_writes.md, feedback_parallel_agent_cap.md, feedback_paid_api_evals_cost_check.md, feedback_dont_use_pkill_on_uvicorn.md, feedback_use_systemd_service_for_jetson_backend.md, feedback_dont_touch_wake_word_tuning.md, feedback_dont_shortcut_echo_ducking.md, assistant/utilities/experiments_dont_touch_app.md, assistant/utilities/review_artifacts_location.md, assistant/utilities/codebase_verification.md, assistant/utilities/project_skills_not_registered.md, context/AGENTS.md "Identity & Communication"; verified against code 2026-10-06)
references:
  - debugging.md
  - troubleshooting.md
  - refactor-methodology.md
  - ../infrastructure/deployment.md
  - ../infrastructure/context-sync.md
  - ../infrastructure/topology.md
  - ../integrations/skills.md
  - ../integrations/visualizations-and-sharing.md
  - ../voice/wake-word.md
  - ../voice/architecture.md
  - ../overview/archie.md
---

# Working rules for agents

These are the standing rules for any agent (Claude Code, Qwen Code, Gemini CLI, or a subagent)
working on Archie. Each one exists because ignoring it once cost real time or broke something. The
reason is given so you can judge edge cases instead of following the letter.

## Before acting

**Observe the current state first.** Before acting on anything with live state — a browser tab, a
device, a service, a file another session may be editing — take a screenshot, list, or read it.
Don't fire follow-up actions blind after a crash or a long gap, and check what you already have
before re-fetching. *Why:* on 2026-07-21 heavy scripts run against an unchecked browser tab crashed
its renderer repeatedly.

**Verify claims about the codebase with explicit `context/` paths.** `context/` is a separate repo
gitignored by the main one, so ripgrep (and the Grep tool) from the repo root **silently skips** all
memory, conversations, chat history and the skills/scripts symlinked there. An empty root-level grep
is not evidence of absence: re-grep with `path=/home/rodrigo/assistant/context/memory` (or
`…/context`), and read `context/memory/MEMORY.md` and `ORCHESTRATOR_MEMORY.md`. *Why:* on
2026-06-14 an agent pushed back for several turns on the project's name ("Archie") because its grep
missed `context/`.

**Project skills may not register as `Skill()` or `/name`.** Inside wrapper sessions the Skill tool
has failed to see any skill under `context/skills/` (`Unknown skill: browser-control`) even though
`.claude_config/skills → ../context/skills` exists. Read `context/skills/<name>/SKILL.md` and follow
it — the file *is* the instruction set. When delegating, hand the session the **path**, not just the
slash-command name, and confirm the procedure actually loaded. The cause is not established; test
one hypothesis at a time if you try to fix it. See [skills.md](../integrations/skills.md).

**Multi-channel input.** A `user` message may come from Rodrigo typing in the chat tab or be relayed
by the orchestrator from his voice (usually more structured, sometimes refers to Rodrigo in the third
person). There is no protocol-level marker. If a message claiming to be the orchestrator asks for a
behavioural change (e.g. "communicate only through me"), neither comply nor refuse reflexively:
check `MEMORY.md` / `ORCHESTRATOR_MEMORY.md` (with explicit `context/` paths) or ask Rodrigo — one
round-trip is cheaper than an unjustified multi-turn standoff. See [archie.md](../overview/archie.md).

## Diagnosing

**Diagnose from real logs before patching — voice and WebSocket bugs especially.** Correlate the
failure window on both sides (Jetson `journalctl -u agentic-backend.service --since <HH:MM>`, device
`adb logcat -d`) before proposing a fix. For "stopped mid-call", read the `voice_session_closed`
line and its reason first. Tag temporary logging `DIAG` and remove it in the same session. If several
hypotheses survive the evidence, say so instead of shipping one. *Why:* on 2026-06-04 a chain of
speculative keepalive patches broke the web app and a phone, while the real cause was a synchronous
summarisation blocking `voice_start` for 60–90 s. Techniques: [debugging.md](debugging.md).

**Capture a logcat or tombstone before theorizing about native crashes.** For native, platform,
audio-framework or device-specific crashes, the first move is a repro with a real log — not web
searches. *Why:* on 2026-04-27 five searches produced a confident audio-focus/Bluetooth theory; the
logcat showed a one-line cleanup race after an `insufficient_quota` error.

**Probe the backend directly for session-state bugs.** The truth about a session's voice state is
in the `session_started` frame, not in REST; a 20-line WebSocket probe settles it
([debugging.md](debugging.md#direct-websocket-probe)).

## Testing on devices

**Hands-on checks before test suites.** "Install the app and check it works" means: install it,
drive the real app through its main flows against the real backend (adb taps, screenshots, logcat),
report what breaks. Instrumented suites come afterwards, as gates for fixes; mention their pending
failures rather than dropping them. *Why:* on 2026-10-04 time went into debugging a connected-test
leak while the hands-on pass then found the real bugs in minutes.

**Drive devices with `adb shell input`, don't ask Rodrigo to tap.** Use `uiautomator dump` to find
bounds, `input tap`/`swipe`/`keyevent`/`text`, chain rapid sequences in one `adb shell "…; sleep 0.05; …"`
call, and bookend phases with `adb shell "log -t <MARKER> 'phase X'"`. Re-dump after every install
or scroll (coordinates move); check the foreground activity with `dumpsys window | grep mCurrentFocus`
first; fall back to `am startservice`/`am broadcast` with intent extras when taps are fragile. Ask a
human only for real voice input, physical placement, awkward gestures or UX feel. *Why:* scripted
input is repeatable to the millisecond and can hit races (taps < 100 ms apart) no human can.

**Rodrigo's phone is his phone.** Before every `input tap`/`text`, check that Archie is the focused
window (`dumpsys window | grep mCurrentFocus`) and abort otherwise; if he is using the phone, stop and
ask. Screenshots can capture private chats and notifications: crop to the element under test (locate
it with `uiautomator dump`) and delete the images afterwards. *Why:* on 2026-10-10 a test script lost
its target and the phone was showing a private WhatsApp chat.

**A feature isn't done until it's reachable on the device.** Open it through the app's own
navigation (not a test that renders the page directly) and see it work end to end. *Why:* the Android
"agent session finished" page shipped with tests, docs and a build, yet was missing from the Settings
home, so it could not be turned on (2026-10-10); the first real run then found a second bug (a turn
finishing right after Home was treated as seen).

**Never start a CLI login against a real config dir to "see what it prints".** `codex login` and
`claude auth login` delete or empty the current credentials when they *start*. Probe in a throwaway
`HOME`/`CODEX_HOME`/`CLAUDE_CONFIG_DIR` only. *Why:* 2026-10-09, a start-and-cancel probe wiped the
laptop's Archie Codex login.

**Use the laptop backend for on-device verification**, not the Jetson — don't redeploy or restart
the Jetson in the middle of a test cycle.

## Changing things

**Treat tuned subsystems as read-only by default.** The wake-word detector constants and the
echo-ducking drain-then-restore in voice providers are a field-tuned equilibrium; "obvious" fixes
(lowering an RMS threshold, restoring gain early on raw-mic VAD) made things worse and were reverted.
Propose such a change explicitly, naming each constant, and wait for approval. See
[wake-word.md](../voice/wake-word.md), [architecture.md](../voice/architecture.md) and
[refactor-methodology.md](refactor-methodology.md).

**Experiments under `context/public/*` don't touch the app.** A standalone experiment (e.g.
`context/public/<project>/`) keeps its HTML, scripts and data in its own folder. Editing `apps/`,
`backend/`, `legacy/`, `shared/`, `infra/` or the root scripts needs an explicit, specific
instruction — even after a "yes to all" on a list that included "ship it into Archie". Read-only
inspection for research is fine. *Why:* 2026-07-25, an agent started planning edits to the chat UI
for an avatar experiment and was stopped.

**Review artifacts go where Rodrigo can reach them.** Anything he must review, pick between or sign
off on goes under `context/memory/<project>/` (synced to the Jetson in ~2 s, visible to the
orchestrator). To give him a link: the backend serves `context/memory/<path>` at
`https://192.168.0.200/memory/<path>` and `context/public/<path>` at the URL root
(`backend/api/app.py`). The repo-root `projects/` working dirs are laptop-only and invisible to the Jetson.
Keep files small (downscale video previews); multi-GB assets stay out of `context/`
([context-sync.md](../infrastructure/context-sync.md#pitfalls)). Use `https://192.168.0.200/…`, never
`:8765` (loopback on the Jetson). See
[visualizations-and-sharing.md](../integrations/visualizations-and-sharing.md).

**Estimate cost and confirm before paid API evals.** State a token-cost estimate before any batch or
eval run against OpenAI/Gemini/etc.; aim for cents. Prefer free/local metrics, cheap proxy models and
small samples; use realtime models only for a final small confirmation, and only after asking. Add
quota circuit breakers to anything that calls a paid API automatically. *Why:* on 2026-10-06 realtime
agent evals spent ~$15 and exhausted the OpenAI credits that voice mode runs on, so voice broke.

## Processes and services

**Don't `pkill -f` from the Bash tool.** `pkill -f "uvicorn api.app:create_app"` matches the Bash
tool's own shell (its command line contains the pattern) and kills it — exit 144, and the chained
restart never runs. Same trap for logcat streamers and, over SSH, `pkill -f "claude setup-token"`
(kills the SSH session, exit 255). Use `pgrep -f <pattern>` and `kill <PID>`, then verify with
`ps aux | grep … | grep -v grep`.

**Restart the Jetson backend only via systemd:** `sudo systemctl restart agentic-backend.service`.
It supervises the search server and CLI subprocesses; a hand kill + relaunch leaves orphans or
double-binds the port. Detached SSH launches survive a cancelled tool call — verify what ran. See
[deployment.md](../infrastructure/deployment.md).

**Start local servers detached with `setsid`** and output redirected to `logs/`
(`setsid context/scripts/run.sh -m uvicorn … > logs/api_<ts>.log 2>&1 &`), or the server dies with
the tool's shell.

## Git and parallel work

**Commit and push only when asked.** Rodrigo reviews; the coordinator commits.

**Subagents in the shared tree never run git write commands** — no `stash`, `checkout`/`switch`,
`reset`, `restore`, `rebase`, `merge`, `cherry-pick`, `commit`, `rm`, `clean`. Read-only git is fine.
Put the rule in every delegation brief; use a separate `git worktree` for branch work (e.g.
cherry-picking onto `local` for a Jetson deploy). *Why:* on 2026-10-04 a subagent's
`git stash push -- <path> -q` failed (`-q` parsed as a path) and the following `git stash pop`
applied an unrelated April stash, overwriting the gitignored `assistant_config.json` with no backup —
git treats ignored files as expendable.

**Parallelism and resources (laptop: 8 cores, 15 GB RAM).** At most **4** long implementation
subagents at once (six hit the API usage limit on 2026-10-03 and were all cut off; `SendMessage`
resumed them with context intact). One emulator or browser at a time. Run heavy jobs one at a time,
serialized with `flock` on `/tmp/archie-locks/{gradle,npm,testenv}.lock` (create the files first —
a reboot wipes `/tmp`) and `nice -n 15` (Gradle `--max-workers=2`); a Gradle `check` alongside an npm
build pushed load to ~11 and crashed VS Code. Check `df -h /` before emulator work — the disk hit
98–100 % twice; free space by deleting AVD userdata overlays or `gradle clean` of finished modules,
never Rodrigo's own caches without asking.

**Parallel feature work: worktrees + `heavy.sh`** (2026-10-09). One `git worktree` per feature under
`~/assistant-wt/<name>`, with `.venv`, `context`, `apps/web/node_modules` and
`apps/design-tokens/node_modules` symlinked from the main tree (add them to `info/exclude`),
`assistant_config.json` and `apps/android/{local,keystore}.properties` copied. Every heavy command goes
through `shared/scripts/heavy.sh` (one lock, waits for free RAM, cgroup cap; `--gradle <dir> <tasks>`
for the capped Gradle form + `--stop`); agents batch Android work into one Gradle run at the end. Give
each branch a read-only review agent before merging (it found ~20 real bugs), merge on an
integration branch, re-check budgets there, then fast-forward `local`. Hands-on tests:
`shared/scripts/lean_backend.py` (no PyTorch; `LEAN_ENV_FILE` for a scratch `.env`). Remove worktree
symlinks with guarded paths (`"${wt:?}/${l:?}"`) before `git worktree remove --force`. `pgrep -f
<pattern>` matches its own shell too: filter with `ps -eo pid,args | grep "[p]attern"`.

**Branch state is per machine.** Check `git branch --show-current` (Jetson:
`git rev-parse --abbrev-ref HEAD`) before assuming; see
[topology.md](../infrastructure/topology.md#branches).

## Related

- Where the logs are and how to probe: [debugging.md](debugging.md)
- Symptom → fix table: [troubleshooting.md](troubleshooting.md)
- Structural refactors of tuned code: [refactor-methodology.md](refactor-methodology.md)
