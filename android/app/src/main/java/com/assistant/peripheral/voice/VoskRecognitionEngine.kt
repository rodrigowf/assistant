package com.assistant.peripheral.voice

import android.media.AudioRecord
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.vosk.Model
import kotlin.math.sqrt

/**
 * Vosk-backed implementation of [WakeWordRecognitionEngine].
 *
 * Plan: `assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md`, §4 V3b.
 *
 * The whole point of the Vosk migration: this engine consumes the same
 * `AudioRecord` stream the silence monitor is already reading, so the user's
 * wake phrase reaches the recognizer from sample zero — no IPC bind latency,
 * no leading-edge clipping. Compare to `SpeechRecognizerEngine` which opens
 * its own mic and incurs Google's STT IPC bind (800ms–3s on Lollipop).
 *
 * Internal architecture:
 *   - Wraps [VoskWakeWordEngine] (the pure per-buffer feeder).
 *   - `recognize(sharedAudioRecord)` runs the read loop synchronously inside
 *     the same coroutine that called us. We don't spawn — the caller already
 *     holds the silence-monitor IO coroutine; we just take over its read
 *     loop for the recognition window.
 *
 * Threading:
 *   - `Recognizer` is NOT thread-safe. Construct, feed, and close from a
 *     single thread. We're called from the silence-monitor IO coroutine
 *     (`Dispatchers.IO` in [WakeWordDetector.startSilenceMonitor]), which
 *     is fine — we stay on that dispatcher inside `recognize`.
 *   - `warm` and `tearDown` are dispatcher-agnostic (no Main-only Android
 *     APIs touched here).
 */
