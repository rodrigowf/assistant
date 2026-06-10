package com.assistant.peripheral.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.util.Base64
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max

/**
 * Voice-session mic capture — owns the [AudioRecord] lifecycle, the
 * capture loop, and the per-second `[MIC_PROBE]` RMS diagnostic.
 *
 * Extracted from `WebSocketPcmProvider.kt` (Increment H, pt 3 of the
 * voice subsystem refactor). Behavior is byte-identical with the
 * pre-Inc-H `startMic` method at L870–L962 of commit `bf64ca9`. Log
 * lines preserved verbatim.
 *
 * Bridges back to the provider via callable accessors:
 *  - [isUserMuted] — `true` means drop the chunk (don't send upstream).
 *  - [getMicGain] — the gain to apply per chunk (the ducker owns this).
 *  - [isAgentSpeaking] — for the staleness probe + the MIC_PROBE log.
 *  - [getLastSpeakerChunkAtMs] — for the staleness probe.
 *  - [onStalenessTriggered] — fired when the capture loop detects the
 *    agent has gone quiet long enough to schedule a restore. The
 *    provider toggles `agentSpeaking = false` and calls
 *    `ducker.scheduleRestore("stale")` in response.
 *  - [isRestorePending] — to avoid re-scheduling on every probe poll
 *    while a restore is already running.
 *  - [sendMicChunk] — the upstream relay.
 *
 * Threading: [start] launches a coroutine on [scope] that blocks on
 * `AudioRecord.read()`. The loop exits when [running] flips to false
 * (caller-owned) OR the job is cancelled.
 *
 * @param inSampleRate Mic sample rate in Hz; chosen by the provider
 *        based on the upstream session config.
 */
