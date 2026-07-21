package com.assistant.peripheral.voice

import android.util.Log
import com.assistant.peripheral.audio.WavUtils
import com.assistant.peripheral.network.ApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody
import org.json.JSONObject

/**
 * Confirms a local Vosk wake/talk-word detection against OpenAI Whisper.
 *
 * The on-device Vosk detector is deliberately fast and permissive (fires on
 * the earliest partial that contains the phrase), so it produces false
 * positives — TV chatter, ambient speech, a phonetically-near phrase. This
 * class is the false-positive filter: it takes the *exact* PCM Vosk flagged,
 * transcribes it with Whisper, and reports whether the transcript actually
 * contains a configured wake/talk variant.
 *
 * Design decisions (locked 2026-07-21):
 *  - **Direct from Android.** We POST straight to
 *    `api.openai.com/v1/audio/transcriptions` for lowest latency rather than
 *    proxying through the backend. The raw key comes from
 *    [ApiClient.fetchOpenAiKey] (backend serves it from `context/.env`) and is
 *    cached here for the process lifetime.
 *  - **Fail-closed.** Any failure — no key, network error, timeout, malformed
 *    response — returns a rejecting [Result]. A real wake word is dropped when
 *    the network is down; that's the accepted trade for zero false positives.
 *
 * Threading: [confirm] is a suspend fun; the network call runs on
 * `Dispatchers.IO`. Safe to call from the wake-word IO coroutine.
 */
class WhisperConfirmer(
    private val apiClient: ApiClient,
    private val talkVariants: List<String>,
    private val wakeVariants: List<String>,
    private val sampleRate: Int = 16_000,
    // Whole-call budget. Whisper on a ~1-2s clip is typically 300-800ms; 2.5s
    // covers a slow LAN + upload without making a real wake feel dead.
    private val timeoutMs: Long = 2_500L,
) {
    /**
     * @param confirmed true → fire the broadcast; false → cancel silently.
     * @param transcript what Whisper heard (empty on failure), for diagnostics.
     * @param isRealtime which trigger matched (only meaningful when confirmed);
     *   realtime wake variants take precedence over talk variants, mirroring
     *   `VoskWakeWordEngine.findMatch`.
     * @param failed true when the call itself failed (vs. a clean no-match) —
     *   lets the caller distinguish "Whisper disagreed" from "couldn't reach
     *   Whisper" in logs.
     */
    data class Result(
        val confirmed: Boolean,
        val transcript: String,
        val isRealtime: Boolean = false,
        val failed: Boolean = false,
    )

    @Volatile
    private var cachedKey: String? = null

    /**
     * Transcribe [pcmFrames] and decide whether a configured variant is
     * present. Fail-closed: returns a rejecting [Result] on any error.
     */
    suspend fun confirm(pcmFrames: List<ShortArray>): Result {
        if (pcmFrames.isEmpty()) {
            return Result(confirmed = false, transcript = "", failed = true)
        }
        val transcript = withTimeoutOrNull(timeoutMs) {
            transcribe(pcmFrames)
        }
        if (transcript == null) {
            Log.w(TAG, "Whisper confirmation timed out (${timeoutMs}ms) — rejecting (fail-closed)")
            return Result(confirmed = false, transcript = "", failed = true)
        }
        if (transcript.failed) {
            return Result(confirmed = false, transcript = "", failed = true)
        }
        val lower = transcript.text.lowercase()
        // Realtime-first precedence, same as VoskWakeWordEngine.findMatch.
        wakeVariants.firstOrNull { lower.contains(it) }?.let {
            Log.d(TAG, "Whisper CONFIRMED wake \"$it\" in \"${transcript.text}\"")
            return Result(confirmed = true, transcript = transcript.text, isRealtime = true)
        }
        talkVariants.firstOrNull { lower.contains(it) }?.let {
            Log.d(TAG, "Whisper CONFIRMED talk \"$it\" in \"${transcript.text}\"")
            return Result(confirmed = true, transcript = transcript.text, isRealtime = false)
        }
        Log.d(TAG, "Whisper REJECTED — no variant in \"${transcript.text}\"")
        return Result(confirmed = false, transcript = transcript.text)
    }

    private data class Transcription(val text: String, val failed: Boolean = false)

    private suspend fun transcribe(pcmFrames: List<ShortArray>): Transcription =
        withContext(Dispatchers.IO) {
            val key = ensureKey()
                ?: return@withContext Transcription("", failed = true).also {
                    Log.w(TAG, "No OpenAI key available — rejecting (fail-closed)")
                }
            val wav = WavUtils.shortFramesToWav(pcmFrames, sampleRate)
            try {
                val body = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "file", "wake.wav",
                        RequestBody.create("audio/wav".toMediaTypeOrNull(), wav),
                    )
                    .addFormDataPart("model", WHISPER_MODEL)
                    .addFormDataPart("response_format", "json")
                    // English keyword spotting — pinning the language shaves a
                    // little latency and avoids spurious language detection on
                    // a 1-2s clip.
                    .addFormDataPart("language", "en")
                    .build()
                val request = Request.Builder()
                    .url(OPENAI_TRANSCRIPTIONS_URL)
                    .addHeader("Authorization", "Bearer $key")
                    .post(body)
                    .build()
                apiClient.httpClient.newCall(request).execute().use { response ->
                    if (response.code == 401) {
                        // Key rotated / bad — drop the cache so the next attempt
                        // re-fetches, and reject this one.
                        Log.w(TAG, "Whisper 401 — clearing cached key")
                        cachedKey = null
                        return@withContext Transcription("", failed = true)
                    }
                    if (!response.isSuccessful) {
                        Log.w(TAG, "Whisper HTTP ${response.code} — rejecting")
                        return@withContext Transcription("", failed = true)
                    }
                    val respBody = response.body?.string()
                        ?: return@withContext Transcription("", failed = true)
                    val text = JSONObject(respBody).optString("text", "").trim()
                    Transcription(text)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Whisper call failed: ${e.message} — rejecting")
                Transcription("", failed = true)
            }
        }

    private suspend fun ensureKey(): String? {
        cachedKey?.let { return it }
        val fetched = apiClient.fetchOpenAiKey()
        if (fetched != null) cachedKey = fetched
        return fetched
    }

    companion object {
        private const val TAG = "WhisperConfirmer"
        private const val OPENAI_TRANSCRIPTIONS_URL =
            "https://api.openai.com/v1/audio/transcriptions"
        // whisper-1 is the cheapest/fastest hosted transcription model and is
        // plenty for 1-2s keyword spotting. gpt-4o-transcribe is more accurate
        // but slower + pricier; not worth it for a yes/no gate.
        private const val WHISPER_MODEL = "whisper-1"
    }
}
