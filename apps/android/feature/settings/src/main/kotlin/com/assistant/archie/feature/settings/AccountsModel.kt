package com.assistant.archie.feature.settings

import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import com.assistant.core.protocol.AccountServiceDto
import com.assistant.core.protocol.EnvChangeDto
import com.assistant.core.protocol.EnvKeyDto
import com.assistant.core.protocol.LoginFlowDto
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.Locale

data class AccountsState(
    val services: List<AccountServiceDto>? = null,
    val loading: Boolean = false,
    val loadError: String? = null,
    /** Action in flight per service ("login", "code", "credentials:<method>", "logout", "verify", "refresh", "cancel", "env:<NAME>"). */
    val busy: Map<String, String> = emptyMap(),
    /** Last failed action per service (the server's detail verbatim). */
    val errors: Map<String, String> = emptyMap(),
    val envKeys: List<EnvKeyDto>? = null,
    val envPath: String = "",
    val envLoading: Boolean = false,
    val envError: String? = null,
    val envBusy: String? = null,
) {
    val anyFlowActive: Boolean get() = services.orEmpty().any { it.flow?.active == true }

    /** One line for the Settings home row (null until the page has loaded). */
    val summary: String?
        get() = services?.let { AccountsModel.summary(it) }
}

/**
 * Settings → Accounts (spec 12 §8.1 Accounts / Env keys; web `accountsStore`): the sign-in of every
 * service on the server and the `context/.env` key manager. Revealed env values are never kept
 * here (ACC-1): [reveal] returns the value to the caller, which holds it in composition state only.
 * An active sign-in flow is polled every 2 s while [watchFlows] runs (the page is on screen, ACC-2).
 */
