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
    // 0.0 (default) = legacy behaviour: partial-or-final substring match, no
    // confidence floor — preserves the original sub-second latency profile.
    // > 0.0 = finals-only matching with a per-word confidence floor. Vosk
    // emits a per-word `conf` (0.0–1.0) when `setWords(true)`; we take the
    // minimum over the matched phrase's words and reject the match if it
    // falls below this threshold. Adds ~300–500 ms latency (we no longer
    // fire on partials) but kills mid-utterance / ambient false positives.
    private val confidenceThreshold: Double = 0.0,
) {
    private val recognizer: Recognizer = Recognizer(
        model,
        sampleRate,
        buildKeywordGrammar(talkVariants, wakeVariants),
    ).also {
        // setWords(true) makes Vosk include the per-word `result: [...]` array
        // (with `word`, `start`, `end`, `conf`) in finalResult JSON. Cheap;
        // we leave it on unconditionally so peekText/diagnostics work the
        // same whether or not the confidence floor is active.
        try { it.setWords(true) } catch (e: Throwable) {
            Log.w(TAG, "Recognizer.setWords(true) failed: ${e.message}")
        }
    }

    @Volatile
    private var closed = false

    /**
     * Feed a chunk of PCM 16-bit mono samples at the engine's sample rate.
     * Returns a non-null [Match] if a configured variant is detected.
     *
     * Two modes, selected by [confidenceThreshold] at construction:
     *  - 0.0 (legacy): matches against partial OR final transcription via
     *    substring contains.
     *  - > 0.0: matches only against final transcription, and only when the
     *    minimum per-word confidence across the matched phrase's words is at
     *    or above the threshold. Sub-threshold matches are returned with
     *    `accepted = false` for diagnostic logging by the caller — the caller
     *    is responsible for ignoring rejected matches.
     *
     * Side effect on accepted match: the recognizer is reset so the next
     * call starts fresh — otherwise Vosk's accumulating partial would keep
     * re-matching the same phrase across many calls, producing a stream of
     * false re-triggers.
     *
     * `length` is the number of samples actually filled (per
     * `AudioRecord.read`'s contract — can be less than `buffer.size`).
     */
    fun feed(buffer: ShortArray, length: Int): Match? {
        if (closed || length <= 0) return null
        val isFinal = recognizer.acceptWaveForm(buffer, length)
        return if (confidenceThreshold > 0.0) {
            if (!isFinal) return null
            val json = recognizer.finalResult
            val match = findMatchWithConfidence(
                json, talkVariants, wakeVariants, confidenceThreshold,
            ) ?: return null
            // Reset on every final regardless of accept/reject — the audio
            // window for this utterance is consumed either way.
            try { recognizer.reset() } catch (e: Throwable) {
                Log.w(TAG, "Recognizer reset failed: ${e.message}")
            }
            match
        } else {
            val json = if (isFinal) recognizer.finalResult else recognizer.partialResult
            val text = extractText(json)
            if (text.isBlank()) return null
            val m = findMatch(text, talkVariants, wakeVariants) ?: return null
            try { recognizer.reset() } catch (e: Throwable) {
                Log.w(TAG, "Recognizer reset failed: ${e.message}")
            }
            m
        }
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
     * A wake-word/talk-word detection.
     *
     * - [accepted] is true when the caller should fire the broadcast. In the
     *   legacy zero-threshold path this is always true. In the confidence
     *   path it can be false: the match's phrase appeared in the final
     *   transcript but the minimum per-word confidence was below the floor.
     *   The caller logs the rejection diagnostically but otherwise ignores it.
     * - [minConfidence] is the minimum `conf` across the matched phrase's
     *   words, or `null` if Vosk didn't emit per-word confidences (legacy
     *   substring path on partial result, or finalResult without a `result`
     *   array).
     */
    data class Match(
        val matchedVariant: String,
        val isRealtime: Boolean,
        val rawText: String,
        val accepted: Boolean = true,
        val minConfidence: Double? = null,
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

        /**
         * Confidence-aware match. Parses Vosk's `setWords(true)` final result
         * JSON (with `result: [{word,start,end,conf}, …]`), runs the same
         * realtime-first substring precedence as [findMatch], then computes
         * the minimum per-word `conf` across the matched phrase's words and
         * marks the [Match] as accepted/rejected against [threshold].
         *
         * Returns null if no phrase matches OR if Vosk's text is blank — the
         * caller should treat null as "keep listening", and a non-null Match
         * with `accepted = false` as "rejected, log it and reset". Matching
         * but unscored words (missing `result` array, or `conf` absent)
         * default to `minConfidence = null`; for safety those are treated as
         * REJECTED when a threshold is in force (a final without per-word
         * confidence shouldn't have happened with setWords(true), but if it
         * does, declining to fire is the safer choice).
         */
        fun findMatchWithConfidence(
            finalJson: String,
            talkVariants: List<String>,
            wakeVariants: List<String>,
            threshold: Double,
        ): Match? {
            val text = extractText(finalJson)
            if (text.isBlank()) return null
            val base = findMatch(text, talkVariants, wakeVariants) ?: return null
            val minConf = minConfForPhrase(finalJson, base.matchedVariant)
            val accepted = minConf != null && minConf >= threshold
            return base.copy(accepted = accepted, minConfidence = minConf)
        }

        /**
         * Compute the minimum `conf` across the words of [phrase] inside
         * Vosk's `result: [...]` array. Returns null if the JSON has no
         * `result` array, the phrase's words can't be located in order, or
         * any matched entry is missing `conf`.
         *
         * Matching strategy: tokenize [phrase] on whitespace, then walk the
         * `result` array greedily looking for those tokens in order. This
         * tolerates leading/trailing extra words in the final transcript
         * (e.g. "[unk] wake up please" still scores the "wake"/"up" pair).
         */
        fun minConfForPhrase(finalJson: String, phrase: String): Double? {
            if (finalJson.isBlank()) return null
            val obj = try { JSONObject(finalJson) } catch (_: Throwable) { return null }
            val result = obj.optJSONArray("result") ?: return null
            val tokens = phrase.trim().lowercase().split(Regex("\\s+"))
                .filter { it.isNotEmpty() }
            if (tokens.isEmpty()) return null
            var tokenIdx = 0
            var minConf = Double.POSITIVE_INFINITY
            for (i in 0 until result.length()) {
                if (tokenIdx >= tokens.size) break
                val entry = result.optJSONObject(i) ?: continue
                val word = entry.optString("word", "").lowercase()
                if (word == tokens[tokenIdx]) {
                    if (!entry.has("conf")) return null
                    val conf = entry.optDouble("conf", Double.NaN)
                    if (conf.isNaN()) return null
                    if (conf < minConf) minConf = conf
                    tokenIdx++
                }
            }
            return if (tokenIdx == tokens.size) minConf else null
        }
    }
}
