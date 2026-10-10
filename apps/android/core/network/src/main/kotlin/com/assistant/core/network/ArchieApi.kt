package com.assistant.core.network

import com.assistant.core.model.AuthStatus
import com.assistant.core.model.HarnessInfo
import com.assistant.core.model.ConfigPatch
import com.assistant.core.model.MemoryNode
import com.assistant.core.model.PoolSession
import com.assistant.core.model.ServerConfig
import com.assistant.core.model.SessionConfig
import com.assistant.core.model.SessionSummary
import com.assistant.core.model.VisualInfo
import com.assistant.core.protocol.AccountActionDto
import com.assistant.core.protocol.AccountCredentialsRequest
import com.assistant.core.protocol.AccountServiceDto
import com.assistant.core.protocol.AccountsDto
import com.assistant.core.protocol.AgentsDto
import com.assistant.core.protocol.AuthStatusDto
import com.assistant.core.protocol.CastProbeDto
import com.assistant.core.protocol.CastRequest
import com.assistant.core.protocol.ConfigDto
import com.assistant.core.protocol.CredentialsRequest
import com.assistant.core.protocol.DebugLogRequest
import com.assistant.core.protocol.CastResponse
import com.assistant.core.protocol.DropLastNRequest
import com.assistant.core.protocol.EnvChangeDto
import com.assistant.core.protocol.EnvCreateRequest
import com.assistant.core.protocol.EnvListDto
import com.assistant.core.protocol.EnvRevealDto
import com.assistant.core.protocol.EnvValueRequest
import com.assistant.core.protocol.GoogleVoiceModelsDto
import com.assistant.core.protocol.HarnessProvidersDto
import com.assistant.core.protocol.HarnessesDto
import com.assistant.core.protocol.InjectRequest
import com.assistant.core.protocol.InjectResponse
import com.assistant.core.protocol.LoginCodeRequest
import com.assistant.core.protocol.LoginFlowDto
import com.assistant.core.protocol.LoginRequest
import com.assistant.core.protocol.McpServersDto
import com.assistant.core.protocol.MemoryNodeDto
import com.assistant.core.protocol.MessagePreviewDto
import com.assistant.core.protocol.OrchestratorModelsDto
import com.assistant.core.protocol.PaginatedMessagesDto
import com.assistant.core.protocol.PoolSessionDto
import com.assistant.core.protocol.QwenModelsDto
import com.assistant.core.protocol.RenameRequest
import com.assistant.core.protocol.RestJson
import com.assistant.core.protocol.SessionConfigDto
import com.assistant.core.protocol.SessionIdResponse
import com.assistant.core.protocol.SessionInfoDto
import com.assistant.core.protocol.SkillsDto
import com.assistant.core.protocol.VisualizationInfoDto
import com.assistant.core.protocol.VisualizationRenameRequest
import com.assistant.core.protocol.VoiceModelsDto
import com.assistant.core.protocol.toDto
import com.assistant.core.protocol.toModel
import com.assistant.core.protocol.toPutBody
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.encodeToJsonElement
import okhttp3.Request

/**
 * Every REST call of inv01 §3 the apps use (spec 14 §1.2). Path ids: `sdkId` for history calls
 * (ID-3), `localId` for `close` (inv01 §3.2). Nothing here is called from lifecycle paths:
 * [closePoolSession] is an **explicit** user action only (decision P-1).
 */
class ArchieApi(private val rest: RestCaller) {
    constructor(stack: HttpStack, serverUrl: () -> String) : this(RestCaller(stack, serverUrl))

    // ───────────── auth (inv01 §3.1) ─────────────
    suspend fun authStatus(): ApiResult<AuthStatus> =
        rest.getJson<AuthStatusDto>(rest.url("/api/auth/status")).map { it.toModel() }

    suspend fun authLogin(): ApiResult<AuthStatus> =
        rest.sendJson<AuthStatusDto>("POST", rest.url("/api/auth/login"), null).map { it.toModel() }

