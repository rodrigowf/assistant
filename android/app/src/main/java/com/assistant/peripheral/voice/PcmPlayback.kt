package com.assistant.peripheral.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.max

/**
 * PCM speaker playback — owns the [AudioTrack] lifecycle, the
 * `speakerQueue`, and the non-blocking playback coroutine.
 *
 * Extracted from `WebSocketPcmProvider.kt` (Increment H, pt 3 of the
 * voice subsystem refactor). Behavior is byte-identical with the
 * pre-Inc-H `startSpeaker`/`buildAudioTrack`/`flushSpeakerOutput`/
 * `stopSpeakerOnly` methods at L222–L250, L678–L687, L706–L866 of
 * commit `bf64ca9`. The log lines emitted here are preserved verbatim
 * — the BEFORE/AFTER on-device validation diffs against them.
 *
 * The 1.5s hardware buffer (`max(minBuf * 4, bytesPerSecond * 1.5)`)
 * plus the unbounded Channel act as the queue. `WRITE_NON_BLOCKING`
 * (API 23+) means a full hardware buffer doesn't stall the playback
 * coroutine — leftover bytes get parked and retried on the next loop
 * tick after the AudioTrack drains. On API 21/22 (Samsung A300M) the
 * 4-arg `write` overload doesn't exist; the fallback is the blocking
 * 3-arg write, which never returns partial 0.
 *
 * @param scope CoroutineScope used to launch the playback coroutine.
 * @param tag Log tag (matches the provider's tag).
 * @param outSampleRate Speaker sample rate in Hz; chosen by the
 *        provider based on the upstream session config.
 */
