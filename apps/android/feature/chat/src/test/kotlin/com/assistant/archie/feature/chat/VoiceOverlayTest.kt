package com.assistant.archie.feature.chat

import android.app.Application
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.IntSize
import com.assistant.archie.feature.chat.support.FakeChatBackend
import com.assistant.archie.feature.chat.support.FakeChatVoice
import com.assistant.archie.feature.chat.support.Frames
import com.assistant.archie.feature.chat.ui.DefaultToolCardRenderer
import com.assistant.archie.feature.chat.ui.OverlayAnchor
import com.assistant.archie.feature.chat.ui.VoiceOverlay
import com.assistant.archie.feature.chat.ui.VoiceOverlayActivity
import com.assistant.archie.feature.chat.ui.VoiceOverlayGeometry
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** The floating voice controls' pure parts: placement, snap, activity, which states float. */
class VoiceOverlayGeometryTest {
    private val region = Rect(400f, 0f, 1400f, 800f)

    @Test
    fun snapsByThirdsAcrossAndHalvesDown() {
        assertEquals(OverlayAnchor.TopLeft, VoiceOverlayGeometry.snap(Offset(450f, 100f), region))
        assertEquals(OverlayAnchor.BottomCenter, VoiceOverlayGeometry.snap(Offset(900f, 700f), region))
        assertEquals(OverlayAnchor.BottomRight, VoiceOverlayGeometry.snap(Offset(1350f, 500f), region))
        assertEquals(OverlayAnchor.TopCenter, VoiceOverlayGeometry.snap(Offset(900f, 10f), region))
    }

    @Test
    fun placesInTheGuttersAndAboveTheComposer() {
        val size = IntSize(500, 80)
        assertEquals(Offset(650f, 700f), VoiceOverlayGeometry.topLeft(OverlayAnchor.BottomCenter, region, size, 28f, 72f, 20f))
        assertEquals(Offset(428f, 72f), VoiceOverlayGeometry.topLeft(OverlayAnchor.TopLeft, region, size, 28f, 72f, 20f))
        assertEquals(Offset(872f, 700f), VoiceOverlayGeometry.topLeft(OverlayAnchor.BottomRight, region, size, 28f, 72f, 20f))
        assertEquals(20f, VoiceOverlayGeometry.bottomPadding(region, 20f, null, 8f))
        assertEquals(118f, VoiceOverlayGeometry.bottomPadding(region, 20f, 690f, 8f))
    }

    @Test
    fun anchorKeysMatchTheWebPref() {
        assertEquals(OverlayAnchor.TopRight, OverlayAnchor.parse("top-right"))
        assertEquals(OverlayAnchor.BottomCenter, OverlayAnchor.parse(null))
        assertEquals(OverlayAnchor.BottomCenter, OverlayAnchor.parse("nonsense"))
    }

    @Test
    fun pressesOnThePillAreItsOwnAndPressesElsewhereWake() {
        val a = VoiceOverlayActivity()
        a.bounds = Rect(0f, 0f, 100f, 50f)
        a.goIdle()
        a.onPointer(Offset(10f, 10f), press = true)
        assertTrue("a press on the pill does not wake through the observer", a.idle)
        a.onPointer(Offset(500f, 500f), press = true)
        assertFalse(a.idle)
    }

    @Test
    fun onlyThisDevicesCallFloats() {
        assertTrue(VoiceOverlayModel.floats(VoiceUi.Active(SessionPhase.ACTIVE, false, false)))
        assertTrue(VoiceOverlayModel.floats(VoiceUi.Connecting("Connecting…")))
        assertTrue(VoiceOverlayModel.floats(VoiceUi.ReconnectFailed(null)))
        assertFalse(VoiceOverlayModel.floats(VoiceUi.Off))
        assertFalse(VoiceOverlayModel.floats(VoiceUi.Elsewhere("Pixel 8", true)))
    }
}

