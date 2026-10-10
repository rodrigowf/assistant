package com.assistant.core.conversation

import com.assistant.core.model.ConnectionState
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.LiveStatus
import com.assistant.core.model.SessionStatus
import com.assistant.core.protocol.ClientFrame
import com.assistant.core.protocol.ModelInfoDto
import com.assistant.core.protocol.OrchestratorModelInfoDto
import com.assistant.core.protocol.ResumeCursor
import com.assistant.core.protocol.ResumeState
import com.assistant.core.protocol.ServerFrame
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Connection-manager rules of spec 12 §3.3–§3.6, §5.2, §5.6 (pure, inside the reducer step). */
class ConnectionManagerTest {
    private val stream = "L1:1759500000000"

    private fun started(rs: ResumeState? = ResumeState(stream, 1), overflow: Boolean = false, id: String = "L1") =
        ServerFrame.SessionStarted(id, contextWindow = 200_000, resumeState = rs, replayOverflow = overflow)

    @Test
    fun t10_resumeFromOnlyForInMemoryClaudeStreams() {
        val live = agent().send("go").on(processing(), ServerFrame.TextDelta("x", 14, stream))
        val start = live.effectsOf(ConversationInput.SocketOpened).filterIsInstance<ConversationEffect.SendStart>().single().frame
        assertEquals(ClientFrame.Start("L1", "sdk-1", ResumeCursor(stream, 14), reattach = true), start)   // OPEN-2
        // not when the timeline was not built from the same stream (history not loaded yet)
        val cold = live.copy(history = live.history.copy(loaded = false))
        assertNull(cold.input(ConversationInput.SocketOpened).startRequest!!.resumeFrom)
        // never for providers without replay
        val qwen = agent(provider = HarnessProvider.QWEN).copy(checkpoint = Checkpoint(stream, 3))
        assertNull(qwen.input(ConversationInput.SocketOpened).startRequest!!.resumeFrom)
        // a brand-new session has no sdk id to resume
        assertEquals(ClientFrame.Start("L1", reattach = true), agent(sdkId = null).input(ConversationInput.SocketOpened).startRequest)
    }

    @Test
    fun seq5_heldFramesAreDiscardedWhenTheReplayIsGranted() {
        val s = agent().send("go").on(processing(), ServerFrame.TextDelta("a", 10, stream))
            .input(ConversationInput.SocketClosed, ConversationInput.SocketOpened)
            .on(ServerFrame.TextDelta("b", 11, stream), ServerFrame.UserMessage("q", queued = true))   // held
        assertTrue(s.awaitingSessionStarted)
        assertEquals(2, s.preStart.size)
        val after = s.on(started(ResumeState(stream, 12)))
        assertShape("U(go) A[T(a)~]", after)                                  // "b" will come in the replay
        assertEquals(listOf(QueuedPrompt("q", QueueOwner.REMOTE)), after.queue.toList())   // unsequenced: applied
        assertShape("U(go) A[T(ab)~]", after.on(ServerFrame.TextDelta("b", 11, stream)))
        assertEquals(ConnectionState.SUBSCRIBED, after.connection)
        assertNull(after.connectionBanner)
    }

    @Test
    fun seq5_heldFramesAreAppliedInOrderWithoutAReplay() {
        val s = agent(provider = HarnessProvider.QWEN).send("go").on(processing())
            .input(ConversationInput.SocketOpened)
            .on(ServerFrame.TextDelta("a"), ServerFrame.TextDelta("b"))
        assertShape("U(go)", s)
        assertShape("U(go) A[T(ab)~]", s.on(started(rs = null)))
    }

    @Test
    fun checkpointFromResumeStateOfAFreshSubscriber() {
        val s = agent().input(ConversationInput.SocketOpened).on(started(ResumeState(stream, 17)))
        assertEquals(Checkpoint(stream, 16), s.checkpoint)
        // same stream: never moves backwards
        val t = agent().copy(checkpoint = Checkpoint(stream, 30)).copy(history = HistoryState(loaded = false))
            .input(ConversationInput.SocketOpened).on(started(ResumeState(stream, 17)))
        assertEquals(Checkpoint(stream, 30), t.checkpoint)
    }

    @Test
    fun seq6_overflowWithoutSdkIdKeepsEntriesAndFlagsAGap() {
        val s = agent(sdkId = null).send("go").on(processing(), ServerFrame.TextDelta("a", 3, stream))
            .input(ConversationInput.SocketOpened)
        val r = ConversationReducer.step(s, ConversationInput.Frame(started(ResumeState("L1:2", 1), overflow = true)))
        assertTrue(ConversationEffect.LookupSdkId in r.effects)
        assertFalse(r.state.reloading)
        assertTrue(r.state.gapPossible)
        assertShape("U(go) A[T(a)~]", r.state)
    }

