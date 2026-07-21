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
    // Whole-call budget for the Whisper round-trip (key fetch on first call +
    // multipart upload of a ~2s WAV + whisper-1 inference + response).
    //
    // Field-measured on the A300M over wifi (2026-07-21): the real round-trip
    // routinely exceeded 2.5s, so the OLD 2.5s budget timed out on nearly every
    // attempt (fail-closed → every wake/talk silently rejected, "wake up
    // stopped working"). whisper-1 on a 2s clip is fast server-side, but the
    // device's upload + TLS + variable mobile-grade wifi RTT dominate. 10s is
    // comfortably above the observed worst case; a talk command isn't latency
    // critical, and for the wake path a reliable confirm beats a fast failure.
    // (okhttp's own connect=10s/read=30s timeouts remain the hard ceiling.)
    private val timeoutMs: Long = 10_000L,
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
        val result = decide(transcript.text, talkVariants, wakeVariants)
        if (result.confirmed) {
            Log.d(TAG, "Whisper CONFIRMED (realtime=${result.isRealtime}) in \"${transcript.text}\"")
        } else {
            Log.d(TAG, "Whisper REJECTED \"${transcript.text}\"")
        }
        return result
    }

    private data class Transcription(val text: String, val failed: Boolean = false)

    private suspend fun transcribe(pcmFrames: List<ShortArray>): Transcription =
        withContext(Dispatchers.IO) {
            val t0 = System.nanoTime()
            val key = ensureKey()
                ?: return@withContext Transcription("", failed = true).also {
                    Log.w(TAG, "No OpenAI key available — rejecting (fail-closed)")
                }
            val tKey = System.nanoTime()
            val wav = WavUtils.shortFramesToWav(pcmFrames, sampleRate)
            val tWav = System.nanoTime()
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
                    // temperature=0 disables whisper-1's temperature-fallback
                    // sampling, which is the main driver of hallucinated canned
                    // phrases on near-silent/ambient clips. Greedy decoding is
                    // both more deterministic and far less likely to invent a
                    // wake phrase out of noise — exactly what a yes/no gate wants.
                    .addFormDataPart("temperature", "0")
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
                    val tHttp = System.nanoTime()
                    fun ms(a: Long, b: Long) = (b - a) / 1_000_000
                    Log.d(
                        TAG,
                        "Whisper timing: key=${ms(t0, tKey)}ms wav=${ms(tKey, tWav)}ms " +
                            "http=${ms(tWav, tHttp)}ms total=${ms(t0, tHttp)}ms " +
                            "(wav ${wav.size}B)",
                    )
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

        /**
         * Pure decision: given Whisper's transcript and the configured
         * variants, decide whether a wake/talk word is present. Extracted so
         * the matching + normalization + hallucination-guard logic is unit
         * testable (the network call around it is not). Mirrors
         * `VoskWakeWordEngine.findMatch`'s realtime-first precedence.
         *
         * Order of checks:
         *  1. Normalize (lowercase, strip punctuation, collapse whitespace) so
         *     Whisper's "Hello, my friend." matches the bare variant.
         *  2. Reject if the WHOLE normalized transcript is a known whisper-1
         *     silence-hallucination (exact equality — a real command that
         *     merely contains such a word still passes).
         *  3. Wake variants (realtime) first, then talk variants, by substring.
         */
        fun decide(
            transcriptText: String,
            talkVariants: List<String>,
            wakeVariants: List<String>,
        ): Result {
            val norm = normalize(transcriptText)
            if (norm in HALLUCINATION_BOILERPLATE) {
                return Result(confirmed = false, transcript = transcriptText)
            }
            wakeVariants.firstOrNull { norm.contains(normalize(it)) }?.let {
                return Result(confirmed = true, transcript = transcriptText, isRealtime = true)
            }
            talkVariants.firstOrNull { norm.contains(normalize(it)) }?.let {
                return Result(confirmed = true, transcript = transcriptText, isRealtime = false)
            }
            return Result(confirmed = false, transcript = transcriptText)
        }

        /** Lowercase, drop non-alphanumerics (punctuation), collapse whitespace. */
        fun normalize(s: String): String =
            s.lowercase().replace(Regex("[^a-z0-9\\s]"), " ").replace(Regex("\\s+"), " ").trim()
        private const val OPENAI_TRANSCRIPTIONS_URL =
            "https://api.openai.com/v1/audio/transcriptions"
        // whisper-1 is the cheapest/fastest hosted transcription model and is
        // plenty for 1-2s keyword spotting. gpt-4o-transcribe is more accurate
        // but slower + pricier; not worth it for a yes/no gate.
        private const val WHISPER_MODEL = "whisper-1"

        /**
         * Normalized transcripts whisper-1 commonly hallucinates from silence
         * or unintelligible noise. Matched by exact equality against the
         * normalized transcript (see [normalize]), so only a transcript that is
         * *entirely* boilerplate is dropped — a real command that happens to
         * contain one of these words still passes. None overlaps a configured
         * wake/talk variant ("wake up" / "my friend").
         */
        private val HALLUCINATION_BOILERPLATE = setOf(
            "you",
            "thank you",
            "thank you very much",
            "thanks for watching",
            "thanks for watching the video",
            "please subscribe",
            "bye",
            "bye bye",
            "so",
            "the",
            "okay",
            "i m sorry",
        )
    }
}
