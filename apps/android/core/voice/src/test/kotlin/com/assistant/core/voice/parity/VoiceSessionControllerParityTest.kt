package com.assistant.core.voice.parity

import com.assistant.core.audio.ports.AppliedRoute
import com.assistant.core.audio.ports.FallbackReason
import com.assistant.core.audio.ports.OutputChoice
import com.assistant.core.audio.ports.ProviderKind
import com.assistant.core.audio.ports.Route
import com.assistant.core.audio.ports.SpeakerMode
import com.assistant.core.testing.FakeAudioSession
import com.assistant.core.testing.FakeClock
import com.assistant.core.testing.FakeOrchestratorContext
import com.assistant.core.testing.FakeVoiceApi
import com.assistant.core.testing.FakeVoiceTransport
import com.assistant.core.testing.FakeVoiceTransportFactory
import com.assistant.core.testing.FakeVoiceWire
import com.assistant.core.testing.FakeWakeHandoff
import com.assistant.core.testing.OrderLog
import com.assistant.core.testing.PinsConstant
import com.assistant.core.testing.RecordingCues
import com.assistant.core.testing.RecordingLog
import com.assistant.core.testing.RecordingTranscriptSink
import com.assistant.core.testing.obj
import com.assistant.core.voice.ports.ConnectionSignal
import com.assistant.core.voice.ports.ConnectionType
import com.assistant.core.voice.ports.ProviderPhase
import com.assistant.core.voice.ports.ProviderSignal
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceInbound
import com.assistant.core.voice.ports.VoiceSessionDeps
import com.assistant.core.voice.ports.VoiceSessionEvent
import com.assistant.core.voice.ports.VoiceStartConfig
import com.assistant.core.voice.ports.VoiceStartRequest
import java.util.Collections
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The realtime voice session (inv04 §3.2 table, ownership and delivery sub-machines; §10.2 "Voice
 * session"; ports the old `VoiceControllerParityTest`; RS-01, RS-09…RS-17, RS-19, RS-27, RS-29, RS-30).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceSessionControllerParityTest {

    private class Rig(private val ts: TestScope) {
        val order = OrderLog()
        val clock = FakeClock.boundTo(ts.testScheduler)
        val t0 = clock.nowMs()
        val log = RecordingLog()
        val api = FakeVoiceApi(clock, order = order)
        val wire = FakeVoiceWire(clock, order)
        val ctx = FakeOrchestratorContext()
        val wake = FakeWakeHandoff(clock, order = order)
        val transports = FakeVoiceTransportFactory(clock)
        val audio = FakeAudioSession(clock)
        val transcripts = RecordingTranscriptSink()
        val cues = RecordingCues(order)
        val controller = voiceCore.sessionController(
            VoiceSessionDeps(ts.backgroundScope, clock, log, api, wire, ctx, wake, transports, audio, transcripts, cues),
        )
        val toasts: MutableList<String> = Collections.synchronizedList(mutableListOf())

        init {
            ts.backgroundScope.launch(UnconfinedTestDispatcher(ts.testScheduler)) {
                controller.events.collect { if (it is VoiceSessionEvent.Toast) toasts += it.message }
            }
        }

        val state get() = controller.state.value
        val phase get() = state.phase
        val transport: FakeVoiceTransport get() = transports.last
        fun rel(abs: Long) = abs - t0
        fun advance(ms: Long) { ts.testScheduler.advanceTimeBy(ms); ts.runCurrent() }
        fun advanceTo(relMs: Long) { val d = t0 + relMs - clock.nowMs(); if (d > 0) ts.testScheduler.advanceTimeBy(d); ts.runCurrent() }

        /** Starts voice and lets it reach ACTIVE (pause ack → config → voice_start → session → transport). */
        fun startActive() {
            controller.startVoice()
            advance(1_000)
            check(phase == SessionPhase.ACTIVE) { "not active: $phase" }
        }

        fun inbound(frame: VoiceInbound) { controller.onInbound(frame); ts.runCurrent() }
        fun connection(signal: ConnectionSignal) { controller.onConnection(signal); ts.runCurrent() }
    }

    private val update = obj("""{"type":"session.update","session":{"voice":"cedar"}}""")

    // ── start ─────────────────────────────────────────────────────────────────────────────────

    @Test
    fun startOutsideAnOrchestratorSessionErrorsWithoutPausingWake() = runTest {
        val r = Rig(this)
        r.ctx.isOrchestratorSession = false
        r.controller.startVoice()
        r.advance(3_000)
        assertEquals(SessionPhase.ERROR, r.phase)
        assertEquals("Voice only available for orchestrator sessions", r.state.errorMessage)
        assertTrue(r.wake.pauses.isEmpty())
        assertTrue(r.api.configCalls.isEmpty())
    }

    @Test
    fun startPausesWakeThenLoadsConfigThenSendsVoiceStartThenOpensTheSession() = runTest {
        val r = Rig(this)
        r.startActive()
        assertEquals(listOf("wake.pause", "api.getVoiceConfig", "wire.voiceStart", "api.startVoiceSession"), r.order.all.take(4))
        assertEquals(
            VoiceStartRequest("local-1", "jsonl-1", "openai", "gpt-realtime", "cedar", "", null),
            r.wire.voiceStarts.single().request,
        )
        assertEquals(listOf("openai" to ConnectionType.WEBRTC), r.transports.requests)
        assertEquals(FakeVoiceApi.WEBRTC_CONNECTION, r.transport.connectInfo)
        assertTrue(r.state.isOwner)
        assertFalse(r.state.remoteVoiceActive)
        assertEquals(1, r.audio.focusAcquired)
    }

    @Test
    fun voiceStartFallsBackToTheCurrentSessionIdAndKeepsANonBlankEndpoint() = runTest {
        val r = Rig(this)
        r.ctx.jsonlSessionId = null
        r.api.config = VoiceStartConfig("google", "gemini-live", "Puck", "pt", "wss://relay")
        r.api.connection = FakeVoiceApi.WS_CONNECTION
        r.startActive()
        assertEquals(VoiceStartRequest("local-1", "sdk-1", "google", "gemini-live", "Puck", "pt", "wss://relay"), r.wire.voiceStarts.single().request)
        assertEquals(listOf("google" to ConnectionType.WEBSOCKET), r.transports.requests)
    }

    /** inv04 §3.2 PAUSING_WAKE: the pause ack is awaited at most 2000 ms, then start proceeds anyway. */
    @Test
    fun pauseAckIsAwaitedAtMostTwoSeconds() = runTest {
        val r = Rig(this)
        r.wake.autoAck = false
        r.controller.startVoice()
        r.advance(1_999)
        assertTrue(r.api.configCalls.isEmpty())
        r.advance(1)
        assertEquals(2_000L, r.rel(r.api.configCalls.single()))
    }

    @Test
    fun aPromptPauseAckProceedsImmediately() = runTest {
        val r = Rig(this)
        r.wake.autoAck = false
        r.controller.startVoice()
        r.advance(300)
        r.wake.pendingAcks.single().complete(Unit)
        r.advance(0)
        assertEquals(300L, r.rel(r.api.configCalls.single()))
    }

    @Test
    fun markConnectingFlipsTheUiSynchronously() = runTest {
        val r = Rig(this)
        r.controller.markConnecting()
        assertEquals(SessionPhase.CONNECTING, r.phase)
        assertTrue(r.api.configCalls.isEmpty())
    }

    @Test
    fun aSecondStartWhileStartingOpensOneTransport() = runTest {
        val r = Rig(this)
        r.controller.startVoice()
        r.advance(60)
        r.controller.startVoice()
        r.advance(2_000)
        assertEquals(1, r.transports.created.size)
    }

    @Test
    fun gainsAreAppliedToTheTransportAtCreationAndClamped() = runTest {
        val r = Rig(this)
        r.controller.setMicGain(5f)
        r.controller.setEchoDuckingGain(0.1f)
        r.startActive()
        assertEquals(2.0f, r.transport.micGain!!, 0f)
        assertEquals(0.1f, r.transport.duckGain!!, 0f)
        r.controller.setMicGain(1.2f)
        assertEquals(1.2f, r.transport.micGain!!, 0f)
    }

    // ── delivery (RS-01, RS-13) ───────────────────────────────────────────────────────────────

    /** RS-01 (`8424f0f`): `session_started` with the update lands before the transport exists → applied before connect. */
    @Test
    fun rs01_sessionUpdateBeforeTheTransportIsDeliveredBeforeConnect() = runTest {
        val r = Rig(this)
        r.api.latencyMs = 500
        r.controller.startVoice()
        r.advance(600) // voice_start sent at 500; startVoiceSession in flight
        assertTrue(r.transports.created.isEmpty())
        r.inbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = true, voiceSessionUpdate = update))
        r.advance(1_000)
        assertEquals(listOf(update), r.transport.commands)
        assertEquals("handed over before connect", 1, r.transport.commandsBeforeConnect)
        assertEquals("the transport's DC-open fallback returns the cached update", update, r.transport.fallback!!.invoke())
    }

    /** RS-13 (`71f19ce`): a non-initiator never applies `voice_session_update`. */
    @Test
    fun rs13_nonInitiatorDoesNotApplySessionUpdate() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = update))
        assertFalse(r.state.isOwner)
        assertTrue(r.state.remoteVoiceActive)
        r.startActive()
        assertTrue("non-initiator update must not reach the provider", r.transport.commands.isEmpty())
    }

    // ── ownership (RS-11, RS-12) ──────────────────────────────────────────────────────────────

    /**
     * RS-11 (`9b24d1a`): a missing `voice_initiator` decodes as false (the codec default, A-02) → the
     * controller treats the device as a passive peer.
     */
    @Test
    @PinsConstant("protocol.voice_initiator_default")
    fun rs11_missingInitiatorMeansNotOwner() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = null))
        assertFalse(r.state.isOwner)
        assertTrue(r.state.remoteVoiceActive)
        r.inbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = true, voiceSessionUpdate = null))
        assertTrue(r.state.isOwner)
        assertFalse(r.state.remoteVoiceActive)
    }

    /** RS-11: a first connect never auto-starts voice. */
    @Test
    fun rs11_firstConnectNeverStartsVoice() = runTest {
        val r = Rig(this)
        r.connection(ConnectionSignal.Connected)
        r.advance(10_000)
        assertTrue(r.wire.voiceStarts.isEmpty())
        assertTrue(r.transports.created.isEmpty())
        assertEquals(SessionPhase.OFF, r.phase)
    }

    /** RS-12 (`b6d184f`/`afe77a4`): a non-owner ignores command / ending / ended / stopped. */
    @Test
    fun rs12_nonOwnerIgnoresCommandEndingEndedStopped() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.SessionStarted(voice = true, voiceInitiator = false, voiceSessionUpdate = null))
        r.inbound(VoiceInbound.Command(obj("""{"type":"response.create"}""")))
        r.inbound(VoiceInbound.Ending("timeout"))
        assertEquals(SessionPhase.OFF, r.phase)
        r.inbound(VoiceInbound.Ended("done"))
        r.inbound(VoiceInbound.Stopped)
        r.advance(10_000)
        assertTrue("no finalize → no wake resume", r.wake.resumes.isEmpty())
        assertFalse(r.transcripts.entries.contains("voiceEnded"))
        assertTrue(r.transports.created.isEmpty())
    }

    @Test
    fun ownerActiveOnlyAffectsNonOwners() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.OwnerActive(active = true, ownerLocalId = "other"))
        assertTrue(r.state.remoteVoiceActive)
        r.inbound(VoiceInbound.OwnerActive(active = false, ownerLocalId = "other"))
        assertFalse(r.state.remoteVoiceActive)
        r.startActive()
        r.inbound(VoiceInbound.OwnerActive(active = true, ownerLocalId = "other"))
        assertFalse("the owner ignores voice_owner_active", r.state.remoteVoiceActive)
    }

    @Test
    fun ownerForwardsCommandsAudioAndProviderEvents() = runTest {
        val r = Rig(this)
        r.startActive()
        val cmd = obj("""{"type":"response.create"}""")
        r.inbound(VoiceInbound.Command(cmd))
        r.inbound(VoiceInbound.AudioOut("AAAA"))
        val ev = obj("""{"type":"voice_status","status":"ready"}""")
        r.inbound(VoiceInbound.ProviderEvent(ev))
        assertEquals(listOf(cmd), r.transport.commands)
        assertEquals(listOf("AAAA"), r.transport.speakerChunks)
        assertEquals(listOf(ev), r.transport.providerEvents)
        r.transport.mirror!!.invoke(obj("""{"type":"response.done"}"""))
        r.transport.micSink!!.invoke("QUJD")
        assertEquals(1, r.wire.events.size)
        assertEquals(listOf("QUJD"), r.wire.audioIn.map { it.audioB64 })
    }

    // ── stop / ending / finalize (RS-09, RS-16, RS-17, RS-30) ──────────────────────────────────

    /** RS-16 (`67a7958`): stop shows ENDING until `voice_ended`, or finalizes after 5000 ms. */
    @Test
    fun rs16_stopShowsEndingUntilTheFiveSecondTimeout() = runTest {
        val r = Rig(this)
        r.startActive()
        r.controller.stopVoice()
        r.advance(0)
        assertEquals(1, r.wire.voiceStops.size)
        assertEquals(SessionPhase.ENDING, r.phase)
        r.advance(4_999)
        assertEquals(SessionPhase.ENDING, r.phase)
        r.advance(1)
        assertEquals(SessionPhase.OFF, r.phase)
        assertEquals(1, r.transport.disconnects)
    }

    @Test
    fun rs16_voiceEndedBeforeTheTimeoutFinalizesOnce() = runTest {
        val r = Rig(this)
        r.startActive()
        r.controller.stopVoice()
        r.advance(1_000)
        r.inbound(VoiceInbound.Ended("user"))
        assertEquals(SessionPhase.OFF, r.phase)
        r.advance(10_000)
        assertEquals("the ending timeout must not finalize again", 1, r.wake.resumes.size)
    }

    @Test
    fun voiceEndingFromTheBackendArmsTheSameTimeout() = runTest {
        val r = Rig(this)
        r.startActive()
        r.inbound(VoiceInbound.Ending("idle"))
        assertEquals(SessionPhase.ENDING, r.phase)
        r.advance(5_000)
        assertEquals(SessionPhase.OFF, r.phase)
    }

    /** RS-17 (`aab5f71`/`90d6913`): an AI-initiated end finalizes streaming text and tears down locally. */
    @Test
    fun rs17_aiInitiatedVoiceEndedFinalizesStreamingAndTearsDown() = runTest {
        for (end in listOf(VoiceInbound.Ended("tool"), VoiceInbound.Stopped)) {
            val r = Rig(this)
            r.startActive()
            r.inbound(end)
            assertTrue("$end", r.transcripts.entries.contains("voiceEnded"))
            assertEquals("$end", SessionPhase.OFF, r.phase)
            r.advance(100)
            assertEquals("$end", 1, r.transport.disconnects)
            r.controller.release()
        }
    }

    /** RS-30 (`0bc612f`): the wake word resumes exactly once, 1500 ms after the stop (mic release). */
    @Test
    fun rs30_wakeResumesOnly1500msAfterStopAndFinalizeIsIdempotent() = runTest {
        val r = Rig(this)
        r.startActive()
        r.inbound(VoiceInbound.VadState("speaking", 4_000))
        r.inbound(VoiceInbound.Ended("x"))
        val stoppedAt = r.rel(r.clock.nowMs())
        r.inbound(VoiceInbound.Stopped)
        r.connection(ConnectionSignal.Disconnected(willReconnect = false))
        assertFalse(r.state.isOwner)
        assertEquals("idle", r.state.vadState)
        assertEquals(0L, r.state.vadDurationMs)
        r.advance(1_499)
        assertTrue(r.wake.resumes.isEmpty())
        r.advance(1)
        assertEquals(listOf(stoppedAt + 1_500), r.wake.resumes.map { r.rel(it) })
        r.advance(10_000)
        assertEquals(1, r.wake.resumes.size)
    }

    /** RS-09 (`c0cad2c`): `connection_info == null` finalizes and re-arms the wake word. */
    @Test
    fun rs09_connectionInfoFailureFinalizesAndResumesWake() = runTest {
        val r = Rig(this)
        r.api.connection = null
        r.controller.startVoice()
        r.advance(3_000)
        assertEquals(SessionPhase.OFF, r.phase)
        assertEquals(1, r.wake.resumes.size)
        assertTrue(r.toasts.toString(), r.toasts.any { it.startsWith("Voice error:") })
        assertTrue(r.transcripts.entries.any { it.startsWith("system:Voice error:") })
    }

    /** RS-09: a fatal transport error toasts, finalizes and re-arms the wake word. */
    @Test
    fun rs09_transportErrorToastsFinalizesAndResumesWake() = runTest {
        val r = Rig(this)
        r.startActive()
        r.transport.emit(ProviderSignal.Error("Instructions too long"))
        r.advance(0)
        assertEquals(listOf("Voice error: Instructions too long"), r.toasts)
        assertEquals(SessionPhase.OFF, r.phase)
        r.advance(1_500)
        assertEquals(1, r.wake.resumes.size)
        assertEquals(1, r.transport.disconnects)
    }

    /** B10: a voice-config failure after the pause must not strand the wake word. */
    @Test
    fun b10_voiceConfigFailureNeverStrandsTheWakeWord() = runTest {
        val r = Rig(this)
        r.api.config = null
        r.controller.startVoice()
        r.advance(5_000)
        assertEquals(1, r.wake.pauses.size)
        assertEquals(1, r.wake.resumes.size)
    }

    // ── socket continuity (RS-10, RS-11) ──────────────────────────────────────────────────────

    /** RS-10 (`9db8f37`): a transient drop keeps the session. */
    @Test
    fun rs10_transientDisconnectKeepsTheSession() = runTest {
        val r = Rig(this)
        r.startActive()
        r.connection(ConnectionSignal.Disconnected(willReconnect = true))
        r.advance(10_000)
        assertEquals(SessionPhase.ACTIVE, r.phase)
        assertTrue(r.wake.resumes.isEmpty())
        assertEquals(0, r.transport.disconnects)
    }

    /** RS-10: a terminal drop finalizes, re-arms the wake word and forgets the config. */
    @Test
    fun rs10_terminalDisconnectFinalizesAndForgetsTheConfig() = runTest {
        val r = Rig(this)
        r.startActive()
        r.connection(ConnectionSignal.Disconnected(willReconnect = false))
        r.advance(2_000)
        assertEquals(SessionPhase.OFF, r.phase)
        assertEquals(1, r.wake.resumes.size)
        r.connection(ConnectionSignal.Reconnected("local-1", "sdk-9"))
        assertEquals(1, r.wire.voiceStarts.size)
        assertEquals(1, r.wire.resumeStarts.size)
    }

    @Test
    fun reconnectDuringLiveVoiceResendsVoiceStartWithTheCachedConfig() = runTest {
        val r = Rig(this)
        r.api.config = VoiceStartConfig("google", "gemini-live", "Puck", "en", "")
        r.api.connection = FakeVoiceApi.WS_CONNECTION
        r.startActive()
        r.connection(ConnectionSignal.Disconnected(willReconnect = true))
        r.connection(ConnectionSignal.Reconnected("local-2", "sdk-2"))
        assertEquals(VoiceStartRequest("local-2", "sdk-2", "google", "gemini-live", "Puck", "en", null, reattach = true), r.wire.voiceStarts.last().request)   // OPEN-2
        assertTrue(r.wire.resumeStarts.isEmpty())
    }

    @Test
    fun reconnectWithoutVoiceSendsAPlainResumeStart() = runTest {
        val r = Rig(this)
        r.connection(ConnectionSignal.Reconnected("local-1", "sdk-1"))
        assertEquals("local-1", r.wire.resumeStarts.single().localId)
        assertTrue(r.wire.voiceStarts.isEmpty())
    }

    // ── pre-start states (RS-14) ──────────────────────────────────────────────────────────────

    @Test
    fun preProviderVoiceStatusIsSurfaced() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.ProviderEvent(obj("""{"type":"voice_status","status":"summarizing"}""")))
        assertEquals(SessionPhase.SUMMARIZING, r.phase)
        r.inbound(VoiceInbound.ProviderEvent(obj("""{"type":"voice_status","status":"preparing"}""")))
        assertEquals(SessionPhase.CONNECTING, r.phase)
    }

    /** RS-14 (`f5b339a`): `session_started voice=false` clears a ghost Summarizing/Connecting. */
    @Test
    fun rs14_sessionStartedVoiceFalseClearsGhostPreStartState() = runTest {
        val r = Rig(this)
        r.inbound(VoiceInbound.ProviderEvent(obj("""{"type":"voice_status","status":"summarizing"}""")))
        r.inbound(VoiceInbound.SessionStarted(voice = false, voiceInitiator = false, voiceSessionUpdate = null))
        assertEquals(SessionPhase.OFF, r.phase)
        r.controller.markConnecting()
        r.inbound(VoiceInbound.SessionStarted(voice = false, voiceInitiator = false, voiceSessionUpdate = null))
        assertEquals(SessionPhase.OFF, r.phase)
    }

    @Test
    fun theTransportsInitialOffDoesNotClobberABackendSummarizing() = runTest {
        val r = Rig(this)
        r.transports.configure = { t -> t.onConnect = { delay(1_000); setPhase(ProviderPhase.CONNECTING); setPhase(ProviderPhase.ACTIVE) } }
        r.inbound(VoiceInbound.ProviderEvent(obj("""{"type":"voice_status","status":"summarizing"}""")))
        r.controller.startVoice()
        r.advance(300)
        assertEquals(1, r.transports.created.size)
        assertEquals(SessionPhase.SUMMARIZING, r.phase)
        r.advance(1_000)
        assertEquals(SessionPhase.ACTIVE, r.phase)
    }

    // ── live session signals ──────────────────────────────────────────────────────────────────

    @Test
    fun vadStateIsMirrored() = runTest {
        val r = Rig(this)
        r.startActive()
        r.inbound(VoiceInbound.VadState("speaking", 3_500))
        assertEquals("speaking", r.state.vadState)
        assertEquals(3_500L, r.state.vadDurationMs)
    }

    /** RS-19 (`b586e4b`/`ff517c2`): goAway → banner + an audible beep; Reconnecting → banner only; ACTIVE clears it. */
    @Test
    fun rs19_reconnectWarningShowsBannerAndBeeps() = runTest {
        val r = Rig(this)
        r.startActive()
        r.transport.emit(ProviderSignal.ReconnectWarning(30))
        r.advance(0)
        assertEquals("Pausing in ~30s to reconnect…", r.state.reconnectBanner)
        assertEquals(listOf("reconnect"), r.cues.played)
        r.transport.emit(ProviderSignal.Reconnecting)
        r.advance(0)
        assertEquals("Pausing for a second to reconnect…", r.state.reconnectBanner)
        assertEquals("Reconnecting does not beep again", listOf("reconnect"), r.cues.played)
        r.transport.setPhase(ProviderPhase.CONNECTING)
        r.transport.setPhase(ProviderPhase.ACTIVE)
        r.advance(0)
        assertNull(r.state.reconnectBanner)
        r.transport.emit(ProviderSignal.ReconnectWarning(null))
        r.advance(0)
        assertEquals("Reconnecting shortly…", r.state.reconnectBanner)
    }

    @Test
    fun transcriptsAndTurnsReachTheSink() = runTest {
        val r = Rig(this)
        r.startActive()
        r.transport.emit(ProviderSignal.UserTranscript("what time is it"))
        r.transport.emit(ProviderSignal.TextComplete(""))
        r.transport.emit(ProviderSignal.TextComplete("It's noon."))
        r.transport.emit(ProviderSignal.TurnComplete)
        r.advance(0)
        assertEquals(listOf("user:what time is it", "assistant:It's noon.", "turnComplete"), r.transcripts.entries)
    }

    @Test
    fun providerPhasesDriveTheUi() = runTest {
        val r = Rig(this)
        r.startActive()
        for (p in listOf(ProviderPhase.SPEAKING, ProviderPhase.THINKING, ProviderPhase.TOOL_USE, ProviderPhase.ACTIVE)) {
            r.transport.setPhase(p)
            r.advance(0)
            assertEquals(p.name, r.phase.name)
        }
    }

    @Test
    fun muteTogglesThroughTheTransportAndResetsOnFinalize() = runTest {
        val r = Rig(this)
        r.startActive()
        r.controller.toggleMute()
        assertTrue(r.state.isMuted)
        r.inbound(VoiceInbound.Ended("x"))
        assertFalse(r.state.isMuted)
    }

    // ── routing (RS-27, RS-29) ────────────────────────────────────────────────────────────────

    /**
     * RS-27 (`d36d31b`): the route is applied at start and re-applied after the provider's audio
     * init — SEQUENTIAL delays 1000, 3000, 5000 ms ⇒ +1 s, +4 s, +9 s after the first apply.
     */
    @Test
    fun rs27_routeReappliedAfterProviderInit() = runTest {
        val r = Rig(this)
        r.startActive()
        r.advance(20_000)
        val first = r.audio.applies.first().atMs
        assertEquals(listOf(0L, 1_000L, 4_000L, 9_000L), r.audio.applies.map { it.atMs - first })
        assertTrue(r.audio.applies.all { it.provider == ProviderKind.WEBRTC && it.desired == OutputChoice.AUTO })
        assertEquals("speaker mode forwarded on every apply", 4, r.transport.speakerModes.size)
    }

    @Test
    fun changingTheOutputMidCallReappliesTheRoute() = runTest {
        val r = Rig(this)
        r.startActive()
        r.advance(20_000)
        r.controller.setAudioOutput(OutputChoice.LOUDSPEAKER)
        assertEquals(OutputChoice.LOUDSPEAKER, r.audio.applies.last().desired)
        r.inbound(VoiceInbound.Ended("x"))
        r.advance(100)
        assertEquals(1, r.audio.released)
    }

    /** RS-29 (`b974756`): a Bluetooth fallback is surfaced as a toast. */
    @Test
    fun rs29_bluetoothFallbackToasts() = runTest {
        val r = Rig(this)
        r.audio.result = { _, _ -> AppliedRoute(Route.BluetoothUnsupported(FallbackReason.BT_A2DP_REQUIRES_WS_PROVIDER), SpeakerMode.CALL, null) }
        r.controller.setAudioOutput(OutputChoice.BLUETOOTH)
        r.startActive()
        assertTrue(r.toasts.toString(), r.toasts.any { it.contains("loudspeaker", ignoreCase = true) })
    }
}
