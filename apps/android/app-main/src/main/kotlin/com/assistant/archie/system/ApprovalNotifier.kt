package com.assistant.archie.system

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.core.app.NotificationCompat
import com.assistant.archie.R
import com.assistant.archie.graph.GraphOwner
import com.assistant.archie.shell.MainActivity
import com.assistant.core.conversation.AssistantEntry
import com.assistant.core.conversation.ConversationState
import com.assistant.core.conversation.PermissionBlock
import com.assistant.core.conversation.PermissionState
import com.assistant.core.data.ApprovalAnswer
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ConversationRepository
import com.assistant.core.data.HistoryRepository
import com.assistant.core.data.ItemKey
import com.assistant.core.data.OpenSessionsRepository
import com.assistant.core.model.SessionKind
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/*
 * OI-2 + OI-6: agent approvals from anywhere (spec 12 §6.9, AN-1..AN-4).
 *
 * A pending agent permission this device knows about (the orchestrator mirror's `agentApprovals`,
 * PM-5, or a `permission_request` on an open agent view) becomes a heads-up notification on the
 * "Approvals" channel unless the user is looking at that agent's view. Approve / Deny answer over
 * REST (`POST /api/sessions/{localId}/permission`), so no socket is needed to answer. The
 * notification goes away when the request leaves `pending` (resolved by anyone, end of turn) or
 * when the user opens that view.
 *
 * Limitation: the device only *learns* about a request over a live socket. Backgrounded with the
 * orchestrator socket closed (T-14: no voice owner, no wake word, no "Stay connected"), or with
 * no Archie conversation subscribed and no agent view open, nothing arrives and nothing is posted.
 */

/** One pending agent permission, keyed by `(localId, requestId)`. */
data class PendingApproval(val localId: String, val requestId: String, val toolName: String, val toolInput: JsonObject) {
    val tag: String get() = "$localId:$requestId"
}

/** What one approval notification shows. Carried in the action intents so an answer works after a process restart. */
data class ApprovalNotice(
    val localId: String,
    val requestId: String,
    val title: String,
    val text: String,
    val detail: String?,
    /** Set when an answer from the notification failed (shown instead of [text]). */
    val error: String? = null,
) {
    val tag: String get() = "$localId:$requestId"
}

/** Where notices go (the system notification manager in production, a list in tests). */
interface ApprovalSink {
    fun post(notice: ApprovalNotice)
    fun cancel(tag: String)
}

/** The pure parts: what is pending, whether the user is looking, what the notification says. */
object ApprovalAttention {
    const val PLAN_TEXT = "Plan ready for approval"
    private const val DETAIL_MAX = 600

    /**
     * AN-1: the user is looking at [agentLocalId] only when the app is in the foreground (not
     * backgrounded, screen on), the workspace is the top screen (no settings / history in front)
     * and that agent's view is the selected one.
     */
    fun isLooking(foreground: Boolean, workspaceOnTop: Boolean, activeLocalId: String?, agentLocalId: String): Boolean =
        foreground && workspaceOnTop && activeLocalId == agentLocalId

    /** Every pending agent permission in [states]: Archie's `agentApprovals` plus pending blocks of agent views. */
    fun pendingOf(states: List<ConversationState>): List<PendingApproval> {
        val out = LinkedHashMap<String, PendingApproval>()
        for (s in states) {
            if (s.kind == SessionKind.ORCHESTRATOR) {
                s.agentApprovals.forEach { a -> PendingApproval(a.localId, a.requestId, a.toolName, a.toolInput).let { out.putIfAbsent(it.tag, it) } }
            } else {
                for (e in s.entries) {
                    val ae = e as? AssistantEntry ?: continue
                    for (b in ae.blocks) {
                        if (b is PermissionBlock && b.state == PermissionState.PENDING) {
                            PendingApproval(s.ref.localId, b.requestId, b.toolName, b.toolInput).let { out.putIfAbsent(it.tag, it) }
                        }
                    }
                }
            }
        }
        return out.values.toList()
    }