    suspend fun authCredentials(credentialsJson: String): ApiResult<AuthStatus> =
        rest.sendJson<AuthStatusDto>("POST", rest.url("/api/auth/credentials"), json(CredentialsRequest(credentialsJson)))
            .map { it.toModel() }

    // ───────────── Settings → Accounts (spec 12 §8.1) ─────────────
    suspend fun accounts(): ApiResult<AccountsDto> = rest.getJson(rest.url("/api/accounts"))

    suspend fun account(id: String): ApiResult<AccountServiceDto> = rest.getJson(accountUrl(id))

    /** Starts the CLI login on the server; answers once the URL is known (≤ ~12 s). 409 = another flow. */
    suspend fun startLogin(id: String, method: String): ApiResult<LoginFlowDto> =
        rest.sendJson("POST", accountUrl(id, "login"), json(LoginRequest(method)))

    suspend fun loginFlow(id: String): ApiResult<LoginFlowDto> = rest.getJson(accountUrl(id, "login"))

    /** Waits ≤ ~15 s for the CLI's verdict. */
    suspend fun submitLoginCode(id: String, code: String): ApiResult<LoginFlowDto> =
        rest.sendJson("POST", accountUrl(id, "login", "code"), json(LoginCodeRequest(code)))

    suspend fun cancelLogin(id: String): ApiResult<LoginFlowDto> =
        rest.call(Request.Builder().url(accountUrl(id, "login")).delete().build()) { RestJson.decodeFromString<LoginFlowDto>(it.body!!.string()) }

    suspend fun saveAccountCredentials(id: String, method: String, content: String): ApiResult<AccountActionDto> =
        rest.sendJson("POST", accountUrl(id, "credentials"), json(AccountCredentialsRequest(method, content)))

    suspend fun signOutAccount(id: String): ApiResult<AccountActionDto> = rest.sendJson("POST", accountUrl(id, "logout"), null)

    suspend fun verifyAccount(id: String): ApiResult<AccountServiceDto> = rest.sendJson("POST", accountUrl(id, "verify"), null)

    suspend fun envKeys(): ApiResult<EnvListDto> = rest.getJson(rest.url("/api/env"))

    /** The only call that returns a full value (spec 12 ACC-1). */
    suspend fun revealEnv(name: String): ApiResult<EnvRevealDto> = rest.sendJson("POST", envUrl(name, "reveal"), null)

    suspend fun createEnv(name: String, value: String): ApiResult<EnvChangeDto> =
        rest.sendJson("POST", rest.url("/api/env"), json(EnvCreateRequest(name, value)))

    /** Create or update. */
    suspend fun putEnv(name: String, value: String): ApiResult<EnvChangeDto> = rest.sendJson("PUT", envUrl(name), json(EnvValueRequest(value)))

    suspend fun deleteEnv(name: String): ApiResult<EnvChangeDto> =
        rest.call(Request.Builder().url(envUrl(name)).delete().build()) { RestJson.decodeFromString<EnvChangeDto>(it.body!!.string()) }

    private fun accountUrl(id: String, vararg tail: String) = rest.url("/api/accounts") {
        addPathSegment(id); tail.forEach { addPathSegment(it) }
    }

    private fun envUrl(name: String, vararg tail: String) = rest.url("/api/env") {
        addPathSegment(name); tail.forEach { addPathSegment(it) }
    }

    // ───────────── sessions (inv01 §3.2) ─────────────
    suspend fun listSessions(): ApiResult<List<SessionSummary>> =
        rest.getJson<List<SessionInfoDto>>(rest.url("/api/sessions")).map { l -> l.map { it.toModel() } }

    suspend fun livePool(): ApiResult<List<PoolSession>> =
        rest.getJson<List<PoolSessionDto>>(rest.url("/api/sessions/pool/live")).map { l -> l.map { it.toModel() } }

    suspend fun messages(sdkId: String, limit: Int = 50, before: Int? = null): ApiResult<PaginatedMessagesDto> =
        rest.getJson(rest.url("/api/sessions") {
            addPathSegment(sdkId); addPathSegment("messages")
            addQueryParameter("limit", limit.coerceIn(1, 200).toString())
            before?.let { addQueryParameter("before", it.toString()) }
        })

