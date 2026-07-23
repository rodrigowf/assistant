package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the adaptive end-of-utterance VAD introduced 2026-07-22 to fix the
 * turn-based talk capture that "never stops until you press pause."
 *
 * Root cause the fix addresses: the old FIXED voice threshold
 * (`RMS_THRESHOLD / micGain ≈ 58`) could not distinguish speech from a hot
 * mic's quiet-room floor. On the A300M the VOICE_COMMUNICATION source idles at
 * RMS ~150–200 in silence (measured live), so every "silent" frame read as
 * "voice" (170 > 58), the silence timer never drained, and capture ran to the
 * 12s hard cap. The adaptive path judges each frame against
 * `max(ambientFloor × sensitivity, ADAPTIVE_VOICE_FLOOR_MIN)`, where the floor
 * self-calibrates to the room.
 *
 * These tests exercise the two pure helpers the VAD is built from:
 * [WakeWordDetector.NoiseFloorTracker] and
 * [WakeWordDetector.adaptiveVoiceThreshold]. The capture loop itself needs a
 * live AudioRecord, so it isn't unit-tested; the decision math is.
 */
class AdaptiveVadParityTest {

    // ── adaptiveVoiceThreshold ────────────────────────────────────────────

    @Test
    fun threshold_scalesWithFloorAndSensitivity() {
        // Floor 170 (the measured A300M quiet-room level), K=2.0 → 340.
        assertEquals(340.0, WakeWordDetector.adaptiveVoiceThreshold(170.0, 2.0), 0.001)
    }

    @Test
    fun threshold_clampedToAbsoluteMinimumInSilentRoom() {
        // A near-silent room (floor ~5) must NOT drop the voice bar to ~10 —
        // clamp to the absolute minimum so a faint stray sound isn't "speech".
        val t = WakeWordDetector.adaptiveVoiceThreshold(5.0, 2.0)
        assertEquals(WakeWordDetector.adaptiveVoiceFloorMinForTest(), t, 0.001)
    }

    @Test
    fun threshold_realSpeechIsVoice_roomTailIsNot_atMeasuredLevels() {
        // The exact scenario from the field trace: floor ~170, speech ~2600,
        // post-speech tail ~170. With K=2.0 the bar is 340.
        val bar = WakeWordDetector.adaptiveVoiceThreshold(170.0, 2.0)
        assertTrue("real speech (2600) must count as voice", 2600.0 >= bar)
        assertTrue("room tail (170) must NOT count as voice", 170.0 < bar)
    }

    // ── NoiseFloorTracker ─────────────────────────────────────────────────

    @Test
    fun tracker_seedsOnFirstReading() {
        val t = WakeWordDetector.NoiseFloorTracker()
        assertEquals(170.0, t.update(170.0), 0.001)
    }

    @Test
    fun tracker_adaptsDownFast_soAPauseIsDetectedQuickly() {
        // Seed high (as if the first frame were loud speech), then feed the
        // real quiet floor. Attack (down) is fast, so after a handful of quiet
        // frames the estimate must be well below the loud seed.
        val t = WakeWordDetector.NoiseFloorTracker(2600.0)
        repeat(6) { t.update(170.0) }
        assertTrue("floor should converge toward 170 quickly, was ${t.floor}", t.floor < 600.0)
    }

    @Test
    fun tracker_adaptsUpSlowly_soALoudWordDoesNotInflateTheFloor() {
        // Establish a low floor, then a loud command word arrives. Release (up)
        // is slow, so one loud frame barely moves the floor — the following
        // silence still falls below the threshold and ends the capture.
        val t = WakeWordDetector.NoiseFloorTracker(170.0)
        t.update(2600.0)
        assertTrue("one loud frame must not inflate the floor much, was ${t.floor}", t.floor < 300.0)
        // Threshold derived from the barely-moved floor still lets the ~170
        // tail read as silence.
        val bar = WakeWordDetector.adaptiveVoiceThreshold(t.floor, 2.0)
        assertTrue("room tail (170) must stay below the bar after a loud word", 170.0 < bar)
    }

    @Test
    fun tracker_seededFromQuietPreRoll_startsNearTrueFloor() {
        // captureTalkCommand seeds the tracker from the quietest pre-roll frame,
        // so it starts near the true floor instead of converging from a loud
        // first command frame. Simulate: seed 160, first command frame is loud.
        val t = WakeWordDetector.NoiseFloorTracker(160.0)
        val bar = WakeWordDetector.adaptiveVoiceThreshold(t.floor, 2.0)
        assertTrue("with a good seed, the first loud word is already voice", 2600.0 >= bar)
    }
}
