package com.assistant.archie.system

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.core.app.NotificationCompat
import com.assistant.archie.R
import com.assistant.archie.shell.MainActivity
import com.assistant.core.model.LiveStatus
import com.assistant.core.model.PoolSession
import com.assistant.core.protocol.ServerFrame
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/*
 * "Agent session finished" notifications (spec 12 §3.7 `agent_turn_started/finished`, §8.2 device
 * setting "Notifications → Agent session finished").
 *
 * Every orchestrator socket is a pool watcher, so this device hears about every agent turn on the
 * server (any harness, started here, elsewhere or by the orchestrator). A finished turn becomes a
 * notification on the "Agent sessions" channel unless the switch is off, the turn was stopped
 * (`interrupted`) or the user is looking at that session (AN-1's rule). One notification per session
 * (tag), replaced by the newest; opening the session clears it. A tap focuses that session
 * (a user action, FOCUS-1).
 *
 * Background delivery: the socket only carries frames while the process runs and is not frozen.
 * [AgentWork] tracks which agent turns are in flight; while the switch is on and one is, the graph
 * holds the voice host's foreground service (VoiceHostRuntime.setAgentWorkHold), which keeps the
 * process and the socket alive until the turn ends. Android 12+ only lets that service *start* from
 * the foreground, so the hold covers turns that were running while Archie was open (the common
 * case: send, pocket the phone). Turns started elsewhere while Archie sat in the background reach
 * the phone only with "Stay connected in background", the wake word or a live voice call.
 *
 * Every decision logs one `ArchieNotify` line for hands-on checks.
 */

/** One "finished" notification. */
data class TurnNotice(val localId: String, val sdkId: String?, val title: String, val text: String, val error: Boolean) {
    val tag: String get() = "turn:$localId"
}

/** Where notices go (the system notification manager in production, a list in tests). */
interface TurnSink {
    fun post(notice: TurnNotice)
    fun cancel(tag: String)
}

/** The pure parts: notify or not, and what the notification says. */
object TurnAttention {
    const val FALLBACK_TITLE = "Agent session"

    enum class Skip { DISABLED, INTERRUPTED, LOOKING, NO_SESSION }

    sealed interface Decision {
        data class Post(val notice: TurnNotice) : Decision
        data class Suppress(val reason: Skip) : Decision
    }

    fun title(f: ServerFrame.AgentTurnFinished, known: String?): String =
        f.title?.trim()?.takeIf { it.isNotEmpty() } ?: known?.trim()?.takeIf { it.isNotEmpty() } ?: FALLBACK_TITLE

    fun text(f: ServerFrame.AgentTurnFinished): String {
        val preview = f.preview?.trim()?.takeIf { it.isNotEmpty() }
        if (f.status == ServerFrame.AgentTurnFinished.STATUS_ERROR) {
            return "Failed: ${f.error?.trim()?.takeIf { it.isNotEmpty() } ?: preview ?: "the turn ended with an error"}"
        }
        return preview ?: "Finished"
    }

    /** [lookingAt]: the agent `localId` the user is looking at right now (ApprovalNotifier.lookingAtFrom), or null. */
    fun decide(f: ServerFrame.AgentTurnFinished, enabled: Boolean, lookingAt: String?, knownTitle: String?): Decision {
        val localId = f.sessionId?.takeIf { it.isNotEmpty() } ?: return Decision.Suppress(Skip.NO_SESSION)
        if (!enabled) return Decision.Suppress(Skip.DISABLED)
        if (f.status == ServerFrame.AgentTurnFinished.STATUS_INTERRUPTED) return Decision.Suppress(Skip.INTERRUPTED)
        if (localId == lookingAt) return Decision.Suppress(Skip.LOOKING)
        return Decision.Post(
            TurnNotice(
                localId, f.sdkSessionId?.takeIf { it.isNotEmpty() }, title(f, knownTitle), text(f),
                error = f.status == ServerFrame.AgentTurnFinished.STATUS_ERROR,
            ),
        )
    }
}

/**
 * Which agent turns are in flight, from the watcher frames, reconciled with the live pool (frames
 * missed while the socket was down). Thread-safe; [busy] drives the background hold.
 */
class AgentWork(private val now: () -> Long = System::currentTimeMillis) {
    private val started = HashMap<String, Long>()
    /** When each session's last turn ended (finish or close): a pool read older than that is stale for it. */
    private val ended = HashMap<String, Long>()
    private val _busy = MutableStateFlow(0)
    /** How many agent turns are in flight (drives the background hold and its notification text). */
    val busy: StateFlow<Int> = _busy.asStateFlow()

    @Synchronized
    fun onFrame(f: ServerFrame) {
        when (f) {
            is ServerFrame.AgentTurnStarted -> f.sessionId?.let { started[it] = now() }
            is ServerFrame.AgentTurnFinished -> f.sessionId?.let { end(it) }
            is ServerFrame.AgentSessionClosed -> if (!f.isOrchestrator) f.sessionId?.let { end(it) }
            else -> return
        }
        publish()
    }

    private fun end(id: String) {
        started.remove(id)
        ended[id] = now()
    }