class PcmPlayback(
    private val scope: CoroutineScope,
    private val tag: String,
    private val outSampleRate: Int,
) {
    /**
     * Queue of decoded PCM speaker chunks waiting to be written to
     * [AudioTrack]. Decoupling the WS receive thread from the
     * AudioTrack.write() blocking call is critical: writing on the
     * main thread (which the WS event dispatch runs on by default)
     * caused 4,500-frame UI freezes mid-conversation. Pre-Inc-H L96–L106.
     *
     * UNLIMITED capacity intentionally — a 200ms hiccup might queue
     * ~10 chunks; we want to never drop audio at this layer (the
     * upstream provider already controls our backpressure).
     */
    private val speakerQueue = Channel<ByteArray>(capacity = Channel.UNLIMITED)

    @Volatile private var audioTrack: AudioTrack? = null
    private var playbackJob: Job? = null

    /**
     * Total frames written to the [AudioTrack] over this session,
     * used to compare against `audioTrack.playbackHeadPosition` to
     * decide when the hardware buffer is truly empty. Resets on
     * [flush] since `AudioTrack.flush()` resets the head position too.
     * Pre-Inc-H L132–L137.
     */
    @Volatile private var totalFramesWritten: Long = 0L

    /** Read by the EchoDuckController to compare against playback
     *  head position. */
    fun getTotalFramesWritten(): Long = totalFramesWritten

    /** Read by the EchoDuckController; returns null when the
     *  AudioTrack is not yet built or has been released. */
    fun getPlaybackHeadPosition(): Long? =
        audioTrack?.takeIf { it.state == AudioTrack.STATE_INITIALIZED }
            ?.playbackHeadPosition?.toLong()

    /**
     * Enqueue a PCM byte array for playback. Non-blocking; the
     * playback coroutine drains the queue on its own dispatcher.
     * Returns false iff the queue is closed (during cleanup).
     */
    fun enqueue(pcm: ByteArray): Boolean {
        val result = speakerQueue.trySend(pcm)
        if (result.isFailure) {
            Log.w(tag, "speakerQueue full or closed; dropping chunk (${pcm.size}B)")
            return false
        }
        return true
    }

    /**
     * Drain queued + in-flight speaker audio. Called on barge-in so
     * the previous response cuts mid-sentence instead of finishing
     * on top of the new turn. Pause + flush + play is the documented
     * dance for clearing the [AudioTrack] hardware buffer in
     * [AudioTrack.MODE_STREAM]. Returns the number of chunks dropped
     * from the queue (caller logs it). Pre-Inc-H L222–L245.
     */
    fun flush(): Int {
        var dropped = 0
        while (true) {
            val r = speakerQueue.tryReceive()
            if (r.isFailure || r.isClosed) break
            dropped++
        }
        try {
            audioTrack?.let {
                if (it.state == AudioTrack.STATE_INITIALIZED) {
                    it.pause()
                    it.flush()
                    it.play()
                    // flush() resets playbackHeadPosition to 0 — keep
                    // our write counter in sync so the drain loop's
                    // comparison stays valid.
                    totalFramesWritten = 0L
                }
            }
        } catch (e: Exception) {
            Log.w(tag, "AudioTrack flush failed: ${e.message}")
        }
        return dropped
    }

    /**
     * Build an [AudioTrack] for the current speaker mode and start
     * the playback loop. Pre-Inc-H L756–L866.
     *
     *  - CALL  → `USAGE_VOICE_COMMUNICATION` / `CONTENT_TYPE_SPEECH`
     *            (legacy: `STREAM_VOICE_CALL`).
     *  - MEDIA → `USAGE_MEDIA` / `CONTENT_TYPE_MUSIC`
     *            (legacy: `STREAM_MUSIC`).
     */
    fun start(
        speakerMode: AudioRouter.SpeakerMode,
        preferredOutputDevice: android.media.AudioDeviceInfo?,
    ) {
        val minBuf = AudioTrack.getMinBufferSize(
            outSampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // 1.5s hardware buffer — gives plenty of jitter headroom for
        // the bursty upstream delivery pattern. Pre-Inc-H L762–L767.
        val bytesPerSecond = outSampleRate * 2  // mono PCM16
        val bufSize = max(minBuf * 4, (bytesPerSecond * 1.5).toInt())

        val track = buildAudioTrack(bufSize, speakerMode)
        // Pin the output endpoint when the router gave us a specific
        // device. setPreferredDevice is API 23+.
        if (preferredOutputDevice != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                val ok = track.setPreferredDevice(preferredOutputDevice)
                Log.d(tag, "AudioTrack pinned to ${preferredOutputDevice.productName} (type=${preferredOutputDevice.type}) → $ok")
            } catch (e: Exception) {
                Log.w(tag, "AudioTrack.setPreferredDevice failed: ${e.message}")
            }
        }
        track.play()
        audioTrack = track
        Log.d(tag, "Speaker started: rate=${outSampleRate}Hz bufSize=$bufSize mode=$speakerMode")

        // Drain the speaker queue on a dedicated IO coroutine. This
        // is the only thread that calls AudioTrack.write() — keeping
        // it off Main/WS threads is what prevents UI freezes during
        // playback.
        playbackJob = scope.launch {
            var pending: ByteArray? = null
            try {
                while (isActive_continueWhileNotCancelled()) {
                    val t = audioTrack ?: break
                    if (t.state != AudioTrack.STATE_INITIALIZED) break

                    val data: ByteArray = if (pending != null) {
                        pending.also { pending = null }
                    } else {
                        val r = speakerQueue.receiveCatching()
                        if (r.isClosed) break
                        r.getOrNull() ?: continue
                    }

                    var offset = 0
                    while (offset < data.size) {
                        // 4-arg write with WRITE_NON_BLOCKING is API
                        // 23+. On API 21/22 the method doesn't exist
                        // and a direct call throws NoSuchMethodError
                        // (a Throwable, NOT an Exception) — naive
                        // ``catch (Exception)`` lets it kill the
                        // process. Branch on SDK_INT and catch
                        // Throwable defensively. Pre-Inc-H L817–L838.
                        val written = try {
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                                t.write(
                                    data,
                                    offset,
                                    data.size - offset,
                                    AudioTrack.WRITE_NON_BLOCKING,
                                )
                            } else {
                                t.write(data, offset, data.size - offset)
                            }
                        } catch (e: Throwable) {
                            Log.w(tag, "AudioTrack.write failed: ${e.message}")
                            -1
                        }
                        if (written < 0) {
                            // Permanent error — drop the rest of this
                            // chunk; next chunk may still play.
                            break
                        }
                        if (written == 0) {
                            // Hardware buffer is full. Park the rest
                            // and yield; the delay lets the buffer
                            // drain a bit before we retry.
                            pending = data.copyOfRange(offset, data.size)
                            delay(10)
                            break
                        }
                        offset += written
                        // PCM16 mono → 2 bytes per frame. Track
                        // cumulative frames so the restore loop can
                        // compare against playbackHeadPosition.
                        totalFramesWritten += (written / 2).toLong()
                    }
                }
            } catch (e: CancellationException) {
                // Normal shutdown
            } catch (e: Exception) {
                Log.w(tag, "Playback loop ended with: ${e.message}")
            }
            Log.d(tag, "Playback loop exited")
        }
    }

    /** Helper for the `while` condition — coroutineContext.isActive
     *  inside a lambda requires currentCoroutineContext() in Kotlin
     *  1.9 to avoid the deprecation. We keep it as a private helper
     *  to localise the change if the API evolves. */
    private suspend fun isActive_continueWhileNotCancelled(): Boolean {
        return kotlinx.coroutines.currentCoroutineContext()[Job]?.isActive ?: true
    }

    /**
     * Rebuild the [AudioTrack] without touching the playback queue
     * or coroutine. Used when the speaker mode (CALL ↔ MEDIA) changes
     * mid-session. Pre-Inc-H L648–L671.
     */
    fun setSpeakerMode(
        speakerMode: AudioRouter.SpeakerMode,
        preferredOutputDevice: android.media.AudioDeviceInfo?,
    ) {
        // If we're not yet playing, the staged values will take effect
        // when start() runs (caller-side guard before invoking us).
        if (audioTrack == null) return
        Log.i(tag, "setSpeakerMode → $speakerMode (rebuilding AudioTrack)")
        stopSpeakerOnly()
        try {
            start(speakerMode, preferredOutputDevice)
        } catch (e: Exception) {
            Log.e(tag, "AudioTrack rebuild failed: ${e.message}", e)
        }
    }

    /**
     * Tear down the current speaker [AudioTrack] without touching
     * the playback queue or coroutine. The next [start] call brings
     * up a new track and the playback loop seamlessly resumes.
     * Pre-Inc-H L678–L687.
     */
    private fun stopSpeakerOnly() {
        val t = audioTrack ?: return
        audioTrack = null
        try {
            if (t.playState == AudioTrack.PLAYSTATE_PLAYING) t.stop()
            t.release()
        } catch (e: Exception) {
            Log.w(tag, "stopSpeakerOnly failed: ${e.message}")
        }
    }

    /**
     * Tear down the playback loop, AudioTrack, and drain the queue.
     * Called from the provider's cleanup. Pre-Inc-H L1039–L1077 (the
     * playback-related parts).
     */
    fun cleanup() {
        playbackJob?.cancel()
        playbackJob = null
        totalFramesWritten = 0L
        // Drain anything still queued so we don't leak buffers.
        while (true) {
            val r = speakerQueue.tryReceive()
            if (r.isFailure || r.isClosed) break
        }
        try {
            audioTrack?.let {
                if (it.playState == AudioTrack.PLAYSTATE_PLAYING) it.stop()
                it.release()
            }
        } catch (e: Exception) {
            Log.w(tag, "Error stopping AudioTrack: ${e.message}")
        }
        audioTrack = null
    }

    private fun buildAudioTrack(
        bufSize: Int,
        mode: AudioRouter.SpeakerMode,
    ): AudioTrack {
        val track = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val attrs = when (mode) {
                AudioRouter.SpeakerMode.CALL -> AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
                AudioRouter.SpeakerMode.MEDIA -> AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            }
            AudioTrack.Builder()
                .setAudioAttributes(attrs)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(outSampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build()
                )
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
        } else {
            @Suppress("DEPRECATION")
            val legacyStream = when (mode) {
                AudioRouter.SpeakerMode.CALL -> AudioManager.STREAM_VOICE_CALL
                AudioRouter.SpeakerMode.MEDIA -> AudioManager.STREAM_MUSIC
            }
            @Suppress("DEPRECATION")
            AudioTrack(
                legacyStream,
                outSampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufSize,
                AudioTrack.MODE_STREAM,
            )
        }
        if (track.state != AudioTrack.STATE_INITIALIZED) {
            track.release()
            throw IllegalStateException("AudioTrack failed to initialize (state=${track.state})")
        }
        return track
    }
}
