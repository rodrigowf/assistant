package com.assistant.archie.feature.settings

import com.assistant.core.data.ConnectionStatus
import com.assistant.core.data.ServerConfigRepository
import com.assistant.core.network.ArchieApi
import com.assistant.core.settings.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.distinctUntilChangedBy
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filter

/** Everything the settings feature is built from (the shell adapts the app graph to it). */
class SettingsDeps(
    val settings: SettingsStore,
    val serverConfig: ServerConfigRepository,
    val api: ArchieApi,
    val connection: ConnectionControl,
    val platform: DevicePlatform,
    val appearance: AppearanceStore,
    val scope: CoroutineScope,
    val sessions: SessionControl = SessionControl.None,
    val voice: VoiceStatusSource = VoiceStatusSource.None,
)

/**
 * The settings feature, process-scoped like the repositories it reads (spec 14 §2.1: domain state
 * lives in process-scoped objects, so the cached server config and catalogs are shared by every
 * settings page and the session sheet, and survive Activity recreation). Screens observe these
 * models directly; UI-local state (open dialogs, drafts) lives in `rememberSaveable`.
 */
class SettingsFeature(val deps: SettingsDeps) {
    val messages = SettingsMessages()
    val server = ServerSettingsModel(deps.serverConfig, deps.api, messages, deps.scope)
    val auth = AuthModel(deps.api, messages, deps.scope)
    val accounts = AccountsModel(deps.api, messages, deps.scope, onClaudeChanged = { auth.check() })
    val device = DeviceSettingsModel(deps.settings, deps.appearance, deps.platform, messages, deps.scope)
    val connection = ConnectionModel(deps.connection, deps.settings, messages, deps.scope)
    val permissions = PermissionCenter(deps.platform)
    val platform: DevicePlatform get() = deps.platform
    val voice: VoiceStatusSource get() = deps.voice

    init {
        // A new server (T-15): its config, catalogs and sign-in state are different.
        deps.connection.status.map { it.serverUrl }.filter { it != null }.distinctUntilChanged().drop(1)
            .onEach { server.reset(); auth.reset(); accounts.reset() }
            .launchIn(deps.scope)
        // AuthGate: check the sign-in each time this device (re)connects to a server.
        deps.connection.status.distinctUntilChangedBy { it.phase to it.serverUrl }
            .onEach { if (it.phase == ConnectionStatus.Phase.CONNECTED) auth.check() }
            .launchIn(deps.scope)
    }

    fun sessionController(localId: String, scope: CoroutineScope) =
        SessionSettingsController(localId, deps.api, deps.sessions, messages, scope)
}
