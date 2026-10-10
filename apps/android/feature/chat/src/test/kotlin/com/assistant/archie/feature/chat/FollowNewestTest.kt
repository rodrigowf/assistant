package com.assistant.archie.feature.chat

import android.app.Application
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.test.junit4.createComposeRule
import com.assistant.archie.feature.chat.support.FakeChatBackend
import com.assistant.archie.feature.chat.support.FakeChatVoice
import com.assistant.archie.feature.chat.support.Frames
import com.assistant.archie.feature.chat.ui.ConversationContent
import com.assistant.archie.feature.chat.ui.DefaultToolCardRenderer
import com.assistant.core.conversation.ConversationInput
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Auto-follow (ChatList `FollowNewest`). A reply streams as many list items (one per markdown block,
 * plus tool cards). LazyList keeps its position on the first visible item's key, so before the fix a
 * list at the bottom stayed on the first finished paragraph while the rest of the turn streamed below
 * the fold (seen on the POCO, 2026-10-10). At the bottom the list must follow; scrolled up it must not move.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w411dp-h700dp", application = Application::class)
class FollowNewestTest {
    @get:Rule val compose = createComposeRule()

    private val backend = FakeChatBackend(Frames.agent())
    private val vm = ConversationViewModel(backend, FakeChatVoice(), DefaultToolCardRenderer::describe, flattenDispatcher = Dispatchers.Unconfined)
    private val listState = LazyListState()

    private fun show() {
        compose.setContent {
            val state by vm.state.collectAsState()
            ArchieTheme(reduceMotion = true) {
                ConversationContent(state, "", vm::onAction, listState = listState)
            }
        }
        backend.apply(ConversationInput.LocalSend("Write a long answer"))
        backend.frames("""{"type":"status","status":"processing"}""")
    }

    /** Streams one paragraph per block with a tool call every other block; [check] runs after each block. */
    private fun stream(blocks: IntRange, check: (Int) -> Unit) {
        for (b in blocks) {
            val text = "Paragraph $b talks about rivers and deltas at some length so that it wraps over several lines.\n\n"
            for (chunk in text.chunked(12)) {
                backend.frames("""{"type":"text_delta","text":${JsonPrimitive(chunk)}}""")
                compose.mainClock.advanceTimeBy(ConversationViewModel.SAMPLE_MS + 1)
                compose.waitForIdle()
            }
            if (b % 2 == 0) {
                backend.frames("""{"type":"tool_use","tool_use_id":"t$b","tool_name":"Bash","tool_input":{"command":"echo $b"}}""")
                backend.frames("""{"type":"tool_result","tool_use_id":"t$b","output":"$b","is_error":false}""")
                compose.mainClock.advanceTimeBy(ConversationViewModel.SAMPLE_MS + 1)
                compose.waitForIdle()
            }
            check(b)
        }
    }

    @Test
    fun atTheBottomTheListFollowsNewItems() {
        show()
        stream(1..24) { b ->
            assertEquals("block $b: the newest item is the first visible one", 0, listState.firstVisibleItemIndex)
            assertEquals("block $b: scrolled to the very bottom", 0, listState.firstVisibleItemScrollOffset)
        }
        assertTrue("the list overflowed the screen", listState.layoutInfo.totalItemsCount > listState.layoutInfo.visibleItemsInfo.size)
    }

    @Test
    fun scrolledUpTheListStaysOnTheText() {
        show()
        stream(1..16) { }
        runBlocking { listState.scrollToItem(6, 40) } // the user scrolls up to an older paragraph
        compose.waitForIdle()
        val key = listState.layoutInfo.visibleItemsInfo.first().key
        val offset = listState.firstVisibleItemScrollOffset
        stream(17..28) { b ->
            assertEquals("block $b: same item on screen", key, listState.layoutInfo.visibleItemsInfo.first().key)
            assertEquals("block $b: same offset", offset, listState.firstVisibleItemScrollOffset)
        }
    }
}