class MicCapture(
    private val scope: CoroutineScope,
    private val tag: String,
    private val inSampleRate: Int,
    private val running: AtomicBoolean,
    private val isUserMuted: () -> Boolean,
    private val getMicGain: () -> Float,
    private val isAgentSpeaking: () -> Boolean,
    private val getLastSpeakerChunkAtMs: () -> Long,
    private val isRestorePending: () -> Boolean,
    private val onStalenessTriggered: () -> Unit,
    private val sendMicChunk: (String) -> Unit,
) {
    private var audioRecord: AudioRecord? = null
    private var captureJob: Job? = null

    /**
     * Open the AudioRecord and spawn the capture loop. Pre-Inc-H
     * L870–L962.
     *
     * Throws if AudioRecord fails to initialize (caller treats it as
     * a fatal connect failure and falls through to cleanup).
     */
    fun start() {
        val minBuf = AudioRecord.getMinBufferSize(
            inSampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // 4x min for headroom against scheduling jitter.
        val bufSize = max(minBuf * 4, inSampleRate * 2 / 5)

        // VOICE_RECOGNITION on Lollipop (API < 24) — Samsung's HAL
        // routes VOICE_COMMUNICATION through aggressive processing
        // that silences audio.
        val source = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        val record = AudioRecord(
            source,
            inSampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize,
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            throw IllegalStateException("AudioRecord failed to initialize (state=${record.state})")
        }
        record.startRecording()
        audioRecord = record
        Log.d(tag, "Mic started: rate=${inSampleRate}Hz source=$source bufSize=$bufSize")

        captureJob = scope.launch {
            val chunkBytes = MIC_CHUNK_FRAMES * 2  // 16-bit samples
            val buf = ByteArray(chunkBytes)
            // Per-second RMS probe: accumulate over ~50 chunks (1s at 20ms each)
            // then emit one line. Pre-Inc-H L906–L948.
            var probeRmsAccum = 0.0
            var probeChunks = 0
            var probePeak = 0
            while (kotlinx.coroutines.currentCoroutineContext()[Job]?.isActive != false
                && running.get()
            ) {
                val read = record.read(buf, 0, chunkBytes)
                if (read <= 0) {
                    if (read == AudioRecord.ERROR_INVALID_OPERATION ||
                        read == AudioRecord.ERROR_BAD_VALUE) {
                        Log.w(tag, "AudioRecord.read error: $read — ending capture loop")
                        break
                    }
                    continue
                }
                if (isUserMuted()) continue  // drop chunks while muted

                // Detect agent-speech-ended via staleness of the
                // speaker queue. Pre-Inc-H L928–L934.
                if (isAgentSpeaking()) {
                    val sinceLastChunk = System.currentTimeMillis() - getLastSpeakerChunkAtMs()
                    if (sinceLastChunk > AGENT_SPEECH_STALE_MS && !isRestorePending()) {
                        onStalenessTriggered()
                    }
                }

                // Pre-gain RMS probe. Pre-Inc-H L936–L948.
                val chunkStats = computeRmsAndPeakPcm16(buf, 0, read)
                probeRmsAccum += chunkStats.first
                probePeak = maxOf(probePeak, chunkStats.second)
                probeChunks++
                if (probeChunks >= 50) {
                    val avgRms = probeRmsAccum / probeChunks
                    Log.i(tag, "[MIC_PROBE] rms_avg=${avgRms.toInt()} peak=$probePeak gain=${getMicGain()} ducking=${isAgentSpeaking()}")
                    probeRmsAccum = 0.0
                    probeChunks = 0
                    probePeak = 0
                }

                val gain = getMicGain()
                if (gain != 1.0f) applyGainPcm16(buf, 0, read, gain)

                val b64 = Base64.encodeToString(buf, 0, read, Base64.NO_WRAP)
                try {
                    sendMicChunk(b64)
                } catch (e: Exception) {
                    Log.w(tag, "sendMicChunk failed: ${e.message}")
                }
            }
            Log.d(tag, "Mic capture loop exited")
        }
    }

    /**
     * Tear down the AudioRecord and the capture loop. Pre-Inc-H
     * L1036–L1067 (the mic-related parts).
     */
    fun cleanup() {
        captureJob?.cancel()
        captureJob = null
        try {
            audioRecord?.let {
                if (it.recordingState == AudioRecord.RECORDSTATE_RECORDING) it.stop()
                it.release()
            }
        } catch (e: Exception) {
            Log.w(tag, "Error stopping AudioRecord: ${e.message}")
        }
        audioRecord = null
    }

    /** Returns (rms, peak) of a PCM16 little-endian buffer slice.
     *  RMS is the diagnostic for "is there signal here at all"; peak
     *  catches transients that average away. Both in raw int16 units.
     *  Pre-Inc-H L995–L1016. */
    private fun computeRmsAndPeakPcm16(buf: ByteArray, offset: Int, length: Int): Pair<Double, Int> {
        var i = offset
        val end = offset + length
        var sumSq = 0.0
        var peak = 0
        var n = 0
        while (i < end - 1) {
            val lo = buf[i].toInt() and 0xff
            val hi = buf[i + 1].toInt()
            val sample = ((hi shl 8) or lo).toShort().toInt()
            sumSq += (sample * sample).toDouble()
            val abs = if (sample < 0) -sample else sample
            if (abs > peak) peak = abs
            n++
            i += 2
        }
        val rms = if (n > 0) kotlin.math.sqrt(sumSq / n) else 0.0
        return Pair(rms, peak)
    }

    private fun applyGainPcm16(buf: ByteArray, offset: Int, length: Int, gain: Float) {
        var i = offset
        val end = offset + length
        while (i < end - 1) {
            val lo = buf[i].toInt() and 0xff
            val hi = buf[i + 1].toInt()
            val sample = ((hi shl 8) or lo).toShort().toInt()
            val amplified = (sample * gain).toInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
            buf[i] = (amplified and 0xff).toByte()
            buf[i + 1] = ((amplified shr 8) and 0xff).toByte()
            i += 2
        }
    }

    companion object {
        /** Mic chunk size: 20ms at 24kHz mono PCM16 = 480 frames =
         *  960 bytes. Matches the web frontend's 20ms cadence.
         *  Pre-Inc-H L142. */
        const val MIC_CHUNK_FRAMES = 480

        /** Agent-speaking → idle detection. If no speaker chunk has
         *  been pushed in this many ms, treat the agent's turn as
         *  ended. 800ms covers the natural gaps between Gemini Live's
         *  bursty chunk delivery without falsely ending mid-turn.
         *  Pre-Inc-H L148. */
        const val AGENT_SPEECH_STALE_MS = 800L
    }
}
