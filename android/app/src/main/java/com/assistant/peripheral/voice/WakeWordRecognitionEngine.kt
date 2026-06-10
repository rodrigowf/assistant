package com.assistant.peripheral.voice

import android.content.Intent
import android.media.AudioRecord

/**
 * Abstraction over the "Stage 2" recognizer in [WakeWordDetector].
 *
 * Plan: `assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md`, §4 V3a.
 *
 * Two implementations:
 *   - [SpeechRecognizerEngine]: wraps Android's `SpeechRecognizer`. Used as
 *     the legacy fallback path. Owns the mic exclusively during recognition.
 *   - [VoskRecognitionEngine] (V3b): wraps the in-process Vosk engine. Feeds
 *     off the same `AudioRecord` the silence monitor is already reading —
 *     no mic handoff, no leading-edge clipping.
 *
 * Lifecycle a [WakeWordDetector] drives:
 *
 *   val engine = ... // selected at start()
 *   engine.warm()                                 // optional pre-warm
 *   // ... silence monitor runs, detects activity ...
 *   val result = engine.recognize(audioRecord)    // blocks until a result
 *   when (result) { Matched -> ... ; NoMatch -> ... ; ... }
 *   // ... silence monitor re-arms or the detector pauses/stops ...
 *   engine.tearDown()                             // pause / stop / refresh
 *
 * Threading: callers invoke `warm`/`recognize`/`tearDown` from the silence-
 * monitor IO coroutine (or from `WakeWordDetector`'s `Dispatchers.Main`
 * scope for lifecycle calls). Each implementation documents its specific
 * thread-confinement requirements.
 */
internal interface WakeWordRecognitionEngine {

    /**
     * True if this engine takes exclusive ownership of the microphone during
     * `recognize`. Drives a key dispatch decision in
     * [WakeWordDetector.startSilenceMonitor]:
     *  - `true`  (Speech​Recognizer): release the `AudioRecord` before calling
     *            `recognize` so the recognizer can open its own mic.
     *  - `false` (Vosk):              keep the `AudioRecord` open and pass it
     *            to `recognize` so the recognizer feeds off the same stream
     *            the silence monitor was already reading. This is the entire
     *            point of the Vosk migration — eliminates the leading-edge
     *            clipping that SpeechRecognizer's IPC bind causes on Lollipop.
     */
    val needsExclusiveMic: Boolean

    /**
     * Pre-arm the engine in parallel with the silence-monitor loop. For
     * SpeechRecognizer this constructs the warm instance (Detour 6). For
     * Vosk this is a no-op (the engine has no pre-warm step beyond the
     * VoskModelLoader-cached `Model`).
     *
     * Idempotent. Safe to call from `Dispatchers.Main` (the only place
     * `SpeechRecognizer.createSpeechRecognizer` works on Lollipop).
     */
    suspend fun warm()

    /**
     * Run one recognition cycle. Returns when a result is available, a
     * watchdog fires, an error occurs, or the cycle times out.
     *
     * Contract for callers:
     *  - When `needsExclusiveMic == true`, the caller must `stop()+release()`
     *    its own `AudioRecord` BEFORE calling `recognize`, and pass `null`
     *    for [sharedAudioRecord]. The engine owns its own mic for the duration.
     *  - When `needsExclusiveMic == false`, the caller must pass a started
     *    `AudioRecord` in `sharedAudioRecord`. The engine reads from it but
     *    does NOT stop or release it. The caller retains ownership.
     */
    /**
     * @param preBuffer Optional pre-roll PCM frames captured by the silence
     *   monitor BEFORE handing off to recognition. Vosk feeds these first
     *   so the user's leading edge isn't lost. Each `ShortArray` is one
     *   read's worth at the engine's expected sample rate / format.
     *   SpeechRecognizer ignores this (it opens its own mic).
     */
    suspend fun recognize(
        sharedAudioRecord: AudioRecord? = null,
        preBuffer: List<ShortArray> = emptyList(),
    ): RecognitionResult

