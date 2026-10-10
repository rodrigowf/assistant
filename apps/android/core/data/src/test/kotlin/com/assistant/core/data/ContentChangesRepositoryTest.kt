package com.assistant.core.data

import com.assistant.core.network.ArchieApi
import com.assistant.core.network.HttpStack
import com.assistant.core.network.SocketState
import com.assistant.core.protocol.ContentChange
import com.assistant.core.protocol.ContentChange.Kind
import com.assistant.core.protocol.ProtocolCodec
import com.assistant.core.protocol.ServerFrame
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Spec 12 §9.3 / VZ-6 (web parity: `services/__tests__/contentChanges.test.ts`): change frames bump
 * per-path counters and refresh the lists once per burst; a reopened socket catches up from the
 * visualization list's `modified`.
 */
class ContentChangesRepositoryTest {
    private val backend = FakeBackend().start()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val frames = MutableSharedFlow<ServerFrame>(extraBufferCapacity = 16)
    private val socket = MutableStateFlow<SocketState>(SocketState.Idle)
    private val api = ArchieApi(HttpStack()) { backend.url }
    private val visuals = VisualsRepository(api, scope)
    private val memory = MemoryRepository(api, scope)
    private val repo = ContentChangesRepository(frames, socket, visuals, memory, scope)

    @After fun tearDown() {
        scope.cancel()
        backend.shutdown()
    }

    private fun gets(path: String) = backend.requests.count { it == "GET $path" }

    private fun viz(path: String, modified: String) =
        """{"path":"$path","url":"/$path","title":"$path","created":"$modified","modified":"$modified","size":1}"""

    @Test fun visualizationFramesBumpEachPathAndRefreshTheListOncePerBurst() {
        val frame = ProtocolCodec.decodeServer(
            """{"type":"visualization_changed","visualizations":[{"path":"dash/index.html","kind":"modified"},{"path":"old.html","kind":"deleted"},{"kind":"x"}],"files":[{"path":"dash/data.json","kind":"modified"}]}""",
        ) as ServerFrame.VisualizationChanged
        assertEquals(listOf(ContentChange("dash/data.json", Kind.MODIFIED)), frame.files)
        repo.onFrame(frame)
        repo.onFrame(ServerFrame.VisualizationChanged(listOf(ContentChange("dash/index.html", Kind.MODIFIED))))
        assertEquals(ContentStamp(2), repo.visuals.value["dash/index.html"])
        assertEquals(ContentStamp(1, deleted = true), repo.visuals.value["old.html"])
        assertEquals(2, repo.visuals.value.size)
        eventually { gets("/api/visualizations") == 1 }
        Thread.sleep(ContentChangesRepository.REFRESH_DEBOUNCE_MS + 200)
        assertEquals(1, gets("/api/visualizations"))
    }

    @Test fun memoryTreeRefetchesOnlyWhenLoadedAndAFileAppearedOrWentAway() {
        repo.onFrame(ServerFrame.MemoryChanged(listOf(ContentChange("projects/x.md", Kind.CREATED))))
        assertEquals(ContentStamp(1), repo.memory.value["projects/x.md"])
        Thread.sleep(ContentChangesRepository.REFRESH_DEBOUNCE_MS + 200)
        assertEquals(0, gets("/api/memory/tree")) // never loaded

        memory.refreshTree()
        eventually { memory.tree.value.loaded }
        repo.onFrame(ServerFrame.MemoryChanged(listOf(ContentChange("projects/x.md", Kind.MODIFIED))))
        Thread.sleep(ContentChangesRepository.REFRESH_DEBOUNCE_MS + 200)
        assertEquals(1, gets("/api/memory/tree"))
        repo.onFrame(ServerFrame.MemoryChanged(listOf(ContentChange("projects/y.md", Kind.DELETED))))
        eventually { gets("/api/memory/tree") == 2 }
    }

    @Test fun framesFromTheChannelFlowAreConsumed() {
        eventually { frames.subscriptionCount.value > 0 }
        frames.tryEmit(ServerFrame.MemoryChanged(listOf(ContentChange("a.md", Kind.MODIFIED))))
        eventually { repo.memory.value["a.md"] == ContentStamp(1) }
    }

    @Test fun aReopenedSocketCatchesUpFromTheList() {
        backend.visualizationsJson = "[${viz("a.html", "2026-10-01T00:00:00+00:00")},${viz("b.html", "2026-10-01T00:00:00+00:00")},${viz("c.html", "2026-10-01T00:00:00+00:00")}]"
        visuals.refresh()
        eventually { visuals.list.value.value?.size == 3 }
        socket.value = SocketState.Open // first open: not a reconnect
        Thread.sleep(200)
        assertEquals(0, repo.resyncEpoch.value)

        backend.visualizationsJson = "[${viz("a.html", "2026-10-09T00:00:00+00:00")},${viz("c.html", "2026-10-01T00:00:00+00:00")}]"
        socket.value = SocketState.Disconnected(willReconnect = true, reason = null)
        Thread.sleep(100)
        socket.value = SocketState.Open
        eventually { repo.visuals.value.size == 2 }
        assertEquals(1, repo.resyncEpoch.value)
        assertEquals(ContentStamp(1), repo.visuals.value["a.html"])
        assertEquals(ContentStamp(1, deleted = true), repo.visuals.value["b.html"])
    }
}
