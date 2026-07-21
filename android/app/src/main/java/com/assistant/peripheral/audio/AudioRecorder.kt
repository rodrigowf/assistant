package com.assistant.peripheral.audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.ByteArrayOutputStream

/**
 * Records audio from the microphone and provides it as base64-encoded WAV data.
 *
 * Uses AudioRecord for low-level PCM capture, then encodes to WAV format
 * for sending to the assistant backend.
 */
class AudioRecorder(
    private val context: Context
) {
    companion object {
        private const val TAG = "AudioRecorder"
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    }

    sealed class RecordingState {
        object Idle : RecordingState()
        object Recording : RecordingState()
        data class Error(val message: String) : RecordingState()
    }

    private var audioRecord: AudioRecord? = null
    private var recordingJob: Job? = null
    private val audioBuffer = ByteArrayOutputStream()

    private val _state = MutableStateFlow<RecordingState>(RecordingState.Idle)
    val state: StateFlow<RecordingState> = _state.asStateFlow()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    /**
     * Check if we have audio recording permission.
     */
    fun hasPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    /**
     * Start recording audio.
     *
     * @return true if recording started successfully
     */
    fun startRecording(): Boolean {
        if (!hasPermission()) {
            _state.value = RecordingState.Error("No audio recording permission")
            return false
        }

        if (_state.value is RecordingState.Recording) {
            Log.w(TAG, "Already recording")
            return false
        }

        val bufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
        if (bufferSize == AudioRecord.ERROR || bufferSize == AudioRecord.ERROR_BAD_VALUE) {
            _state.value = RecordingState.Error("Failed to get buffer size")
            return false
        }

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                bufferSize * 2
            )

            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                _state.value = RecordingState.Error("Failed to initialize AudioRecord")
                audioRecord?.release()
                audioRecord = null
                return false
            }

            audioBuffer.reset()
            audioRecord?.startRecording()
            _state.value = RecordingState.Recording

            // Start reading audio data
            recordingJob = scope.launch {
                val buffer = ByteArray(bufferSize)
                while (isActive && _state.value is RecordingState.Recording) {
                    val bytesRead = audioRecord?.read(buffer, 0, buffer.size) ?: 0
                    if (bytesRead > 0) {
                        synchronized(audioBuffer) {
                            audioBuffer.write(buffer, 0, bytesRead)
                        }
                    }
                }
            }

            Log.d(TAG, "Recording started")
            return true

        } catch (e: SecurityException) {
            _state.value = RecordingState.Error("Permission denied: ${e.message}")
            return false
        } catch (e: Exception) {
            _state.value = RecordingState.Error("Failed to start recording: ${e.message}")
            return false
        }
    }

    /**
     * Stop recording and return the audio data as base64-encoded WAV.
     *
     * @return Base64-encoded WAV audio, or null if recording failed
     */
    fun stopRecording(): String? {
        if (_state.value !is RecordingState.Recording) {
            Log.w(TAG, "Not recording")
            return null
        }

        recordingJob?.cancel()
        recordingJob = null

        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null

        _state.value = RecordingState.Idle

        val pcmData: ByteArray
        synchronized(audioBuffer) {
            pcmData = audioBuffer.toByteArray()
            audioBuffer.reset()
        }

        if (pcmData.isEmpty()) {
            Log.w(TAG, "No audio data recorded")
            return null
        }

        Log.d(TAG, "Recording stopped, ${pcmData.size} bytes captured")

        // Convert PCM to WAV
        val wavData = WavUtils.pcmToWav(pcmData, SAMPLE_RATE)
        return Base64.encodeToString(wavData, Base64.NO_WRAP)
    }

    /**
     * Cancel recording without returning data.
     */
    fun cancelRecording() {
        recordingJob?.cancel()
        recordingJob = null

        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null

        synchronized(audioBuffer) {
            audioBuffer.reset()
        }

        _state.value = RecordingState.Idle
        Log.d(TAG, "Recording cancelled")
    }

    /**
     * Release all resources.
     */
    fun release() {
        cancelRecording()
        scope.cancel()
    }

}
