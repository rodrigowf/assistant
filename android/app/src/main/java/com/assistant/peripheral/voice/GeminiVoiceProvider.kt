package com.assistant.peripheral.voice

import android.content.Context
import com.assistant.peripheral.data.VoiceState

/**
 * Gemini Live voice provider.
 *
 * The wire shape differs from OpenAI / Qwen in two ways:
 *   - no ``type`` field — events are top-level envelopes
 *     (``serverContent``, ``toolCall``, ``setupComplete``,
 *     ``sessionResumptionUpdate``);
 *   - assistant transcripts arrive as deltas under
 *     ``serverContent.outputTranscription.text`` with no consolidated
 *     ``done`` event — completion is signaled by
 *     ``serverContent.turnComplete``, so we accumulate deltas locally
 *     and emit a single [VoiceEvent.TextComplete] when the turn ends.
 *     Mirrors ``orchestrator/session.py``'s
 *     ``_pending_assistant_transcript``.
 *
 * The Half-Cascade Live preview variant streams text via
 * ``modelTurn.parts[].text`` instead of ``outputTranscription``; we
 * accept both.
 *
 * All audio plumbing lives in [WebSocketPcmProvider].
 */
class GeminiVoiceProvider(
    context: Context,
    providerId: String = "google",
) : WebSocketPcmProvider(context, providerId) {

    private val assistantStaged = StringBuilder()
    // Gemini Live streams ``inputTranscription.text`` as token-level
    // deltas. Buffer and emit one [VoiceEvent.UserTranscript] per turn —
    // flushed when the model starts replying (first output delta) or on
    // turnComplete. Without this, the UI got one user bubble per word.
    private val userStaged = StringBuilder()

    override fun parseProviderEvent(event: Map<String, Any?>) {
        val sc = event["serverContent"]
        if (sc != null) {
            handleServerContent(sc)
        }
        val toolCall = event["toolCall"]
        if (toolCall != null) {
            handleToolCall(toolCall)
        }
    }

    private fun flushUserStaged() {
        if (userStaged.isEmpty()) return
        val text = userStaged.toString()
        userStaged.setLength(0)
        emit(VoiceEvent.UserTranscript(text))
    }

    private fun handleServerContent(sc: Any?) {
        val inputText = readNestedString(readNestedAny(sc, "inputTranscription"), "text")
        if (!inputText.isNullOrEmpty()) {
            userStaged.append(inputText)
        }
        val outputText = readNestedString(readNestedAny(sc, "outputTranscription"), "text")
        if (!outputText.isNullOrEmpty()) {
            // Model started replying → user's turn ended.
            flushUserStaged()
            assistantStaged.append(outputText)
            emit(VoiceEvent.TextDelta(outputText))
        }
        // Half-cascade Live preview streams text via modelTurn.parts[].text.
        val modelTurn = readNestedAny(sc, "modelTurn")
        val parts = readNestedAny(modelTurn, "parts")
        if (parts is List<*>) {
            flushUserStaged()
            for (p in parts) {
                val t = readNestedString(p, "text")
                if (!t.isNullOrEmpty()) {
                    assistantStaged.append(t)
                    emit(VoiceEvent.TextDelta(t))
                }
            }
        }
        if (readNestedBoolean(sc, "interrupted") == true) {
            flushSpeakerOutput()
            setState(VoiceState.Active)
            // Drop the partial transcript — barge-in cuts the turn.
            assistantStaged.setLength(0)
        }
        if (readNestedBoolean(sc, "turnComplete") == true) {
            setState(VoiceState.Active)
            // Failsafe: covers audio-only turns where neither output
            // path fired.
            flushUserStaged()
            val staged = assistantStaged.toString()
            assistantStaged.setLength(0)
            emit(VoiceEvent.TextComplete(staged))
            emit(VoiceEvent.TurnComplete)
        }
    }

    private fun handleToolCall(toolCall: Any?) {
        setState(VoiceState.ToolUse)
        val calls = readNestedAny(toolCall, "functionCalls")
        if (calls is List<*>) {
            for (c in calls) {
                val callId = readNestedString(c, "id") ?: ""
                val name = readNestedString(c, "name") ?: ""
                val argsAny = readNestedAny(c, "args")
                val args: Map<String, Any?> = when (argsAny) {
                    is Map<*, *> -> {
                        @Suppress("UNCHECKED_CAST")
                        argsAny as Map<String, Any?>
                    }
                    is org.json.JSONObject -> jsonObjectToMap(argsAny)
                    else -> emptyMap()
                }
                if (callId.isNotEmpty() && name.isNotEmpty()) {
                    emit(VoiceEvent.ToolUse(callId, name, args))
                }
            }
        }
    }
}