    @Test
    fun canonicalReloadHoldsFramesUntilThePageArrives() {
        val s = agent().send("go").on(processing())
            .input(ConversationInput.SocketOpened)
        val r = ConversationReducer.step(s, ConversationInput.Frame(started(ResumeState("L1:2", 1), overflow = true)))
        assertTrue(ConversationEffect.CanonicalReload in r.effects)
        val held = r.state.on(ServerFrame.TextComplete("live tail"))
        assertEquals(1, held.reloadBuffer.size)
        val done = held.input(ConversationInput.HistoryPage(PageMode.REPLACE, page(0, 2, userLine("go"), assistantText("from rest"))))
        assertFalse(done.reloading)
        assertShape("U(go):history A[T(from rest) T(live tail)]", done)
    }

    @Test
    fun reloadFailureFlushesTheBufferAndOffersReload() {
        val s = agent().input(ConversationInput.BeginReload).on(ServerFrame.UserMessage("hello"))
        assertTrue(s.entries.isEmpty())
        val f = s.input(ConversationInput.ReloadFailed)
        assertShape("U(hello):echo", f)
        assertTrue(f.gapPossible)
    }

    @Test
    fun coldOpenAppliesHeldFramesAfterTheRestPage() {
        // §5.2: subscribe first, fetch, then apply the frames that arrived meanwhile (incl. session_started)
        var s = agent().copy(history = HistoryState()).input(ConversationInput.BeginReload, ConversationInput.SocketOpened)
        s = s.on(started(ResumeState(stream, 41)), ServerFrame.TextComplete("streamed", 41, stream))
        assertTrue(s.entries.isEmpty())
        s = s.input(ConversationInput.HistoryPage(PageMode.REPLACE, page(0, 2, userLine("q"), assistantText("old"))))
        assertShape("U(q):history A[T(old) T(streamed)]", s)
        assertEquals(Checkpoint(stream, 41), s.checkpoint)
        assertTrue(s.history.loaded)
    }

    @Test
    fun id1_adoptsTheServerLocalIdWithoutLosingState() {
        val s = agent().send("go").input(ConversationInput.SocketOpened).on(started(id = "L-other"))
        assertEquals("L-other", s.ref.localId)
        assertShape("U(go)", s)
    }

    @Test
    fun id2_sdkIdLearning() {
        val fresh = agent(sdkId = null).send("go").on(processing())
        assertEquals("sdk-9", fresh.on(turnComplete("sdk-9")).ref.sdkId)
        // Gemini sends no session_id: ask pool/live
        assertTrue(ConversationEffect.LookupSdkId in fresh.effectsOf(turnComplete(null)))
        assertEquals("sdk-7", fresh.input(ConversationInput.SdkIdLearned("sdk-7")).ref.sdkId)
        assertEquals("sdk-1", agent().input(ConversationInput.SdkIdLearned("other")).ref.sdkId)
    }

    @Test
    fun orchestratorSessionStartedFields() {
        val info = OrchestratorModelInfoDto("gpt-audio-mini", "openai", 4096, true, ModelInfoDto(modelId = "gpt-audio-mini", contextWindow = 128_000))
        val s = orchestrator().copy(ref = orchestrator().ref.copy(sdkId = null)).input(ConversationInput.SocketOpened)
            .on(ServerFrame.SessionStarted("O1", jsonlId = "J1", voice = true, modelInfo = info))
        assertEquals("J1", s.ref.sdkId)
        assertEquals(128_000L, s.counters.contextWindow)
        assertTrue(s.voiceActive)
        val noWindow = orchestrator().input(ConversationInput.SocketOpened).on(ServerFrame.SessionStarted("O1", voice = false))
        assertEquals(200_000L, noWindow.counters.contextWindow)
        assertEquals(64_000L, noWindow.on(ServerFrame.ModelChanged(info.copy(modelInfo = info.modelInfo!!.copy(contextWindow = 64_000)))).counters.contextWindow)
    }

