package com.assistant.archie.feature.chat

import androidx.compose.runtime.Immutable
import com.assistant.archie.feature.chat.model.ChatItem
import com.assistant.core.conversation.AgentApproval
import com.assistant.core.conversation.ConversationState
import com.assistant.core.conversation.PermissionBlock
import com.assistant.core.conversation.QueuedPrompt
import com.assistant.core.design.components.ComposerPrimary
import com.assistant.core.model.ConnectionState
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionStatus
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.collections.immutable.ImmutableList
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toImmutableList
import kotlin.math.roundToInt

/** Everything the conversation screen draws (spec 14 §2.1: immutable, persistent collections). */
@Immutable
data class ConversationUiState(
    val kind: SessionKind = SessionKind.AGENT,
    val loaded: Boolean = false,
    /** In timeline order (top to bottom). The list shows them reversed (reverseLayout, §3.6). */
    val items: ImmutableList<ChatItem> = persistentListOf(),
    val oldestEntryId: String? = null,
    val hasMore: Boolean = false,
    val empty: Boolean = true,
    val composer: ComposerUi = ComposerUi(),
    val cards: ImmutableList<InlineCardUi> = persistentListOf(),
    val queue: ImmutableList<QueuedPrompt> = persistentListOf(),
    val voice: VoiceUi = VoiceUi.Off,
    val counters: CountersUi = CountersUi(),
    /** A rewind/fork/upload is running: the busy overlay covers the list (02 F-12). */
    val busyOverlay: String? = null,
) {
    val isArchie: Boolean get() = kind == SessionKind.ORCHESTRATOR
}

@Immutable
data class ComposerUi(
    val primary: ComposerPrimary = ComposerPrimary.SendDisabled,
    /** Agent working: Enter / Send queues the text (tray, I-12). */
    val working: Boolean = false,
    val enabled: Boolean = true,
    /** Why the field is disabled ("This session has ended"), shown as its placeholder. */
    val disabledReason: String? = null,
    val placeholder: String = "Message…",
    val canAttach: Boolean = false,
    val canRecord: Boolean = false,
    /** Reason the voice-message button is disabled, or null. */
    val recordDisabledReason: String? = null,
    val recording: Boolean = false,
    val slashCommands: Boolean = false,
    /** Context use 0..1, or null when unknown (`?`, §6.4). */
    val context: Float? = null,
    val canCompact: Boolean = false,
)

@Immutable
data class CountersUi(
    val cost: Double = 0.0,
    val turns: Int = 0,
    val contextTokens: Long? = null,
    val contextWindow: Long? = null,
) {
    val percent: Int? get() = if (contextTokens != null && contextWindow != null && contextWindow > 0) {
        (contextTokens * 100.0 / contextWindow).roundToInt()
    } else {
        null
    }
}

/** The cards above the composer (IA §6), in display order. */
@Immutable
sealed interface InlineCardUi {
    val id: String

    /** The newest pending permission of this view (PM-3). */
    data class Permission(val block: PermissionBlock, val answered: Boolean) : InlineCardUi {
        override val id get() = "perm:${block.requestId}"
    }

    /** An agent's permission seen from the Archie view (PM-5); answerable from here (§6.9). */
    data class AgentApprovalCard(val approval: AgentApproval, val answered: Boolean) : InlineCardUi {
        override val id get() = "agent-perm:${approval.localId}:${approval.requestId}"
    }

    /** §6.12: "<tool> silent for <t>" + Interrupt. */
    data class Stall(val toolName: String?, val elapsedSeconds: Double?) : InlineCardUi {
        override val id get() = "stall"
    }

    /** Connection banner / start failure / unsent message / voice error, with Retry when it helps. */
    data class Error(
        override val id: String,
        val title: String,
        val body: String,
        val detail: String?,
        val retry: RetryKind?,
    ) : InlineCardUi

    /** A `replay_overflow` / failed reload left a possible gap: offer Reload (§5.6). */
    data object GapPossible : InlineCardUi {
        override val id get() = "gap"
    }
}

enum class RetryKind { Reconnect, Resend, Reload }

/** The voice dock / composer state for the Archie view (IA §6). */
@Immutable
sealed interface VoiceUi {
    data object Off : VoiceUi
    data class Connecting(val label: String) : VoiceUi
    data class Active(val phase: SessionPhase, val micMuted: Boolean, val speakerMuted: Boolean) : VoiceUi
    data class Reconnecting(val since: Long) : VoiceUi
    data class Reconnected(val afterSeconds: Int) : VoiceUi
    data class ReconnectFailed(val message: String?) : VoiceUi

    /** Voice runs on another device: read-only dock, transcripts mirror, text input stays usable. */
    data class Elsewhere(val device: String, val mirrored: Boolean) : VoiceUi
}