/** The overlay's state comes from the voice host with the same rules (and timeline) as the dock. */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceOverlayModelTest {
    @Test
    fun followsTheHostAndActsLikeTheDock() = runTest {
        val voice = FakeChatVoice(VoiceSessionState(phase = SessionPhase.ACTIVE, isOwner = true))
        val m = VoiceOverlayModel(VoiceDockModel(voice, backgroundScope) { testScheduler.currentTime }, backgroundScope)
        runCurrent()
        assertEquals(VoiceUi.Active(SessionPhase.ACTIVE, false, false), m.ui.value)
        m.onAction(ChatAction.ToggleMic)
        runCurrent()
        assertEquals(VoiceUi.Active(SessionPhase.ACTIVE, true, false), m.ui.value)

        voice.state.value = voice.state.value.copy(reconnectBanner = "Reconnecting…")
        runCurrent()
        assertTrue(m.ui.value is VoiceUi.Reconnecting)
        advanceTimeBy(4_000)
        voice.state.value = voice.state.value.copy(reconnectBanner = null)
        runCurrent()
        assertEquals(VoiceUi.Reconnected(4), m.ui.value)

        m.onAction(ChatAction.EndVoice)
        assertEquals(1, voice.stops)
    }

    @Test
    fun oneTimelineForTheDockAndTheFloatingControls() = runTest {
        val voice = FakeChatVoice(VoiceSessionState(phase = SessionPhase.ACTIVE, isOwner = true))
        val dock = VoiceDockModel(voice, backgroundScope) { testScheduler.currentTime }
        val vm = ConversationViewModel(
            FakeChatBackend(Frames.archie()), voice, DefaultToolCardRenderer::describe,
            flattenDispatcher = StandardTestDispatcher(testScheduler), externalScope = backgroundScope,
            clock = { testScheduler.currentTime }, voiceDock = dock,
        )
        val overlay = VoiceOverlayModel(dock, backgroundScope)
        runCurrent()
        voice.state.value = voice.state.value.copy(reconnectBanner = "Reconnecting…")
        runCurrent()
        voice.state.value = VoiceSessionState(phase = SessionPhase.ERROR, errorMessage = "5 tries over 60 s")
        runCurrent()
        assertEquals(VoiceUi.ReconnectFailed("5 tries over 60 s"), vm.state.value.voice)
        assertEquals(VoiceUi.ReconnectFailed("5 tries over 60 s"), overlay.ui.value)

        // ending from the floating controls clears the card in the conversation too (and vice versa)
        overlay.onAction(ChatAction.EndVoice)
        runCurrent()
        assertEquals(VoiceUi.Off, overlay.ui.value)
        assertEquals(VoiceUi.Off, vm.state.value.voice)
    }
}

