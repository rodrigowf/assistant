package com.assistant.core.conversation

import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionStatus

/**
 * The spec-12 data layer for one conversation: connection-manager rules (§3.6, §5.2, §5.6) plus the
 * reducer (§4.3, §4.7, §5). A pure, synchronous, deterministic function (L-1, I-13): no I/O, no
 * clock, no randomness. Work it needs done outside comes back as [ConversationEffect]s.
 *
 * All inputs of one conversation must be applied in one ordered stream (L-2), on one thread (L-4).
 */
object ConversationReducer {
    fun reduce(state: ConversationState, input: ConversationInput): ConversationState = step(state, input).state

    fun step(state: ConversationState, input: ConversationInput): ReduceResult {
        val d = Draft(state)
        d.apply(input)
        return ReduceResult(d.build(), d.effects.toList())
    }

    /** Applies several inputs in order (convenience for tests and replays). */
    fun reduceAll(state: ConversationState, inputs: Iterable<ConversationInput>): ConversationState =
        inputs.fold(state) { s, i -> reduce(s, i) }

    internal val TURN_FAILURE_AGENT = setOf("send_failed", "upstream_wedged", "turn_timeout", "command_failed", "compact_failed")
    internal val TURN_FAILURE_ORCH = setOf(
        "api_error", "provider_error", "send_failed", "send_audio_failed",
        "invalid_audio", "inject_text_failed", "compact_failed",
    )
    /** Orchestrator exception paths: no `status{idle}` follows. */
    internal val ORCH_NO_IDLE_AFTER = setOf("send_failed", "send_audio_failed", "compact_failed")
    /** Start / connection errors (§4.4.4): a banner, never an entry, never held behind `session_started`. */
    internal val START_ERRORS = setOf("start_timeout", "start_failed", "orchestrator_active", "orchestrator_stopping")

    internal const val DEFAULT_CONTEXT_WINDOW = 200_000L

    internal fun isAgent(kind: SessionKind) = kind == SessionKind.AGENT

    internal val BUSY_LIVE = setOf(SessionStatus.STREAMING, SessionStatus.TOOL_USE, SessionStatus.THINKING)
}
