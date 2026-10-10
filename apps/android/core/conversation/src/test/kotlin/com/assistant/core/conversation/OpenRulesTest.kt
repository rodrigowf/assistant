package com.assistant.core.conversation

import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.SessionStatus
import com.assistant.core.protocol.ServerFrame
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Spec 12 OPEN-2 (reattach, never re-create) and OPEN-3 (closed = the view closes) in the reducer. */
class OpenRulesTest {
    private fun ConversationState.start() =
        ConversationReducer.step(this, ConversationInput.SocketOpened).effects
            .filterIsInstance<ConversationEffect.SendStart>().single().frame

    private fun ConversationState.closes(vararg frames: ServerFrame) =
        frames.fold(this to emptyList<ConversationEffect>()) { (s, fx), f ->
            ConversationReducer.step(s, ConversationInput.Frame(f)).let { it.state to fx + it.effects }
        }.second.filterIsInstance<ConversationEffect.Closed>()

    @Test
    fun automaticStartsReattach_onlyTheUsersOwnFirstStartMayCreate() {
        assertEquals(true, agent().start().reattach)
        val fromHistory = ConversationState.initial(SessionRef("H1", "sdk-h", SessionKind.AGENT, null), userStart = true)
        assertNull("History / new / fork: the user's start creates or resumes", fromHistory.start().reattach)
        // once subscribed, every later start (reconnect, foreground, not_started) reattaches
        val subscribed = fromHistory.input(ConversationInput.SocketOpened).on(ServerFrame.SessionStarted("H1"))
        assertFalse(subscribed.userStart)
        assertEquals(true, subscribed.input(ConversationInput.SocketClosed).start().reattach)
        val notStarted = ConversationReducer.step(subscribed, ConversationInput.Frame(ServerFrame.Error("not_started")))
        assertEquals(true, notStarted.effects.filterIsInstance<ConversationEffect.SendStart>().single().frame.reattach)
    }

    @Test
    fun theSessionLeavingThePoolClosesTheView() {
        assertEquals(1, agent().closes(ServerFrame.SessionStopped()).size)                       // closed elsewhere
        assertEquals(1, agent().closes(ServerFrame.Error("session_closed")).size)              // the reattach found it gone
        assertEquals(1, orchestrator().closes(ServerFrame.AgentSessionClosed("O1", isOrchestrator = true)).size)
        assertTrue("FOCUS-2: wrong flag", orchestrator().closes(ServerFrame.AgentSessionClosed("O1", isOrchestrator = false)).isEmpty())
        assertTrue("another session", agent().closes(ServerFrame.AgentSessionClosed("OTHER", isOrchestrator = false)).isEmpty())
        assertTrue("our own stop ack", agent().input(ConversationInput.LocalStop).closes(ServerFrame.SessionStopped()).isEmpty())
        val crashed = agent().closes(ServerFrame.SessionTerminated("subprocess_crashed", "claude exited with code 1"), ServerFrame.SessionStopped())
        assertEquals(listOf(ConversationEffect.Closed(Termination("subprocess_crashed", "claude exited with code 1", null))), crashed)
    }

    @Test
    fun sessionClosedReleasesTheHeldFramesAndStopsTheTurn() {
        val pending = agent().send("go").on(processing()).input(ConversationInput.SocketClosed, ConversationInput.SocketOpened)
            .on(ServerFrame.Status("streaming"))
        assertTrue(pending.awaitingSessionStarted)
        val closed = pending.on(ServerFrame.Error("session_closed"))
        assertFalse(closed.awaitingSessionStarted)
        assertFalse(closed.inTurn)
        assertEquals(SessionStatus.STOPPED, closed.status)
    }
}
