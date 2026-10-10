package com.assistant.archie.shell

import androidx.navigation3.runtime.NavKey
import kotlinx.serialization.Serializable

/*
 * Navigation 3 keys (spec 14 §2.4). The back stack is plain observable state; Navigation 3 saves
 * these @Serializable keys across process death. Workspace tabs are NOT routes: selecting a tab
 * changes OpenSessionsRepository state, so Back from a chat closes the drawer or the app instead of
 * walking through previously viewed tabs.
 */

/** The conversation workspace (home). Always the root of the back stack. */
@Serializable data object Workspace : NavKey

/** All past conversations (full screen; B-06 replaces the placeholder). */
@Serializable data object History : NavKey

@Serializable data object MemoryTree : NavKey

/** A memory document as a detail screen (Compact). On Expanded documents open as tabs. */
@Serializable data class MemoryDoc(val path: String) : NavKey

@Serializable data object VisualsList : NavKey

@Serializable data class VisualDoc(val path: String) : NavKey

@Serializable data object SettingsHome : NavKey

/** IA §7 settings pages. */
@Serializable
enum class SettingsPageId {
    CONNECTION, AUDIO, WAKE_WORD, APPEARANCE, NOTIFICATIONS, PERMISSIONS, BACKGROUND,
    CONVERSATION_MODEL, VOICE, VOICE_TUNING, AGENT_SESSIONS, WORKING_DIRECTORIES, MCP_SERVERS, ACCOUNT,
    ABOUT,
}

@Serializable data class SettingsPage(val id: SettingsPageId) : NavKey

/** Per-session settings (IA §7 "Session settings", from the ⋮ menu). */
@Serializable data class SessionSettings(val localId: String) : NavKey
