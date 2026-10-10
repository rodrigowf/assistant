package com.assistant.archie.feature.chat

import android.app.Application
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import com.assistant.archie.feature.chat.support.FakeChatBackend
import com.assistant.archie.feature.chat.support.FakeChatVoice
import com.assistant.archie.feature.chat.support.Frames
import com.assistant.archie.feature.chat.ui.ConversationScreen
import com.assistant.archie.feature.chat.ui.DefaultToolCardRenderer
import com.assistant.core.conversation.ConnectionBanner
import com.assistant.core.conversation.ConversationInput
import com.assistant.core.conversation.ConversationState
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.ConnectionState
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

abstract class ScreenTestBase {
    @get:Rule val compose = createComposeRule()
    lateinit var backend: FakeChatBackend
    val voice = FakeChatVoice()

    fun show(initial: ConversationState) {
        backend = FakeChatBackend(initial)
        val vm = ConversationViewModel(backend, voice, DefaultToolCardRenderer::describe, flattenDispatcher = Dispatchers.Unconfined)
        compose.setContent { ArchieTheme(reduceMotion = true) { ConversationScreen(vm) } }
        compose.waitForIdle()
    }

    fun field() = compose.onNodeWithTag("composer-field")
}

/** Spec 14 §6.3 `ComposerStatesUiTest`: Voice → Send → Stop; queued send while working; disabled states. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class ComposerStatesUiTest : ScreenTestBase() {
    @Test
    fun archiePrimaryMorphsVoiceSendStop() {
        show(Frames.archie())
        compose.onNodeWithContentDescription("Start voice").assertExists().performClick()
        assertEquals(1, voice.starts)
        field().performTextInput("hello")
        compose.onNodeWithContentDescription("Send").assertExists()
        field().performTextClearance()
        compose.onNodeWithContentDescription("Start voice").assertExists()
        backend.frames("""{"type":"status","status":"streaming"}""", """{"type":"text_delta","text":"Hi"}""")
        compose.waitForIdle()
        compose.onNodeWithContentDescription("Stop").assertExists().performClick()
        assertEquals(1, backend.interrupts)
    }

    @Test
    fun agentEmptyFieldDisablesSend() {
        show(Frames.agent())
        compose.onNodeWithContentDescription("Send").assertIsNotEnabled()
        compose.onNodeWithText("Send a message to start.").assertExists()
    }

    @Test
    fun sendWhileWorkingQueuesIntoTheTray() {
        show(Frames.agent())
        backend.apply(ConversationInput.LocalSend("first"))
        backend.frames("""{"type":"status","status":"processing"}""")
        compose.waitForIdle()
        compose.onNodeWithContentDescription("Stop").assertExists()
        field().performTextInput("second")
        compose.onNodeWithContentDescription("Send").performClick()
        compose.waitForIdle()
        assertEquals(listOf("second"), backend.sent)
        compose.onNodeWithTag("queue-tray").assertExists()
        compose.onNode(hasText("second") and hasTestTag("composer-field").not()).assertExists()
        compose.onNodeWithText("Queued").assertExists()
        // The prompt is dispatched later: it leaves the tray and enters the timeline exactly once (I-12).
        // The sender gets no echo: status{processing} after the turn ends dispatches it (queued_prompt_sender).
        backend.frames("""{"type":"turn_complete"}""", """{"type":"status","status":"processing"}""")
        compose.waitForIdle()
        compose.onNodeWithTag("queue-tray").assertDoesNotExist()
        compose.onNodeWithTag("item:m:" + backend.state.value!!.entries.last().id).assertExists()
    }

    @Test
    fun imeSendAndSlashCommands() {
        show(Frames.agent())
        field().performTextInput("/help")
        field().performImeAction()
        assertEquals(listOf("/help"), backend.commands)
        field().performTextInput("/home/rodrigo is a path")
        field().performImeAction()
        assertEquals(listOf("/home/rodrigo is a path"), backend.sent)
    }

    @Test
    fun disabledStatesCarryReasons() {
        show(Frames.archie())
        backend.frames("""{"type":"status","status":"streaming"}""")
        compose.waitForIdle()
        compose.onNode(hasContentDescription("Record voice message (Wait for the reply to finish)")).assertIsNotEnabled()

        val ended = Frames.reduce(
            Frames.agent(),
            """{"type":"session_terminated","reason":"subprocess_crashed","detail":"claude exited with code 1","sdk_session_id":"sdk-1"}""",
            """{"type":"session_stopped"}""",
        )
        backend.set(ended)
        compose.waitForIdle()
        compose.onNodeWithText("This session has ended").assertExists()
    }

    @Test
    fun offlineSendKeepsTheTextAndRetrySendsIt() {
        show(Frames.agent().copy(connection = ConnectionState.OFFLINE))
        field().performTextInput("hello")
        compose.onNodeWithContentDescription("Send").performClick()
        compose.waitForIdle()
        compose.onNodeWithText("Message not sent").assertExists()
        assertTrue(backend.sent.isEmpty())
        compose.onNodeWithText("Retry").performClick()
        assertEquals(1, backend.retries)
        backend.set(backend.state.value!!.copy(connection = ConnectionState.SUBSCRIBED))
        compose.waitForIdle()
        assertEquals(listOf("hello"), backend.sent)
        compose.onNodeWithText("Message not sent").assertDoesNotExist()
    }
}

/** Spec 14 §6.3 `InlineCardsUiTest`: permission, type-to-reject, stall, termination endpoint. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class InlineCardsUiTest : ScreenTestBase() {
    private val permission = arrayOf(
        """{"type":"status","status":"processing"}""",
        """{"type":"permission_request","request_id":"r1","tool_name":"ExitPlanMode","tool_input":{"plan":"1. Check Kodi\n2. Queue three films"}}""",
    )

    @Test
    fun approveAnswersOnceAndClosesOnResolve() {
        show(Frames.reduce(Frames.agent(), *permission))
        compose.onNodeWithText("Plan ready: exit plan mode?").assertExists()
        compose.onNodeWithText("Or type below to give feedback").assertExists()
        compose.onNodeWithText("Approve").performClick()
        compose.waitForIdle()
        assertEquals(listOf("r1" to true), backend.permissions)
        compose.onNodeWithText("Approve").assertDoesNotExist()   // one click only (§6.9)
        backend.frames("""{"type":"permission_resolved","request_id":"r1","decision":"allow","responder":"user","message":null}""")
        compose.waitForIdle()
        compose.onNodeWithTag("card:perm:r1").assertDoesNotExist()
    }

    @Test
    fun reject() {
        show(Frames.reduce(Frames.agent(), *permission))
        compose.onNodeWithText("Reject").performClick()
        assertEquals(listOf("r1" to false), backend.permissions)
    }

    @Test
    fun typingRejectsWithFeedback() {
        show(Frames.reduce(Frames.agent(), *permission))
        field().performTextInput("Use Plex instead")
        compose.onNodeWithContentDescription("Send").performClick()
        compose.waitForIdle()
        // A plain send: the server denies the pending permission with the text as feedback (§6.9).
        assertEquals(listOf("Use Plex instead"), backend.sent)
        assertTrue(backend.permissions.isEmpty())
        backend.frames("""{"type":"permission_resolved","request_id":"r1","decision":"deny","responder":"user","message":"Use Plex instead"}""")
        compose.waitForIdle()
        compose.onNodeWithTag("card:perm:r1").assertDoesNotExist()
    }

    @Test
    fun stallInterrupts() {
        show(
            Frames.reduce(
                Frames.agent(),
                """{"type":"status","status":"processing"}""",
                """{"type":"tool_use","tool_use_id":"t1","tool_name":"WebFetch","tool_input":{"url":"https://developer.android.com/x"}}""",
                """{"type":"session_stalled","elapsed_seconds":120.0,"last_tool_name":"WebFetch","last_tool_use_id":"t1"}""",
            ),
        )
        compose.onNodeWithText("WebFetch silent for 2 min").assertExists()
        compose.onNodeWithText("Interrupt").performClick()
        assertEquals(1, backend.interrupts)
    }

    @Test
    fun agentApprovalsOnTheArchieView() {
        show(
            Frames.reduce(
                Frames.archie(),
                """{"type":"nested_session_event","session_id":"L5","event_type":"permission_request","event_data":{"type":"permission_request","request_id":"r1","tool_name":"ExitPlanMode","tool_input":{"plan":"1. Build"}}}""",
            ),
        )
        compose.onNodeWithText("An agent finished planning").assertExists()
        compose.onNodeWithText("Build", substring = true).assertExists()
        compose.onNodeWithText("Approve").performClick()
        assertEquals(listOf(Triple("L5", "r1", true)), backend.agentApprovals)
        compose.onNodeWithText("Answer sent…").assertExists()
    }

    @Test
    fun agentApprovalFailureReEnablesTheCard() {
        show(
            Frames.reduce(
                Frames.archie(),
                """{"type":"nested_session_event","session_id":"L5","event_type":"permission_request","event_data":{"type":"permission_request","request_id":"r1","tool_name":"Bash","tool_input":{"command":"ls"}}}""",
            ),
        )
        backend.approvalAnswer = com.assistant.core.data.ApprovalAnswer.Failed("That agent session is no longer running")
        compose.onNodeWithText("Reject").performClick()
        compose.waitForIdle()
        assertEquals(listOf(Triple("L5", "r1", false)), backend.agentApprovals)
        compose.onNodeWithText("That agent session is no longer running").assertExists()
        compose.onNodeWithText("Approve").assertExists()                 // buttons back (not "Answer sent…")
    }

    @Test
    fun connectionBannerRetries() {
        show(Frames.agent().copy(connectionBanner = ConnectionBanner("start_failed", "Agent failed to start")))
        compose.onNodeWithText("The session couldn't start").assertExists()
        compose.onNodeWithText("Agent failed to start").assertExists()
        compose.onNodeWithText("Retry").performClick()
        assertEquals(1, backend.retries)
    }
}
