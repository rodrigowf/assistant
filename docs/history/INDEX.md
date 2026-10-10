# history/

How Archie got here: a dated timeline, and historical records kept verbatim (handoffs, plans and
execution logs of past work). Records describe the system **as it was** when written — paths and
names in them may be stale; the current state is in the topic docs. For the 2026-10 web/Android
rebuild see [projects/frontend-refactor](../projects/frontend-refactor/README.md); for the 2026-10-06 search
rebuild see [projects/history-search](../projects/history-search/RESULTS.md).

- [timeline.md](timeline.md) — **Start here**: dated milestones and incidents, February–October 2026, with commits.
- [HANDOFF-2026-10-10.md](HANDOFF-2026-10-10.md) — Notifications, floating voice controls, Accounts + `.env` manager, live reload + in-app links + canvas skill, the API security fixes and the context-sync rework: where each lives, decisions, verification, open items.
- [HANDOFF-2026-08-25.md](HANDOFF-2026-08-25.md) — Session-crash + model fixes: SDK 0.1.39 → 0.1.81 and the retired Sonnet 4 model id, and how to apply them to another install.
- [PLAN-sync-wakeword-fixes.md](PLAN-sync-wakeword-fixes.md) — Plan for three old-app bugs: the wake word stopping after screen lock / service restart, and the Android and web clients drifting out of sync with the server (tabs, orchestrator conversation).
- [wakeword-vosk-migration-2026-06/plan.md](wakeword-vosk-migration-2026-06/plan.md) — The June 2026 plan to replace Android SpeechRecognizer with on-device Vosk in the old app's wake-word path (increments V1–V6).
- [wakeword-vosk-migration-2026-06/log.md](wakeword-vosk-migration-2026-06/log.md) — Its execution log: one entry per shipped increment with commit, on-device verification and deviations.
