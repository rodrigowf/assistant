package com.assistant.core.voice.session

import com.assistant.core.testing.FakeAudioSession
import com.assistant.core.testing.FakeClock
import com.assistant.core.testing.FakeOrchestratorContext
import com.assistant.core.testing.FakeVoiceApi
import com.assistant.core.testing.FakeVoiceTransportFactory
import com.assistant.core.testing.FakeVoiceWire
import com.assistant.core.testing.FakeWakeHandoff
import com.assistant.core.testing.RecordingCues
import com.assistant.core.testing.RecordingLog
import com.assistant.core.testing.RecordingTranscriptSink
import com.assistant.core.voice.ports.ConnectionSignal
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceInbound
import com.assistant.core.voice.ports.VoiceSessionDeps
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 2026-10-08, POCO X7 Pro: unlocking the phone mid-call re-sent a plain `start` (T-9 resync); the
 * backend answered `voice_initiator:false` and the controller only flipped `owner`. The WebRTC call
 * stayed up as a zombie: it dropped the `voice_command`s carrying a tool result (the model kept
 * saying the search was "still running") and ignored `voice_ended` (the call outlived
 * `end_voice_session`), while the UI showed "Voice active on another device".
 *
 * Now: a device that holds the call either stays the owner (a `voice_start` answer is pending) or
 * ends the call locally (V-13) — never both "not owner" and "still talking".
 */
@OptIn(ExperimentalCoroutinesApi::class)
class OwnershipLossTest {

    private class Rig(private val ts: TestScope) {
        val clock = FakeClock.boundTo(ts.testScheduler)
        val wire = FakeVoiceWire(clock)
        val wake = FakeWakeHandoff(clock)
        val transports = FakeVoiceTransportFactory(clock)
        val controller = DefaultVoiceSessionController(
            VoiceSessionDeps(
                ts.backgroundScope, clock, RecordingLog(), FakeVoiceApi(clock), wire, FakeOrchestratorContext(), wake,
                transports, FakeAudioSession(clock), RecordingTranscriptSink(), RecordingCues(),
            ),
        )

        val state get() = controller.state.value
        val transport get() = transports.created.single()
        fun advance(ms: Long) { ts.testScheduler.advanceTimeBy(ms); ts.runCurrent() }

        /** A live call whose `voice_start` was answered (session_started + its owner_active echo). */
        fun startActive() {
            controller.startVoice()
            advance(1_000)
            check(state.phase == SessionPhase.ACTIVE) { "not active: ${state.phase}" }
            controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = true, voiceSessionUpdate = null))
            controller.onInbound(VoiceInbound.OwnerActive(active = true, ownerLocalId = "orch"))
            advance(0)
            check(state.isOwner && !state.remoteVoiceActive)
        }
    }

    private val responseCreate = Json.parseToJsonElement("""{"type":"response.create"}""").jsonObject

    @Test
    fun ownerResyncAnsweredAsInitiatorKeepsTheCall() = runTest {
        val r = Rig(this)
        r.startActive()
        // Fixed backend: the owner's plain start is answered voice_initiator:true, metadata only.
        r.controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = true, voiceSessionUpdate = null))
        r.advance(0)
        assertEquals(SessionPhase.ACTIVE, r.state.phase)
        assertTrue(r.state.isOwner)
        assertEquals(0, r.transport.disconnects)

        r.controller.onInbound(VoiceInbound.Command(responseCreate))
        r.advance(0)
        assertEquals("tool-result commands still reach the provider", listOf(responseCreate), r.transport.commands)
    }

    @Test
    fun demotedWhileHoldingTheCallEndsItLocally() = runTest {
        val r = Rig(this)
        r.startActive()
        r.controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = null))
        r.advance(0)
        assertEquals(SessionPhase.OFF, r.state.phase)
        assertEquals("transport torn down", 1, r.transport.disconnects)
        assertFalse(r.state.isOwner)
        assertTrue("shows active elsewhere", r.state.remoteVoiceActive)
        assertTrue("never voice_stop: that would end the new owner's call", r.wire.voiceStops.isEmpty())
    }

    @Test
    fun takeoverByAnotherDeviceEndsTheLocalCall() = runTest {
        val r = Rig(this)
        r.startActive()
        // V-13: another device's voice_start (our own echo was already consumed).
        r.controller.onInbound(VoiceInbound.OwnerActive(active = true, ownerLocalId = "orch"))
        r.advance(0)
        assertEquals(SessionPhase.OFF, r.state.phase)
        assertEquals(1, r.transport.disconnects)
        assertTrue(r.state.remoteVoiceActive)
        assertTrue(r.wire.voiceStops.isEmpty())
    }

    @Test
    fun plainStartAnswerDuringAReconnectReArmKeepsTheCall() = runTest {
        val r = Rig(this)
        r.startActive()
        // Socket dropped and came back: the controller re-arms with voice_start while the conversation
        // layer sends its plain start; the plain start's answer may come first.
        r.controller.onConnection(ConnectionSignal.Disconnected(willReconnect = true))
        r.controller.onConnection(ConnectionSignal.Reconnected("orch", null))
        assertEquals(2, r.wire.voiceStarts.size)
        r.controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = null))
        r.advance(0)
        assertTrue("the voice_start answer decides", r.state.isOwner)
        assertEquals(0, r.transport.disconnects)

        r.controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = true, voiceSessionUpdate = null))
        r.controller.onInbound(VoiceInbound.OwnerActive(active = true, ownerLocalId = "orch")) // its echo
        r.advance(0)
        assertTrue(r.state.isOwner)
        assertFalse(r.state.remoteVoiceActive)
        assertEquals(0, r.transport.disconnects)
    }

    @Test
    fun voiceEndedAfterOwnershipLossLeavesNothingRunning() = runTest {
        val r = Rig(this)
        r.startActive()
        r.controller.onInbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = null))
        r.controller.onInbound(VoiceInbound.Ended("agent_end"))
        r.advance(10_000)
        assertEquals(SessionPhase.OFF, r.state.phase)
        assertEquals("disconnected exactly once", 1, r.transport.disconnects)
    }
}
