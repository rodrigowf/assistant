package com.assistant.archie.feature.sessions

import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.LoadState
import com.assistant.core.data.TabStatus
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.PoolSession
import com.assistant.core.model.SessionSummary
import com.assistant.core.network.ApiResult
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The B-06 decisions in isolation (the end-to-end flows are in SessionFlowsUiTest). */
@OptIn(ExperimentalCoroutinesApi::class)
class SessionsControllerTest {
    private class Fake : SessionsBackend {
        val calls = mutableListOf<String>()
        override val items = MutableStateFlow<List<WorkspaceItem>>(emptyList())
        override val sessions = MutableStateFlow(LoadState<List<SessionSummary>>(emptyList()))
        override val pool = MutableStateFlow<List<PoolSession>>(emptyList())
        override val conflicts = MutableSharedFlow<Unit>(extraBufferCapacity = 4)
        override val notices = MutableSharedFlow<String>(extraBufferCapacity = 4)
        var here: RunningArchie? = null
        var view: Pair<String, String?>? = null
        var poolRows: List<PoolSession>? = emptyList()
        var renameResult: ApiResult<Unit> = ApiResult.Ok(Unit)

        override fun liveArchieHere() = here
        override fun archieViewIds() = view
        override fun lastKnownOrchestrator(): RunningArchie? = null
        override suspend fun syncPool() = poolRows
        override fun newArchie() { calls += "newArchie" }
        override fun resumeArchie(sdkId: String, attachLocalId: String?) { calls += "resume:$sdkId:$attachLocalId" }
        override fun focusArchie() { calls += "focusArchie" }
        override fun dropArchieView() { calls += "drop"; view = null }
        override suspend fun closeArchieHere(): Boolean { calls += "closeHere"; return true }
        override suspend fun closePoolSession(localId: String): Boolean { calls += "closePool:$localId"; return true }
        override fun newAgent() { calls += "newAgent" }
        override fun openAgent(summary: SessionSummary) { calls += "openAgent:${summary.sdkId}" }
        override fun openAgent(sdkId: String, provider: HarnessProvider?) { calls += "openAgent:$sdkId" }
        override suspend fun close(key: ItemKey): Boolean { calls += "close:$key"; return true }
        override fun compact(key: ItemKey) { calls += "compact:$key" }
        override fun storedTitle(sdkId: String): String? = "Old title"
        override fun isLive(sdkId: String) = false
        override suspend fun rename(sdkId: String, title: String): ApiResult<Unit> { calls += "rename:$sdkId:$title"; return renameResult }
        override suspend fun duplicate(sdkId: String): ApiResult<String> { calls += "duplicate:$sdkId"; return ApiResult.Ok("D") }
        override suspend fun fork(sdkId: String): ApiResult<String> { calls += "fork:$sdkId"; return ApiResult.Ok("F") }
        override suspend fun delete(sdkId: String, localId: String?): ApiResult<Unit> { calls += "delete:$sdkId"; return ApiResult.Ok(Unit) }
    }

    private fun orchRow(local: String, sdk: String) = PoolSession(local, sdk, null, 0.0, 0, "x", true)

    private fun TestScope.controller(f: Fake) = SessionsController(f, backgroundScope, now = { testScheduler.currentTime })

    private val agent = WorkspaceItem(ItemKey.Agent(ConversationKey.agent("A")), ItemKind.AGENT, "Agent", HarnessProvider.CLAUDE, TabStatus.IDLE, localId = "A", sdkId = "S")

