package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WhisperConfirmer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the pure decision logic of [WhisperConfirmer] — the Whisper-transcript
 * → confirm/reject verdict that gates every wake/talk detection.
 *
 * Context (2026-07-21): the field-reported "opens the conversation for no
 * reason, even in silence" bug traced to whisper-1 hallucinating canned
 * phrases when handed near-silent audio from a phantom Vosk match. The primary
 * fix is an RMS speech-floor pre-gate in `WakeWordDetector` (silence never
 * reaches Whisper); this class covers the confirmer's own belt-and-suspenders
 * layer: normalization, realtime-first precedence, and the
 * hallucination-boilerplate reject.
 *
 * Only the pure `decide()`/`normalize()` companion helpers are testable in
 * plain JUnit — the network call + `android.util.Log` around them are not.
 */
class WhisperConfirmerDecideTest {

    private val talk = listOf("my friend")
    private val wake = listOf("wake up")

    // -------------------------------------------------------------------------
    // Happy path — a real variant present
    // -------------------------------------------------------------------------

    @Test
    fun `confirmsWakeOnExactPhrase`() {
        val r = WhisperConfirmer.decide("wake up", talk, wake)
        assertTrue(r.confirmed)
        assertTrue(r.isRealtime)
    }

    @Test
    fun `confirmsTalkOnExactPhrase`() {
        val r = WhisperConfirmer.decide("my friend", talk, wake)
        assertTrue(r.confirmed)
        assertFalse(r.isRealtime)
    }

    @Test
    fun `confirmsThroughWhisperPunctuationAndCasing`() {
        // The exact real-world reject we fixed: Whisper returns "Hello, my
        // friend." but the variant is bare "my friend".
        val r = WhisperConfirmer.decide("Hello, my friend.", talk, wake)
        assertTrue(r.confirmed)
        assertFalse(r.isRealtime)
    }

    @Test
    fun `confirmsWakeWithConversationalLeadIn`() {
        val r = WhisperConfirmer.decide("Hey, wake up please!", talk, wake)
        assertTrue(r.confirmed)
        assertTrue(r.isRealtime)
    }

    @Test
    fun `prefersWakeWhenBothPresent`() {
        // Realtime-first precedence, mirroring VoskWakeWordEngine.findMatch.
        val r = WhisperConfirmer.decide("wake up my friend", talk, wake)
        assertTrue(r.confirmed)
        assertTrue(r.isRealtime)
    }

    // -------------------------------------------------------------------------
    // Reject path — no variant
    // -------------------------------------------------------------------------

    @Test
    fun `rejectsUnrelatedSpeech`() {
        val r = WhisperConfirmer.decide("what time is it", talk, wake)
        assertFalse(r.confirmed)
    }

    @Test
    fun `rejectsEmptyTranscript`() {
        val r = WhisperConfirmer.decide("", talk, wake)
        assertFalse(r.confirmed)
    }

    // -------------------------------------------------------------------------
    // Hallucination boilerplate — the silence-leak defence
    // -------------------------------------------------------------------------

    @Test
    fun `rejectsWhisperSilenceHallucinations`() {
        // whisper-1's canned outputs on silence must never confirm.
        listOf("you", "Thank you.", "Thanks for watching!", "Bye.", "So.", "Okay").forEach {
            assertFalse("hallucination \"$it\" must be rejected", WhisperConfirmer.decide(it, talk, wake).confirmed)
        }
    }

    @Test
    fun `boilerplateRejectIsExactMatchOnly`() {
        // A real command that merely CONTAINS a boilerplate word still passes if
        // a variant is present — the reject is whole-transcript equality, not a
        // substring ban. Here "thank you my friend" contains the talk variant.
        val r = WhisperConfirmer.decide("thank you my friend", talk, wake)
        assertTrue(r.confirmed)
        assertFalse(r.isRealtime)
    }

    @Test
    fun `boilerplateWordInsideRealPhraseDoesNotBlockWake`() {
        val r = WhisperConfirmer.decide("okay wake up", talk, wake)
        assertTrue(r.confirmed)
        assertTrue(r.isRealtime)
    }

    // -------------------------------------------------------------------------
    // normalize
    // -------------------------------------------------------------------------

    @Test
    fun `normalizeStripsPunctuationLowercasesCollapsesSpace`() {
        assertEquals("hello my friend", WhisperConfirmer.normalize("  Hello,   MY  friend!! "))
    }

    @Test
    fun `normalizeKeepsDigits`() {
        assertEquals("agent 7", WhisperConfirmer.normalize("Agent-7"))
    }
}
