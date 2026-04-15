package com.assistant.peripheral.voice

import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import kotlinx.coroutines.*
import kotlin.math.sqrt

/**
 * Wake word detector with two-stage pipeline:
 *
 * Stage 1 — Silence monitor (AudioRecord, lightweight):
 *   Continuously reads raw PCM and computes RMS. When audio exceeds
 *   the threshold, releases the mic and hands off to Stage 2.
 *
 * Stage 2 — Speech recognizer (SpeechRecognizer, heavy):
 *   Runs a single recognition cycle. If the wake word is found in
 *   the results (exact or phonetic variant), fires a broadcast.
 *   Either way, returns to Stage 1.
 *
 * This avoids the constant SpeechRecognizer start/stop cycle (and beeps)
 * when the room is quiet.
 */
class WakeWordDetector(
    private val context: Context,
    private val wakeWord: String,       // triggers turn-based recording
    private val voiceWord: String = ""  // triggers realtime voice session (empty = disabled)
) {
    companion object {
        private const val TAG = "WakeWordDetector"
        const val ACTION_WAKE_WORD_DETECTED = "com.assistant.peripheral.WAKE_WORD_DETECTED"
        const val ACTION_VOICE_WORD_DETECTED = "com.assistant.peripheral.VOICE_WORD_DETECTED"

        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        // RMS threshold — 0..32767 scale. ~300 catches normal speech without being too sensitive.
        private const val RMS_THRESHOLD = 300.0

        // How long audio must stay above threshold before we start recognizer (avoids clicks/pops)
        private const val ACTIVITY_HOLD_MS = 30L

        // After a successful wake word, pause before re-arming
        private const val POST_WAKEWORD_DELAY_MS = 3000L

        // Base delay after a missed recognition — doubles on each consecutive miss (backoff)
        private const val POST_RECOGNITION_BASE_MS = 1000L
        private const val POST_RECOGNITION_MAX_MS = 30_000L

        private const val CLIENT_ERROR_DELAY_MS = 1000L

        /**
         * Generate phonetic variants for a wake word phrase.
         * SpeechRecognizer may mishear words (e.g. "hey assistant" → "a system", "resistant").
         * Only full-phrase substitutions are generated — no bare single words — to avoid
         * matching every utterance that contains a common word.
         */
        fun buildVariants(phrase: String): List<String> {
            val normalized = phrase.lowercase().trim()
            val variants = mutableListOf(normalized)

            // Per-word substitutions for common mishearings.
            // Each entry replaces the word in the full phrase (not added standalone).
            val wordSubs = mapOf(
                "hey" to listOf("a", "hay", "he", "hate", "8"),
                "assistant" to listOf("system", "assist", "distance", "resistant",
                    "existence", "insistent", "assistance"),
                "realtime" to listOf("real time", "real-time", "realm time", "real tight",
                    "reel time"),
                "computer" to listOf("commuter", "computers"),
                "jarvis" to listOf("jar vis", "jarvi"),
            )

            val phraseWords = normalized.split(" ")
            for ((word, subs) in wordSubs) {
                if (phraseWords.contains(word)) {
                    for (sub in subs) {
                        // Replace the word inside the full phrase context only
                        variants.add(normalized.replace(word, sub))
                    }
                }
            }

            return variants.distinct()
        }
    }

    var isActive = false
        private set
    var isPaused = false
        private set
    private var isRecognizing = false
    private var consecutiveMisses = 0  // exponential backoff counter

    // Pre-computed phonetic variants for faster matching.
    // wakeWord / voiceWord may be comma-separated lists of phrases.
    private val wakeVariants = wakeWord.split(",")
        .map { it.trim() }.filter { it.isNotEmpty() }
        .flatMap { buildVariants(it) }.distinct()
    private val voiceVariants = if (voiceWord.isNotEmpty())
        voiceWord.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            .flatMap { buildVariants(it) }.distinct()
    else emptyList()

    // Stage 1: silence monitor runs on a background IO thread
    private var audioRecord: AudioRecord? = null
    private var silenceMonitorJob: Job? = null

    // Stage 2: speech recognizer always runs on Main
    private var speechRecognizer: SpeechRecognizer? = null

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private val BEEP_STREAMS = intArrayOf(
        AudioManager.STREAM_RING,
        AudioManager.STREAM_NOTIFICATION,
        AudioManager.STREAM_SYSTEM,
        AudioManager.STREAM_MUSIC,
    )

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    fun start() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            Log.w(TAG, "Speech recognition not available on this device")
            return
        }
        Log.d(TAG, "Starting — wake variants: $wakeVariants")
        if (voiceVariants.isNotEmpty()) Log.d(TAG, "Voice variants: $voiceVariants")
        isActive = true
        isPaused = false
        startSilenceMonitor()
    }

    /**
     * Temporarily suspend detection without fully stopping. Call resume() to re-arm.
     * Safe to call from any thread.
     */
    fun pause() {
        if (!isActive || isPaused) return
        Log.d(TAG, "Pausing wake word detection")
        isPaused = true
        // Stop mic + recognizer so they don't compete with voice session
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        if (isRecognizing) {
            isRecognizing = false
            scope.launch { destroyRecognizer(); unmuteBeep() }
        }
    }

    /**
     * Resume after pause(). Re-arms the silence monitor.
     */
    fun resume() {
        if (!isActive || !isPaused) return
        Log.d(TAG, "Resuming wake word detection")
        isPaused = false
        consecutiveMisses = 0
        startSilenceMonitor()
    }

    fun stop() {
        isActive = false
        isPaused = false
        isRecognizing = false
        consecutiveMisses = 0
        try { audioManager.mode = AudioManager.MODE_NORMAL } catch (_: Exception) {}
        unmuteBeep()
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        destroyRecognizer()
        scope.coroutineContext.cancelChildren()
    }

    fun release() {
        stop()
        scope.cancel()
    }

    // -------------------------------------------------------------------------
    // Stage 1 — Silence monitor
    // -------------------------------------------------------------------------

    private fun startSilenceMonitor() {
        if (!isActive || isPaused) return
        stopAudioRecord()

        val bufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
            .coerceAtLeast(3200)  // at least 100ms of audio at 16kHz 16-bit mono

        silenceMonitorJob = scope.launch(Dispatchers.IO) {
            // Retry loop: mic may be held by AudioRecorder (turn-based recording) for a few seconds.
            // Keep trying until the mic is free or we're no longer active.
            var recorder: AudioRecord? = null
            while (isActive && recorder == null) {
                val candidate = try {
                    AudioRecord(
                        MediaRecorder.AudioSource.MIC,
                        SAMPLE_RATE,
                        CHANNEL_CONFIG,
                        AUDIO_FORMAT,
                        bufferSize
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to create AudioRecord (will retry): ${e.message}")
                    kotlinx.coroutines.delay(500L)
                    continue
                }

                if (candidate.state != AudioRecord.STATE_INITIALIZED) {
                    Log.w(TAG, "AudioRecord not initialized (mic busy, will retry)")
                    candidate.release()
                    kotlinx.coroutines.delay(500L)
                    continue
                }

                recorder = candidate
            }
            if (recorder == null || !isActive) return@launch

            audioRecord = recorder
            recorder.startRecording()
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                // startRecording() failed (mic still held by another process, e.g. WebRTC)
                Log.w(TAG, "AudioRecord.startRecording() failed — mic busy, will retry")
                recorder.release()
                audioRecord = null
                kotlinx.coroutines.delay(500L)
                // Restart the whole monitor so we retry mic acquisition from scratch
                withContext(Dispatchers.Main) {
                    if (isActive && !isPaused) startSilenceMonitor()
                }
                return@launch
            }
            Log.d(TAG, "Silence monitor started (threshold=$RMS_THRESHOLD)")

            val buffer = ShortArray(bufferSize / 2)
            var activityStartMs = 0L

            while (isActive && !isRecognizing) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read <= 0) continue

                val rms = computeRms(buffer, read)

                if (rms >= RMS_THRESHOLD) {
                    if (activityStartMs == 0L) {
                        activityStartMs = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - activityStartMs >= ACTIVITY_HOLD_MS) {
                        Log.d(TAG, "Audio activity detected (rms=${"%.0f".format(rms)}) — starting recognizer")
                        // Release mic so SpeechRecognizer can use it
                        stopAudioRecord()
                        withContext(Dispatchers.Main) {
                            if (isActive && !isRecognizing) {
                                startRecognizer()
                            }
                        }
                        return@launch
                    }
                } else {
                    activityStartMs = 0L
                }
            }

            try {
                recorder.stop()
                recorder.release()
            } catch (e: Exception) {
                Log.w(TAG, "Error stopping recorder at end of loop: ${e.message}")
            }
            audioRecord = null
        }
    }

    private fun stopAudioRecord() {
        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping AudioRecord: ${e.message}")
        }
        audioRecord = null
    }

    private fun computeRms(buffer: ShortArray, count: Int): Double {
        var sum = 0.0
        for (i in 0 until count) {
            val s = buffer[i].toDouble()
            sum += s * s
        }
        return sqrt(sum / count)
    }

    // -------------------------------------------------------------------------
    // Stage 2 — Speech recognizer
    // -------------------------------------------------------------------------

    private fun startRecognizer() {
        if (!isActive || isRecognizing) return
        isRecognizing = true

        muteBeep()
        // Set communication mode to suppress system beep on older devices
        try { audioManager.mode = AudioManager.MODE_IN_COMMUNICATION } catch (_: Exception) {}
        destroyRecognizer()
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context)

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // Force English so the wake word phrase is recognized correctly
            // regardless of the device's system language
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-US")
            // Prefer offline recognition for lower latency (falls back to online if unavailable)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            // Suppress the start/stop beep on most Android devices
            putExtra("android.speech.extra.DICTATION_MODE", true)
            // Wait longer for silence so speech isn't cut off mid-phrase
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2000L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
        }

        // Guard against double finishRecognition calls (onPartialResults early-exit + onResults/onError)
        var listenerFinished = false

        speechRecognizer?.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                Log.d(TAG, "Recognizer ready")
            }

            override fun onBeginningOfSpeech() {
                Log.d(TAG, "Speech begun")
            }

            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}

            override fun onError(error: Int) {
                if (listenerFinished) return
                listenerFinished = true
                Log.d(TAG, "Recognizer error: $error")
                val delay = if (error == SpeechRecognizer.ERROR_CLIENT)
                    CLIENT_ERROR_DELAY_MS else -1L
                finishRecognition(wakeWordDetected = false, delay = delay)
            }

            override fun onResults(results: Bundle?) {
                if (listenerFinished) return
                listenerFinished = true
                val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                Log.d(TAG, "Results: $matches")
                val detected = matches != null && checkForWakeWord(matches)
                finishRecognition(wakeWordDetected = detected)
            }

            override fun onPartialResults(partialResults: Bundle?) {
                if (listenerFinished) return
                val partial = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                if (!partial.isNullOrEmpty()) {
                    Log.d(TAG, "Partial: $partial")
                    if (checkForWakeWord(partial)) {
                        // Early match on partial — stop immediately
                        listenerFinished = true
                        finishRecognition(wakeWordDetected = true)
                    }
                }
            }

            override fun onEvent(eventType: Int, params: Bundle?) {}
        })

        speechRecognizer?.startListening(intent)
    }

    private fun finishRecognition(wakeWordDetected: Boolean, delay: Long = -1L) {
        isRecognizing = false
        destroyRecognizer()
        // Only reset audio mode if no wake word — if detected, VoiceManager will take ownership
        if (!wakeWordDetected) try { audioManager.mode = AudioManager.MODE_NORMAL } catch (_: Exception) {}
        unmuteBeep()
        val restartDelay = when {
            delay >= 0 -> delay
            wakeWordDetected -> {
                consecutiveMisses = 0
                POST_WAKEWORD_DELAY_MS
            }
            else -> {
                // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (cap)
                val backoff = (POST_RECOGNITION_BASE_MS shl consecutiveMisses)
                    .coerceAtMost(POST_RECOGNITION_MAX_MS)
                consecutiveMisses++
                Log.d(TAG, "No match — miss #$consecutiveMisses, waiting ${backoff}ms")
                backoff
            }
        }
        scope.launch {
            delay(restartDelay)
            if (isActive && !isPaused) startSilenceMonitor()
        }
    }

    private fun destroyRecognizer() {
        try {
            speechRecognizer?.cancel()
            speechRecognizer?.destroy()
        } catch (e: Exception) {
            Log.w(TAG, "Error destroying recognizer: ${e.message}")
        }
        speechRecognizer = null
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    // Saved volumes for pre-M devices
    private var savedVolumes: List<Int>? = null

    private fun muteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (stream in BEEP_STREAMS) {
                    audioManager.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, 0)
                }
            } else {
                savedVolumes = BEEP_STREAMS.map { audioManager.getStreamVolume(it) }
                for (stream in BEEP_STREAMS) {
                    audioManager.setStreamVolume(stream, 0, 0)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to mute beep streams: ${e.message}")
        }
    }

    private fun unmuteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (stream in BEEP_STREAMS) {
                    audioManager.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, 0)
                }
            } else {
                savedVolumes?.let { saved ->
                    BEEP_STREAMS.forEachIndexed { i, stream ->
                        audioManager.setStreamVolume(stream, saved[i], 0)
                    }
                }
                savedVolumes = null
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to unmute beep streams: ${e.message}")
        }
    }

    private fun checkForWakeWord(results: List<String>): Boolean {
        for (result in results) {
            val lower = result.lowercase()
            // Check voice word first (more specific / longer phrase wins if both match)
            if (voiceVariants.isNotEmpty() && voiceVariants.any { lower.contains(it) }) {
                Log.d(TAG, "Voice word detected in: \"$result\"")
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_VOICE_WORD_DETECTED))
                return true
            }
            if (wakeVariants.any { lower.contains(it) }) {
                Log.d(TAG, "Wake word detected in: \"$result\"")
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_WAKE_WORD_DETECTED))
                return true
            }
        }
        return false
    }
}
