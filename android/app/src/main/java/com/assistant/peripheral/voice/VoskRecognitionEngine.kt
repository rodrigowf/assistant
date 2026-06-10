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

            // 1) Feed the pre-buffer FIRST. This is the entire architectural
            //    point of switching off SpeechRecognizer: the silence monitor
            //    captures the user's leading edge while watching RMS, and
            //    Vosk consumes those frames before tapping into the live
            //    stream. Otherwise the leading "wake" of "wake up" is lost
            //    and Vosk has nothing to lock onto.
            if (preBuffer.isNotEmpty()) {
                var totalFrames = 0
                for (frame in preBuffer) {
                    val m = cycleFeeder.feed(frame, frame.size)
                    totalFrames += frame.size
                    if (m != null) {
                        Log.d(
                            TAG,
                            "Vosk match in pre-buffer: \"${m.matchedVariant}\" " +
                                "(realtime=${m.isRealtime}) in \"${m.rawText}\"",
                        )
                        return@withContext RecognitionResult.Matched(
                            matchedPhrase = m.matchedVariant,
                            isRealtime = m.isRealtime,
                            rawText = m.rawText,
                        )
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
                        return@withContext RecognitionResult.Matched(
                            matchedPhrase = match.matchedVariant,
                            isRealtime = match.isRealtime,
                            rawText = match.rawText,
                        )
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
