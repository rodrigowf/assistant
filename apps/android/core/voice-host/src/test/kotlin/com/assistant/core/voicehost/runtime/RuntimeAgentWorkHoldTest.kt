package com.assistant.core.voicehost.runtime

import android.app.Activity
import com.assistant.core.voicehost.HostConfig
import com.assistant.core.voicehost.HostRig
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Main app: "Agent session finished" notifications keep the FGS (and the orchestrator socket) while
 * an agent turn runs, then let it go. The service still only starts from a foreground context.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class RuntimeAgentWorkHoldTest {
    private fun mainRig(ts: kotlinx.coroutines.test.TestScope) =
        HostRig(ts, config = HostConfig.main(Activity::class.java, Activity::class.java), autoStart = false)

    @Test
    fun holdStartsTheServiceInTheForegroundAndReleasesItAfterwards() = runTest {
        val r = mainRig(this)
        r.connectAndAdopt(HostRig.SETTINGS.copy(enableWakeWord = false))
        r.runtime.onUiStarted()
        r.settle()
        assertFalse("nothing needs the service yet", r.runtime.serviceWanted())
        val starts = r.service.starts

        r.runtime.setAgentWorkHold(1)
        assertTrue(r.runtime.serviceWanted())
        assertFalse("hold-only never takes the microphone FGS type", r.runtime.micTypeWanted())
        assertEquals(starts + 1, r.service.starts)
        r.settle()
        assertEquals(1, r.state.agentTurnsWaiting)

        r.runtime.setAgentWorkHold(1)                       // idempotent
        r.runtime.setAgentWorkHold(2)                       // more turns: no second start
        assertEquals(starts + 1, r.service.starts)
        r.settle()
        assertEquals(2, r.state.agentTurnsWaiting)

        val stops = r.service.stops
        r.runtime.setAgentWorkHold(0)
        assertFalse(r.runtime.serviceWanted())
        assertEquals(stops + 1, r.service.stops)
    }

    @Test
    fun holdNeverStartsTheServiceFromTheBackground() = runTest {
        val r = mainRig(this)
        r.connectAndAdopt(HostRig.SETTINGS.copy(enableWakeWord = false))
        val starts = r.service.starts
        r.runtime.setAgentWorkHold(1)                       // UI not started: Android 12+ would refuse
        assertEquals(starts, r.service.starts)
        assertTrue("still wanted: the socket stays allowed to reconnect", r.runtime.serviceWanted())
    }

    @Test
    fun releasingTheHoldKeepsAServiceOtherReasonsNeed() = runTest {
        val r = mainRig(this)
        r.connectAndAdopt(HostRig.SETTINGS.copy(enableWakeWord = false, stayConnectedInBackground = true))
        r.runtime.onUiStarted()
        r.settle()
        r.runtime.setAgentWorkHold(1)
        r.settle()
        assertTrue("another reason runs the service: the mic type stays allowed", r.runtime.micTypeWanted())
        assertEquals("not hold-only: the usual notification text", 0, r.state.agentTurnsWaiting)
        val stops = r.service.stops
        r.runtime.setAgentWorkHold(0)
        assertTrue(r.runtime.serviceWanted())
        assertEquals(stops, r.service.stops)
    }
}