/** The floating controls on screen: the dock's controls, the idle pill after 4 s, wake on activity. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class VoiceOverlayUiTest {
    @get:Rule val compose = createComposeRule()

    /** A state written outside composition: pump the looper (apply notifications), then run frames. */
    private fun settle() {
        compose.waitForIdle()
        compose.mainClock.advanceTimeBy(100)
    }

    @Test
    fun dockControlsThenThePillThenWake() {
        val activity = VoiceOverlayActivity()
        val actions = mutableListOf<ChatAction>()
        var opened = 0
        var voice by mutableStateOf<VoiceUi>(VoiceUi.Active(SessionPhase.ACTIVE, false, false))
        compose.mainClock.autoAdvance = false
        compose.setContent {
            ArchieTheme(reduceMotion = true) {
                Box(Modifier.fillMaxSize()) {
                    VoiceOverlay(
                        voice = voice,
                        onAction = { actions += it },
                        activity = activity,
                        region = Rect(0f, 0f, 1329f, 2958f),
                        anchor = OverlayAnchor.BottomCenter,
                        onAnchorChange = {},
                        onOpenConversation = { opened++ },
                        compact = true,
                    )
                }
            }
        }
        settle()
        compose.onNodeWithTag("voice-dock").assertExists()
        compose.onNodeWithContentDescription("Mute microphone").performClick()
        assertEquals(listOf<ChatAction>(ChatAction.ToggleMic), actions)

        compose.mainClock.advanceTimeBy(VoiceOverlayGeometry.IDLE_AFTER_MS + 1_500)
        compose.onNodeWithTag("voice-pill").assertExists()
        compose.onNodeWithTag("voice-dock").assertDoesNotExist()
        compose.onNodeWithContentDescription("Voice call: Listening. Show voice controls").assertExists()

        // a tap on the pill only expands it
        compose.onNodeWithTag("voice-pill").performClick()
        settle()
        compose.onNodeWithTag("voice-dock").assertExists()
        assertEquals(1, actions.size)

        // idle again; a press elsewhere wakes it
        compose.mainClock.advanceTimeBy(VoiceOverlayGeometry.IDLE_AFTER_MS + 1_500)
        compose.onNodeWithTag("voice-pill").assertExists()
        compose.runOnIdle { activity.onPointer(Offset(5f, 5f), press = true) }
        settle()
        compose.onNodeWithTag("voice-dock").assertExists()

        // states that need the user never shrink; a mute change wakes it
        compose.runOnIdle { voice = VoiceUi.Connecting("Connecting…") }
        settle()
        compose.mainClock.advanceTimeBy(VoiceOverlayGeometry.IDLE_AFTER_MS * 3)
        compose.onNodeWithTag("voice-pill").assertDoesNotExist()
        compose.runOnIdle { voice = VoiceUi.Active(SessionPhase.ACTIVE, false, false) }
        settle()
        compose.mainClock.advanceTimeBy(VoiceOverlayGeometry.IDLE_AFTER_MS + 1_500)
        compose.onNodeWithTag("voice-pill").assertExists()
        compose.runOnIdle { voice = VoiceUi.Active(SessionPhase.ACTIVE, true, false) }
        settle()
        compose.onNodeWithTag("voice-dock").assertExists()

        // the state text opens the Archie conversation
        compose.onNodeWithText("Listening").performClick()
        assertEquals(1, opened)
    }
}

/** Drag-to-snap: saved through the latest callback, from the latest region, back to the start too. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w1200dp-h800dp-mdpi", application = Application::class)
class VoiceOverlayDragTest {
    @get:Rule val compose = createComposeRule()

    private fun settle() {
        compose.waitForIdle()
        compose.mainClock.advanceTimeBy(100)
    }

    private fun drag(by: Offset) {
        compose.onNodeWithTag("voice-overlay").performTouchInput {
            down(center)
            repeat(4) { moveBy(by / 4f) }
            up()
        }
        settle()
    }

    @Test
    fun snapsAreSavedIncludingBackToTheStartAndUseTheCurrentRegion() {
        val saved = mutableListOf<OverlayAnchor>()
        var anchor by mutableStateOf(OverlayAnchor.BottomCenter)
        var region by mutableStateOf(Rect(0f, 0f, 1200f, 800f))
        compose.mainClock.autoAdvance = false
        compose.setContent {
            ArchieTheme(reduceMotion = true) {
                Box(Modifier.fillMaxSize()) {
                    VoiceOverlay(
                        voice = VoiceUi.Active(SessionPhase.ACTIVE, false, false),
                        onAction = {},
                        activity = remember { VoiceOverlayActivity() },
                        region = region,
                        anchor = anchor,
                        onAnchorChange = { saved += it; anchor = it },
                        onOpenConversation = {},
                        compact = false,
                    )
                }
            }
        }
        settle()
        // 560 wide at the bottom centre (x 320..880): dropped with its centre at x≈300, near the top
        drag(Offset(-300f, -600f))
        assertEquals(listOf(OverlayAnchor.TopLeft), saved)
        // back to where it started: saved too (the anchor it compares against is the current one)
        drag(Offset(300f, 600f))
        assertEquals(listOf(OverlayAnchor.TopLeft, OverlayAnchor.BottomCenter), saved)

        // the workspace shrinks to the right half: a small nudge stays bottom-centre of the NEW region
        compose.runOnIdle { region = Rect(600f, 0f, 1200f, 800f) }
        settle()
        drag(Offset(30f, 0f))
        assertEquals(OverlayAnchor.BottomCenter, saved.last())
    }
}
