package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.VoskWakeWordEngine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for V3 (`VoskWakeWordEngine`) of the Vosk migration plan
 * (assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md, §4 V3).
 *
 * Refactor base: HEAD `ddfb53f` (V2 — VoskModelLoader + Lollipop polyfill).
 *
 * V3 replaces the `SpeechRecognizer` recognition stage with a
 * `VoskWakeWordEngine` that accepts raw PCM `ShortArray` buffers via
 * `feed(buffer, length)` and emits matches against the configured
 * `talkVariants` / `wakeVariants` lists.
 *
 * The actual `org.vosk.Recognizer` instance depends on the native lib + a
 * loaded `Model`, neither of which is unit-testable in plain JUnit. But the
 * **match-detection and variant-precedence logic** lives in pure-Kotlin
 * companion-object helpers that we pin here.
 *
 * ---
 *
 * **Contract under test** (V3):
 *
 *   - `extractText(json)` parses Vosk's `{"text": "..."}` (final) or
 *       `{"partial": "..."}` (incremental) JSON and returns the inner
 *       string, or empty if absent / blank. Robust to whitespace.
 *
 *   - `findMatch(text, talkVariants, wakeVariants)` returns a
 *       `VoskWakeWordEngine.Match` with `isRealtime=true` if a `wakeVariants`
 *       entry is a substring (lowercased contains), `isRealtime=false` if a
 *       `talkVariants` entry matches, null otherwise. Realtime checked FIRST
 *       per Detour 3 precedence (same as `WakeWordDetector.checkForWakeWord`).
 *
 *   - `buildKeywordGrammar(talkVariants, wakeVariants)` returns a JSON array
 *       of the distinct phrases plus the special `"[unk]"` token (Vosk's
 *       sentinel for unconstrained vocab). Used to constrain the Recognizer
 *       to just the configured phrases — drastically improves accuracy and
 *       speed (plan §5.3).
 *
 * **Tuned behaviors preserved**:
 *   - Realtime-first precedence (matches `WakeWordDetector.checkForWakeWord`
 *     lines 1015–1031 at HEAD `ddfb53f`).
 *   - `lower.contains(variant)` substring semantics (same as the SR path).
 *
 * **Implications for downstream increments**:
 *   - V4 may flip from "feed-when-RMS-active" (V4a, default) to "feed-always"
 *     (V4b). Either way the match contract is unchanged.
 *   - V5 health check repurposes Inc 8's threshold mechanic against
 *     "no Vosk output despite non-silent RMS" instead of NO_SPEECH errors.
 */
class VoskEngineParityTest {

    // -------------------------------------------------------------------------
    // extractText — JSON parsing
    // -------------------------------------------------------------------------

    @Test
    fun `extractTextReturnsTextField`() {
        assertEquals("hey wake up", VoskWakeWordEngine.extractText("""{"text": "hey wake up"}"""))
    }

    @Test
    fun `extractTextReturnsPartialField`() {
        assertEquals("wake", VoskWakeWordEngine.extractText("""{"partial": "wake"}"""))
    }

    @Test
    fun `extractTextReturnsEmptyForEmptyText`() {
        assertEquals("", VoskWakeWordEngine.extractText("""{"text": ""}"""))
    }

    @Test
    fun `extractTextReturnsEmptyForBlankPartial`() {
        assertEquals("", VoskWakeWordEngine.extractText("""{"partial": "   "}"""))
    }

    @Test
    fun `extractTextReturnsEmptyForUnrelatedJson`() {
        // Vosk's final result on no audio: `{"text": ""}` is the typical
        // shape; a malformed/empty object should also yield empty without
        // throwing (engine must be robust to recognizer hiccups).
        assertEquals("", VoskWakeWordEngine.extractText("{}"))
        assertEquals("", VoskWakeWordEngine.extractText(""))
    }

    // -------------------------------------------------------------------------
    // findMatch — variant matching with realtime-first precedence
    // -------------------------------------------------------------------------

    @Test
    fun `findMatchReturnsNullForBlankText`() {
        assertNull(VoskWakeWordEngine.findMatch("", listOf("my friend"), listOf("wake up")))
        assertNull(VoskWakeWordEngine.findMatch("   ", listOf("my friend"), listOf("wake up")))
    }

