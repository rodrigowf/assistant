package com.assistant.archie.system

import android.app.Application
import android.app.NotificationManager
import androidx.test.core.app.ApplicationProvider
import com.assistant.archie.shell.TestArchieApp
import com.assistant.core.model.LiveStatus
import com.assistant.core.model.PoolSession
import com.assistant.core.protocol.ServerFrame
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/** "Agent session finished" (spec 12 §3.7): the decision, the copy, the in-flight tracker, post / clear. */
class TurnNotifierTest {
    private fun finished(
        id: String = "A1",
        status: String = "ok",
        title: String? = "Energy dashboard",
        preview: String? = "All 12 tests pass.",
        error: String? = null,
    ) = ServerFrame.AgentTurnFinished(id, "S1", "claude", title, status, preview, error)

    @Test fun postsAFinishedTurnWithTitleAndPreview() {
        val d = TurnAttention.decide(finished(), enabled = true, lookingAt = null, knownTitle = null)
        assertEquals(
            TurnAttention.Decision.Post(TurnNotice("A1", "S1", "Energy dashboard", "All 12 tests pass.", error = false)),
            d,
        )
        assertEquals("turn:A1", (d as TurnAttention.Decision.Post).notice.tag)
    }

    @Test fun suppressesWhenOffStoppedOrLookingAtIt() {
        fun reason(d: TurnAttention.Decision) = (d as TurnAttention.Decision.Suppress).reason
        assertEquals(TurnAttention.Skip.DISABLED, reason(TurnAttention.decide(finished(), false, null, null)))
        assertEquals(TurnAttention.Skip.INTERRUPTED, reason(TurnAttention.decide(finished(status = "interrupted"), true, null, null)))
        assertEquals(TurnAttention.Skip.LOOKING, reason(TurnAttention.decide(finished(), true, "A1", null)))
        assertEquals(TurnAttention.Skip.NO_SESSION, reason(TurnAttention.decide(finished(id = ""), true, null, null)))
        assertTrue("another session in view", TurnAttention.decide(finished(), true, "B2", null) is TurnAttention.Decision.Post)
    }

    @Test fun copyForFailuresAndFallbacks() {
        assertEquals("Failed: Credit balance is too low", TurnAttention.text(finished(status = "error", error = "Credit balance is too low")))
        assertEquals("Failed: the turn ended with an error", TurnAttention.text(finished(status = "error", preview = null)))
        assertEquals("Finished", TurnAttention.text(finished(preview = " ")))
        assertEquals("From the list", TurnAttention.title(finished(title = null), "From the list"))
        assertEquals(TurnAttention.FALLBACK_TITLE, TurnAttention.title(finished(title = " "), null))
        val d = TurnAttention.decide(finished(status = "error", error = "boom"), true, null, null) as TurnAttention.Decision.Post
        assertTrue(d.notice.error)
    }

    @Test fun notifierPostsOncePerSessionAndClearsWhenOpened() {
        val posted = mutableListOf<TurnNotice>()
        val cancelled = mutableListOf<String>()
        val logs = mutableListOf<String>()
        val sink = object : TurnSink {
            override fun post(notice: TurnNotice) { posted += notice }
            override fun cancel(tag: String) { cancelled += tag }
        }
        val n = TurnNotifier(sink, { _, _ -> "Known title" }, logs::add)
        n.onFinished(finished(title = null), enabled = true, lookingAt = null)
        n.onFinished(finished(status = "interrupted"), enabled = true, lookingAt = null)
        assertEquals(listOf("Known title"), posted.map { it.title })
        assertEquals("posted A1 status=ok title=\"Known title\"", logs[0])
        assertEquals("suppressed A1 status=interrupted reason=interrupted", logs[1])
        n.onLooking("B2")
        n.onLooking("A1")
        n.onLooking("A1")
        assertEquals(listOf("turn:A1"), cancelled)
    }

    @Test fun agentWorkTracksTurnsFromFrames() {
        val w = AgentWork { 0L }
        assertEquals(0, w.busy.value)
        w.onFrame(ServerFrame.AgentTurnStarted("A1", "S1", "claude"))
        w.onFrame(ServerFrame.AgentTurnStarted("B2", "S2", "codex"))
        assertEquals(2, w.busy.value)
        w.onFrame(ServerFrame.AgentTurnFinished("A1", status = "ok"))
        assertEquals(1, w.busy.value)
        w.onFrame(ServerFrame.AgentSessionClosed("B2", isOrchestrator = false))
        assertEquals(0, w.busy.value)
        w.onFrame(ServerFrame.AgentTurnStarted("C3"))
        w.onFrame(ServerFrame.AgentSessionClosed("C3", isOrchestrator = true))  // not an agent: ignored
        assertEquals(1, w.busy.value)
    }