    /**
     * A `GET /api/sessions/pool/live` result whose request started at [requestedAt]: a session no
     * longer in the pool is not busy; one the pool reports idle is not busy unless its start is very
     * recent (the row may predate it); a busy row counts even if its start was missed, unless that
     * session's turn ended after the request started (a stale read racing the finish frame).
     * Entries older than [MAX_AGE_MS] go (a missed finish). Reads of unknown age (another caller's
     * sync) pass `now - RESULT_SLACK_MS`.
     */
    @Synchronized
    fun reconcile(pool: List<PoolSession>, requestedAt: Long = now() - RESULT_SLACK_MS) {
        val t = now()
        val rows = pool.filter { !it.isOrchestrator }.associateBy { it.localId }
        started.entries.removeAll { (id, at) ->
            val row = rows[id]
            row == null || t - at > MAX_AGE_MS || (!isBusy(row.status) && t - at > RECENT_START_MS)
        }
        rows.values.filter { isBusy(it.status) && (ended[it.localId] ?: Long.MIN_VALUE) < requestedAt }
            .forEach { started.putIfAbsent(it.localId, t) }
        ended.entries.removeAll { (_, at) -> t - at > MAX_AGE_MS }
        publish()
    }

    /** No pool answer (offline): at least age out missed finishes. */
    @Synchronized
    fun expire() {
        val t = now()
        started.entries.removeAll { (_, at) -> t - at > MAX_AGE_MS }
        publish()
    }

    @Synchronized
    fun inFlight(): Set<String> = started.keys.toSet()

    private fun publish() { _busy.value = started.size }

    companion object {
        /** A turn longer than this is assumed finished (its `agent_turn_finished` was missed). */
        const val MAX_AGE_MS = 2 * 60 * 60 * 1000L

        /** A start this recent survives an "idle" pool row read just before it. */
        const val RECENT_START_MS = 30_000L

        /** How old a pool read of unknown request time is assumed to be. */
        const val RESULT_SLACK_MS = 15_000L

        /** While turns are in flight the graph re-reads the pool this often (missed finishes, backend restarts). */
        const val RESYNC_MS = 3 * 60 * 1000L

        fun isBusy(s: LiveStatus?): Boolean = s == LiveStatus.STREAMING || s == LiveStatus.TOOL_USE || s == LiveStatus.THINKING
    }
}

/** Posts / clears the notices; [title] is what the app already knows for a `localId` (session list, open view). */
class TurnNotifier(
    private val sink: TurnSink,
    private val title: (localId: String, sdkId: String?) -> String?,
    private val log: (String) -> Unit = { Log.i(TAG, it) },
) {
    private val shown = HashSet<String>()

    @Synchronized
    fun onFinished(f: ServerFrame.AgentTurnFinished, enabled: Boolean, lookingAt: String?) {
        val id = f.sessionId.orEmpty()
        when (val d = TurnAttention.decide(f, enabled, lookingAt, id.takeIf { it.isNotEmpty() }?.let { title(it, f.sdkSessionId) })) {
            is TurnAttention.Decision.Suppress -> log("suppressed $id status=${f.status} reason=${d.reason.name.lowercase()}")
            is TurnAttention.Decision.Post -> {
                sink.post(d.notice)
                shown += d.notice.tag
                log("posted $id status=${f.status} title=\"${d.notice.title}\"")
            }
        }
    }

    /** The user opened [localId]: its notification has done its job. */
    @Synchronized
    fun onLooking(localId: String?) {
        localId ?: return
        val tag = "turn:$localId"
        if (shown.remove(tag)) {
            sink.cancel(tag)
            log("cleared $localId (opened)")
        }
    }

    companion object {
        const val TAG = "ArchieNotify"
    }
}

/** The "Agent sessions" channel: high importance (heads-up; the feature is opt-in), private on the lock screen. */
class SystemTurnSink(private val app: Application) : TurnSink {
    private val nm = app.getSystemService(NotificationManager::class.java)

    private fun ensureChannel() {
        nm?.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Agent sessions", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "An agent session finished its work"
            },
        )
    }

    override fun post(notice: TurnNotice) {
        nm ?: return
        ensureChannel()
        val n = NotificationCompat.Builder(app, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_archie)
            .setContentTitle(notice.title)
            .setContentText(notice.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(notice.text))
            .setCategory(if (notice.error) NotificationCompat.CATEGORY_ERROR else NotificationCompat.CATEGORY_STATUS)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(openIntent(notice))
            .build()
        try {
            nm.notify(notice.tag, NOTIFICATION_ID, n)
        } catch (e: SecurityException) {
            Log.w(TurnNotifier.TAG, "notification not allowed: ${e.message}")  // POST_NOTIFICATIONS denied
        }
    }

    override fun cancel(tag: String) {
        nm?.cancel(tag, NOTIFICATION_ID)
    }

    /** A tap opens MainActivity with the session to focus (the same path as an approval tap). */
    private fun openIntent(n: TurnNotice): PendingIntent = PendingIntent.getActivity(
        app, 0,
        Intent(app, MainActivity::class.java)
            .setData(Uri.parse("archie-turn://open/${Uri.encode(n.localId)}"))
            .putExtra(SystemApprovalSink.EXTRA_OPEN_AGENT, n.localId)
            .putExtra(EXTRA_OPEN_AGENT_SDK, n.sdkId)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    companion object {
        const val CHANNEL_ID = "agent_turns"
        const val NOTIFICATION_ID = 1004

        /** With [SystemApprovalSink.EXTRA_OPEN_AGENT]: the session's sdk id, to reopen it once it left the pool. */
        const val EXTRA_OPEN_AGENT_SDK = "com.assistant.archie.extra.OPEN_AGENT_SDK"
    }
}
