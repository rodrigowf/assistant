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
        val wavData = pcmToWav(pcmData)
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

    /**
     * Convert PCM data to WAV format.
     */
    private fun pcmToWav(pcmData: ByteArray): ByteArray {
        val totalDataLen = pcmData.size + 36
        val totalAudioLen = pcmData.size
        val channels = 1
        val byteRate = SAMPLE_RATE * channels * 2

        val header = ByteArray(44)

        // RIFF header
        header[0] = 'R'.code.toByte()
        header[1] = 'I'.code.toByte()
        header[2] = 'F'.code.toByte()
        header[3] = 'F'.code.toByte()

        // File size - 8
        header[4] = (totalDataLen and 0xff).toByte()
        header[5] = ((totalDataLen shr 8) and 0xff).toByte()
        header[6] = ((totalDataLen shr 16) and 0xff).toByte()
        header[7] = ((totalDataLen shr 24) and 0xff).toByte()

        // WAVE header
        header[8] = 'W'.code.toByte()
        header[9] = 'A'.code.toByte()
        header[10] = 'V'.code.toByte()
        header[11] = 'E'.code.toByte()

        // fmt chunk
        header[12] = 'f'.code.toByte()
        header[13] = 'm'.code.toByte()
        header[14] = 't'.code.toByte()
        header[15] = ' '.code.toByte()

        // Subchunk1 size (16 for PCM)
        header[16] = 16
        header[17] = 0
        header[18] = 0
        header[19] = 0

        // Audio format (1 = PCM)
        header[20] = 1
        header[21] = 0

        // Number of channels
        header[22] = channels.toByte()
        header[23] = 0

        // Sample rate
        header[24] = (SAMPLE_RATE and 0xff).toByte()
        header[25] = ((SAMPLE_RATE shr 8) and 0xff).toByte()
        header[26] = ((SAMPLE_RATE shr 16) and 0xff).toByte()
        header[27] = ((SAMPLE_RATE shr 24) and 0xff).toByte()

        // Byte rate
        header[28] = (byteRate and 0xff).toByte()
        header[29] = ((byteRate shr 8) and 0xff).toByte()
        header[30] = ((byteRate shr 16) and 0xff).toByte()
        header[31] = ((byteRate shr 24) and 0xff).toByte()

        // Block align
        header[32] = (channels * 2).toByte()
        header[33] = 0

        // Bits per sample
        header[34] = 16
        header[35] = 0

        // data chunk
        header[36] = 'd'.code.toByte()
        header[37] = 'a'.code.toByte()
        header[38] = 't'.code.toByte()
        header[39] = 'a'.code.toByte()

        // Data size
        header[40] = (totalAudioLen and 0xff).toByte()
        header[41] = ((totalAudioLen shr 8) and 0xff).toByte()
        header[42] = ((totalAudioLen shr 16) and 0xff).toByte()
        header[43] = ((totalAudioLen shr 24) and 0xff).toByte()

        // Combine header and PCM data
        return header + pcmData
    }
}
