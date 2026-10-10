package com.assistant.archie.feature.sessions

import android.app.Application
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import com.assistant.archie.feature.sessions.ui.HistoryListState
import com.assistant.archie.feature.sessions.ui.HistoryScreen
import com.assistant.archie.feature.sessions.ui.RenameDialogContent
import com.assistant.archie.feature.sessions.ui.SessionMenuPanel
import com.assistant.archie.feature.sessions.ui.SessionSwitcherContent
import com.assistant.archie.feature.sessions.ui.SessionsHost
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.TabStatus
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.design.theme.ThemeMode
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.SessionSummary
import com.github.takahirom.roborazzi.ExperimentalRoborazziApi
import com.github.takahirom.roborazzi.captureScreenRoboImage
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.time.Instant
import java.time.ZoneOffset
import java.util.Locale

/** Mockup content (archie-mockups.html phone (b)/(c), "Menu and dialog") for the B-06 goldens. */
object SessionFixtures {
    val agentKey = ItemKey.Agent(ConversationKey.agent("AG1"))
    private val agent2 = ItemKey.Agent(ConversationKey.agent("AG2"))

    val items = listOf(
        WorkspaceItem(ItemKey.Archie, ItemKind.ARCHIE, "Living-room TV", status = TabStatus.NEEDS_YOU, detail = "Archie · waiting for approval", localId = "ORCH", sdkId = "JSONL"),
        WorkspaceItem(agentKey, ItemKind.AGENT, "Refactor voice module", HarnessProvider.CLAUDE, TabStatus.IDLE, "Ready · 14 turns", localId = "AG1", sdkId = "S1"),
        WorkspaceItem(agent2, ItemKind.AGENT, "Energy dashboard", HarnessProvider.QWEN, TabStatus.WORKING, "Using Bash…", localId = "AG2", sdkId = "S2"),
        WorkspaceItem(ItemKey.Memory("assistant/architecture/voice_subsystem.md"), ItemKind.MEMORY, "voice_subsystem.md", detail = "Memory · assistant/architecture"),
    )

    val newAgent = WorkspaceItem(ItemKey.Agent(ConversationKey.agent("NEW")), ItemKind.AGENT, "New agent session", HarnessProvider.CLAUDE, TabStatus.IDLE, "Ready", localId = "NEW", sdkId = null)

    /** Sat 3 Oct 2026, 15:00 UTC; shown in UTC so goldens do not depend on the host zone. */
    val now: Instant = Instant.parse("2026-10-03T15:00:00Z")

    private fun s(id: String, title: String, last: String, orch: Boolean, n: Int = 6, p: HarnessProvider? = HarnessProvider.CLAUDE) =
        SessionSummary(id, last, last, title, n, orch, if (orch) null else p, null)

    val sessions = listOf(
        s("h1", "Weekly energy report", "2026-10-03T14:20:00+00:00", true, 12),
        s("h2", "Fix context-sync delete race", "2026-10-03T11:05:00+00:00", false, 31),
        s("h3", "Morning briefing", "2026-10-02T08:12:00+00:00", true, 8),
        s("h4", "Wake-word tuning review", "2026-10-01T07:40:00+00:00", false, 22, HarnessProvider.QWEN),
        s("h6", "Compat build for iPad mini", "2026-09-28T10:00:00+00:00", false, 17),
        s("h7", "Jetson thermal check", "2026-08-28T21:49:03.550000+00:00", false, 9, HarnessProvider.GEMINI),
    )

    fun historyState(query: String = "") = HistoryListState(
        items = items.take(3),
        active = ItemKey.Archie,
        groups = HistoryList.groups(sessions, items, query, now, ZoneOffset.UTC, Locale.US),
        query = query,
    )

    val voiceTarget = SessionTarget("S1", "AG1", "Refactor voice module", false, HarnessProvider.CLAUDE, 14, agentKey)
    val archieTarget = SessionTarget("JSONL", "ORCH", "Living-room TV", true, key = ItemKey.Archie)
}

/*
 * Roborazzi goldens of B-06 (spec 14 §6.4) on the POCO size (w443dp xxhdpi), dark and light →
 * src/test/screenshots/<scene>_<theme>.png. Compared by hand with the approved mockups: phone (b)
 * drawer lists (History screen uses the same sections), (c) session switcher, "Menu and dialog".
 *
 *   flock /tmp/archie-locks/gradle.lock ./gradlew :feature:sessions:recordRoborazziDebug   record
 *   flock /tmp/archie-locks/gradle.lock ./gradlew :feature:sessions:verifyRoborazziDebug   compare
 */
