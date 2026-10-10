package com.assistant.archie.feature.settings

import com.assistant.core.model.AuthStatus
import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import com.assistant.core.protocol.LoginFlowDto
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

enum class AuthPhase { IDLE, CHECKING, SIGNING_IN, SAVING }

data class AuthState(
    val status: AuthStatus? = null,
    val phase: AuthPhase = AuthPhase.IDLE,
    /** Verbatim message of the last failed status check (status stays as it was). */
    val checkError: String? = null,
    /** Message of the last failed sign-in / credentials attempt. */
    val actionError: String? = null,
    /** "Not now" on the gate, for this app process. */
    val gateDismissed: Boolean = false,
    /** The link sign-in in progress (`/api/accounts/claude/login`, method `token`), or its result. */
    val flow: LoginFlowDto? = null,
) {
    /** The AuthGate covers the app only for a known "not signed in" (a failed check never blocks). */
    val gateBlocked: Boolean get() = status != null && !status.authenticated && !gateDismissed

    /** One line for the Settings home row until Accounts has loaded ("Claude · signed in"). */
    val summary: String
        get() = when {
            phase == AuthPhase.CHECKING && status == null -> "Claude · checking…"
            status == null -> if (checkError != null) "Claude · couldn't check" else "Claude"
            status.authenticated -> "Claude · signed in"
            else -> "Claude · not signed in"
        }
}

sealed interface CredentialsCheck {
    data class Ok(val json: String) : CredentialsCheck
    data class Bad(val error: String) : CredentialsCheck
}

/**
 * Claude CLI sign-in state of the backend (inv01 §3.1, inv02 §1.11, spec 12 §8.1), a port of the
 * web `authStore`. Used by the AuthGate and the Settings home row. A failed status check is "unknown",
 * not "signed out"; server errors are shown verbatim; credentials can be replaced while signed in
 * (the headless check does not look at expiry, so an expired token still reads "signed in").
 */