    /** `(text, detail)`: `ExitPlanMode` → "Plan ready for approval" + the start of the plan; else the tool and its main input. */
    fun textOf(toolName: String, input: JsonObject): Pair<String, String?> {
        fun str(k: String) = (input[k] as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf { it.isNotBlank() }
        if (toolName == "ExitPlanMode") return PLAN_TEXT to str("plan")?.let(::clip)
        val detail = listOf("description", "command", "file_path", "url", "pattern", "query", "prompt").firstNotNullOfOrNull(::str)
        return "Wants to use $toolName" to detail?.let(::clip)
    }

    private fun clip(s: String): String = s.trim().let { if (it.length <= DETAIL_MAX) it else it.take(DETAIL_MAX).trimEnd() + "…" }
}

/**
 * AN-1..AN-4 bookkeeping over two inputs: what is [pending] and which agent the user [lookingAt]
 * (its pool `localId`, or null). [answerer] answers over REST (never a possibly half-dead socket).
 */
class ApprovalNotifier(
    private val sink: ApprovalSink,
    private val scope: CoroutineScope,
    private val title: (localId: String) -> String,
    private val answerer: suspend (localId: String, requestId: String, allow: Boolean) -> ApprovalAnswer,
) {
    private val mutex = Mutex()

    /** Tags currently shown. */
    private val shown = HashSet<String>()

    /** Tags notified or seen in their view: never posted (again) while they stay pending. */
    private val handled = HashSet<String>()

    fun attach(pending: Flow<List<PendingApproval>>, lookingAt: Flow<String?>) {
        combine(pending, lookingAt.distinctUntilChanged()) { p, l -> p to l }
            .onEach { (p, l) -> reconcile(p, l) }
            .launchIn(scope)
    }

    suspend fun reconcile(pending: List<PendingApproval>, lookingAt: String?) = mutex.withLock {
        val tags = pending.mapTo(HashSet()) { it.tag }
        shown.filter { it !in tags }.forEach { sink.cancel(it); shown -= it }        // resolved / expired (AN-3)
        handled.retainAll(tags)
        for (a in pending) {
            if (a.localId == lookingAt) {                                              // the in-app bar shows it (AN-1)
                handled += a.tag
                if (shown.remove(a.tag)) sink.cancel(a.tag)
            } else if (a.tag !in handled) {
                handled += a.tag
                shown += a.tag
                sink.post(noticeOf(a))
            }
        }
    }

    /** A notification action (AN-2): answer over REST; on failure keep the notification with the error. */
    suspend fun answer(notice: ApprovalNotice, allow: Boolean): ApprovalAnswer {
        val r = try {
            answerer(notice.localId, notice.requestId, allow)
        } catch (e: Exception) {
            ApprovalAnswer.Failed(e.message ?: "Couldn't send the answer")
        }
        mutex.withLock {
            when (r) {
                is ApprovalAnswer.Failed -> { shown += notice.tag; sink.post(notice.copy(error = r.message)) }
                else -> { shown -= notice.tag; sink.cancel(notice.tag) }
            }
        }
        return r
    }

    private fun noticeOf(a: PendingApproval): ApprovalNotice {
        val (text, detail) = ApprovalAttention.textOf(a.toolName, a.toolInput)
        return ApprovalNotice(a.localId, a.requestId, title(a.localId), text, detail)
    }

    companion object {
        /** Production inputs from the process-scoped repositories. */
        @OptIn(ExperimentalCoroutinesApi::class)
        fun pendingFrom(conversations: ConversationRepository): Flow<List<PendingApproval>> =
            conversations.openKeys.flatMapLatest { keys ->
                if (keys.isEmpty()) flowOf(emptyList())
                else combine(keys.map { conversations.state(it) }) { arr -> ApprovalAttention.pendingOf(arr.filterNotNull()) }
            }

        fun lookingAtFrom(
            open: OpenSessionsRepository,
            conversations: ConversationRepository,
            foreground: Flow<Boolean>,
            workspaceOnTop: Flow<Boolean>,
        ): Flow<String?> = combine(open.active, foreground, workspaceOnTop, open.items) { active, fg, top, _ ->
            val localId = (active as? ItemKey.Agent)?.let { conversations.current(it.conversation)?.ref?.localId }
            localId?.takeIf { ApprovalAttention.isLooking(fg, top, localId, it) }
        }

        fun titleFrom(history: HistoryRepository, conversations: ConversationRepository): (String) -> String = { localId ->
            val sdk = conversations.current(ConversationKey.agent(localId))?.ref?.sdkId
            history.titleFor(sdk, localId, "Agent session")
        }
    }
}

/** The "Approvals" channel (AN-4): high importance (heads-up), public on the lock screen. */
class SystemApprovalSink(private val app: Application) : ApprovalSink {
    private val nm = app.getSystemService(NotificationManager::class.java)

