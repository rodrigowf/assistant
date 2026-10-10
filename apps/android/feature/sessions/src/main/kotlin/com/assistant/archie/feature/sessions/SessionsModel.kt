package com.assistant.archie.feature.sessions

import androidx.compose.runtime.Immutable
import com.assistant.core.data.HistoryGroup
import com.assistant.core.data.HistoryGrouping
import com.assistant.core.data.HistoryRepository
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.OpenSessionsRepository
import com.assistant.core.data.TabStatus
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.SessionSummary
import java.time.Instant
import java.time.ZoneId
import java.util.Locale

/*
 * B-06 session model (spec 14 §7 B-06; web parity: frontend features/session-actions, W-11,
 * and features/history). Pure types and helpers; the flows live in SessionsController.
 */

/** ID-3 copy (spec 12 §2.1): shown instead of silently ignoring an action. */
const val NEEDS_FIRST_REPLY = "Available after the first reply"
const val STOP_FIRST = "Stop the current reply first"
const val NOT_CONNECTED = "Not connected"
const val NOT_FOR_ARCHIE = "Not for Archie"

/** IA §1: an untitled Archie conversation is "New conversation" (never "Orchestrator"). */
const val NEW_CONVERSATION = "New conversation"

object SessionTitles {
    private val GENERIC_ARCHIE = Regex("^\\s*(orchestrator|archie)?\\s*$", RegexOption.IGNORE_CASE)

    fun conversationTitle(raw: String?, isArchie: Boolean): String {
        val t = raw.orEmpty().trim()
        if (isArchie) return if (GENERIC_ARCHIE.matches(t)) NEW_CONVERSATION else t
        // A live agent session with no history file yet is listed as "(active session)".
        if (t == HistoryRepository.ACTIVE_SESSION_PLACEHOLDER) return OpenSessionsRepository.AGENT_PLACEHOLDER
        return t.ifEmpty { "Untitled" }
    }

    /** The placeholder titles of B-03 (`OpenSessionsRepository`) are not real titles. */
    fun isPlaceholder(title: String, isArchie: Boolean): Boolean =
        conversationTitle(title, isArchie) == NEW_CONVERSATION || title == "New agent session" || title.isBlank()
}

/** A session an action applies to: an open workspace item, or a history row. */
@Immutable
data class SessionTarget(
    val sdkId: String?,
    val localId: String?,
    val title: String,
    val isArchie: Boolean,
    val provider: HarnessProvider? = null,
    val messageCount: Int? = null,
    /** The workspace item when the session is open on this device. */
    val key: ItemKey? = null,
) {
    companion object {
        fun of(item: WorkspaceItem) = SessionTarget(
            sdkId = item.sdkId,
            localId = item.localId,
            title = SessionTitles.conversationTitle(item.title, item.kind == ItemKind.ARCHIE),
            isArchie = item.kind == ItemKind.ARCHIE,
            provider = item.provider,
            key = item.key,
        )

        fun of(s: SessionSummary) = SessionTarget(
            sdkId = s.sdkId,
            localId = s.localId,
            title = SessionTitles.conversationTitle(s.title, s.isOrchestrator),
            isArchie = s.isOrchestrator,
            provider = s.provider,
            messageCount = s.messageCount,
        )
    }
}

/** Whether a ⋮ action may run now, and why not (shown as the item's supporting text, ID-3). */
@Immutable
data class ActionState(val enabled: Boolean, val reason: String? = null) {
    companion object {
        val OK = ActionState(true)
        fun no(reason: String) = ActionState(false, reason)
    }
}

/** The session ⋮ menu of the active item (IA §3, mockups "Menu and dialog"). */
@Immutable
data class SessionMenuModel(
    /** Archie or agent conversation (documents only offer Close). */
    val conversation: Boolean,
    val rename: ActionState,
    val settings: ActionState,
    val compact: ActionState,
    val fork: ActionState,
    val delete: ActionState,
) {
    companion object {
        /** `useSessionActions` of the web (W-11), from the item's derived status. */
        fun of(item: WorkspaceItem): SessionMenuModel {
            val conversation = item.kind == ItemKind.ARCHIE || item.kind == ItemKind.AGENT
            val needsSdk = if (item.sdkId != null) ActionState.OK else ActionState.no(NEEDS_FIRST_REPLY)
            val busy = item.status == TabStatus.WORKING
            val connected = item.status == TabStatus.IDLE || item.status == TabStatus.WORKING || item.status == TabStatus.NEEDS_YOU
            return SessionMenuModel(
                conversation = conversation,
                rename = needsSdk,
                // Session settings are per agent session (inv02 F-18); Archie's live in Settings → Archie.
                settings = if (item.kind != ItemKind.AGENT) ActionState.no(NOT_FOR_ARCHIE) else needsSdk,
                compact = when {
                    busy -> ActionState.no(STOP_FIRST)
                    !connected -> ActionState.no(NOT_CONNECTED)
                    else -> ActionState.OK
                },
                fork = needsSdk,
                delete = needsSdk,
            )
        }
    }
}

