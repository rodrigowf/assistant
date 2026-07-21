package com.assistant.peripheral.audio

import java.io.ByteArrayOutputStream

/**
 * Shared PCM→WAV helpers.
 *
 * Both the turn-based [AudioRecorder] and the wake-word Whisper confirmation
 * ([com.assistant.peripheral.voice.WhisperConfirmer]) need to wrap raw 16-bit
 * mono PCM in a standard 44-byte RIFF/WAVE header before shipping it off the
 * device. Kept in one place so the header layout can't drift between them.
 */
object WavUtils {

    /** Wrap little-endian 16-bit mono PCM bytes in a 44-byte WAV header. */
    fun pcmToWav(pcmData: ByteArray, sampleRate: Int): ByteArray {
        val channels = 1
        val bitsPerSample = 16
        val byteRate = sampleRate * channels * bitsPerSample / 8
        val totalDataLen = pcmData.size + 36
        val totalAudioLen = pcmData.size

        val header = ByteArray(44)

        // RIFF header
        header[0] = 'R'.code.toByte()
        header[1] = 'I'.code.toByte()
        header[2] = 'F'.code.toByte()
        header[3] = 'F'.code.toByte()
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
        header[16] = 16  // Subchunk1 size (PCM)
        header[17] = 0
        header[18] = 0
        header[19] = 0
        header[20] = 1   // Audio format (1 = PCM)
        header[21] = 0
        header[22] = channels.toByte()
        header[23] = 0
        header[24] = (sampleRate and 0xff).toByte()
        header[25] = ((sampleRate shr 8) and 0xff).toByte()
        header[26] = ((sampleRate shr 16) and 0xff).toByte()
        header[27] = ((sampleRate shr 24) and 0xff).toByte()
        header[28] = (byteRate and 0xff).toByte()
        header[29] = ((byteRate shr 8) and 0xff).toByte()
        header[30] = ((byteRate shr 16) and 0xff).toByte()
        header[31] = ((byteRate shr 24) and 0xff).toByte()
        header[32] = (channels * bitsPerSample / 8).toByte()  // block align
        header[33] = 0
        header[34] = bitsPerSample.toByte()
        header[35] = 0

        // data chunk
        header[36] = 'd'.code.toByte()
        header[37] = 'a'.code.toByte()
        header[38] = 't'.code.toByte()
        header[39] = 'a'.code.toByte()
        header[40] = (totalAudioLen and 0xff).toByte()
        header[41] = ((totalAudioLen shr 8) and 0xff).toByte()
        header[42] = ((totalAudioLen shr 16) and 0xff).toByte()
        header[43] = ((totalAudioLen shr 24) and 0xff).toByte()

        return header + pcmData
    }

    /**
     * Wrap 16-bit mono PCM samples (as `ShortArray` frames) in a WAV header.
     * Convenience for callers that accumulate audio as `ShortArray` chunks
     * (the wake-word pipeline reads `AudioRecord` into `ShortArray`).
     */
    fun shortFramesToWav(frames: List<ShortArray>, sampleRate: Int): ByteArray {
        val pcm = ByteArrayOutputStream()
        for (frame in frames) {
            for (s in frame) {
                pcm.write(s.toInt() and 0xff)
                pcm.write((s.toInt() shr 8) and 0xff)
            }
        }
        return pcmToWav(pcm.toByteArray(), sampleRate)
    }
}
