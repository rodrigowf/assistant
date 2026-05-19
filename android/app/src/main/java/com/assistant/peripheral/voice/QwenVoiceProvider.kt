package com.assistant.peripheral.voice

import android.content.Context
import android.util.Log
import com.assistant.peripheral.data.VoiceState

/**
 * Qwen-Omni Realtime voice provider.
 *
 * Qwen-Omni uses byte-identical event names to OpenAI Realtime
 * (``response.created``, ``response.audio_transcript.delta``,
 * ``input_audio_buffer.speech_started``, …) so the event parser
 * mirrors that shape.
 *
 * All audio plumbing (AudioRecord, AudioTrack, mic gain, ducking,
 * barge-in flush, JSONL queueing) lives in [WebSocketPcmProvider].
 */
class QwenVoiceProvider(
    context: Context,
    providerId: String = "qwen",
) : WebSocketPcmProvider(context, providerId) {

    override fun parseProviderEvent(event: Map<String, Any?>) {
        when (event["type"] as? String) {
            "error" -> {
                val (code, msg) = readErrorFields(event["error"])
                Log.e(tag, "Qwen upstream error code=$code msg=$msg")
                emit(VoiceEvent.Error(msg))
            }
            "response.created" -> setState(VoiceState.Speaking)
            "response.done" -> {
                setState(VoiceState.Active)
                emit(VoiceEvent.TurnComplete)
            }
            "response.output_item.added" -> {
                if (readNestedString(event["item"], "type") == "function_call") {
                    setState(VoiceState.ToolUse)
                }
            }
            "response.function_call_arguments.done" -> {
                setState(VoiceState.Thinking)
                val callId = event["call_id"] as? String ?: ""
                val name = event["name"] as? String ?: ""
                val argsStr = event["arguments"] as? String ?: "{}"
                val args = try {
                    jsonObjectToMap(org.json.JSONObject(argsStr))
                } catch (_: Exception) {
                    emptyMap()
                }
                emit(VoiceEvent.ToolUse(callId, name, args))
            }
            "input_audio_buffer.speech_started" -> {
                // Server VAD detected user barge-in. Drop any speaker audio
                // we've buffered (channel + AudioTrack hardware buffer) so
                // the model's previous turn cuts immediately instead of
                // playing the residue while the new turn waits.
                flushSpeakerOutput()
                setState(VoiceState.Active)
                emit(VoiceEvent.SpeechStarted)
            }
            "input_audio_buffer.speech_stopped" -> {
                setState(VoiceState.Thinking)
                emit(VoiceEvent.SpeechStopped)
            }
            "conversation.item.input_audio_transcription.completed" -> {
                val transcript = event["transcript"] as? String ?: ""
                if (transcript.isNotEmpty()) emit(VoiceEvent.UserTranscript(transcript))
            }
            "response.audio_transcript.delta" -> {
                val delta = event["delta"] as? String ?: ""
                if (delta.isNotEmpty()) emit(VoiceEvent.TextDelta(delta))
            }
            "response.audio_transcript.done" -> {
                val transcript = event["transcript"] as? String ?: ""
                emit(VoiceEvent.TextComplete(transcript))
            }
            // session.created / session.updated / response.audio.* etc.
            // are noise from the client's perspective — backend persists
            // them; we don't need to react.
        }
    }

    /**
     * Read `code` and `message` from an `error` value that may arrive
     * as either a fully-walked Map or a raw JSONObject (depending on
     * which conversion path the WS layer took).
     */
    private fun readErrorFields(value: Any?): Pair<String, String> {
        val code: String
        val msg: String
        when (value) {
            is org.json.JSONObject -> {
                code = value.optString("code", "unknown")
                msg = value.optString("message", "Unknown error")
            }
            is Map<*, *> -> {
                code = value["code"] as? String ?: "unknown"
                msg = value["message"] as? String ?: "Unknown error"
            }
            else -> {
                code = "unknown"
                msg = "Unknown error"
            }
        }
        return code to msg
    }
}