    @Test
    fun `findMatchReturnsNullWhenNoVariantMatches`() {
        assertNull(
            VoskWakeWordEngine.findMatch(
                "good morning world",
                talkVariants = listOf("my friend"),
                wakeVariants = listOf("wake up"),
            )
        )
    }

    @Test
    fun `findMatchReturnsWakeOnExactWakePhrase`() {
        val m = VoskWakeWordEngine.findMatch(
            "wake up",
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        assertEquals(true, m?.isRealtime)
        assertEquals("wake up", m?.matchedVariant)
    }

    @Test
    fun `findMatchReturnsWakeOnSubstringMatch`() {
        // Vosk partial result with conversational lead-in.
        val m = VoskWakeWordEngine.findMatch(
            "hey wake up please",
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        assertEquals(true, m?.isRealtime)
    }

    @Test
    fun `findMatchReturnsTalkOnTalkPhrase`() {
        val m = VoskWakeWordEngine.findMatch(
            "hey my friend",
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        assertEquals(false, m?.isRealtime)
        assertEquals("my friend", m?.matchedVariant)
    }

    @Test
    fun `findMatchPrefersWakeWhenBothMatch`() {
        // Precedence: realtime wakeVariants checked FIRST. Matches
        // WakeWordDetector.checkForWakeWord at HEAD ddfb53f lines 1015–1031.
        val m = VoskWakeWordEngine.findMatch(
            "wake up my friend",
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        assertEquals(true, m?.isRealtime)
    }

    @Test
    fun `findMatchIsCaseInsensitive`() {
        val m = VoskWakeWordEngine.findMatch(
            "WAKE UP NOW",
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        assertEquals(true, m?.isRealtime)
    }

    @Test
    fun `findMatchHandlesEmptyVariantLists`() {
        // Wake-disabled config (wakeWord = "" → empty wakeVariants).
        // Talk-only must still work.
        val m = VoskWakeWordEngine.findMatch(
            "my friend",
            talkVariants = listOf("my friend"),
            wakeVariants = emptyList(),
        )
        assertEquals(false, m?.isRealtime)
    }

    // -------------------------------------------------------------------------
    // buildKeywordGrammar — Vosk constrained-vocab grammar
    // -------------------------------------------------------------------------

    @Test
    fun `buildKeywordGrammarReturnsAllPhrasesPlusUnk`() {
        val g = VoskWakeWordEngine.buildKeywordGrammar(
            talkVariants = listOf("my friend"),
            wakeVariants = listOf("wake up"),
        )
        // Order-agnostic — Vosk doesn't care, but we want stable assertions.
        // Strip JSON quoting and split. `[unk]` is Vosk's special sentinel
        // for "any other speech" — required so the recognizer doesn't reject
        // unknown audio with -ENOTRECOG.
        val phrases = parseJsonArray(g)
        assertTrue("must include talk variant", phrases.contains("my friend"))
        assertTrue("must include wake variant", phrases.contains("wake up"))
        assertTrue("must include [unk] sentinel", phrases.contains("[unk]"))
    }

    @Test
    fun `buildKeywordGrammarDeduplicatesPhrases`() {
        // If a user configures the same phrase in both slots (silly but
        // possible), the grammar shouldn't list it twice.
        val g = VoskWakeWordEngine.buildKeywordGrammar(
            talkVariants = listOf("hello"),
            wakeVariants = listOf("hello"),
        )
        val phrases = parseJsonArray(g)
        assertEquals(
            "hello + [unk] = 2 entries",
            2,
            phrases.size,
        )
    }

    @Test
    fun `buildKeywordGrammarHandlesEmptyVariantLists`() {
        val g = VoskWakeWordEngine.buildKeywordGrammar(
            talkVariants = emptyList(),
            wakeVariants = emptyList(),
        )
        val phrases = parseJsonArray(g)
        assertEquals(listOf("[unk]"), phrases)
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private fun parseJsonArray(json: String): List<String> {
        val arr = org.json.JSONArray(json)
        return (0 until arr.length()).map { arr.getString(it) }
    }
}
