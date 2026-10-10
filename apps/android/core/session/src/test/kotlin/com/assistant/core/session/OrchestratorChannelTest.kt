package com.assistant.core.session

import com.assistant.core.network.SocketState
import com.assistant.core.protocol.ClientFrame
import com.assistant.core.protocol.ResumeCursor
import com.assistant.core.protocol.ServerFrame
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Ports the intent of `connection/parity/OrchestratorConnectionControllerParityTest` (spec 14 §1.2)
 * with virtual time: adoption + 400 ms retry, reconnect gating, recovery 0/500/1000 ms (errata
 * §12, old code), outbox for `inject_text`, L-3 audio routing, OPEN-2/3/4, and P-1.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class OrchestratorChannelTest {
    private class Rig(val ts: TestScope, autoStart: Boolean = false, cursor: (String) -> ResumeCursor? = { null }) {
        val socket = FakeFrameSocket()
        val pool = FakePool { ts.testScheduler.currentTime }
        val ids = FakeIds()
        private var minted = 0
        val channel = OrchestratorChannel(socket, pool, ids, ts.backgroundScope, OrchestratorChannel.Config(autoStart, cursor)) { "NEW${++minted}" }
        val events = channel.subscribeEvents()
        val frames = channel.subscribeFrames()
        fun settle() = ts.runCurrent()
    }

    private val url = "ws://192.168.0.200:80"

    /** OPEN-2: every automatic start reattaches. */
    private fun reattach(localId: String, sdk: String?, resumeFrom: ResumeCursor? = null) =
        ClientFrame.Start(localId, sdk, resumeFrom, reattach = true)

    @Test fun adoptionRetriesOnceAfter400msOnAnEmptyPool() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(listOf(agent()), listOf(agent(), orch()))
        r.channel.connect(url); r.settle()
        assertEquals(listOf("ws://192.168.0.200:80/api/orchestrator/chat"), r.socket.connects)
        r.socket.open(); r.settle()
        assertEquals(listOf(0L), r.pool.calls)
        advanceTimeBy(399); runCurrent()
        assertTrue(r.events.drain().none { it is ChannelEvent.Adopted })
        advanceTimeBy(1); runCurrent()
        assertEquals(listOf(0L, SessionTuning.POOL_PROBE_RETRY_MS), r.pool.calls)
        assertEquals(listOf(ChannelEvent.Adopted(OrchestratorRef("ORCH", "JSONL"), reconnect = false)), r.events.drain())
        assertEquals("ORCH", r.ids.value)
        assertEquals(listOf<ClientFrame>(reattach("ORCH", "JSONL")), r.socket.sent)
    }

    @Test fun noOrchestratorAfterTheRetry() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(agent())
        r.channel.connect(url); r.socket.open(); r.settle()
        advanceTimeBy(400); runCurrent()
        assertEquals(2, r.pool.calls.size)
        assertEquals(listOf<ChannelEvent>(ChannelEvent.NoOrchestrator), r.events.drain())
        assertTrue(r.channel.state.value.noOrchestrator)
        assertTrue("no start without an orchestrator", r.socket.sent.isEmpty())
    }

    @Test fun reconnectedOnlyOnGenuineReconnects_andServerChangeResetsTheGate() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Adopted(OrchestratorRef("ORCH", "JSONL"), false)), r.events.drain())

        r.socket.drop(); r.settle()
        r.socket.open(); r.settle()
        val ref = OrchestratorRef("ORCH", "JSONL")
        assertEquals(
            listOf(ChannelEvent.Disconnected(true), ChannelEvent.Adopted(ref, true), ChannelEvent.Reconnected(ref)),
            r.events.drain(),
        )

        // T-15: new server → teardown, per-server reset, and it actually connects.
        r.channel.changeServer("ws://10.0.0.9:8765"); r.settle()
        assertEquals(1, r.socket.disconnects)
        assertEquals("ws://10.0.0.9:8765/api/orchestrator/chat", r.socket.connects.last())
        assertEquals(null, r.ids.value)
        assertEquals(null, r.channel.state.value.orchestrator)
        r.socket.open(); r.settle()
        assertEquals("first connect to a new server is not a reconnect (dead teardownForServerUrlChange fixed)",
            listOf<ChannelEvent>(ChannelEvent.Adopted(ref, false)), r.events.drain().filter { it !is ChannelEvent.Disconnected })
    }

    @Test fun open4_reconnectToAPoolWithoutOurs_closesIt_neverRestartsIt() = runTest {
        // 2026-10-10: a backgrounded device missed the close; its reconnect used to re-send `start`
        // (taking the empty pool for a backend restart) and re-opened the conversation for everyone.
        // The pool survives restarts now, so missing = closed.
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(listOf(orch()), emptyList())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.events.drain(); r.frames.drain(); r.socket.sent.clear()

        r.socket.drop(); r.settle()
        r.socket.open(); r.settle()
        advanceTimeBy(SessionTuning.POOL_PROBE_RETRY_MS); runCurrent()
        assertEquals(listOf(ChannelEvent.Disconnected(true), ChannelEvent.OrchestratorClosed("ORCH")), r.events.drain())
        assertEquals(emptyList<ClientFrame>(), r.socket.sent)
        assertEquals(null, r.channel.state.value.orchestrator)
        assertEquals(null, r.ids.value)
        assertEquals(listOf<ServerFrame>(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true)), r.frames.drain())
    }

    @Test fun open2_aRestoredOrchestratorAfterABackendRestartIsReattached() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())                                     // listed again after the restart
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.socket.sent.clear()
        r.socket.drop(); r.settle(); r.socket.open(); r.settle()
        assertEquals(listOf<ClientFrame>(reattach("ORCH", "JSONL")), r.socket.sent)
    }

    @Test fun open2_theUsersResumeNotStartedYet_isSentAgainAsTheUsers() = runTest {
        // Resumed from History; the socket dropped before its session_started. Not in the pool yet,
        // so it is not "closed": its start goes again, still allowed to create (no reattach).
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(emptyList())
        r.channel.armNewSession("NEW"); r.channel.connect(url); r.socket.open(); r.settle()
        assertEquals(listOf<ClientFrame>(ClientFrame.Start("NEW", "NEW")), r.socket.sent)
        r.socket.drop(); r.settle(); r.socket.sent.clear()
        r.socket.open(); r.settle()
        advanceTimeBy(SessionTuning.POOL_PROBE_RETRY_MS); runCurrent()
        assertEquals(listOf<ClientFrame>(ClientFrame.Start("NEW", "NEW")), r.socket.sent)
        assertTrue(r.events.drain().none { it is ChannelEvent.OrchestratorClosed })
    }

    @Test fun open3_sessionClosedAnswerToAReattachClosesIt() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle()
        r.events.drain(); r.frames.drain()
        r.socket.frame(ServerFrame.Error("session_closed", "not open", null, null)); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.OrchestratorClosed("ORCH")), r.events.drain())
        assertEquals(listOf<ServerFrame>(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true)), r.frames.drain())
        assertTrue(r.channel.state.value.noOrchestrator)
    }

    @Test fun recoveryBacksOff0_500_1000_thenGivesUp() = runTest {
        val r = Rig(this, cursor = { ResumeCursor("st", 9) })
        r.pool.script += listOf(orch("A", "JA"))
        r.channel.connect(url); r.socket.open(); r.settle()
        r.events.drain(); r.pool.calls.clear()
        assertEquals(listOf(0L, 500L, 1_000L), SessionTuning.RECOVERY_BACKOFF_MS)

        val t0 = currentTime
        val sentBefore = r.socket.sent.size
        for ((i, wait) in SessionTuning.RECOVERY_BACKOFF_MS.withIndex()) {
            val errAt = currentTime
            r.socket.frame(orchestratorActive()); runCurrent()
            if (wait > 0) { advanceTimeBy(wait - 1); runCurrent(); assertEquals(i, r.pool.calls.size); advanceTimeBy(1); runCurrent() }
            assertEquals("attempt $i at +${wait}ms", errAt + wait, r.pool.calls.last())
            // Re-sends start with the adopted ids (socket open), resume cursor from memory.
            assertEquals(reattach("A", "JA", ResumeCursor("st", 9)), r.socket.sent.last())
            advanceTimeBy(10); runCurrent()
        }
        assertEquals(sentBefore + 3, r.socket.sent.size)
        assertTrue(r.events.drain().filterIsInstance<ChannelEvent.Adopted>().all { it.viaRecovery })
        r.socket.frame(orchestratorActive()); runCurrent()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.GaveUp), r.events.drain())
        assertTrue(r.channel.state.value.noOrchestrator)
        assertEquals(3, r.pool.calls.size)
        assertTrue(currentTime - t0 >= 1_500)
    }

    @Test fun recoveryIsSingleFlight_resetBySessionStarted_andReconnectsWhenTheSocketIsDown() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle()
        r.events.drain(); r.pool.calls.clear()

        r.socket.frame(orchestratorActive()); runCurrent()                 // attempt 0 (immediate)
        r.socket.frame(orchestratorActive()); runCurrent()                 // attempt 1 scheduled at +500
        r.socket.frame(orchestratorActive()); runCurrent()                 // in flight → ignored
        advanceTimeBy(500); runCurrent()
        assertEquals(2, r.pool.calls.size)

        assertEquals(0, r.socket.backoffResets)
        r.socket.frame(started()); runCurrent()
        assertEquals("session_started resets the T-13 backoff", 1, r.socket.backoffResets)
        assertEquals(ChannelEvent.Recovered(OrchestratorRef("ORCH", "JSONL")), r.events.drain().last())
        assertFalse(r.channel.state.value.recovering)

        // Counter was reset: the next error is attempt 0 again (immediate). Socket down → reconnect, no send.
        r.socket.drop(); r.settle()
        val sent = r.socket.sent.size
        r.socket.frame(orchestratorActive()); runCurrent()
        assertEquals(3, r.pool.calls.size)
        assertEquals(1, r.socket.reconnectNows)
        assertEquals(sent, r.socket.sent.size)
    }

    @Test fun userIntentTurnsOrchestratorActiveIntoAConflict() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.events.drain(); r.pool.calls.clear()
        r.channel.markUserIntent(); r.socket.frame(orchestratorActive()); runCurrent()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Conflict("another orchestrator is running")), r.events.drain())
        assertTrue("no recovery when the user is choosing", r.pool.calls.isEmpty())
    }

    @Test fun injectOutboxHoldsUntilSessionStartedThenFlushesInOrder() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.inject("[shared text] one"); r.settle()                  // not even connected
        r.channel.connect(url); r.socket.open(); r.settle()
        r.channel.inject("[shared text] two"); r.settle()                  // open, not subscribed
        assertEquals(2, r.channel.state.value.pendingInjects)
        assertTrue(r.socket.sent.none { it is ClientFrame.InjectText })

        r.socket.frame(started()); r.settle()
        assertEquals(
            listOf(ClientFrame.InjectText("[shared text] one"), ClientFrame.InjectText("[shared text] two")),
            r.socket.sent.filterIsInstance<ClientFrame.InjectText>(),
        )
        assertEquals(0, r.channel.state.value.pendingInjects)
        r.channel.inject("three"); r.settle()                              // subscribed → straight out
        assertEquals(ClientFrame.InjectText("three"), r.socket.sent.last())

        r.socket.drop(); r.settle()                                        // drop → unsubscribed → held again
        r.channel.inject("four"); r.settle()
        assertEquals(1, r.channel.state.value.pendingInjects)
    }

    @Test fun audioGoesOnlyToTheAudioPath_otherFramesInOrder() = runTest {
        val r = Rig(this)
        val audio = r.channel.audioFramesChannel()
        r.channel.connect(url); r.settle()
        r.socket.frame(ServerFrame.TextDelta("a"))
        r.socket.frame(ServerFrame.VoiceAudioOut("AAAA"))
        r.socket.frame(ServerFrame.VoiceEvent(kotlinx.serialization.json.buildJsonObject { put("type", kotlinx.serialization.json.JsonPrimitive("response.done")) }))
        r.socket.frame(ServerFrame.TextDelta("b"))
        r.settle()
        assertEquals(listOf("text_delta", "voice_event", "text_delta"), r.frames.drain().map { it.type })
        assertEquals(listOf("voice_audio_out"), audio.drain().map { it.type })
    }

    @Test fun watch1_closedElsewhereClearsTheRef_otherOrchestratorsAreIgnored() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle(); r.events.drain()
        r.socket.frame(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = false)); r.settle()   // FOCUS-2
        assertEquals("ORCH", r.channel.state.value.orchestrator?.localId)
        r.socket.frame(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true)); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.OrchestratorClosed("ORCH")), r.events.drain())
        assertEquals(null, r.channel.state.value.orchestrator)
        assertFalse(r.channel.state.value.subscribed)
        assertTrue("open socket, no conversation: connected, not connecting", r.channel.state.value.noOrchestrator)
        assertTrue(r.frames.drain().any { it is ServerFrame.AgentSessionClosed && it.isOrchestrator })
    }

    @Test fun followsAnOrchestratorOpenedElsewhere_afterNoOrchestratorOrClosedElsewhere() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(agent())
        r.channel.connect(url); r.socket.open(); r.settle(); advanceTimeBy(400); runCurrent()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.NoOrchestrator), r.events.drain())

        r.socket.frame(ServerFrame.AgentSessionOpened("AGENT", "S1", isOrchestrator = false)); r.settle()
        assertTrue("an agent session is not Archie", r.events.drain().isEmpty())

        r.socket.frame(ServerFrame.AgentSessionOpened("OTHER", "JSONL2", isOrchestrator = true)); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Adopted(OrchestratorRef("OTHER", "JSONL2"), reconnect = false)), r.events.drain())
        assertEquals("OTHER", r.channel.state.value.orchestrator?.localId)
        assertFalse(r.channel.state.value.noOrchestrator)
        assertEquals("OTHER", r.ids.value)
        assertEquals(listOf<ClientFrame>(reattach("OTHER", "JSONL2")), r.socket.sent)
        assertTrue(r.frames.drain().any { it is ServerFrame.AgentSessionOpened })

        r.socket.frame(started("OTHER", "JSONL2")); r.settle(); r.events.drain(); r.socket.sent.clear()
        r.socket.frame(ServerFrame.AgentSessionOpened("OTHER", "JSONL2", isOrchestrator = true)); r.settle()
        assertTrue("the one already followed is not adopted again", r.events.drain().isEmpty() && r.socket.sent.isEmpty())

        r.socket.frame(ServerFrame.AgentSessionClosed("OTHER", isOrchestrator = true)); r.settle()   // "New conversation" elsewhere
        r.socket.frame(ServerFrame.AgentSessionOpened("THIRD", "THIRD", isOrchestrator = true)); r.settle()
        assertEquals(
            listOf(ChannelEvent.OrchestratorClosed("OTHER"), ChannelEvent.Adopted(OrchestratorRef("THIRD", "THIRD"), reconnect = false)),
            r.events.drain(),
        )
        assertEquals(listOf<ClientFrame>(reattach("THIRD", "THIRD")), r.socket.sent)
    }

    @Test fun ownStartIsNotFollowed_userIntentOrArmedNew() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(agent())
        r.channel.connect(url); r.socket.open(); r.settle(); advanceTimeBy(400); runCurrent(); r.events.drain()

        r.channel.markUserIntent(); r.settle()                    // resumeArchie: this device sends its own start
        r.socket.frame(ServerFrame.AgentSessionOpened("MINE", "JSONL", isOrchestrator = true)); r.settle()
        assertTrue(r.events.drain().isEmpty())
        assertTrue(r.socket.sent.isEmpty())

        r.socket.frame(started("MINE", "JSONL")); r.settle(); r.events.drain()
        r.socket.frame(ServerFrame.AgentSessionClosed("MINE", isOrchestrator = true)); r.settle(); r.events.drain()
        r.channel.armNewSession("NEW"); r.settle(); r.events.drain(); r.socket.sent.clear()
        r.socket.frame(ServerFrame.AgentSessionOpened("NEW", "NEW", isOrchestrator = true)); r.settle()
        assertTrue("the echo of our own new session", r.events.drain().isEmpty() && r.socket.sent.isEmpty())
    }

    @Test fun armNewSessionSkipsTheProbe() = runTest {
        val r = Rig(this, autoStart = true)
        r.channel.armNewSession("NEW"); r.channel.connect(url); r.socket.open(); r.settle()
        assertTrue(r.pool.calls.isEmpty())
        assertEquals(listOf<ChannelEvent>(ChannelEvent.NewSessionArmed(OrchestratorRef("NEW", "NEW"))), r.events.drain())
        assertEquals("the user's new conversation: no reattach (OPEN-2)", listOf<ClientFrame>(ClientFrame.Start("NEW", "NEW")), r.socket.sent)
        assertEquals("NEW", r.ids.value)
    }

    @Test fun foregroundResyncsWhenOpen_reconnectsWhenNot_backgroundGatesReconnects() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.events.drain(); r.socket.sent.clear()
        r.channel.onForeground(); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Resync), r.events.drain())
        assertEquals(listOf<ClientFrame>(reattach("ORCH", "JSONL")), r.socket.sent)   // T-9, OPEN-2

        r.channel.onBackground(keepAlive = false); r.settle()
        r.channel.onBackground(keepAlive = true); r.settle()                                  // owns voice
        assertEquals(listOf(true, false, true), r.socket.reconnectAllowed)
        r.socket.drop(); r.settle()
        r.channel.onForeground(); r.settle()
        assertEquals(1, r.socket.reconnectNows)
        r.channel.onNetworkAvailable()
        assertEquals(2, r.socket.reconnectNows)
        val network = kotlinx.coroutines.flow.MutableSharedFlow<Unit>(extraBufferCapacity = 4)
        r.channel.attachNetwork(network); runCurrent()
        network.tryEmit(Unit); network.tryEmit(Unit); runCurrent()
        assertEquals("T-14 network callback", 4, r.socket.reconnectNows)
    }

    // ───────────── OPEN-4 on foreground (the socket stayed open) ─────────────

    private fun Rig.subscribed() {
        pool.script += listOf(listOf(orch()))
        channel.connect(url); socket.open(); settle()
        socket.frame(started()); settle()
        events.drain(); frames.drain(); socket.sent.clear()
    }

    @Test fun open3_foregroundAfterALiveClose_sendsNoStart() = runTest {
        val r = Rig(this, autoStart = true)
        r.subscribed()
        r.socket.frame(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true)); r.settle()
        r.events.drain(); r.pool.script.clear(); r.pool.script += listOf(emptyList())
        r.channel.onForeground(); r.settle()
        assertTrue(r.events.drain().isEmpty())
        assertTrue(r.socket.sent.isEmpty())
    }

    @Test fun open4_foregroundReReadsThePool_closesOrFollows() = runTest {
        val r = Rig(this, autoStart = true)
        r.subscribed()
        r.pool.script.clear(); r.pool.script += listOf(listOf(agent()))          // the close never reached this socket
        r.channel.onForeground(); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.OrchestratorClosed("ORCH")), r.events.drain())
        assertEquals(listOf<ServerFrame>(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true)), r.frames.drain())
        assertTrue(r.socket.sent.isEmpty())

        r.pool.script.clear(); r.pool.script += listOf(listOf(orch("OTHER", "J2")))   // opened on another device
        r.channel.onForeground(); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Adopted(OrchestratorRef("OTHER", "J2"), reconnect = false)), r.events.drain())
        assertEquals(listOf<ClientFrame>(reattach("OTHER", "J2")), r.socket.sent)
    }

    @Test fun open4_foregroundWhenThePoolCannotBeRead_keepsT9() = runTest {
        val r = Rig(this, autoStart = true)
        r.subscribed()
        r.pool.script.clear(); r.pool.script += listOf(null)
        r.channel.onForeground(); r.settle()
        assertEquals(listOf<ChannelEvent>(ChannelEvent.Resync), r.events.drain())
        assertEquals(listOf<ClientFrame>(reattach("ORCH", "JSONL")), r.socket.sent)
    }

    /** Decision P-1: app close / background / disconnect / server change never send stop/close. */
    @Test fun p1_lifecyclePathsNeverStopOrCloseAnything() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()

        r.channel.onBackground(keepAlive = false); r.settle()
        r.socket.drop(); r.settle()
        r.channel.onForeground(); r.socket.open(); r.settle()
        r.channel.disconnect(); r.settle()
        r.channel.connect(url); r.socket.open(); r.settle()
        r.channel.changeServer("ws://10.0.0.9:80"); r.socket.open(); r.settle()
        advanceTimeBy(10_000); runCurrent()
        backgroundScope.coroutineContext[kotlinx.coroutines.Job]!!.children.forEach { it.cancel() }   // process teardown
        runCurrent()

        assertEquals(emptyList<ClientFrame>(), r.socket.stopLikeFrames())
        assertTrue(r.socket.sent.all { it is ClientFrame.Start })
        assertEquals(emptyList<String>(), r.pool.closes)
    }

    @Test fun p1_explicitCloseClosesForEveryone() = runTest {
        val r = Rig(this)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle()
        assertTrue(r.channel.closeOrchestrator())
        assertEquals(listOf("ORCH"), r.pool.closes)
        assertEquals(null, r.ids.value)
        assertEquals(SocketState.Open, r.socket.state.value)                 // the socket stays: watcher events (T-7)
        assertTrue(r.channel.state.value.noOrchestrator)                     // connected, nothing open
    }

    // ───────────── §6.11a orchestrator_switch (SW-1, SW-4) ─────────────

    private fun switchFrame(voice: Boolean = true, from: String? = "ORCH") =
        ServerFrame.OrchestratorSwitch("PAST", "Lamps", voice, from)

    /** The server's sequence: voice ended, the old orchestrator closed (OPEN-3), then the switch to ONE socket. */
    private fun Rig.serverSwitches(voice: Boolean = true) {
        socket.frame(ServerFrame.VoiceEnded("switch", "ORCH"))
        socket.frame(ServerFrame.AgentSessionClosed("ORCH", isOrchestrator = true))
        socket.frame(switchFrame(voice))
        settle()
    }

    @Test fun switchArmsTheResumeWithANewLocalId_andAutoStartSendsItsStart() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.events.drain(); r.socket.sent.clear()

        r.serverSwitches()
        val ref = OrchestratorRef("NEW1", "PAST")
        assertEquals(
            listOf(ChannelEvent.OrchestratorClosed("ORCH"), ChannelEvent.SwitchRequested(ref, "Lamps", voice = true, fromLocalId = "ORCH")),
            r.events.drain(),
        )
        assertEquals("§6.11: start{local_id: uuid, resume_sdk_id}", listOf<ClientFrame>(ClientFrame.Start("NEW1", "PAST")), r.socket.sent)
        val st = r.channel.state.value
        assertEquals(ref, st.orchestrator)
        assertFalse(st.noOrchestrator)
        assertFalse("not subscribed until its session_started", st.subscribed)
        assertEquals("NEW1", r.ids.value)
        assertTrue("the frame still reaches the frame subscribers", r.frames.drain().any { it is ServerFrame.OrchestratorSwitch })

        r.socket.frame(started("NEW1", "PAST")); r.settle()
        assertEquals(ref, r.channel.state.value.orchestrator)
        assertTrue(r.channel.state.value.subscribed)
        assertTrue(r.events.drain().isEmpty())
        assertEquals("P-1: no stop / voice_stop", emptyList<ClientFrame>(), r.socket.stopLikeFrames())
    }

    @Test fun sw1_aSwitchIsActedOnAtMostOnce() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.serverSwitches()
        r.events.drain(); r.socket.sent.clear()

        r.socket.frame(switchFrame()); r.settle()                              // a duplicate delivery
        assertTrue(r.events.drain().isEmpty())
        assertTrue(r.socket.sent.isEmpty())
        assertEquals(OrchestratorRef("NEW1", "PAST"), r.channel.state.value.orchestrator)

        // A later switch (another from id) is a new request.
        r.socket.frame(ServerFrame.OrchestratorSwitch("PAST", "Lamps", false, "NEW1")); r.settle()
        assertEquals(
            listOf<ChannelEvent>(ChannelEvent.SwitchRequested(OrchestratorRef("NEW2", "PAST"), "Lamps", false, "NEW1")),
            r.events.drain(),
        )
        assertEquals(listOf<ClientFrame>(ClientFrame.Start("NEW2", "PAST")), r.socket.sent)
    }

    @Test fun sw4_aSwitchIsUserIntent_noAutoAdoptionUndoesIt_andTheRepositoryOwnsTheStart() = runTest {
        val r = Rig(this)                                                      // main app: no autoStart
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.serverSwitches(voice = false)
        r.events.drain()
        assertTrue("the conversation repository sends the start", r.socket.sent.isEmpty())

        // Another device's orchestrator showing up before our session_started must not be adopted.
        r.socket.frame(ServerFrame.AgentSessionOpened("OTHER", "J-OTHER", isOrchestrator = true)); r.settle()
        assertEquals(OrchestratorRef("NEW1", "PAST"), r.channel.state.value.orchestrator)
        assertTrue(r.events.drain().isEmpty())
        // The echo of our own resumed session neither.
        r.socket.frame(ServerFrame.AgentSessionOpened("NEW1", "PAST", isOrchestrator = true)); r.settle()
        assertTrue(r.events.drain().isEmpty())
        assertTrue(r.socket.sent.isEmpty())
    }

    @Test fun aSwitchWithoutAnSdkIdIsIgnored() = runTest {
        val r = Rig(this, autoStart = true)
        r.pool.script += listOf(orch())
        r.channel.connect(url); r.socket.open(); r.settle(); r.socket.frame(started()); r.settle()
        r.events.drain(); r.socket.sent.clear()
        r.socket.frame(ServerFrame.OrchestratorSwitch(null, "x", true, "ORCH")); r.settle()
        assertTrue(r.events.drain().isEmpty())
        assertTrue(r.socket.sent.isEmpty())
        assertEquals(OrchestratorRef("ORCH", "JSONL"), r.channel.state.value.orchestrator)
    }
}