@OptIn(ExperimentalRoborazziApi::class)
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class SessionsGoldens {
    @get:Rule val compose = createComposeRule()

    private fun scene(name: String, content: @Composable () -> Unit) {
        var mode by mutableStateOf(ThemeMode.Dark)
        compose.setContent { ArchieTheme(mode = mode, reduceMotion = true) { content() } }
        for (theme in listOf(ThemeMode.Dark, ThemeMode.Light)) {
            mode = theme
            compose.mainClock.advanceTimeBy(1_000)
            compose.waitForIdle()
            captureScreenRoboImage("src/test/screenshots/${name}_${theme.name.lowercase()}.png")
        }
    }

    @Composable
    private fun History(state: SessionsUiState = SessionsUiState()) {
        Box(Modifier.fillMaxSize()) {
            HistoryScreen(SessionFixtures.historyState(), {}, {}, {}, {}, {}, {})
            SessionsHost(state, {})
        }
    }

    /** The History screen: Open now (two-line, live status), date groups with local stamps, ⋮ per row. */
    @Test fun history() = scene("b06-history") { History() }

    /** Phone (c): the session switcher sheet over the conversation. */
    @Test fun switcher() = scene("b06-switcher") {
        val c = ArchieTheme.colors
        Box(Modifier.fillMaxSize().background(c.surface)) {
            Box(Modifier.fillMaxSize().background(c.scrim.copy(alpha = 0.32f)))
            Column(
                Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .background(c.surfaceContainerLow, RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp))
                    .padding(top = 34.dp),
            ) {
                SessionSwitcherContent(SessionFixtures.items, ItemKey.Archie, {}, {}, {}, {}, {}, {})
            }
        }
    }

    /** "Menu and dialog": the session ⋮ menu of an agent, and of a new agent (disabled items say why, ID-3). */
    @Test fun menu() = scene("b06-menu") {
        val c = ArchieTheme.colors
        Column(Modifier.fillMaxSize().background(c.surface).padding(24.dp)) {
            SessionMenuPanel(SessionFixtures.items[1])
            Box(Modifier.padding(top = 24.dp)) { SessionMenuPanel(SessionFixtures.newAgent) }
        }
    }

    @Test fun deleteDialog() = scene("b06-delete-dialog") {
        History(SessionsUiState(dialog = SessionDialog.Delete(SessionFixtures.voiceTarget, 14, stopsLive = true)))
    }

    /** The three-action dialog, new conversation while Archie runs on another device. */
    @Test fun conflictNew() = scene("b06-conflict-new") {
        History(SessionsUiState(dialog = SessionDialog.ArchieConflict(ConflictMode.NEW, null, RunningArchie("OTHER", "J2", here = false))))
    }

    /** Resuming a past conversation while this device's Archie runs. */
    @Test fun conflictResume() = scene("b06-conflict-resume") {
        History(SessionsUiState(dialog = SessionDialog.ArchieConflict(ConflictMode.RESUME, "h3", RunningArchie("ORCH", "JSONL", here = true))))
    }

    /**
     * Drawn in place over a scrim: under Robolectric a text field inside a Dialog window never idles
     * on a sized screen (see RenameFlowUiTest), so the golden shows the same surface without the window.
     */
    @Test fun renameDialog() = scene("b06-rename-dialog") {
        Box(Modifier.fillMaxSize()) {
            History()
            Box(Modifier.fillMaxSize().background(ArchieTheme.colors.scrim.copy(alpha = 0.32f)), contentAlignment = Alignment.Center) {
                RenameDialogContent(SessionDialog.Rename(SessionFixtures.voiceTarget, "Refactor voice module"), {}, autofocus = false)
            }
        }
    }

    @Test fun closeArchieDialog() = scene("b06-close-dialog") {
        History(SessionsUiState(dialog = SessionDialog.Close(ItemKey.Archie, "Living-room TV", archie = true)))
    }

    @Test fun forkDialog() = scene("b06-fork-dialog") {
        History(SessionsUiState(dialog = SessionDialog.Fork(SessionFixtures.voiceTarget)))
    }

    /** Busy overlay (inv02 F-12) and a snackbar with Undo. */
    @Test fun busyAndSnackbar() = scene("b06-busy-snackbar") {
        History(SessionsUiState(busy = "Deleting…", snack = Snack(1, "Renamed to “Voice state machine”", "Undo")))
    }
}