/** The orchestrator that runs now (spec 12 §6.11). [here] = it is the Archie view of this device. */
@Immutable
data class RunningArchie(val localId: String, val sdkId: String?, val here: Boolean)

enum class ConflictMode { NEW, RESUME }

enum class ConflictChoice { OPEN, REPLACE, CANCEL }

/** At most one session dialog shows at a time. */
@Immutable
sealed interface SessionDialog {
    data class Rename(val target: SessionTarget, val initial: String, val saving: Boolean = false) : SessionDialog
    data class Delete(val target: SessionTarget, val messageCount: Int?, val stopsLive: Boolean) : SessionDialog
    data class Fork(val target: SessionTarget) : SessionDialog

    /** §6.7: Archie always asks; an agent asks while it is working. */
    data class Close(val key: ItemKey, val title: String, val archie: Boolean) : SessionDialog

    /** The three-action dialog (spec 12 §6.11, inv02 F-25, fixes inv03 §1.7 "crammed into AlertDialog slots"). */
    data class ArchieConflict(val mode: ConflictMode, val resumeSdkId: String?, val running: RunningArchie) : SessionDialog
}

@Immutable
data class Snack(val id: Long, val message: String, val actionLabel: String? = null, val error: Boolean = false)

/** Everything the session dialogs, busy overlay and snackbar render. */
@Immutable
data class SessionsUiState(
    val dialog: SessionDialog? = null,
    /** App-wide busy overlay label (inv02 F-12: "Deleting…", "Forking…", "Starting Archie…"). */
    val busy: String? = null,
    val snack: Snack? = null,
)

/** UI → controller. */
sealed interface SessionsIntent {
    data object NewArchie : SessionsIntent
    data object NewAgent : SessionsIntent
    data class OpenHistory(val session: SessionSummary) : SessionsIntent
    data class RequestClose(val item: WorkspaceItem) : SessionsIntent
    data object ConfirmClose : SessionsIntent
    data class RequestRename(val target: SessionTarget) : SessionsIntent
    data class CommitRename(val title: String) : SessionsIntent
    data class RequestDelete(val target: SessionTarget) : SessionsIntent
    data object ConfirmDelete : SessionsIntent
    data class RequestFork(val target: SessionTarget) : SessionsIntent
    data object ConfirmFork : SessionsIntent
    data class Duplicate(val target: SessionTarget) : SessionsIntent
    data class Compact(val key: ItemKey) : SessionsIntent
    data class ResolveConflict(val choice: ConflictChoice) : SessionsIntent
    data object DismissDialog : SessionsIntent
    data class SnackAction(val id: Long) : SessionsIntent
    data class SnackDismissed(val id: Long) : SessionsIntent
}

/**
 * The history list (IA §3/§5, web `groupSessions`): every word of the query must appear in the title;
 * sessions already in "Open now" (the server's open set, OPEN-1) are left out; the rest is
 * grouped by **local** calendar day with offset-correct parsing (B-03 [HistoryGrouping], fixes the
 * old app's UTC skew, inv03 §1.5).
 */
object HistoryList {
    fun matchesQuery(text: String, query: String): Boolean {
        val words = query.lowercase(Locale.ROOT).split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (words.isEmpty()) return true
        val hay = text.lowercase(Locale.ROOT)
        return words.all { hay.contains(it) }
    }

    fun groups(
        sessions: List<SessionSummary>,
        open: List<WorkspaceItem>,
        query: String,
        now: Instant,
        zone: ZoneId,
        locale: Locale = Locale.getDefault(),
    ): List<HistoryGroup> {
        val openSdk = open.mapNotNull { it.sdkId }.toSet()
        val openLocal = open.mapNotNull { it.localId }.toSet()
        val rows = sessions.filter { s ->
            s.sdkId !in openSdk && (s.localId == null || s.localId !in openLocal) &&
                matchesQuery(SessionTitles.conversationTitle(s.title, s.isOrchestrator), query)
        }
        return HistoryGrouping.group(rows, now, zone, locale)
    }

    /** "Open now" rows that match the search. */
    fun openMatching(items: List<WorkspaceItem>, query: String): List<WorkspaceItem> =
        items.filter { matchesQuery(it.title, query) }
}
