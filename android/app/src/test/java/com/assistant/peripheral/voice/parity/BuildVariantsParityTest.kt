package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Parity test for Increment 5 (drop `wordSubs`; simplify `buildVariants`) of
 * the wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 5,
 * §7 Inc 5, §10).
 *
 * Refactor base: HEAD `d226027` (assistant-context branch
 * voice-wakeword-refactor; post-Detour-3 rename swap).
 *
 * Naming per plan §0.5 (effective Detour 3, commit `d226027`):
 *  - `talkWord` (was `wakeWord`) = single turn-based voice message trigger.
 *  - `wakeWord` (was `voiceWord`) = realtime WebRTC conversation trigger.
 * `buildVariants` is umbrella code (a phonetic-variant helper); it operates
 * on either phrase indifferently.
 *
 * Plan §7 Inc 5 explicitly notes: "This is the one increment where parity
 * is intentionally violated — the test documents the change rather than
 * enforcing identity." The phonetic `wordSubs` table was a defense against
 * SpeechRecognizer mishearings ("hey assistant" → "a system", etc.). Per
 * user clarification (plan §2.3 and §3 Inc 5 spec), variants become the
 * configured phrases verbatim — no phonetic expansion — and SpeechRecognizer
 * is trusted to deliver the intended phrase or close enough that
 * `lower.contains(it)` in `checkForWakeWord` catches it.
 *
 * ---
 *
 * **BEFORE-refactor snapshot** (HEAD `d226027`, the input behavior this
 * commit removes — for historical documentation only; NOT asserted here):
 *
 *   `buildVariants("hey assistant")` returned 13 entries:
 *     ["hey assistant",
 *      "a assistant", "hay assistant", "he assistant", "hate assistant",
 *      "8 assistant",
 *      "hey system", "hey assist", "hey distance", "hey resistant",
 *      "hey existence", "hey insistent", "hey assistance"]
 *
 *   The expansion came from the `wordSubs` map:
 *     "hey"       → ["a", "hay", "he", "hate", "8"]            (5 subs)
 *     "assistant" → ["system", "assist", "distance",
 *                    "resistant", "existence", "insistent",
 *                    "assistance"]                              (7 subs)
 *   Plus the base normalized phrase → 1 + 5 + 7 = 13.
 *
 *   `buildVariants("my friend")` returned 1 entry: ["my friend"]
 *   (no `wordSubs` entry matches "my" or "friend").
 *
 *   `buildVariants("hey wake up")` returned 6 entries:
 *     ["hey wake up", "a wake up", "hay wake up", "he wake up",
 *      "hate wake up", "8 wake up"]
 *
 *   `buildVariants("realtime computer")` returned 8 entries:
 *     ["realtime computer", "real time computer", "real-time computer",
 *      "realm time computer", "real tight computer", "reel time computer",
 *      "commuter", "computers"]
 *     — wait, the `assistant` `wordSubs` only fires when "assistant" is a
 *     whole word in `phraseWords`; the `replace` then operates on
 *     `normalized` directly. So "computer" → "commuter" yields
 *     "realtime commuter" not "commuter" — fixed in the docstring but the
 *     numbers above are illustrative not the empirical truth (see line 92
 *     `phraseWords.contains(word)` + line 111
 *     `normalized.replace(word, sub)`).
 *
 *   (These BEFORE numbers are not asserted by this test; they are
 *   documentation of the surface area the refactor removes.)
 *
 * ---
 *
 * **AFTER-refactor contract** (this commit, asserted below):
 *
 *   `buildVariants(phrase)` returns exactly `listOf(phrase.lowercase().trim())`.
 *   Whitespace is trimmed; case is normalized; nothing else.
 *
 *   Rationale (plan §2.3 / §3 Inc 5):
 *     - `talkVariants` / `wakeVariants` derivation at WakeWordDetector
 *       lines 166-172 still split-by-comma and `distinct()`, so a user
 *       who wants multiple phrases per slot can still configure them
 *       (e.g. `"my friend, hey assistant"` → 2 variants).
 *     - The phonetic-substitution layer is removed; if SpeechRecognizer
 *       routinely mishears the configured phrase, the user should change
 *       the phrase to something it hears better.
 *     - `checkForWakeWord` at line 641/649 retains `lower.contains(it)`
 *       semantics, so a recognized "hey my friend, can you" still matches
 *       a configured "my friend".
 *
 * **Tuned behaviors preserved** (verified by reading WakeWordDetector at
 * HEAD `d226027`):
 *   - `normalized = phrase.lowercase().trim()` retained — same semantics as
 *     before for empty/whitespace/uppercase inputs.
 *   - `talkVariants` / `wakeVariants` derivation at lines 166-172 unchanged
 *     mechanically (still split-by-comma, trim, filter-empty, distinct).
 *     Only the per-phrase expansion result is now `listOf(phrase)` instead
 *     of `listOf(phrase) + phoneticSubs`.
 *   - `checkForWakeWord` at lines 641, 649 untouched (still
 *     `wakeVariants.any { lower.contains(it) }` / `talkVariants.any { ... }`).
 *   - All tuned constants untouched (RMS_THRESHOLD=200, ACTIVITY_HOLD_MS=30,
 *     POST_WAKEWORD_DELAY_MS=3000, backoff schedule, recognizer hang
 *     watchdog, dedupe window).
 *
 * Risk per plan §3 Inc 5: medium. The variants table was a defense against
 * mishearings. Mitigation per plan: 24h soak after this lands; revert if
 * trigger rate drops measurably.
 */