    suspend fun preview(sdkId: String, max: Int = 5): ApiResult<List<MessagePreviewDto>> =
        rest.getJson(rest.url("/api/sessions") {
            addPathSegment(sdkId); addPathSegment("preview"); addQueryParameter("max", max.toString())
        })

    suspend fun sessionConfig(sdkId: String): ApiResult<SessionConfig> =
        rest.getJson<SessionConfigDto>(sessionUrl(sdkId, "config")).map { it.toModel() }

    /** Keys named in [inherit] are sent as JSON `null` (spec 12 §6.14). */
    suspend fun putSessionConfig(sdkId: String, config: SessionConfig, inherit: Set<String> = emptySet()): ApiResult<SessionConfig> =
        rest.sendJson<SessionConfigDto>("PUT", sessionUrl(sdkId, "config"), config.toPutBody(inherit)).map { it.toModel() }

    /** 204; 404 is tolerated by callers (spec 12 §6.6). */
    suspend fun rename(sdkId: String, title: String): ApiResult<Unit> =
        rest.sendJson("PATCH", sessionUrl(sdkId, "rename"), json(RenameRequest(title)))

    suspend fun delete(sdkId: String): ApiResult<Unit> =
        rest.call(Request.Builder().url(sessionUrl(sdkId)).delete().build()) { }

    suspend fun duplicate(sdkId: String): ApiResult<String> =
        rest.sendJson<SessionIdResponse>("POST", sessionUrl(sdkId, "duplicate"), null).map { it.sessionId }

    suspend fun truncate(sdkId: String, dropLastN: Int): ApiResult<String> =
        rest.sendJson<SessionIdResponse>("POST", sessionUrl(sdkId, "truncate"), json(DropLastNRequest(dropLastN))).map { it.sessionId }

    suspend fun fork(sdkId: String, dropLastN: Int): ApiResult<String> =
        rest.sendJson<SessionIdResponse>("POST", sessionUrl(sdkId, "fork"), json(DropLastNRequest(dropLastN))).map { it.sessionId }

    suspend fun inject(text: String, localId: String? = null, sdkId: String? = null): ApiResult<InjectResponse> =
        rest.sendJson("POST", rest.url("/api/sessions/inject"), json(InjectRequest(text, localId, sdkId)))

    /**
     * `POST /api/sessions/{localId}/close` (204; unknown ids are a no-op). Closes the session **for
     * every device**. Only for an explicit close in the UI (P-1); never from unload/teardown.
     */
    suspend fun closePoolSession(localId: String): ApiResult<Unit> =
        rest.call(Request.Builder().url(sessionUrl(localId, "close")).post(rest.emptyBody()).build()) { }

    /**
     * `POST /api/sessions/{localId}/permission` (spec 12 §6.9): answers an agent's pending
     * permission without its chat socket (Agent approvals list, notification actions). 404 = not in
     * the pool (detail "Not Found" = a server without this route); 409 = already answered / unknown.
     */
    suspend fun resolvePermission(localId: String, requestId: String, allow: Boolean, message: String? = null): ApiResult<Unit> =
        rest.sendJson("POST", sessionUrl(localId, "permission"), buildJsonObject {
            put("request_id", requestId)
            put("decision", if (allow) "allow" else "deny")
            if (message != null) put("message", message)
        })

    // ───────────── visualizations (inv01 §3.3, BX-2) ─────────────
    suspend fun visualizations(): ApiResult<List<VisualInfo>> =
        rest.getJson<List<VisualizationInfoDto>>(rest.url("/api/visualizations")).map { l -> l.map { it.toModel() } }

    suspend fun renameVisualization(path: String, title: String): ApiResult<Unit> =
        rest.sendJson("PATCH", rest.url("/api/visualizations/rename"), json(VisualizationRenameRequest(path, title)))

    suspend fun castProbe(): ApiResult<CastProbeDto> = rest.getJson(rest.url("/api/visualizations/cast"))

