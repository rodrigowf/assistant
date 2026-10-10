package com.assistant.archie.feature.sessions

import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.TabStatus
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.model.SessionSummary
import com.assistant.core.network.ApiResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * The session flows of the main app (B-06), mirroring the web's `session-actions` (W-11) and the
 * shell actions (W-07):
 *
 * - **Every Archie entry point** goes through [requestNewArchie] / [requestResumeArchie] (spec 12
 *   §6.11, inv02 F-25): with Archie running anywhere, the three-action dialog asks (Open the running
 *   one / Stop it and start new — or resume this one / Cancel); otherwise a fresh `start`. A start
 *   that still loses the race (`orchestrator_active`) drops the failed view (it never existed
 *   server-side, so nothing is closed) and shows the same dialog for the one that won.
 * - **New agent session** (§6.10) — missing on the old Android app (inv03 §1.5, §5).
 * - Rename / duplicate / fork / close / delete with confirmations, a busy overlay (inv02 F-12) and a
 *   snackbar; actions that need the sdk id say "available after the first reply" (ID-3).
 * - P-1: closing happens only from an explicit user close, delete, or "Stop it and …".
 */
class SessionsController(
    private val backend: SessionsBackend,
    private val scope: CoroutineScope,
    private val now: () -> Long = System::currentTimeMillis,
    private val startWatchMs: Long = START_WATCH_MS,
    private val snackShortMs: Long = SNACK_SHORT_MS,
    private val snackLongMs: Long = SNACK_LONG_MS,
) {
    private val _state = MutableStateFlow(SessionsUiState())
    val state: StateFlow<SessionsUiState> = _state.asStateFlow()

    private val snackActions = java.util.concurrent.ConcurrentHashMap<Long, () -> Unit>()
    private var snackSeq = 0L

    /** The start this device just asked for (§6.11 race watch). */
    private data class Watch(val mode: ConflictMode, val resumeSdkId: String?, val until: Long)

    @Volatile private var watch: Watch? = null

    init {
        scope.launch { backend.conflicts.collect { onConflict() } }
        scope.launch { backend.notices.collect { snack(it) } }
    }

    fun onIntent(i: SessionsIntent) {
        when (i) {
            SessionsIntent.NewArchie -> requestNewArchie()
            SessionsIntent.NewAgent -> backend.newAgent()
            is SessionsIntent.OpenHistory -> openFromHistory(i.session)
            is SessionsIntent.RequestClose -> requestClose(i.item)
            SessionsIntent.ConfirmClose -> confirmClose()
            is SessionsIntent.RequestRename -> requestRename(i.target)
            is SessionsIntent.CommitRename -> commitRename(i.title)
            is SessionsIntent.RequestDelete -> requestDelete(i.target)
            SessionsIntent.ConfirmDelete -> confirmDelete()
            is SessionsIntent.RequestFork -> requestFork(i.target)
            SessionsIntent.ConfirmFork -> confirmFork()
            is SessionsIntent.Duplicate -> duplicate(i.target)
            is SessionsIntent.Compact -> backend.compact(i.key)
            is SessionsIntent.ResolveConflict -> resolveConflict(i.choice)
            SessionsIntent.DismissDialog -> setDialog(null)
            is SessionsIntent.SnackAction -> {
                val run = snackActions.remove(i.id)
                clearSnack(i.id)
                run?.invoke()
            }
            is SessionsIntent.SnackDismissed -> {
                snackActions.remove(i.id)
                clearSnack(i.id)
            }
        }
    }

    // ───────────────────────── Archie (§6.11) ─────────────────────────

    /** New Archie conversation (switcher, ＋ New, empty workspace, drawer). */
    fun requestNewArchie() {
        scope.launch {
            val running = runningArchie()
            if (running != null) {
                setDialog(SessionDialog.ArchieConflict(ConflictMode.NEW, null, running))
                return@launch
            }
            watchStart(ConflictMode.NEW, null)
            backend.newArchie()
        }
    }

    /**
     * A past Archie conversation: the running one already is that conversation → open it; another
     * one runs → the dialog; nothing runs → `start{local_id: uuid(), resume_sdk_id}`.
     */
    fun requestResumeArchie(sdkId: String) {
        scope.launch {
            val running = runningArchie()
            when {
                running != null && running.sdkId == sdkId -> openRunning(running)
                running != null -> setDialog(SessionDialog.ArchieConflict(ConflictMode.RESUME, sdkId, running))
                else -> {
                    watchStart(ConflictMode.RESUME, sdkId)
                    backend.resumeArchie(sdkId, null)
                }
            }
        }
    }

    fun openFromHistory(s: SessionSummary) {
        if (s.isOrchestrator) requestResumeArchie(s.sdkId) else backend.openAgent(s)
    }

    private fun resolveConflict(choice: ConflictChoice) {
        val c = _state.value.dialog as? SessionDialog.ArchieConflict ?: return
        setDialog(null)
        when (choice) {
            ConflictChoice.CANCEL -> Unit
            ConflictChoice.OPEN -> openRunning(c.running)
            ConflictChoice.REPLACE -> scope.launch {
                withBusy(if (c.mode == ConflictMode.NEW) "Starting Archie…" else "Resuming…") {
                    // P-1: the user chose to stop it, so it closes for every device. The running one
                    // may live only on another device: its pool entry is closed explicitly.
                    if (c.running.here) backend.closeArchieHere() else backend.closePoolSession(c.running.localId)
                    watchStart(c.mode, c.resumeSdkId)
                    val sdk = c.resumeSdkId
                    if (c.mode == ConflictMode.RESUME && sdk != null) backend.resumeArchie(sdk, null) else backend.newArchie()
                }
            }
        }
    }

    /** Attach to the running orchestrator (G-15): its own pool key, so the server reattaches. */
    private fun openRunning(r: RunningArchie) {
        if (r.here) backend.focusArchie() else backend.resumeArchie(r.sdkId ?: r.localId, r.localId)
    }

    /** The running orchestrator: this device's live Archie view, else the pool's row, else the last known ref (offline). */
    private suspend fun runningArchie(): RunningArchie? {
        backend.liveArchieHere()?.let { return it }
        val pool = backend.syncPool()
        if (pool != null) return pool.firstOrNull { it.isOrchestrator }?.let { RunningArchie(it.localId, it.sdkId, here = false) }
        return backend.lastKnownOrchestrator()
    }

    private fun watchStart(mode: ConflictMode, resumeSdkId: String?) {
        watch = Watch(mode, resumeSdkId, now() + startWatchMs)
    }

    /**
     * `orchestrator_active` after a user start: drop the failed view (no close — it never existed
     * server-side) and ask about the orchestrator that won the race.
     */
    private suspend fun onConflict() {
        val w = watch?.takeIf { it.until >= now() }
        watch = null
        val failed = backend.archieViewIds()
        backend.dropArchieView()
        val mode = w?.mode ?: if (failed?.second != null) ConflictMode.RESUME else ConflictMode.NEW
        val resumeSdk = if (mode == ConflictMode.RESUME) (w?.resumeSdkId ?: failed?.second) else null
        val row = backend.syncPool()?.firstOrNull { it.isOrchestrator && it.localId != failed?.first }
        val running = row?.let { RunningArchie(it.localId, it.sdkId, here = false) }
            ?: backend.lastKnownOrchestrator()?.takeIf { it.localId != failed?.first }
        if (running != null) setDialog(SessionDialog.ArchieConflict(mode, resumeSdk, running))
        else snack("Archie is already running on another device. Try again.", error = true)
    }

    // ───────────────────────── close (§6.7) ─────────────────────────

    /** ×, swipe, the ⋮ menu's Close. Archie always asks; a working agent asks; the rest closes now. */
    fun requestClose(item: WorkspaceItem) {
        val archie = item.kind == ItemKind.ARCHIE
        val asks = archie || (item.kind == ItemKind.AGENT && item.status == TabStatus.WORKING)
        if (asks) setDialog(SessionDialog.Close(item.key, SessionTitles.conversationTitle(item.title, archie), archie))
        else scope.launch { backend.close(item.key) }
    }

    private fun confirmClose() {
        val d = _state.value.dialog as? SessionDialog.Close ?: return
        setDialog(null)
        scope.launch { backend.close(d.key) }
    }

    // ───────────────────────── rename (§6.6) ─────────────────────────

    private fun requestRename(t: SessionTarget) {
        if (t.sdkId == null) { snack("Rename is ${NEEDS_FIRST_REPLY.lowercase()}"); return }
        val initial = if (t.isArchie && t.title == NEW_CONVERSATION) "" else t.title
        setDialog(SessionDialog.Rename(t, initial))
    }

    private fun commitRename(title: String) {
        val d = _state.value.dialog as? SessionDialog.Rename ?: return
        val sdk = d.target.sdkId ?: return
        val t = title.trim()
        if (t.isEmpty() || t == d.initial.trim()) { setDialog(null); return }
        val previous = backend.storedTitle(sdk)?.takeIf { it.isNotBlank() }
        setDialog(d.copy(saving = true))
        scope.launch {
            when (val r = backend.rename(sdk, t)) {
                is ApiResult.Ok -> {
                    setDialog(null)
                    // Undo where the backend allows it: a rename is undone by renaming back.
                    if (previous != null && previous != t) {
                        snack("Renamed to “$t”", "Undo") { scope.launch { backend.rename(sdk, previous) } }
                    } else {
                        snack("Renamed to “$t”")
                    }
                }
                else -> {
                    setDialog(d.copy(saving = false))   // keep the dialog open: the user may retry
                    snack(r.errorMessage() ?: "Rename failed", error = true)
                }
            }
        }
    }

    // ───────────────────────── duplicate / fork (§6.5, §6.8) ─────────────────────────

    /** `POST …/duplicate`: does not open the copy (web parity); the snackbar offers it. */
    private fun duplicate(t: SessionTarget) {
        val sdk = t.sdkId ?: run { snack("Duplicate is ${NEEDS_FIRST_REPLY.lowercase()}"); return }
        scope.launch {
            when (val r = withBusy("Duplicating…") { backend.duplicate(sdk) }) {
                is ApiResult.Ok -> snack("Duplicated “${t.title}”", "Open") { openCopy(t, r.value) }
                else -> snack(r.errorMessage() ?: "Duplicate failed", error = true)
            }
        }
    }

    private fun requestFork(t: SessionTarget) {
        if (t.sdkId == null) { snack("Fork is ${NEEDS_FIRST_REPLY.lowercase()}"); return }
        setDialog(SessionDialog.Fork(t))
    }

    /**
     * The ⋮ menu's Fork: a copy of the whole conversation. An agent copy opens focused; an Archie
     * copy is offered from the snackbar, because opening it goes through §6.11 (it would stop the
     * running Archie, which the conflict dialog asks about).
     */
    private fun confirmFork() {
        val d = _state.value.dialog as? SessionDialog.Fork ?: return
        setDialog(null)
        val t = d.target
        val sdk = t.sdkId ?: return
        scope.launch {
            when (val r = withBusy("Forking…") { backend.fork(sdk) }) {
                is ApiResult.Ok -> if (t.isArchie) {
                    snack("Forked “${t.title}”", "Open") { openCopy(t, r.value) }
                } else {
                    backend.openAgent(r.value, t.provider)
                }
                else -> snack(r.errorMessage() ?: "Fork failed", error = true)
            }
        }
    }

    private fun openCopy(t: SessionTarget, sdkId: String) {
        if (t.isArchie) requestResumeArchie(sdkId) else backend.openAgent(sdkId, t.provider)
    }

    // ───────────────────────── delete (§6.8) ─────────────────────────

    private fun requestDelete(t: SessionTarget) {
        val sdk = t.sdkId ?: run { snack("Delete is ${NEEDS_FIRST_REPLY.lowercase()}"); return }
        val count = t.messageCount ?: backend.sessions.value.value?.firstOrNull { it.sdkId == sdk }?.messageCount
        val live = t.key != null || backend.isLive(sdk)
        setDialog(SessionDialog.Delete(t, count, stopsLive = live))
    }

    /** Every open view and the pool entry close **before** `DELETE` (the CLI stops writing the file). */
    private fun confirmDelete() {
        val d = _state.value.dialog as? SessionDialog.Delete ?: return
        setDialog(null)
        val sdk = d.target.sdkId ?: return
        scope.launch {
            when (val r = withBusy("Deleting…") { backend.delete(sdk, d.target.localId) }) {
                is ApiResult.Ok -> snack("Deleted “${d.target.title}”")
                else -> snack(r.errorMessage() ?: "Delete failed", error = true)
            }
        }
    }

    // ───────────────────────── state helpers ─────────────────────────

    private fun setDialog(d: SessionDialog?) = _state.update { it.copy(dialog = d) }

    private fun snack(message: String, actionLabel: String? = null, error: Boolean = false, action: (() -> Unit)? = null) {
        val id = ++snackSeq
        _state.value.snack?.let { snackActions.remove(it.id) }
        if (action != null) snackActions[id] = action
        _state.update { it.copy(snack = Snack(id, message, actionLabel.takeIf { action != null }, error)) }
        scope.launch {
            delay(if (action != null || error) snackLongMs else snackShortMs)
            snackActions.remove(id)
            clearSnack(id)
        }
    }

    private fun clearSnack(id: Long) = _state.update { if (it.snack?.id == id) it.copy(snack = null) else it }

    private suspend fun <T> withBusy(label: String, block: suspend () -> T): T {
        _state.update { it.copy(busy = label) }
        try {
            return block()
        } finally {
            _state.update { it.copy(busy = null) }
        }
    }

    companion object {
        /** How long a start is watched for `orchestrator_active` (web `START_WATCH_MS`). */
        const val START_WATCH_MS = 15_000L
        const val SNACK_SHORT_MS = 4_000L
        const val SNACK_LONG_MS = 10_000L
    }
}
