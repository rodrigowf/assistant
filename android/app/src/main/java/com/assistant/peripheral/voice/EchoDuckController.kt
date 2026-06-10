package com.assistant.peripheral.voice

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Echo-ducking + mic-restore controller — the load-bearing piece of the
 * voice subsystem. Owns the mic gain state during an active voice
 * session: drops it to `echoDuckingGain` while the assistant is
 * speaking, then waits for the speaker hardware buffer to fully drain
 * before restoring it. The drain wait is non-negotiable per
 * `feedback_dont_shortcut_echo_ducking.md` — it's what stopped the
 * residual speaker tail from feeding back into the open mic and
 * tripping Gemini Live / Qwen server-side VAD into self-interrupts.
 *
 * Extracted from `WebSocketPcmProvider.kt` (Increment H of the voice
 * subsystem refactor). Behavior is byte-identical with the pre-Inc-H
 * methods at L254–L400 of HEAD `cff6afd`. The log lines emitted here
 * are the parity oracle for the BEFORE/AFTER on-device validation —
 * any rewording would break the validation harness.
 *
 * Inputs (callable so the caller owns the source of truth):
 *  - [getPlaybackHeadPosition]: returns the current AudioTrack head in
 *    PCM frames, or `null` if the AudioTrack is gone (released).
 *  - [getTotalFramesWritten]: returns the cumulative frame counter
 *    maintained by the playback loop (post-`flush()` it MUST be reset
 *    to 0 by the caller — see L239 of HEAD).
 *  - [log]: defaults to `android.util.Log.i/d`; injected for test
 *    assertion on the exact emitted lines.
 *
 * State owned:
 *  - `micGainLevel` — current applied gain on the capture path.
 *  - `gainBeforeSpeaking` — the user's "restore-to" gain, set on duck
 *    rising edge, mutated mid-duck via [setMicGain].
 *  - `echoDuckingGain` — the user's "duck-to" gain, settable mid-duck
 *    (applies immediately).
 *  - `micRestoreJob` — the currently-active drain-restore coroutine.
 *
 * Threading: `duck`, `restoreImmediately`, `scheduleRestore`,
 * `cancelPendingRestore`, and the gain setters are intended to be
 * called from the same coroutine context as the capture loop (in
 * practice: the WS dispatch thread / capture coroutine). The drain
 * coroutine itself runs on [scope].
 */