    @Test
    fun t12_startErrorsAreBannersAndNeverHeld() {
        val s = agent().input(ConversationInput.SocketOpened)
        val r = ConversationReducer.step(s, ConversationInput.Frame(ServerFrame.Error("start_timeout", "30 s")))
        assertEquals(ConnectionState.FAILED, r.state.connection)
        assertEquals(ConnectionBanner("start_timeout", "30 s"), r.state.connectionBanner)
        assertFalse(r.state.awaitingSessionStarted)
        assertEquals(listOf(ConversationEffect.StartError("start_timeout", "30 s")), r.effects)
        // orchestrator conflict → dialog (§6.11); still no entry
        val o = orchestrator().input(ConversationInput.SocketOpened).effectsOf(ServerFrame.Error("orchestrator_active"))
        assertEquals(listOf(ConversationEffect.StartError("orchestrator_active", null)), o)
        // not_started → re-send start
        assertTrue(agent().effectsOf(ServerFrame.Error("not_started")).single() is ConversationEffect.SendStart)
    }

    @Test
    fun seq8_startErrorReleasesHeldFramesInOrder() {
        val waiting = agent().input(ConversationInput.SocketOpened)
            .on(ServerFrame.UserMessage("one"), ServerFrame.Error("invalid_json"), ServerFrame.UserMessage("two"))
        // a protocol error is applied at once (SEQ-5 exception) but keeps the wait
        assertTrue(waiting.awaitingSessionStarted)
        assertEquals(2, waiting.preStart.size)
        assertTrue(waiting.entries.isEmpty())
        val failed = waiting.on(ServerFrame.Error("start_failed", "not authenticated"))
        assertFalse(failed.awaitingSessionStarted)
        assertTrue(failed.preStart.isEmpty())
        assertShape("U(one):echo U(two):echo", failed)                          // released, never dropped (L-2)
        assertEquals(ConnectionBanner("start_failed", "not authenticated"), failed.connectionBanner)
        assertEquals(ConnectionState.FAILED, failed.connection)
    }

    @Test
    fun seq8_orchestratorStoppingIsRetriedOnceThenFails() {
        val s = orchestrator().input(ConversationInput.SocketOpened).on(ServerFrame.UserMessage("held"))
        val first = ConversationReducer.step(s, ConversationInput.Frame(ServerFrame.Error("orchestrator_stopping")))
        assertEquals(listOf(ConversationEffect.ScheduleStartRetry(1_000)), first.effects)
        assertTrue("still waiting", first.state.awaitingSessionStarted)
        assertNull(first.state.connectionBanner)
        // the retry: Resync re-sends start; a second orchestrator_stopping fails and releases the held frame
        val retried = first.state.input(ConversationInput.Resync)
        val second = ConversationReducer.step(retried, ConversationInput.Frame(ServerFrame.Error("orchestrator_stopping")))
        assertEquals(listOf(ConversationEffect.StartError("orchestrator_stopping", null)), second.effects)
        assertEquals(ConnectionState.FAILED, second.state.connection)
        // a successful start resets the one-retry budget
        assertFalse(first.state.on(ServerFrame.SessionStarted("O1")).stoppingRetried)
    }

    @Test
    fun watch1_ownAgentSessionClosedStopsTheView() {
        val busy = orchestrator().send("x").on(ServerFrame.Status("streaming"), toolUse("c1"))
        assertEquals(busy, busy.on(ServerFrame.AgentSessionClosed("L9", false)))   // another session
        assertEquals(busy, busy.on(ServerFrame.AgentSessionClosed("O1", false)))   // FOCUS-2: wrong flag
        val closed = busy.on(ServerFrame.AgentSessionClosed("O1", true))
        assertEquals(SessionStatus.STOPPED, closed.status)
        assertShape("U(x) A[X(c1:no_result)]", closed)                         // the entries stay; the repository then closes the view (OPEN-3)
        // an agent view that receives its own close (agent flag) stops too; terminated is kept
        val agentClosed = agent().send("y").on(processing(), ServerFrame.AgentSessionClosed("L1", false))
        assertEquals(SessionStatus.STOPPED, agentClosed.status)
        val dead = agent().on(ServerFrame.SessionTerminated("subprocess_lost"), ServerFrame.AgentSessionClosed("L1", false))
        assertEquals(SessionStatus.TERMINATED, dead.status)
    }