    @Test fun agentWorkReconcilesWithThePool() {
        var now = 0L
        val w = AgentWork { now }
        fun row(id: String, s: LiveStatus?) = PoolSession(id, "sdk-$id", s, 0.0, 0, null, isOrchestrator = false)
        w.onFrame(ServerFrame.AgentTurnStarted("A1"))
        w.onFrame(ServerFrame.AgentTurnStarted("GONE"))
        w.reconcile(listOf(row("A1", LiveStatus.IDLE), row("MISSED", LiveStatus.TOOL_USE)), requestedAt = now)
        assertEquals("a fresh start survives an idle row; a session out of the pool goes; a missed start is added", setOf("A1", "MISSED"), w.inFlight())
        now += AgentWork.RECENT_START_MS + 1
        w.reconcile(listOf(row("A1", LiveStatus.IDLE), row("MISSED", LiveStatus.STREAMING)), requestedAt = now)
        assertEquals(setOf("MISSED"), w.inFlight())
        now += AgentWork.MAX_AGE_MS + 1
        w.reconcile(listOf(row("MISSED", LiveStatus.STREAMING)), requestedAt = now)
        assertEquals("a missed finish expires, then the busy row counts as a new start", setOf("MISSED"), w.inFlight())
        w.reconcile(emptyList())
        assertEquals(0, w.busy.value)
    }

    @Test fun aStalePoolReadDoesNotResurrectAFinishedTurn() {
        var now = 1_000L
        val w = AgentWork { now }
        val busyRow = PoolSession("A1", "S1", LiveStatus.STREAMING, 0.0, 0, null, isOrchestrator = false)
        w.onFrame(ServerFrame.AgentTurnStarted("A1"))
        val requestedAt = now                      // the sync request goes out…
        now += 500
        w.onFrame(ServerFrame.AgentTurnFinished("A1", status = "ok"))   // …the finish arrives…
        now += 500
        w.reconcile(listOf(busyRow), requestedAt)  // …then the stale "busy" answer
        assertEquals(0, w.busy.value)
        now += 1_000
        w.reconcile(listOf(busyRow), requestedAt = now)   // a read sent after the finish: a new turn
        assertEquals(1, w.busy.value)
    }

    @Test fun expireAgesOutMissedFinishesWithoutThePool() {
        var now = 0L
        val w = AgentWork { now }
        w.onFrame(ServerFrame.AgentTurnStarted("A1"))
        w.expire()
        assertEquals(1, w.busy.value)
        now += AgentWork.MAX_AGE_MS + 1
        w.expire()
        assertEquals(0, w.busy.value)
    }
}

/** The real notification: channel, content, the tap intent (the same extras MainActivity reads for approvals). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], application = TestArchieApp::class)
class TurnNotificationRobolectricTest {
    private val app: Application get() = ApplicationProvider.getApplicationContext()
    private val nm get() = app.getSystemService(NotificationManager::class.java)

    @Test fun postsOnTheAgentSessionsChannel_replacesPerSession_andOpensTheSession() {
        val sink = SystemTurnSink(app)
        sink.post(TurnNotice("A1", "S1", "Energy dashboard", "Done", error = false))
        sink.post(TurnNotice("A1", "S1", "Energy dashboard", "Failed: boom", error = true))
        val ch = nm.getNotificationChannel(SystemTurnSink.CHANNEL_ID)
        assertEquals(NotificationManager.IMPORTANCE_HIGH, ch.importance)
        val sbn = shadowOf(nm).activeNotifications.single()
        assertEquals("turn:A1", sbn.tag)
        val n = sbn.notification
        assertEquals("Energy dashboard", n.extras.getString(android.app.Notification.EXTRA_TITLE))
        assertEquals("Failed: boom", n.extras.getCharSequence(android.app.Notification.EXTRA_TEXT).toString())
        assertEquals(android.app.Notification.CATEGORY_ERROR, n.category)
        val open = shadowOf(n.contentIntent).savedIntent
        assertEquals("A1", open.getStringExtra(SystemApprovalSink.EXTRA_OPEN_AGENT))
        assertEquals("S1", open.getStringExtra(SystemTurnSink.EXTRA_OPEN_AGENT_SDK))
        sink.cancel("turn:A1")
        assertTrue(shadowOf(nm).activeNotifications.isEmpty())
    }
}