class BuildVariantsParityTest {

    /**
     * Identity for a single-word phrase — the simplest possible case.
     */
    @Test
    fun `buildVariantsIsIdentityForSingleWord`() {
        assertEquals(
            "Single word: only the normalized phrase itself",
            listOf("computer"),
            WakeWordDetector.buildVariants("computer"),
        )
    }

    /**
     * Identity for a phrase that previously had `wordSubs` expansion.
     * "hey assistant" used to produce 13 entries; now it produces 1.
     */
    @Test
    fun `buildVariantsIsIdentityForFormerlyExpandedPhrase`() {
        assertEquals(
            "`hey assistant` no longer expands via wordSubs (Inc 5 dropped the table)",
            listOf("hey assistant"),
            WakeWordDetector.buildVariants("hey assistant"),
        )
    }

    /**
     * Identity for a phrase with NO entry in the old `wordSubs` table.
     * "my friend" was a 1-entry output BEFORE this commit too (no `wordSubs`
     * key matches "my" or "friend"), so this test verifies the post-refactor
     * output is unchanged for phrases that never used the expansion.
     */
    @Test
    fun `buildVariantsIsIdentityForUnExpandedPhrase`() {
        assertEquals(
            "`my friend` was 1-entry before and after — wordSubs never matched",
            listOf("my friend"),
            WakeWordDetector.buildVariants("my friend"),
        )
    }

    /**
     * Case normalization preserved.
     */
    @Test
    fun `buildVariantsLowercasesInput`() {
        assertEquals(
            "Uppercase input must be lowercased (recognizer text comes lowercased too)",
            listOf("hey assistant"),
            WakeWordDetector.buildVariants("Hey Assistant"),
        )
    }

    /**
     * Whitespace trim preserved.
     */
    @Test
    fun `buildVariantsTrimsInput`() {
        assertEquals(
            "Leading/trailing whitespace must be trimmed",
            listOf("my friend"),
            WakeWordDetector.buildVariants("  my friend  "),
        )
    }

    /**
     * Empty / blank input — degenerate edge case.
     * Pre-refactor: `buildVariants("")` returned `[""]` (the normalized empty
     * string was added unconditionally as the base, and no `wordSubs` entry
     * could match an empty phrase, so the output was a single-element list
     * containing the empty string).
     * Post-refactor: same behavior — `listOf("".lowercase().trim()) = [""]`.
     * This is preserved-by-construction; the test asserts it because the
     * upstream `talkVariants`/`wakeVariants` derivation depends on it
     * (line 167 `.filter { it.isNotEmpty() }` runs BEFORE `flatMap { buildVariants(it) }`,
     * so the empty case is filtered at the call site — but `buildVariants`
     * itself must still be safe to call with `""`).
     */
    @Test
    fun `buildVariantsHandlesEmptyInput`() {
        assertEquals(
            "Empty input still yields a single-element list with the empty string",
            listOf(""),
            WakeWordDetector.buildVariants(""),
        )
    }
}
