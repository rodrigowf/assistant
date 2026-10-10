package com.assistant.core.conversation

import com.assistant.core.model.ConnectionState
import com.assistant.core.model.LiveStatus
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.SessionStatus
import com.assistant.core.protocol.ClientFrame
import com.assistant.core.protocol.PaginatedMessagesDto
import com.assistant.core.protocol.ServerFrame
import kotlinx.collections.immutable.PersistentList
import kotlinx.collections.immutable.PersistentMap
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.persistentMapOf
import kotlinx.serialization.json.JsonObject

data class Stall(val elapsedSeconds: Double?, val lastToolName: String?, val lastToolUseId: String?)

data class Termination(val reason: String?, val detail: String?, val sdkSessionId: String?)

/** Transport/start problems. Never an entry (I-15, A-8.2). */
data class ConnectionBanner(val code: String, val detail: String? = null)

/** In-memory resume cursor (§3.6, T-10). */
data class Checkpoint(val streamId: String, val seq: Long)

data class Counters(
    val cost: Double = 0.0,
    val turns: Int = 0,
    val contextTokens: Long? = null,
    val contextWindow: Long? = null,
)

data class HistoryState(
    val loaded: Boolean = false,
    val startIndex: Int = 0,
    val totalCount: Int = 0,
    val hasMore: Boolean = false,
)

/** An agent permission seen on the orchestrator WS via `nested_session_event` (PM-5, §6.9). */
data class AgentApproval(val localId: String, val requestId: String, val toolName: String, val toolInput: JsonObject)

/** A streaming block closed by an interleaved entry; a continuation may follow (I-5). */
data class SplitRef(val blockId: String, val thinking: Boolean, val scope: BlockScope, val text: String)

/**
 * The full state of one open conversation view (spec 12 §2.3), immutable, with persistent
 * collections so snapshots share structure (L-4). Produced only by [ConversationReducer].
 *
 * Index maps point at the **entry id** that holds the block; the block is found inside that entry
 * by its `tool_use_id` / `request_id`.
 */
data class ConversationState(
    val ref: SessionRef,
    val entries: PersistentList<Entry> = persistentListOf(),
    val tools: PersistentMap<String, String> = persistentMapOf(),
    val perms: PersistentMap<String, String> = persistentMapOf(),
    /** Insertion-ordered (R-2). */
    val orphanResults: PersistentMap<String, PendingResult> = persistentMapOf(),
    val unattributed: PersistentList<PendingResult> = persistentListOf(),
    val queue: PersistentList<QueuedPrompt> = persistentListOf(),
    /** Texts popped from the tray at `status{processing}`; swallows a pre-O-6 re-echo; cleared at turn end. */
    val dispatchedFromTray: PersistentList<String> = persistentListOf(),

    val status: SessionStatus = SessionStatus.CONNECTING,
    val inTurn: Boolean = false,
    val turnDepth: Int = 0,
    val turnIsLocal: Boolean = false,
    val turnHasContent: Boolean = false,
    val localTurnsPending: Int = 0,
    val promptSinceTurnEnd: Boolean = false,
    val compactPending: Boolean = false,
    val pendingInjects: PersistentList<String> = persistentListOf(),
    /** Orchestrator only: the "Agent approvals" list (PM-5). */
    val agentApprovals: PersistentList<AgentApproval> = persistentListOf(),
    val voiceActive: Boolean = false,
    val openVoiceUserId: String? = null,
    val speechAnchor: Int? = null,

    val stall: Stall? = null,
    val termination: Termination? = null,
    val connectionBanner: ConnectionBanner? = null,
    val gapPossible: Boolean = false,

    val checkpoint: Checkpoint? = null,
    val counters: Counters = Counters(),
    val history: HistoryState = HistoryState(),

    val pendingSplit: SplitRef? = null,
    val expectStopAck: Boolean = false,
    /** Set when this turn produced something only a REST reconcile can fix (R-7). */
    val turnNeedsReconcile: Boolean = false,

    val connection: ConnectionState = ConnectionState.OFFLINE,
    /**
     * Spec 12 OPEN-2: the next `start` is the user's own request (new, History, fork, continue), so it
     * may create the session. Cleared by the first `session_started`; every other start reattaches.
     */
    val userStart: Boolean = false,
    val awaitingSessionStarted: Boolean = false,
    /** `orchestrator_stopping` already retried once for this start (T-12, SEQ-8). */
    val stoppingRetried: Boolean = false,
    /** The last `start` produced by [ConversationInput.SocketOpened] / [ConversationInput.Resync]. */
    val startRequest: ClientFrame.Start? = null,
    val preStart: PersistentList<ServerFrame> = persistentListOf(),
    val reloading: Boolean = false,
    val reloadBuffer: PersistentList<ServerFrame> = persistentListOf(),

    /** Counter for live ids (deterministic, I-13). */
    val nextId: Long = 1,
) {
    val kind: SessionKind get() = ref.kind

    /** The newest `pending` permission (PM-3), or `null`. */
    fun newestPendingPermission(): PermissionBlock? {
        for (i in entries.indices.reversed()) {
            val e = entries[i] as? AssistantEntry ?: continue
            for (j in e.blocks.indices.reversed()) {
                val b = e.blocks[j]
                if (b is PermissionBlock && b.state == PermissionState.PENDING) return b
            }
        }
        return null
    }

    /** Results the UI must make reachable (R-9): live orphans always, history ones once `hasMore` is false. */
    fun reachableOrphans(): Map<String, PendingResult> =
        orphanResults.filter { (_, r) -> r.origin == ResultOrigin.LIVE || !history.hasMore }

    companion object {
        /**
         * A new view (spec 12 §2.3 initial values). For an agent session that `pool/live` reports busy,
         * ST-2 applies: `inTurn = true` with that status.
         */
        fun initial(ref: SessionRef, userStart: Boolean = false): ConversationState {
            val busy = ref.kind == SessionKind.AGENT && ref.live &&
                ref.liveStatus in setOf(LiveStatus.STREAMING, LiveStatus.TOOL_USE, LiveStatus.THINKING)
            return if (busy) {
                ConversationState(ref = ref, userStart = userStart, inTurn = true, status = SessionStatus.fromWire(ref.liveStatus!!.wire)!!)
            } else {
                ConversationState(ref = ref, userStart = userStart)
            }
        }
    }
}