/** Pure mapping helpers (unit-tested; the ViewModel only wires them). */
object ConversationUiMapper {
    fun composer(
        s: ConversationState,
        draftEmpty: Boolean,
        voice: VoiceUi,
        recording: Boolean,
        title: String?,
    ): ComposerUi {
        val archie = s.kind == SessionKind.ORCHESTRATOR
        val working = s.inTurn || s.status.busy
        val ended = s.status == SessionStatus.TERMINATED
        val primary = when {
            !draftEmpty -> ComposerPrimary.Send
            working -> ComposerPrimary.Stop
            archie && voice !is VoiceUi.Elsewhere -> ComposerPrimary.Voice
            else -> ComposerPrimary.SendDisabled
        }
        val c = s.counters
        val window = c.contextWindow
        val ctx = if (c.contextTokens != null && window != null && window > 0) (c.contextTokens!!.toFloat() / window) else null
        val connected = s.connection == ConnectionState.SUBSCRIBED || s.connection == ConnectionState.OPEN
        return ComposerUi(
            primary = if (ended && draftEmpty) ComposerPrimary.SendDisabled else primary,
            working = working,
            enabled = !ended,
            disabledReason = if (ended) "This session has ended" else null,
            placeholder = when {
                archie -> "Message Archie…"
                title != null -> "Message $title…"
                else -> "Message agent…"
            },
            canAttach = archie,
            canRecord = archie,
            recordDisabledReason = when {
                !archie -> null
                !connected -> "Not connected"
                working && !recording -> "Wait for the reply to finish"
                voice !is VoiceUi.Off && voice !is VoiceUi.Elsewhere -> "Voice is on"
                else -> null
            },
            recording = recording,
            slashCommands = !archie,
            context = ctx,
            canCompact = !working && !ended && connected,
        )
    }

    fun cards(
        s: ConversationState,
        answered: Set<String>,
        dismissed: Set<String>,
        transient: List<InlineCardUi.Error>,
    ): ImmutableList<InlineCardUi> {
        val out = ArrayList<InlineCardUi>()
        s.newestPendingPermission()?.let { p ->
            val card = InlineCardUi.Permission(p, answered = p.requestId in answered)
            if (card.id !in dismissed) out += card
        }
        if (s.kind == SessionKind.ORCHESTRATOR) {
            for (a in s.agentApprovals) {
                val card = InlineCardUi.AgentApprovalCard(a, "${a.localId}:${a.requestId}" in answered)
                if (card.id !in dismissed) out += card
            }
        }
        val stall = s.stall
        if (stall != null && (s.inTurn || s.status.busy) && "stall:${stall.elapsedSeconds}" !in dismissed && "stall" !in dismissed) {
            out += InlineCardUi.Stall(stall.lastToolName, stall.elapsedSeconds)
        }
        s.connectionBanner?.let { b ->
            out += InlineCardUi.Error(
                id = "banner:${b.code}",
                title = bannerTitle(b.code),
                body = b.detail ?: "The connection to the server was interrupted.",
                detail = b.code,
                retry = RetryKind.Reconnect,
            )
        }
        transient.filter { it.id !in dismissed }.forEach { out += it }
        if (s.gapPossible && "gap" !in dismissed) out += InlineCardUi.GapPossible
        return out.toImmutableList()
    }

    fun bannerTitle(code: String): String = when (code) {
        "start_timeout" -> "The session didn't start in time"
        "start_failed" -> "The session couldn't start"
        "orchestrator_active" -> "Archie is already running elsewhere"
        "orchestrator_stopping" -> "Archie is restarting"
        else -> "Connection problem"
    }

    /** §6.12: `Ns` under 90 s, else `XmYs`; titles follow the mockup ("WebFetch silent for 2 min"). */
    fun stallElapsed(seconds: Double?): String {
        val s = (seconds ?: 0.0).roundToInt()
        return when {
            s < 90 -> "${s}s"
            s % 60 == 0 -> "${s / 60}m"
            else -> "${s / 60}m${s % 60}s"
        }
    }

    fun stallTitle(tool: String?, seconds: Double?): String {
        val s = (seconds ?: 0.0).roundToInt()
        val t = if (s >= 60 && s % 60 == 0) "${s / 60} min" else stallElapsed(seconds)
        return if (tool != null) "$tool silent for $t" else "No response for $t"
    }

    fun stallBody(tool: String?, seconds: Double?): String = if (tool != null) {
        "$tool has been running for ${stallElapsed(seconds)} with no response. Interrupt to stop the call."
    } else {
        "No response from the agent for ${stallElapsed(seconds)}."
    }

    /**
     * Voice dock state from the voice host (Archie view only). [reconnectSince]/[previous] carry the
     * reconnect timeline: a banner while reconnecting, then Reconnected or Couldn't reconnect.
     */
    fun voice(
        v: VoiceSessionState,
        speakerMuted: Boolean,
        remoteDevice: String?,
        mirrored: Boolean,
        reconnectSince: Long?,
        outcome: VoiceUi?,
    ): VoiceUi = when {
        outcome != null -> outcome
        v.reconnectBanner != null && reconnectSince != null -> VoiceUi.Reconnecting(reconnectSince)
        v.remoteVoiceActive && !v.isOwner -> VoiceUi.Elsewhere(remoteDevice ?: "another device", mirrored)
        v.phase == SessionPhase.CONNECTING -> VoiceUi.Connecting("Connecting…")
        v.phase == SessionPhase.SUMMARIZING -> VoiceUi.Connecting("Preparing conversation…")
        v.phase == SessionPhase.ENDING -> VoiceUi.Connecting("Ending…")
        v.phase == SessionPhase.ACTIVE || v.phase == SessionPhase.SPEAKING ||
            v.phase == SessionPhase.THINKING || v.phase == SessionPhase.TOOL_USE ->
            VoiceUi.Active(v.phase, v.isMuted, speakerMuted)
        else -> VoiceUi.Off
    }

    fun agentApprovalTitle(a: AgentApproval): String = "${a.toolName} needs approval"
}
