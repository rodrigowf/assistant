package com.assistant.peripheral.voice

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer

/**
 * Wraps a Vosk `Recognizer` for the wake-word detection stage.
 *
 * Plan: `assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md`, §4 V3.
 *
 * Replaces Android's `SpeechRecognizer` in the recognition stage. Whereas
 * `SpeechRecognizer` requires releasing the mic and incurring 800ms–3s of
 * IPC bind latency before it starts listening — clipping the leading edge
 * of the user's wake phrase on Lollipop — this engine consumes the SAME
 * PCM stream the silence monitor is already reading. No handoff, no
 * latency, no clipping.
 *
 * Lifecycle
 * ---------
 *   val engine = VoskWakeWordEngine(model, talkVariants, wakeVariants)
 *   while (recording) {
 *       val read = audioRecord.read(buffer, 0, buffer.size)
 *       val match = engine.feed(buffer, read)
 *       if (match != null) { ... }
 *   }
 *   engine.close()
 *
 * Threading
 * ---------
 * `Recognizer` is NOT thread-safe. Construct, feed, and close from a
 * single coroutine/thread (typically the silence-monitor IO coroutine in
 * `WakeWordDetector.startSilenceMonitor`). `Model` is shared via
 * `VoskModelLoader` and IS thread-safe.
 *
 * Match precedence
 * ----------------
 * Realtime wake variants are checked FIRST — preserves the Detour 3
 * precedence ordering (also enforced by `WakeWordDetector.checkForWakeWord`
 * at HEAD `ddfb53f` lines 1015–1031).
 *
 * Constrained vocabulary
 * ----------------------
 * Per plan §5.3, we pass a JSON array of the configured phrases to the
 * `Recognizer` constructor. This constrains decoding to just those words
 * plus the `[unk]` sentinel, drastically improving both accuracy and
 * decode speed for our 2-phrase keyword-spotting use case.
 */
class VoskWakeWordEngine(
    model: Model,
    sampleRate: Float = 16000f,
    private val talkVariants: List<String>,
    private val wakeVariants: List<String>,
) {
    private val recognizer: Recognizer = Recognizer(
        model,
        sampleRate,
        buildKeywordGrammar(talkVariants, wakeVariants),
    )

    @Volatile
    private var closed = false

    /**
     * Feed a chunk of PCM 16-bit mono samples at the engine's sample rate.
     * Returns a non-null [Match] if the partial or final result contains
     * one of the configured variants.
     *
     * Side effect on match: the recognizer is reset so the next call
     * starts fresh — otherwise Vosk's accumulating partial would keep
     * re-matching the same phrase across many calls, producing a stream
     * of false re-triggers.
     *
     * `length` is the number of samples actually filled (per
     * `AudioRecord.read`'s contract — can be less than `buffer.size`).
     */
    fun feed(buffer: ShortArray, length: Int): Match? {
        if (closed || length <= 0) return null
        val isFinal = recognizer.acceptWaveForm(buffer, length)
        val json = if (isFinal) recognizer.finalResult else recognizer.partialResult
        val text = extractText(json)
        if (text.isBlank()) return null
        val match = findMatch(text, talkVariants, wakeVariants) ?: return null
        // Reset so we don't re-trigger on the next call's partial.
        try { recognizer.reset() } catch (e: Throwable) {
            Log.w(TAG, "Recognizer reset failed: ${e.message}")
        }
        return match
    }

    fun close() {
        if (closed) return
        closed = true
        try { recognizer.close() } catch (e: Throwable) {
            Log.w(TAG, "Recognizer close failed: ${e.message}")
        }
    }

    /**
     * Return Vosk's current partial transcription without consuming or
     * resetting state. Used for diagnostic logging during the recognition
     * window so we can see what Vosk actually hears, not just whether it
     * matched.
     */
    fun peekText(): String =
        if (closed) "" else extractText(recognizer.partialResult)

    /**
     * A successful wake-word/talk-word detection. Carries enough info for
     * the caller to fire the right LocalBroadcast (`ACTION_WAKE_WORD_DETECTED`
     * vs `ACTION_TALK_WORD_DETECTED`) and log the matched phrase.
     */
    data class Match(
        val matchedVariant: String,
        val isRealtime: Boolean,
        val rawText: String,
    )

    companion object {
        private const val TAG = "VoskWakeWordEngine"

        /**
         * Extract the spoken text from a Vosk result JSON. Vosk emits two
         * shapes:
         *   final:    {"text": "..."}     — committed transcription
         *   partial:  {"partial": "..."}  — incremental during ongoing speech
         * Returns the inner string trimmed, or empty if missing/blank/malformed.
         */
        fun extractText(json: String): String {
            if (json.isBlank()) return ""
            return try {
                val obj = JSONObject(json)
                val raw = when {
                    obj.has("text") -> obj.optString("text", "")
                    obj.has("partial") -> obj.optString("partial", "")
                    else -> ""
                }
                raw.trim()
            } catch (_: Throwable) {
                ""
            }
        }

        /**
         * Variant matcher with realtime-first precedence. Returns the first
         * `wakeVariants` entry that is a substring of the lowercased input,
         * else the first `talkVariants` entry, else null.
         *
         * Substring semantics match `WakeWordDetector.checkForWakeWord`'s
         * `lower.contains(it)` check — so "hey wake up please" matches the
         * variant "wake up".
         */
        fun findMatch(
            text: String,
            talkVariants: List<String>,
            wakeVariants: List<String>,
        ): Match? {
            val trimmed = text.trim()
            if (trimmed.isEmpty()) return null
            val lower = trimmed.lowercase()
            wakeVariants.firstOrNull { lower.contains(it) }?.let {
                return Match(matchedVariant = it, isRealtime = true, rawText = trimmed)
            }
            talkVariants.firstOrNull { lower.contains(it) }?.let {
                return Match(matchedVariant = it, isRealtime = false, rawText = trimmed)
            }
            return null
        }

        /**
         * Build a Vosk constrained-vocab grammar from the configured phrases.
         * The JSON array is passed to the `Recognizer` constructor — the
         * decoder only considers these phrases plus the `[unk]` sentinel for
         * everything else (silence, background noise, unrelated speech). Per
         * plan §5.3 this dramatically improves both accuracy and decode
         * speed compared to the free-form small-model decoder.
         */
        fun buildKeywordGrammar(
            talkVariants: List<String>,
            wakeVariants: List<String>,
        ): String {
            val phrases = (talkVariants + wakeVariants).distinct() + "[unk]"
            return JSONArray(phrases).toString()
        }
    }
}