class EchoDuckController(
    private val scope: CoroutineScope,
    private val getPlaybackHeadPosition: () -> Long?,
    private val getTotalFramesWritten: () -> Long,
    private val tag: String = "EchoDuck",
    private val logger: ((String) -> Unit)? = null,
) {

    /** `[MIC_STATE]` lines — the parity oracles. Originally emitted as
     *  `Log.i(tag, ...)`. AFTER logcat must match BEFORE byte-for-byte
     *  on these. */
    private fun logI(line: String) {
        if (logger != null) logger.invoke(line) else Log.i(tag, line)
    }

    /** Debug-tagged housekeeping lines ("Mic gain set to: X", etc.).
     *  Originally `Log.d(tag, ...)`. Routed through the same test hook
     *  so unit tests can assert on them, but production uses Log.d to
     *  match pre-Inc-H verbosity exactly. */
    private fun logD(line: String) {
        if (logger != null) logger.invoke(line) else Log.d(tag, line)
    }
    @Volatile private var micGainLevel: Float = 1.0f
    @Volatile private var echoDuckingGain: Float = 0.05f
    @Volatile private var gainBeforeSpeaking: Float? = null
    private var micRestoreJob: Job? = null

    /** Current gain applied to captured mic chunks. Read every chunk by
     *  the capture loop. */
    val currentMicGain: Float get() = micGainLevel

    /** When non-null, mic is currently ducked and this is the value the
     *  next restore will return to. Exposed for the capture loop's
     *  agent-speaking probe log. */
    val savedGainOrNull: Float? get() = gainBeforeSpeaking

    /** True iff the mic is currently ducked. */
    val isDucked: Boolean get() = gainBeforeSpeaking != null

    /** True iff a drain-restore is currently scheduled / in-flight. The
     *  capture loop uses this to avoid re-scheduling on every poll while
     *  the staleness window is still open and a restore is already
     *  running. Matches pre-Inc-H `micRestoreJob == null` check at L930. */
    val isRestorePending: Boolean get() = micRestoreJob?.isActive == true

    // --- Public configuration ---------------------------------------------

    /**
     * Update the user's chosen mic gain (the "restore-to" value). If
     * the mic is currently ducked, this updates the saved value so the
     * new gain takes effect on restore; it does NOT change the current
     * ducking gain. If not ducked, applies immediately.
     *
     * Pre-Inc-H behavior at L607–L619 of HEAD.
     */
    fun setMicGain(level: Float) {
        val clamped = level.coerceIn(0.0f, 2.0f)
        if (gainBeforeSpeaking != null) {
            gainBeforeSpeaking = clamped
            logD("Mic gain set to: $clamped (deferred — applies on restore)")
        } else {
            micGainLevel = clamped
            logD("Mic gain set to: $clamped")
        }
    }

    /**
     * Update the user's chosen ducking gain. If currently ducked, the
     * new value is applied immediately so the slider responds in real
     * time. Per L623–L636 of HEAD.
     */
    fun setEchoDuckingGain(gain: Float) {
        val clamped = gain.coerceIn(0.0f, 1.0f)
        echoDuckingGain = clamped
        if (gainBeforeSpeaking != null) {
            micGainLevel = clamped
            logD("Echo ducking gain set to: $clamped (applied immediately, ducking active)")
        } else {
            logD("Echo ducking gain set to: $clamped")
        }
    }

    /** Surface for the (read-only) "what should the user see as gain" UI
     *  query. Matches the pre-Inc-H semantics at L621 of HEAD. */
    fun getEffectiveMicGain(): Float = gainBeforeSpeaking ?: micGainLevel

    // --- Duck / restore ---------------------------------------------------

    /**
     * Duck the mic for assistant speech. Saves the current gain to
     * `gainBeforeSpeaking`, applies `echoDuckingGain`, cancels any
     * pending restore.
     *
     * Idempotent: a second call while already ducked is a no-op (no
     * log, no gain change). Pre-Inc-H at L254–L261 of HEAD.
     */
    fun duck() {
        if (gainBeforeSpeaking != null) return  // already ducked
        gainBeforeSpeaking = micGainLevel
        micGainLevel = echoDuckingGain
        micRestoreJob?.cancel()
        micRestoreJob = null
        logI("[MIC_STATE] DUCK → gain: ${gainBeforeSpeaking}→$echoDuckingGain")
    }

    /**
     * Cancel any pending drain-restore job. Used when a new speaker
     * chunk arrives mid-drain — the staleness was a false signal.
     * Pre-Inc-H at L588–L589 of HEAD (inline cancel in pushSpeakerChunk).
     */
    fun cancelPendingRestore() {
        micRestoreJob?.cancel()
        micRestoreJob = null
    }

    /**
     * Restore mic gain immediately, no drain wait. Used on barge-in
     * (the speaker output is being flushed anyway) and as the terminal
     * call of the drain-restore loop.
     *
     * No-op if not currently ducked. Pre-Inc-H at L392–L400 of HEAD.
     */
    fun restoreImmediately(reason: String) {
        micRestoreJob?.cancel()
        micRestoreJob = null
        val saved = gainBeforeSpeaking ?: return
        micGainLevel = saved
        gainBeforeSpeaking = null
        logI("[MIC_STATE] RESTORE_IMMEDIATE($reason) → gain: $echoDuckingGain→$micGainLevel")
    }

    /**
     * Schedule a mic restore once the speaker hardware buffer has
     * fully drained. The drain loop has no timeout: previous 4s and
     * 20s caps panic-restored the mic mid-speech on legitimate long
     * agent turns, leaking the speaker tail into the open mic and
     * triggering Gemini's server-side VAD into a self-interrupt.
     *
     * Restore policy (preserved verbatim from L316–L390 of HEAD):
     *
     *   (a) writes-quiet: `totalFramesWritten` has stopped growing for
     *       [MIC_RESTORE_WRITES_QUIET_MS] — the playback loop has run
     *       out of queued chunks AND no new chunks are arriving.
     *   (b) one of:
     *       - primary: `head >= written` — DAC has played everything;
     *       - fallback: `head` has been stuck for the same quiet
     *         window — Lollipop post-underrun case where the head
     *         pointer freezes at the underrun frame.
     *
     * After both (a) and (b) are satisfied, wait [MIC_RESTORE_TAIL_MS]
     * for BT transducer latency + room reverb, then restore.
     *
     * If [cancelPendingRestore] / [duck] / [restoreImmediately] is
     * called mid-drain, the job is cancelled.
     */
    fun scheduleRestore(reason: String) {
        if (gainBeforeSpeaking == null) return
        micRestoreJob?.cancel()
        val startedHead = (getPlaybackHeadPosition() ?: 0L) and 0xFFFFFFFFL
        logI("[MIC_STATE] RESTORE_DRAIN($reason) waiting; written=${getTotalFramesWritten()} head=$startedHead")
        micRestoreJob = scope.launch {
            val startMs = System.currentTimeMillis()
            var lastWritten = getTotalFramesWritten()
            var lastWrittenAtMs = startMs
            var lastHead = startedHead
            var lastHeadAtMs = startMs
            var writesQuietLoggedAt: Long = 0L
            var pollCount = 0
            while (true) {
                val nowMs = System.currentTimeMillis()
                val maybeHead = getPlaybackHeadPosition()
                if (maybeHead == null) {
                    logD("[MIC_STATE] RESTORE_DRAIN($reason) AudioTrack gone; restoring")
                    break
                }

                val written = getTotalFramesWritten()
                val head = maybeHead and 0xFFFFFFFFL
                pollCount++

                if (written != lastWritten) {
                    lastWritten = written
                    lastWrittenAtMs = nowMs
                }
                if (head != lastHead) {
                    lastHead = head
                    lastHeadAtMs = nowMs
                }

                if (pollCount % 10 == 0) {
                    logD("[MIC_STATE] RESTORE_DRAIN($reason) poll=$pollCount head=$head written=$written remaining=${written - head} writesQuiet=${nowMs - lastWrittenAtMs}ms")
                }

                val writesQuietForMs = nowMs - lastWrittenAtMs
                val writesAreQuiet = writesQuietForMs >= MIC_RESTORE_WRITES_QUIET_MS

                if (writesAreQuiet && writesQuietLoggedAt == 0L) {
                    writesQuietLoggedAt = nowMs
                    logD("[MIC_STATE] RESTORE_DRAIN($reason) writes quiet at written=$written head=$head remaining=${written - head}")
                }

                // (a) AND (b)-primary
                if (writesAreQuiet && head >= written) {
                    logI("[MIC_STATE] RESTORE_DRAIN($reason) AudioTrack drained at head=$head poll=$pollCount; tail wait ${MIC_RESTORE_TAIL_MS}ms")
                    delay(MIC_RESTORE_TAIL_MS)
                    break
                }

                // (a) AND (b)-fallback
                if (writesAreQuiet && nowMs - lastHeadAtMs >= MIC_RESTORE_WRITES_QUIET_MS) {
                    logI("[MIC_STATE] RESTORE_DRAIN($reason) head stuck at $head (written=$written) AND writes quiet; treating as drained; tail wait ${MIC_RESTORE_TAIL_MS}ms")
                    delay(MIC_RESTORE_TAIL_MS)
                    break
                }

                delay(MIC_RESTORE_DRAIN_POLL_MS)
            }
            restoreImmediately("drained:$reason")
        }
    }

    /**
     * Reset all transient state for a new session. Should be called
     * before `connect()` begins to put a fresh AudioTrack/AudioRecord
     * pair into service. Matches the inline reset at L443–L448 of HEAD.
     */
    fun resetForNewSession() {
        gainBeforeSpeaking = null
        micRestoreJob?.cancel()
        micRestoreJob = null
    }

    /**
     * Cleanup for session teardown. If we were mid-duck, restore the
     * saved gain so a reconnect doesn't start with the attenuated
     * value as the "real" one. Matches L1042–L1049 of HEAD.
     */
    fun cleanup() {
        micRestoreJob?.cancel()
        micRestoreJob = null
        gainBeforeSpeaking?.let { saved ->
            micGainLevel = saved
            gainBeforeSpeaking = null
        }
    }

    companion object {
        /** After the speaker hardware buffer has finished draining,
         *  wait this long before restoring the mic. Pre-Inc-H L155. */
        const val MIC_RESTORE_TAIL_MS = 600L

        /** Poll interval while waiting for the buffer to drain.
         *  Pre-Inc-H L173. */
        const val MIC_RESTORE_DRAIN_POLL_MS = 80L

        /** How long `totalFramesWritten` must stay constant before we
         *  believe the playback loop is genuinely done writing.
         *  Pre-Inc-H L182. */
        const val MIC_RESTORE_WRITES_QUIET_MS = 400L
    }
}