    @Test
    fun pm5_agentApprovalsComeFromNestedEventsOnly() {
        fun nested(local: String, type: String, rid: String) = ServerFrame.NestedSessionEvent(
            local, type, obj("""{"type":"$type","request_id":"$rid","tool_name":"ExitPlanMode","tool_input":{"plan":"p"},"seq":3,"stream_id":"$local:1"}"""),
        )
        val s = orchestrator().on(
            nested("A1", "permission_request", "r1"),
            nested("A1", "permission_request", "r1"),                            // once per (localId, request_id)
            nested("A2", "permission_request", "r1"),                            // same rid, other agent: distinct
            nested("A3", "permission_request", "r9"),
            nested("A2", "permission_resolved", "r1"),
            nested("A2", "permission_resolved", "zz"),                           // unknown: ignored
        )
        assertEquals(listOf("A1" to "r1", "A3" to "r9"), s.agentApprovals.map { it.localId to it.requestId })
        assertEquals(obj("""{"plan":"p"}"""), s.agentApprovals[0].toolInput)
        assertTrue("never timeline content", s.entries.isEmpty())
        assertEquals(listOf("A3"), s.input(ConversationInput.ClearAgentApprovals("A1")).agentApprovals.map { it.localId })
        // agent conversations do not keep an approvals list
        assertTrue(agent().on(nested("A1", "permission_request", "r1")).agentApprovals.isEmpty())
    }

    @Test
    fun seq7_gapPossibleOnlyWithoutReplay() {
        fun closedInTurn(s: ConversationState) = s.input(ConversationInput.SocketClosed).gapPossible
        assertFalse(closedInTurn(agent().send("go").on(processing())))
        assertTrue(closedInTurn(agent(provider = HarnessProvider.GEMINI).send("go").on(processing())))
        assertTrue(closedInTurn(orchestrator().send("go").on(ServerFrame.Status("streaming"))))
        assertFalse(closedInTurn(orchestrator()))
    }

    @Test
    fun st2_poolStatusIsAuthoritativeForAgents() {
        val busy = agent().input(ConversationInput.PoolStatus(LiveStatus.TOOL_USE))
        assertTrue(busy.inTurn)
        assertEquals(SessionStatus.TOOL_USE, busy.status)
        val idle = busy.on(toolUse("t")).input(ConversationInput.PoolStatus(LiveStatus.IDLE))
        assertFalse(idle.inTurn)
        assertShape("A[X(t:no_result)]", idle)
        // interrupted (Codex/Gemini/Qwen keep it after a stop) and disconnected: no turn runs either
        for (settled in listOf(LiveStatus.INTERRUPTED, LiveStatus.DISCONNECTED)) {
            val ended = busy.on(toolUse("t")).input(ConversationInput.PoolStatus(settled))
            assertFalse(ended.inTurn)
            assertShape("A[X(t:no_result)]", ended)
        }
        // the orchestrator row always says idle (G-15): ignored
        val o = orchestrator().send("x").on(ServerFrame.Status("streaming"))
        assertEquals(o, o.input(ConversationInput.PoolStatus(LiveStatus.IDLE)))
        // a cold open of a busy agent session starts in its turn
        val initial = ConversationState.initial(agent().ref.copy(live = true, liveStatus = LiveStatus.THINKING))
        assertTrue(initial.inTurn)
        assertEquals(SessionStatus.THINKING, initial.status)
    }

    @Test
    fun sessionStartedRestoresIdleAndClearsTermination() {
        val dead = agent().send("go").on(processing(), ServerFrame.SessionTerminated("subprocess_crashed", "x", "sdk-1"), ServerFrame.SessionStopped())
        assertEquals(SessionStatus.TERMINATED, dead.status)
        val back = dead.input(ConversationInput.SocketOpened).on(started())
        assertEquals(SessionStatus.IDLE, back.status)
        assertNull(back.termination)
        // status{connecting} held before session_started does not leave the view "connecting"
        val c = agent().input(ConversationInput.SocketOpened).on(ServerFrame.Status("connecting"), started())
        assertEquals(SessionStatus.IDLE, c.status)
    }

    @Test
    fun resyncOnlyWhileTheSocketIsOpen() {
        assertTrue(agent().effectsOf(ConversationInput.Resync).single() is ConversationEffect.SendStart)
        assertTrue(agent().input(ConversationInput.SocketClosed).effectsOf(ConversationInput.Resync).isEmpty())
    }

    @Test
    fun ownStopAckIsSwallowed() {
        val s = agent().send("go").on(processing()).input(ConversationInput.LocalStop).on(ServerFrame.SessionStopped())
        assertEquals(SessionStatus.PROCESSING, s.status)                       // our own stop: not "stopped"
        assertFalse(s.expectStopAck)
        assertEquals(SessionStatus.STOPPED, s.on(ServerFrame.SessionStopped()).status)
    }

    @Test
    fun audioFramesBypassTheReducer() {
        val s = orchestrator(voiceActive = true)
        assertEquals(s, s.on(ServerFrame.VoiceAudioOut("AAAA")))
        // and are never held or buffered
        val r = s.input(ConversationInput.BeginReload).on(ServerFrame.VoiceAudioOut("AAAA"))
        assertTrue(r.reloadBuffer.isEmpty())
    }
}