internal class VoskRecognitionEngine(
    private val model: Model,
    private val talkVariants: List<String>,
    private val wakeVariants: List<String>,
    private val callbacks: RecognitionCallbacks,
    private val sampleRate: Float = 16_000f,
) : WakeWordRecognitionEngine {

    companion object {
        private const val TAG = "VoskRecogEngine"

        /**
         * Max duration to feed audio to Vosk after activity detection before
         * giving up and re-arming. The user's wake phrase is short (1–2 words
         * ≈ 1 s); 5 s is a generous bound that covers a slow speaker without
         * burning power on a runaway match attempt.
         */
        private const val VOSK_RECOGNITION_TIMEOUT_MS = 5_000L

        /**
         * `onRecognitionStarted` callback gate: fire it on the first non-empty
         * partial result, analogous to SR's `onBeginningOfSpeech`. Only used
         * for state-stamping; not a watchdog trigger here (Vosk doesn't hang
         * the way SR does).
         */
        private const val RMS_STARTED_THRESHOLD = 30.0

        /**
         * How much audio to keep capturing AFTER Vosk's (possibly mid-word)
         * partial match, so the Whisper confirmation gate hears the complete
         * phrase. 400ms comfortably covers the tail of "wake up" / "my friend"
         * once the leading edge already triggered.
         */
        private const val MATCH_TAIL_MS = 400L
    }

    /**
     * The per-cycle feeder. Constructed lazily on first `recognize` so we
     * don't allocate a `Recognizer` instance until we actually need one.
     * Closed at the end of every cycle so each call starts with a fresh
     * recognizer state (Vosk has no "reset to factory" beyond `reset()`,
     * and `close+reopen` is cheap — model is shared and stays cached).
     */
    private var feeder: VoskWakeWordEngine? = null

    @Volatile
    private var needsRefresh = false

    @Volatile
    private var cancelled = false

    override val needsExclusiveMic: Boolean
        get() = false

    override val hasPendingState: Boolean
        get() = feeder != null

    override fun markNeedsRefresh() {
        needsRefresh = true
    }

    override suspend fun warm() {
        // Vosk has no pre-warm step beyond the model load that
        // VoskModelLoader already did. The feeder is constructed lazily on
        // first `recognize` because it can't outlive a cycle without
        // accumulating stale partials.
        if (needsRefresh) {
            closeFeeder()
            needsRefresh = false
        }
        Log.d(TAG, "Vosk engine warm (model already loaded; feeder lazy on first cycle)")
    }

    override suspend fun recognize(
        sharedAudioRecord: AudioRecord?,
        preBuffer: List<ShortArray>,
    ): RecognitionResult {
        val audioRecord = sharedAudioRecord
            ?: error("VoskRecognitionEngine requires a shared AudioRecord")
        cancelled = false
        return withContext(Dispatchers.IO) {
            // Build a fresh feeder per cycle so we don't carry partial state
            // from the previous attempt. Vosk's `reset()` mostly does this
            // but a fresh `Recognizer` is unambiguous and cheap (model is
            // shared & cached).
            val cycleFeeder = VoskWakeWordEngine(
                model = model,
                sampleRate = sampleRate,
                talkVariants = talkVariants,
                wakeVariants = wakeVariants,
            ).also { feeder = it }

            // Every frame Vosk consumes this cycle, oldest first — pre-buffer
            // then live reads. On a match this is handed back verbatim to the
            // Whisper confirmation gate so it transcribes exactly what Vosk
            // flagged (defensively copied — the live `buffer` is reused).
            val captured = ArrayList<ShortArray>(preBuffer.size + 8)

            // 1) Feed the pre-buffer FIRST. This is the entire architectural
            //    point of switching off SpeechRecognizer: the silence monitor
            //    captures the user's leading edge while watching RMS, and
            //    Vosk consumes those frames before tapping into the live
            //    stream. Otherwise the leading "wake" of "wake up" is lost
            //    and Vosk has nothing to lock onto.
            if (preBuffer.isNotEmpty()) {
                var totalFrames = 0
                for (frame in preBuffer) {
                    captured.add(frame)
                    val m = cycleFeeder.feed(frame, frame.size)
                    totalFrames += frame.size
                    if (m != null) {
                        Log.d(
                            TAG,
                            "Vosk match in pre-buffer: \"${m.matchedVariant}\" " +
                                "(realtime=${m.isRealtime}) in \"${m.rawText}\"",
                        )
                        return@withContext matchedResult(m, audioRecord, captured)
                    }
                }
                Log.d(TAG, "Pre-buffer fed: $totalFrames samples " +
                    "(${totalFrames * 1000 / sampleRate.toInt()}ms of leading-edge audio)")
            }

            val started = System.currentTimeMillis()
            // 6400 samples = 400ms at 16kHz mono — large enough for Vosk to
            // make progress per call, small enough to keep loop latency low.
            val buffer = ShortArray(6400)
            var firedStarted = false
            var lastPartial: String? = null
            try {
                while (!cancelled) {
                    if (System.currentTimeMillis() - started >= VOSK_RECOGNITION_TIMEOUT_MS) {
                        // Diagnostic: log what Vosk actually heard so we can
                        // tell whether it just didn't recognize, the audio
                        // was bad, or the wake word never crossed.
                        val tail = feeder?.peekText().orEmpty()
                        Log.d(
                            TAG,
                            "Vosk recognition window elapsed — heard: \"$tail\" " +
                                "(last partial: \"${lastPartial.orEmpty()}\")",
                        )
                        return@withContext RecognitionResult.NoMatch
                    }
                    val read = audioRecord.read(buffer, 0, buffer.size)
                    if (read <= 0) {
                        // Spurious 0/-1 read — yield briefly so we don't tight-loop.
                        delay(10L)
                        continue
                    }
                    val frame = buffer.copyOf(read)
                    captured.add(frame)
                    if (!firedStarted && rms(buffer, read) >= RMS_STARTED_THRESHOLD) {
                        firedStarted = true
                        callbacks.onRecognitionStarted()
                    }
                    val match = cycleFeeder.feed(buffer, read)
                    if (match != null) {
                        Log.d(
                            TAG,
                            "Vosk match: \"${match.matchedVariant}\" " +
                                "(realtime=${match.isRealtime}) in \"${match.rawText}\"",
                        )
                        return@withContext matchedResult(match, audioRecord, captured)
                    }
                    // Track partials between feeds for diagnostic logging.
                    val partial = cycleFeeder.peekText()
                    if (partial.isNotEmpty() && partial != lastPartial) {
                        Log.d(TAG, "Vosk partial: \"$partial\"")
                        lastPartial = partial
                    }
                }
                RecognitionResult.Cancelled
            } finally {
                closeFeeder()
            }
        }
    }

    /**
     * Package a Vosk match into a [RecognitionResult.Matched], first reading a
     * short tail of audio past the trigger so the Whisper confirmation gate
     * hears the full phrase.
     *
     * Vosk fires on the earliest *partial* that contains the phrase, which can
     * land mid-word ("wake u…"). Whisper transcribing only up to that instant
     * might miss the tail and reject a real wake word. So we keep reading for
     * [MATCH_TAIL_MS] more, appending to [captured], before handing it off.
     * This runs off the same shared AudioRecord and adds only a few hundred ms.
     */
    private suspend fun matchedResult(
        match: VoskWakeWordEngine.Match,
        audioRecord: AudioRecord,
        captured: MutableList<ShortArray>,
    ): RecognitionResult {
        val tailBuffer = ShortArray(3200)  // 200ms at 16kHz
        val tailEnd = System.currentTimeMillis() + MATCH_TAIL_MS
        while (!cancelled && System.currentTimeMillis() < tailEnd) {
            val read = audioRecord.read(tailBuffer, 0, tailBuffer.size)
            if (read <= 0) { delay(10L); continue }
            captured.add(tailBuffer.copyOf(read))
        }
        val totalSamples = captured.sumOf { it.size }
        Log.d(
            TAG,
            "Captured ${captured.size} frames / $totalSamples samples " +
                "(${totalSamples * 1000 / sampleRate.toInt()}ms) for Whisper confirmation",
        )
        return RecognitionResult.Matched(
            matchedPhrase = match.matchedVariant,
            isRealtime = match.isRealtime,
            rawText = match.rawText,
            capturedPcm = captured.toList(),
        )
    }

    override suspend fun tearDown() {
        cancelled = true
        closeFeeder()
    }

    private fun closeFeeder() {
        feeder?.close()
        feeder = null
    }

    private fun rms(buffer: ShortArray, count: Int): Double {
        if (count <= 0) return 0.0
        var sum = 0.0
        for (i in 0 until count) {
            val v = buffer[i].toDouble()
            sum += v * v
        }
        return sqrt(sum / count)
    }
}