    private fun ensureChannel() {
        nm?.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Approvals", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Agent sessions waiting for your approval"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            },
        )
    }

    override fun post(notice: ApprovalNotice) {
        nm ?: return
        ensureChannel()
        val body = listOfNotNull(notice.error?.let { "Couldn't send: $it" }, notice.text, notice.detail).joinToString("\n")
        val approve = NotificationCompat.Action.Builder(0, "Approve", actionIntent(notice, allow = true))
            .setAuthenticationRequired(true)                     // never approve from a locked phone
            .build()
        val deny = NotificationCompat.Action.Builder(0, "Deny", actionIntent(notice, allow = false)).build()
        val n = NotificationCompat.Builder(app, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_archie)
            .setContentTitle(notice.title)
            .setContentText(notice.error?.let { "Couldn't send: $it" } ?: notice.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOnlyAlertOnce(true)                               // an error update does not ring again
            .setAutoCancel(true)
            .setContentIntent(openIntent(notice.localId, notice.tag))
            .addAction(deny)
            .addAction(approve)
            .build()
        try {
            nm.notify(notice.tag, NOTIFICATION_ID, n)
        } catch (e: SecurityException) {
            Log.w(TAG, "notification not allowed: ${e.message}")  // POST_NOTIFICATIONS denied
        }
    }

    override fun cancel(tag: String) {
        nm?.cancel(tag, NOTIFICATION_ID)
    }

    private fun openIntent(localId: String, tag: String): PendingIntent = PendingIntent.getActivity(
        app, 0,
        Intent(app, MainActivity::class.java)
            .setData(Uri.parse("archie-approval://open/${Uri.encode(tag)}"))
            .putExtra(EXTRA_OPEN_AGENT, localId)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun actionIntent(n: ApprovalNotice, allow: Boolean): PendingIntent = PendingIntent.getBroadcast(
        app, 0,
        Intent(app, ApprovalActionReceiver::class.java)
            .setAction(if (allow) ACTION_APPROVE else ACTION_DENY)
            .setData(Uri.parse("archie-approval://${if (allow) "allow" else "deny"}/${Uri.encode(n.tag)}"))
            .putExtra(EXTRA_LOCAL_ID, n.localId)
            .putExtra(EXTRA_REQUEST_ID, n.requestId)
            .putExtra(EXTRA_TITLE, n.title)
            .putExtra(EXTRA_TEXT, n.text)
            .putExtra(EXTRA_DETAIL, n.detail),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    companion object {
        const val CHANNEL_ID = "approvals"
        const val NOTIFICATION_ID = 1003
        const val ACTION_APPROVE = "com.assistant.archie.action.APPROVAL_ALLOW"
        const val ACTION_DENY = "com.assistant.archie.action.APPROVAL_DENY"
        const val EXTRA_LOCAL_ID = "com.assistant.archie.extra.APPROVAL_LOCAL_ID"
        const val EXTRA_REQUEST_ID = "com.assistant.archie.extra.APPROVAL_REQUEST_ID"
        const val EXTRA_TITLE = "com.assistant.archie.extra.APPROVAL_TITLE"
        const val EXTRA_TEXT = "com.assistant.archie.extra.APPROVAL_TEXT"
        const val EXTRA_DETAIL = "com.assistant.archie.extra.APPROVAL_DETAIL"

        /** MainActivity: focus (open) this agent session. */
        const val EXTRA_OPEN_AGENT = "com.assistant.archie.extra.OPEN_AGENT"
        private const val TAG = "ArchieApprovals"

        /** The notice an action intent carries, or null when it is not one of ours. */
        fun noticeOf(intent: Intent?): Pair<ApprovalNotice, Boolean>? {
            intent ?: return null
            val allow = when (intent.action) {
                ACTION_APPROVE -> true
                ACTION_DENY -> false
                else -> return null
            }
            val localId = intent.getStringExtra(EXTRA_LOCAL_ID) ?: return null
            val requestId = intent.getStringExtra(EXTRA_REQUEST_ID) ?: return null
            return ApprovalNotice(
                localId, requestId,
                intent.getStringExtra(EXTRA_TITLE) ?: "Agent session",
                intent.getStringExtra(EXTRA_TEXT).orEmpty(),
                intent.getStringExtra(EXTRA_DETAIL),
            ) to allow
        }
    }
}

/** Approve / Deny from the notification (AN-2): REST, off the main thread; never crashes the app. */
class ApprovalActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val (notice, allow) = SystemApprovalSink.noticeOf(intent) ?: return
        val graph = (context.applicationContext as? GraphOwner)?.graph ?: return
        val pending = goAsync()
        graph.scope.launch {
            try {
                graph.approvals.answerFromNotification(notice, allow)
            } catch (e: Exception) {
                Log.w("ArchieApprovals", "answer failed: ${e.message}")
            } finally {
                pending?.finish()
            }
        }
    }
}

/** A notification tap: focus agent [localId] (pool key); [sdkId] when known. */
data class OpenRequest(val localId: String, val sdkId: String? = null)

/** The app's foreground / workspace-on-top signals plus the notifier, owned by the graph. */
class ApprovalCenter(
    val notifier: ApprovalNotifier,
    private val settingsLoaded: suspend () -> Unit,
) {
    val foreground = MutableStateFlow(false)
    val workspaceOnTop = MutableStateFlow(true)

    /**
     * A notification tap (approval or "agent finished"): the agent to focus. A StateFlow so a
     * cold-launch tap is not lost. [OpenRequest.sdkId] reopens it from history once it left the pool.
     */
    private val _openRequest = MutableStateFlow<OpenRequest?>(null)
    val openRequest: kotlinx.coroutines.flow.StateFlow<OpenRequest?> = _openRequest

    fun requestOpen(localId: String, sdkId: String? = null) { _openRequest.value = OpenRequest(localId, sdkId) }

    fun takeOpenRequest(): OpenRequest? = _openRequest.value.also { _openRequest.value = null }

    /** A cold process (receiver after process death) must read the stored server URL before calling. */
    suspend fun answerFromNotification(notice: ApprovalNotice, allow: Boolean): ApprovalAnswer {
        settingsLoaded()
        return notifier.answer(notice, allow)
    }
}