    @Test fun aServerCloseOfTheActiveViewIsTheSnackbar_OPEN3() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        f.notices.emit(com.assistant.core.data.OpenSessionsRepository.closedNotice("Energy dashboard", null))
        assertEquals("Energy dashboard was closed elsewhere", c.state.value.snack?.message)
        f.notices.emit(
            com.assistant.core.data.OpenSessionsRepository.closedNotice(
                "Energy dashboard", com.assistant.core.conversation.Termination("subprocess_crashed", "claude exited with code 1", null),
            ),
        )
        assertEquals("Energy dashboard crashed: claude exited with code 1", c.state.value.snack?.message)
    }

    @Test fun beforeTheFirstReply_actionsSayWhy_insteadOfDoingNothing() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        val t = SessionTarget(null, "A", "New agent session", false)
        c.onIntent(SessionsIntent.RequestRename(t))
        assertEquals("Rename is available after the first reply", c.state.value.snack?.message)
        c.onIntent(SessionsIntent.RequestDelete(t))
        assertEquals("Delete is available after the first reply", c.state.value.snack?.message)
        c.onIntent(SessionsIntent.RequestFork(t))
        assertEquals("Fork is available after the first reply", c.state.value.snack?.message)
        assertNull(c.state.value.dialog)
        assertTrue(f.calls.isEmpty())
        advanceTimeBy(SessionsController.SNACK_SHORT_MS + 1)
        assertNull("snackbar times out", c.state.value.snack)
    }

    @Test fun close_asksOnlyForArchieAndAWorkingAgent() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        c.onIntent(SessionsIntent.RequestClose(agent))
        assertEquals(listOf("close:${agent.key}"), f.calls)
        val memory = WorkspaceItem(ItemKey.Memory("a.md"), ItemKind.MEMORY, "a.md")
        c.onIntent(SessionsIntent.RequestClose(memory))
        assertEquals("close:${memory.key}", f.calls.last())

        c.onIntent(SessionsIntent.RequestClose(agent.copy(status = TabStatus.WORKING)))
        assertEquals(SessionDialog.Close(agent.key, "Agent", archie = false), c.state.value.dialog)
        c.onIntent(SessionsIntent.DismissDialog)
        assertEquals(2, f.calls.size)

        val archie = WorkspaceItem(ItemKey.Archie, ItemKind.ARCHIE, "Archie", status = TabStatus.IDLE)
        c.onIntent(SessionsIntent.RequestClose(archie))
        assertEquals(SessionDialog.Close(ItemKey.Archie, NEW_CONVERSATION, archie = true), c.state.value.dialog)
        c.onIntent(SessionsIntent.ConfirmClose)
        assertEquals("close:${ItemKey.Archie}", f.calls.last())
    }

    @Test fun replace_whenRunningHere_closesTheViewExplicitly_notThePoolBehindIt() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake().apply { here = RunningArchie("ORCH", "J", here = true) }
        val c = controller(f)
        c.requestResumeArchie("H3")
        val d = c.state.value.dialog as SessionDialog.ArchieConflict
        assertEquals(ConflictMode.RESUME, d.mode)
        c.onIntent(SessionsIntent.ResolveConflict(ConflictChoice.REPLACE))
        assertEquals(listOf("closeHere", "resume:H3:null"), f.calls)
        assertNull(c.state.value.busy)
    }

    @Test fun resumingTheRunningConversation_justOpensIt() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake().apply { poolRows = listOf(orchRow("OTHER", "J2")) }
        val c = controller(f)
        c.requestResumeArchie("J2")
        assertNull(c.state.value.dialog)
        assertEquals(listOf("resume:J2:OTHER"), f.calls)       // attach with the running one's pool key (G-15)
    }

    @Test fun offline_unknownPool_fallsBackToFreshStart() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake().apply { poolRows = null }
        val c = controller(f)
        c.requestNewArchie()
        assertEquals(listOf("newArchie"), f.calls)
    }

    @Test fun unsolicitedConflict_afterAContinuation_asksToResumeThatConversation() = runTest(UnconfinedTestDispatcher()) {
        // e.g. B-04's "Continue in a new view" on an ended Archie view lost to another device.
        val f = Fake().apply { view = "NEWLOCAL" to "OLD_SDK"; poolRows = listOf(orchRow("OTHER", "J2")) }
        val c = controller(f)
        f.conflicts.emit(Unit)
        val d = c.state.value.dialog as SessionDialog.ArchieConflict
        assertEquals(ConflictMode.RESUME, d.mode)
        assertEquals("OLD_SDK", d.resumeSdkId)
        assertEquals(RunningArchie("OTHER", "J2", here = false), d.running)
        assertEquals(listOf("drop"), f.calls)
    }

    @Test fun conflictWithNothingFound_saysTryAgain() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        f.conflicts.emit(Unit)
        assertNull(c.state.value.dialog)
        assertEquals("Archie is already running on another device. Try again.", c.state.value.snack?.message)
    }

    @Test fun rename_trimmedAndUnchangedTitles() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        val t = SessionTarget("S", "A", "Agent", false)
        c.onIntent(SessionsIntent.RequestRename(t))
        c.onIntent(SessionsIntent.CommitRename("  Agent "))
        assertNull(c.state.value.dialog)
        assertTrue(f.calls.isEmpty())
        c.onIntent(SessionsIntent.RequestRename(t))
        c.onIntent(SessionsIntent.CommitRename("  New  "))
        assertEquals(listOf("rename:S:New"), f.calls)
        assertEquals("Undo", c.state.value.snack?.actionLabel)
        c.onIntent(SessionsIntent.SnackAction(c.state.value.snack!!.id))
        assertEquals("rename:S:Old title", f.calls.last())
    }

    @Test fun renameOfAnUntitledArchie_startsEmpty() = runTest(UnconfinedTestDispatcher()) {
        val c = controller(Fake())
        c.onIntent(SessionsIntent.RequestRename(SessionTarget("J", "O", NEW_CONVERSATION, true)))
        assertEquals("", (c.state.value.dialog as SessionDialog.Rename).initial)
    }

    @Test fun forkOfArchie_isOfferedNotOpened_agentForkOpens() = runTest(UnconfinedTestDispatcher()) {
        val f = Fake()
        val c = controller(f)
        c.onIntent(SessionsIntent.RequestFork(SessionTarget("J", "O", "Talk", true)))
        c.onIntent(SessionsIntent.ConfirmFork)
        assertEquals(listOf("fork:J"), f.calls)
        assertEquals("Open", c.state.value.snack?.actionLabel)
        c.onIntent(SessionsIntent.RequestFork(SessionTarget("S", "A", "Agent", false, HarnessProvider.QWEN)))
        c.onIntent(SessionsIntent.ConfirmFork)
        assertEquals(listOf("fork:J", "fork:S", "openAgent:F"), f.calls)
    }

    @Test fun menuModel_reasons() {
        val m = SessionMenuModel.of(agent.copy(sdkId = null, status = TabStatus.CONNECTING))
        assertEquals(ActionState.no(NEEDS_FIRST_REPLY), m.rename)
        assertEquals(ActionState.no(NOT_CONNECTED), m.compact)
        assertEquals(ActionState.no(STOP_FIRST), SessionMenuModel.of(agent.copy(status = TabStatus.WORKING)).compact)
        assertEquals(ActionState.no(NOT_FOR_ARCHIE), SessionMenuModel.of(agent.copy(key = ItemKey.Archie, kind = ItemKind.ARCHIE)).settings)
        assertEquals(false, SessionMenuModel.of(WorkspaceItem(ItemKey.Visual("v.html"), ItemKind.VISUAL, "v")).conversation)
    }
}