class AccountsModel(
    private val api: ArchieApi,
    private val messages: SettingsMessages,
    private val scope: CoroutineScope,
    /** A Claude change re-checks the AuthGate's `/api/auth/status`. */
    private val onClaudeChanged: () -> Unit = {},
) {
    private val _state = MutableStateFlow(AccountsState())
    val state: StateFlow<AccountsState> = _state.asStateFlow()
    private var loadJob: Job? = null

    fun load() {
        if (loadJob?.isActive == true) return
        loadJob = scope.launch { loadNow() }
    }

    suspend fun loadNow() {
        _state.update { it.copy(loading = true, loadError = null) }
        when (val r = api.accounts()) {
            is ApiResult.Ok -> _state.update { it.copy(services = r.value.services, loading = false) }
            else -> _state.update { it.copy(loading = false, loadError = r.errorMessage()) }
        }
    }

    fun reset() { _state.value = AccountsState() }

    private fun replace(service: AccountServiceDto) = _state.update { s ->
        s.copy(services = s.services?.map { if (it.id == service.id) service else it })
    }

    private fun setFlow(id: String, flow: LoginFlowDto?) = _state.update { s ->
        s.copy(services = s.services?.map { if (it.id == id) it.copy(flow = flow) else it })
    }

    private fun busy(id: String, action: String?) = _state.update { s ->
        s.copy(busy = if (action == null) s.busy - id else s.busy + (id to action), errors = if (action != null) s.errors - id else s.errors)
    }

    private fun fail(id: String, r: ApiResult<*>) = _state.update { s -> s.copy(busy = s.busy - id, errors = s.errors + (id to (r.errorMessage() ?: "Request failed"))) }

    fun clearError(id: String) = _state.update { it.copy(errors = it.errors - id) }

    /** A snackbar line (e.g. "Link copied"). */
    fun post(text: String) = messages.post(SettingsMessage(text))

    private fun touched(id: String) { if (id == "claude") onClaudeChanged() }

    private suspend inline fun <T> act(id: String, action: String, call: () -> ApiResult<T>, onOk: (T) -> Unit): Boolean {
        busy(id, action)
        val r = call()
        return if (r is ApiResult.Ok) { busy(id, null); onOk(r.value); true } else { fail(id, r); false }
    }

    suspend fun refresh(id: String): Boolean = act(id, "refresh", { api.account(id) }) { replace(it) }

    suspend fun startLogin(id: String, method: String): Boolean = act(id, "login", { api.startLogin(id, method) }) { setFlow(id, it) }

    suspend fun submitCode(id: String, code: String): Boolean {
        var flow: LoginFlowDto? = null
        val ok = act(id, "code", { api.submitLoginCode(id, code.trim()) }) { setFlow(id, it); flow = it }
        flow?.let { afterFlow(id, it) }
        return ok
    }

    suspend fun cancelLogin(id: String): Boolean = act(id, "cancel", { api.cancelLogin(id) }) { setFlow(id, it) }

    fun dismissFlow(id: String) = setFlow(id, null)

    /** One poll of every active flow. */
    suspend fun pollFlows() {
        for (s in _state.value.services.orEmpty()) {
            if (s.flow?.active != true) continue
            val r = api.loginFlow(s.id)
            if (r is ApiResult.Ok) {
                setFlow(s.id, r.value)
                if (!r.value.active) afterFlow(s.id, r.value)
            }
        }
    }

    /** Polls while a flow is active; run it from the page's composition (`LaunchedEffect`). */
    suspend fun watchFlows(intervalMs: Long = FLOW_POLL_MS) {
        while (kotlin.coroutines.coroutineContext.isActive) {
            delay(intervalMs)
            if (_state.value.anyFlowActive) pollFlows()
        }
    }

    private suspend fun afterFlow(id: String, flow: LoginFlowDto) {
        if (flow.status != "succeeded") return
        messages.post(SettingsMessage(flow.message.ifEmpty { "Signed in" }))
        touched(id)
        (api.account(id) as? ApiResult.Ok)?.let { replace(it.value) }
    }

    suspend fun saveCredentials(id: String, method: String, content: String): Boolean =
        act(id, "credentials:$method", { api.saveAccountCredentials(id, method, content) }) {
            replace(it.service); messages.post(SettingsMessage(it.message)); touched(id)
        }

    suspend fun signOut(id: String): Boolean = act(id, "logout", { api.signOutAccount(id) }) {
        replace(it.service); messages.post(SettingsMessage(it.message)); touched(id)
    }

    suspend fun verify(id: String): Boolean = act(id, "verify", { api.verifyAccount(id) }) { replace(it) }

    /** Set a key from a service card (`""` + [remove] = delete it). Keys are shared: refetch every service (ACC-3). */
    suspend fun setKey(id: String, name: String, value: String): Boolean {
        val ok = act(id, "env:$name", { api.putEnv(name, value) }) { notice(it, "$name saved") }
        if (ok) afterEnvChange(id)
        return ok
    }

    suspend fun removeKey(id: String, name: String): Boolean {
        val ok = act(id, "env:$name", { api.deleteEnv(name) }) { notice(it, "$name removed") }
        if (ok) afterEnvChange(id)
        return ok
    }

    private fun notice(change: EnvChangeDto, what: String) =
        messages.post(SettingsMessage(if (change.applies == "backend_restart") "$what. ${change.note}" else what))

    private suspend fun afterEnvChange(id: String?) {
        loadNow()
        if (_state.value.envKeys != null) loadEnvNow()
        if (id != null) touched(id)
    }

    // ───────────── env keys ─────────────

    fun loadEnv() { scope.launch { loadEnvNow() } }

    suspend fun loadEnvNow() {
        _state.update { it.copy(envLoading = true, envError = null) }
        when (val r = api.envKeys()) {
            is ApiResult.Ok -> _state.update { it.copy(envKeys = r.value.keys, envPath = r.value.path, envLoading = false) }
            else -> _state.update { it.copy(envLoading = false, envError = r.errorMessage()) }
        }
    }

    /** The full value of one key (shown only where the user asked; never stored in [state]). */
    suspend fun reveal(name: String): String? = when (val r = api.revealEnv(name)) {
        is ApiResult.Ok -> r.value.value
        else -> { _state.update { it.copy(envError = r.errorMessage()) }; null }
    }

    private suspend fun envAction(name: String, what: String, call: suspend () -> ApiResult<EnvChangeDto>): Boolean {
        _state.update { it.copy(envBusy = name, envError = null) }
        return when (val r = call()) {
            is ApiResult.Ok -> {
                _state.update { it.copy(envBusy = null) }
                notice(r.value, what)
                afterEnvChange(if (name in CLAUDE_KEYS) "claude" else null)
                true
            }
            else -> { _state.update { it.copy(envBusy = null, envError = r.errorMessage()) }; false }
        }
    }

    suspend fun createKey(name: String, value: String) = envAction(name, "$name added") { api.createEnv(name, value) }
    suspend fun updateKey(name: String, value: String) = envAction(name, "$name saved") { api.putEnv(name, value) }
    suspend fun deleteKey(name: String) = envAction(name, "$name deleted") { api.deleteEnv(name) }

    fun clearEnvError() = _state.update { it.copy(envError = null) }

    companion object {
        const val FLOW_POLL_MS = 2_000L
        private val CLAUDE_KEYS = setOf("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        private val NAME_RE = Regex("^[A-Z_][A-Z0-9_]*$")
        private val json = Json { ignoreUnknownKeys = true }
        private val DAY = DateTimeFormatter.ofPattern("d MMM yyyy", Locale.ENGLISH).withZone(ZoneOffset.UTC)

        val GROUPS = listOf("harness" to "Agent harnesses", "api" to "Voice & AI APIs", "other" to "Other")

        val STATE_LABEL = mapOf(
            "signed_in" to "Signed in", "signed_out" to "Not signed in", "expired" to "Expired",
            "unavailable" to "Unavailable", "unknown" to "Unknown",
        )

        val FLOW_LABEL = mapOf(
            "starting" to "Starting…", "waiting" to "Waiting for you", "verifying" to "Checking…", "succeeded" to "Signed in",
            "failed" to "Failed", "cancelled" to "Cancelled", "expired" to "Timed out",
        )

        fun summary(services: List<AccountServiceDto>): String {
            val harnesses = services.filter { it.group == "harness" }
            if (harnesses.isEmpty()) return "${services.count { it.state == "signed_in" }} of ${services.size} signed in"
            val signedIn = harnesses.count { it.state == "signed_in" }
            return if (signedIn == harnesses.size) "All agent harnesses signed in" else "$signedIn of ${harnesses.size} agent harnesses signed in"
        }

        /** "until 9 Oct 2027" / "expired 2 Oct 2026"; null for a missing or bad date. */
        fun formatExpiry(iso: String?, now: Instant = Instant.now()): String? {
            val t = iso?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return null
            return (if (t.isBefore(now)) "expired " else "until ") + DAY.format(t)
        }

        /** null when valid, else the message. */
        fun envNameError(name: String, existing: Collection<String> = emptyList()): String? {
            val n = name.trim()
            return when {
                n.isEmpty() -> "Enter a name"
                !NAME_RE.matches(n) -> "Capital letters, digits and underscores; not starting with a digit (MY_API_KEY)"
                n in existing -> "$n already exists"
                else -> null
            }
        }

        /** Client-side check before sending a credentials file. */
        fun jsonError(text: String): String? {
            val t = text.trim()
            if (t.isEmpty()) return "Paste the file contents"
            val parsed = runCatching { json.parseToJsonElement(t) }.getOrNull()
                ?: return "That isn't valid JSON. Copy the whole file, including the braces."
            return if (parsed is JsonObject) null else "The file is a JSON object ({ … })"
        }
    }
}