    /**
     * Tear down internal state (cancel watchdogs, destroy or release native
     * resources, etc.). Called on pause/stop/release and on `recognize`
     * completion for engines that don't keep state between cycles.
     *
     * Idempotent. Engines decide their own granularity — e.g. the SR engine
     * keeps the warm instance across cycles but destroys it on pause; Vosk
     * closes its `Recognizer` between cycles (cheap, no native teardown of
     * the shared `Model`).
     */
    suspend fun tearDown()

    /**
     * Mark the engine for a full rebuild on the next `warm`. Used by the
     * outer health checks and the post-voice re-arm path. For SR this
     * triggers Detour 6 safeguards B/C/E; for Vosk it forces a fresh
     * `Recognizer` (cheap).
     */
    fun markNeedsRefresh()

    /**
     * True when the engine has internal state that must be cleaned up by
     * a `tearDown` call. Drives the Inc 2 idempotency guard
     * `shouldShortCircuitFinishRecognition` — when this is false AND
     * recognition has already completed, redundant `tearDown` calls can
     * short-circuit safely.
     *
     * For SR: `speechRecognizer != null` (the instance is held across
     * cycles per Detour 6).
     * For Vosk: `recognizer != null` between cycles, briefly.
     */
    val hasPendingState: Boolean
}

/**
 * Outcome of one [WakeWordRecognitionEngine.recognize] cycle. The outer
 * orchestrator ([WakeWordDetector]) maps each variant to its post-cycle
 * action: re-arm immediately, re-arm with backoff, force a flat delay,
 * fire a health broadcast, etc.
 */
internal sealed class RecognitionResult {

    /**
     * The user said one of the configured wake/talk phrases. The engine
     * has already done the variant matching; the orchestrator just fires
     * the broadcast (via [matchedPhrase] / [isRealtime]).
     */
    data class Matched(
        val matchedPhrase: String,
        val isRealtime: Boolean,
        /** The raw recognizer output ("text" / "partial" / SR result string). */
        val rawText: String,
    ) : RecognitionResult()

    /**
     * Recognition completed with no wake/talk phrase in the output. The
     * orchestrator applies exponential backoff before re-arming
     * ([WakeWordDetector.POST_RECOGNITION_BASE_MS] schedule).
     */
    object NoMatch : RecognitionResult()

    /**
     * Recognition completed without ever capturing any speech (e.g. SR's
     * `ERROR_NO_SPEECH`). Distinct from `NoMatch` because:
     *   - it advances the Inc 8 health counter;
     *   - it uses a flat delay before re-arm (no exponential backoff).
     *
     * Vosk doesn't currently emit this — its analogous signal lands in V5
     * as a different mechanism.
     */
    object NoSpeech : RecognitionResult()

    /**
     * Recognition aborted via an SR-side error or an engine-internal
     * watchdog. The orchestrator uses a flat delay before re-arm.
     */
    data class Error(val message: String, val flatDelay: Boolean = true) : RecognitionResult()

    /**
     * The engine was cancelled mid-cycle (typically from `pause()` /
     * `stop()`). The orchestrator does NOT re-arm.
     */
    object Cancelled : RecognitionResult()
}

/**
 * Callback the engine uses to ask the orchestrator for state-mutation it
 * can't perform itself. Keeps the engine ignorant of `WakeWordState` and
 * the outer broadcasts.
 *
 * Kept minimal on purpose — anything richer should be returned from
 * [WakeWordRecognitionEngine.recognize] as a [RecognitionResult] variant
 * instead. The hooks here are for events that fire DURING a recognize
 * cycle (e.g. SR's `onBeginningOfSpeech`), not at its completion.
 */
internal interface RecognitionCallbacks {

    /**
     * The engine has confirmed audio is being received and recognition has
     * actively begun. Used by [WakeWordDetector] to:
     *  - stamp `beganSpeechAtMs` on the `WakeWordState.Recognizing` state.
     *
     * For Vosk, this fires on the first non-empty partial result (analogous
     * to SR's `onBeginningOfSpeech` callback).
     */
    fun onRecognitionStarted()

    /**
     * The engine wants the outer service to do a full detector rebuild —
     * its internal state is stuck and a refresh isn't enough. Fires the
     * Inc 8 `ACTION_RECOGNIZER_UNHEALTHY` broadcast.
     */
    fun onUnhealthy()
}