enum class PageMode { REPLACE, PREPEND, RECONCILE }

/** Everything that can change a conversation, applied in one ordered stream (L-2). */
sealed interface ConversationInput {
    /** A decoded server frame, exactly as received (goes through §3.6 `onFrame`). */
    data class Frame(val frame: ServerFrame) : ConversationInput

    /** A REST page (§5.1). A `REPLACE` while [ConversationState.reloading] completes the reload (§5.6). */
    data class HistoryPage(val mode: PageMode, val page: PaginatedMessagesDto) : ConversationInput

    /** Cold open / canonical reload starts: hold frames until the page arrives (§5.2, §5.6). */
    data object BeginReload : ConversationInput

    /** The reload's REST call failed: flush the held frames and offer "Reload" (`gapPossible`). */
    data object ReloadFailed : ConversationInput

    /** OpenAI data-channel inbound event, voice owner only (§4.7). */
    data class DataChannelEvent(val event: JsonObject) : ConversationInput

    data class LocalSend(val text: String) : ConversationInput
    data object LocalSendAudio : ConversationInput
    data class LocalInject(val text: String) : ConversationInput
    data object LocalInterrupt : ConversationInput
    data object LocalCompact : ConversationInput

    /** Before sending `stop` or `POST …/close`: the next `session_stopped` is our own ack. */
    data object LocalStop : ConversationInput

    /** The voice controller tore voice down locally (timeout, fatal error). */
    data object VoiceLocalEnd : ConversationInput

    /** The socket opened: produce a `start` (§3.3); frames are held until `session_started`. */
    data object SocketOpened : ConversationInput

    /** Visible again with the socket still open: re-send `start` (T-9, §3.5). */
    data object Resync : ConversationInput

    /** The socket closed. Entries are untouched (A-4.3 b). */
    data object SocketClosed : ConversationInput

    /** `sdkId` learned from `pool/live` or `agent_session_opened` (ID-2). */
    data class SdkIdLearned(val sdkId: String) : ConversationInput

    /** `pool/live` status for this agent session on (re)subscribe (ST-2). */
    data class PoolStatus(val status: LiveStatus) : ConversationInput

    data object DismissBanner : ConversationInput

    /** Orchestrator view: the agent view [localId] saw its turn end; drop its approvals (PM-5). */
    data class ClearAgentApprovals(val localId: String) : ConversationInput
}

/** Work the reducer asks its owner (the repository) to do. The reducer itself performs no I/O (L-1). */
sealed interface ConversationEffect {
    data class SendStart(val frame: ClientFrame.Start) : ConversationEffect

    /** Run `GET …/messages?limit=50` and feed it back as `HistoryPage(REPLACE)` (or `ReloadFailed`). */
    data object CanonicalReload : ConversationEffect

    /** Run `GET /api/sessions/pool/live` and feed back `SdkIdLearned` (ID-2, SEQ-6). */
    data object LookupSdkId : ConversationEffect

    /** R-7: debounce 500 ms, then `GET …/messages?limit=50` → `HistoryPage(RECONCILE)`. */
    data object ScheduleReconcile : ConversationEffect

    /** A turn ended (MC-2 list refresh, visualization refresh). */
    data object TurnEnded : ConversationEffect

    /** `start_timeout` / `start_failed` / `orchestrator_active` / `orchestrator_stopping` after its retry (T-12, SEQ-8). */
    data class StartError(val code: String, val detail: String?) : ConversationEffect

    /** First `orchestrator_stopping`: feed [ConversationInput.Resync] after [delayMillis] (T-12). Still waiting. */
    data class ScheduleStartRetry(val delayMillis: Long) : ConversationEffect

    /** Protocol or voice error codes: logged / toast / voice banner, never an entry (§4.4.4). */
    data class SideError(val code: String, val detail: String?) : ConversationEffect

    /**
     * Spec 12 OPEN-3: the session left the server's pool (`session_stopped` not ours,
     * `agent_session_closed` for this view, `error{session_closed}`): the view closes, no close request.
     * [detail]: the `session_terminated` detail when the session ended (crash, lost host) rather than closed.
     */
    data class Closed(val termination: Termination?) : ConversationEffect
}

data class ReduceResult(val state: ConversationState, val effects: List<ConversationEffect>)