    suspend fun castVisualization(path: String): ApiResult<CastResponse> =
        rest.sendJson("POST", rest.url("/api/visualizations/cast"), json(CastRequest(path)))

    // ───────────── memory (inv01 §3.4) ─────────────
    suspend fun memoryTree(): ApiResult<List<MemoryNode>> =
        rest.getJson<List<MemoryNodeDto>>(rest.url("/api/memory/tree")).map { l -> l.map { it.toModel() } }

    /** Raw markdown of `GET /memory/{path}`. */
    suspend fun memoryDocument(path: String): ApiResult<String> =
        rest.call(Request.Builder().url(rest.url("/memory") { addPathSegments(path.trim('/')) }).get().build()) {
            it.body?.string().orEmpty()
        }

    // ───────────── global config (inv01 §3.6, spec 12 §8.1) ─────────────
    suspend fun config(): ApiResult<ServerConfig> = rest.getJson<ConfigDto>(rest.url("/api/config")).map { it.toModel() }

    /** Partial PUT; the response replaces the local copy (CFG-1). 400 → `HttpError.detail` (CFG-2). */
    suspend fun updateConfig(patch: ConfigPatch): ApiResult<ServerConfig> =
        rest.sendJson<ConfigDto>("PUT", rest.url("/api/config"), json(patch.toDto())).map { it.toModel() }

    suspend fun providers(): ApiResult<HarnessProvidersDto> = rest.getJson(rest.url("/api/config/providers"))
    suspend fun qwenModels(): ApiResult<QwenModelsDto> = rest.getJson(rest.url("/api/config/harness/qwen/models"))

    /** The Qwen model rows as raw JSON (ids or objects), for the harness fallback catalog. */
    suspend fun qwenModelRows(): ApiResult<List<JsonElement>> =
        rest.getJson<JsonObject>(rest.url("/api/config/harness/qwen/models")).map { (it["models"] as? JsonArray).orEmpty() }

    /**
     * `GET /api/config/harnesses` (spec 12 §8.1): every harness with its catalog (models + options).
     * [refresh] asks the server to rebuild its ~5 min catalog cache. Older servers answer 404
     * (callers fall back to [providers] + [qwenModelRows]).
     */
    suspend fun harnesses(refresh: Boolean = false): ApiResult<List<HarnessInfo>> =
        rest.getJson<HarnessesDto>(rest.url("/api/config/harnesses") { if (refresh) addQueryParameter("refresh", "true") })
            .map { it.toModel() }
    suspend fun googleVoiceModels(endpoint: String? = null): ApiResult<GoogleVoiceModelsDto> =
        rest.getJson(rest.url("/api/config/voice/google/models") { endpoint?.let { addQueryParameter("endpoint", it) } })

    // ───────────── catalogs (inv01 §3.7, §3.8) ─────────────
    suspend fun skills(): ApiResult<SkillsDto> = rest.getJson(rest.url("/api/skills"))
    suspend fun agents(): ApiResult<AgentsDto> = rest.getJson(rest.url("/api/agents"))
    suspend fun mcpServers(): ApiResult<McpServersDto> = rest.getJson(rest.url("/api/mcp/servers"))
    suspend fun orchestratorModels(): ApiResult<OrchestratorModelsDto> = rest.getJson(rest.url("/api/orchestrator/models"))
    suspend fun voiceModels(): ApiResult<VoiceModelsDto> = rest.getJson(rest.url("/api/orchestrator/voice/models"))

    // ───────────── debug (inv01 §3.9) ─────────────
    suspend fun debugLog(level: String, message: String, ts: String? = null): ApiResult<Unit> =
        rest.sendJson("POST", rest.url("/api/debug/log"), json(DebugLogRequest(level, message, ts)))

    private fun sessionUrl(id: String, vararg tail: String) = rest.url("/api/sessions") {
        addPathSegment(id); tail.forEach { addPathSegment(it) }
    }

    private inline fun <reified T> json(value: T): JsonElement = RestJson.encodeToJsonElement(value)
}