class AuthModel(
    private val api: ArchieApi,
    private val messages: SettingsMessages,
    private val scope: CoroutineScope,
) {
    private val _state = MutableStateFlow(AuthState())
    val state: StateFlow<AuthState> = _state.asStateFlow()
    private var inFlight: Job? = null

    /** `GET /api/auth/status`. On failure the previous status is kept. */
    fun check() {
        if (inFlight?.isActive == true) return
        _state.update { it.copy(phase = AuthPhase.CHECKING, checkError = null) }
        inFlight = scope.launch { checkNow() }
    }

    suspend fun checkNow() {
        when (val r = api.authStatus()) {
            is ApiResult.Ok -> _state.update { it.copy(status = r.value, phase = AuthPhase.IDLE) }
            else -> _state.update { it.copy(phase = AuthPhase.IDLE, checkError = r.errorMessage()) }
        }
    }

    /**
     * The link sign-in (works on a headless server): the server runs `claude setup-token`; the app
     * shows its URL (opened in the browser), the user signs in and pastes back the code; the 1-year
     * token is saved on the server. A server without `/api/accounts` falls back to [signIn] when
     * it has a screen.
     */
    suspend fun startLink() {
        _state.update { it.copy(phase = AuthPhase.SIGNING_IN, actionError = null) }
        when (val r = api.startLogin("claude", LINK_METHOD)) {
            is ApiResult.Ok -> _state.update {
                it.copy(flow = r.value, phase = AuthPhase.IDLE, actionError = r.value.message.takeIf { r.value.status == "failed" })
            }
            is ApiResult.HttpError -> if (r.code == 404 && _state.value.status?.headless == false) {
                _state.update { it.copy(phase = AuthPhase.IDLE) }
                signIn()
            } else {
                _state.update { it.copy(phase = AuthPhase.IDLE, actionError = r.errorMessage()) }
            }
            else -> _state.update { it.copy(phase = AuthPhase.IDLE, actionError = r.errorMessage()) }
        }
    }

    suspend fun submitLinkCode(code: String) {
        _state.update { it.copy(phase = AuthPhase.SIGNING_IN, actionError = null) }
        when (val r = api.submitLoginCode("claude", code.trim())) {
            is ApiResult.Ok -> {
                _state.update { it.copy(flow = r.value, phase = AuthPhase.IDLE, actionError = r.value.message.takeIf { r.value.status == "failed" }) }
                afterLink(r.value)
            }
            else -> _state.update { it.copy(phase = AuthPhase.IDLE, actionError = r.errorMessage()) }
        }
    }

    /** One poll while the flow is active (the panel polls every 2 s while shown). */
    suspend fun pollLink() {
        val before = _state.value.flow ?: return
        if (!before.active) return
        val r = api.loginFlow("claude") as? ApiResult.Ok ?: return
        if (r.value.id != before.id || _state.value.flow?.id != before.id) return // superseded
        _state.update { it.copy(flow = r.value) }
        if (!r.value.active) afterLink(r.value)
    }

    suspend fun cancelLink() {
        api.cancelLogin("claude")
        _state.update { it.copy(flow = null, actionError = null) }
    }

    private suspend fun afterLink(flow: LoginFlowDto) {
        if (flow.status != "succeeded") return
        messages.post(SettingsMessage(SIGNED_IN))
        checkNow()
    }

    /**
     * Legacy `POST /api/auth/login` (servers without `/api/accounts`, non-headless only): runs
     * `claude setup-token` on the server and blocks until it exits.
     */
    suspend fun signIn(): Boolean {
        _state.update { it.copy(phase = AuthPhase.SIGNING_IN, actionError = null) }
        return when (val r = api.authLogin()) {
            is ApiResult.Ok -> {
                val ok = r.value.authenticated
                _state.update {
                    it.copy(
                        status = r.value,
                        phase = AuthPhase.IDLE,
                        actionError = if (ok) null else "Sign-in didn't finish on the server. Try again, or paste credentials.",
                    )
                }
                if (ok) messages.post(SettingsMessage(SIGNED_IN))
                ok
            }
            else -> { _state.update { it.copy(phase = AuthPhase.IDLE, actionError = r.errorMessage()) }; false }
        }
    }

    /** `POST /api/auth/credentials`. */
    suspend fun submitCredentials(text: String): Boolean {
        when (val c = checkCredentialsText(text)) {
            is CredentialsCheck.Bad -> { _state.update { it.copy(actionError = c.error) }; return false }
            is CredentialsCheck.Ok -> {
                _state.update { it.copy(phase = AuthPhase.SAVING, actionError = null) }
                return when (val r = api.authCredentials(c.json)) {
                    is ApiResult.Ok -> {
                        val ok = r.value.authenticated
                        _state.update {
                            it.copy(
                                status = r.value,
                                phase = AuthPhase.IDLE,
                                actionError = if (ok) null else "Invalid credentials: the server didn't accept them.",
                            )
                        }
                        if (ok) messages.post(SettingsMessage(SIGNED_IN))
                        ok
                    }
                    else -> {
                        _state.update { it.copy(phase = AuthPhase.IDLE, actionError = "Failed to set credentials: ${r.errorMessage()}") }
                        false
                    }
                }
            }
        }
    }

    fun clearError() = _state.update { it.copy(actionError = null) }
    fun dismissGate() = _state.update { it.copy(gateDismissed = true) }

    /** A new server: forget the old server's status. */
    fun reset() { _state.update { AuthState(gateDismissed = it.gateDismissed) } }

    companion object {
        const val SIGNED_IN = "Signed in to Claude"
        private const val LINK_METHOD = "token"
        private val json = Json { ignoreUnknownKeys = true }

        /** Client-side check before sending (mirrors `manager/auth.py`: `claudeAiOauth.accessToken`). */
        fun checkCredentialsText(text: String): CredentialsCheck {
            val t = text.trim()
            if (t.isEmpty()) return CredentialsCheck.Bad("Paste the contents of .credentials.json")
            val parsed = try { json.parseToJsonElement(t) } catch (_: Exception) {
                return CredentialsCheck.Bad("That isn't valid JSON. Copy the whole file, including the braces.")
            }
            val token = ((parsed as? JsonObject)?.get("claudeAiOauth") as? JsonObject)?.get("accessToken") as? JsonPrimitive
            if (token == null || !token.isString || token.content.isEmpty()) {
                return CredentialsCheck.Bad("Invalid credentials: the file has no claudeAiOauth.accessToken.")
            }
            return CredentialsCheck.Ok(t)
        }
    }
}
