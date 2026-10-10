package com.assistant.archie.feature.settings

import android.content.Intent
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.emptyPreferences
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.data.ServerConfigRepository
import com.assistant.core.model.AudioOutput
import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import com.assistant.core.network.CertificateInfo
import com.assistant.core.network.HttpStack
import com.assistant.core.settings.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import java.util.concurrent.CopyOnWriteArrayList

/** In-memory DataStore for [SettingsStore]. */
class MemoryDataStore(initial: Preferences = emptyPreferences()) : DataStore<Preferences> {
    private val state = MutableStateFlow(initial)
    private val mutex = Mutex()
    override val data: Flow<Preferences> = state
    override suspend fun updateData(transform: suspend (t: Preferences) -> Preferences): Preferences = mutex.withLock {
        transform(state.value).also { state.value = it }
    }
}

/** The web mock server's `GET /api/config/harnesses` body (`apps/web/mock-server/data/harnesses.json`). */
fun harnessCatalogsFixture(): String =
    java.io.File(requireNotNull(System.getProperty("archie.harnessCatalogs")) { "archie.harnessCatalogs not set" }).readText()

fun fixture(name: String): String =
    requireNotNull(SettingsBackend::class.java.classLoader!!.getResource("jetson/$name")) { "missing fixture $name" }.readText()

/**
 * The settings slice of an Archie backend on MockWebServer, answering with the live Jetson's GET
 * responses (captured read-only on 2026-10-04, `src/test/resources/jetson/`). `PUT /api/config`
 * merges the body into the stored config like the server, or fails with [failNextPut] (status +
 * `detail`, which the UI must show verbatim). Never talks to the real Jetson.
 */
class SettingsBackend {
    val server = MockWebServer()
    private val json = Json { ignoreUnknownKeys = true }
    @Volatile var config: JsonObject = json.parseToJsonElement(fixture("config.json")) as JsonObject
    @Volatile var auth: String = fixture("auth_status.json")
    @Volatile var mcp: String = fixture("mcp_servers.json")
    @Volatile var sessionConfig: String = """{"working_directory":null,"enabled_mcps":null,"chrome_extension":null,"provider":null,"harness_model":null,"harness_options":null}"""
    /** `GET /api/config/harnesses`; null = an older server (404 → providers + Qwen models fallback). */
    @Volatile var harnesses: String? = harnessCatalogsFixture()
    @Volatile var failNextPut: Pair<Int, String>? = null
    /** `GET /api/accounts` and `GET /api/env` (synthetic, `src/test/resources/jetson/`). */
    @Volatile var accounts: String = fixture("accounts.json")
    @Volatile var envKeys: String = fixture("env.json")
    /** `GET /api/accounts/{id}/login` while a flow runs; set by the test. */
    @Volatile var loginFlow: String? = null
    /** Bodies of the accounts / env writes, as "METHOD path body". */
    val accountWrites = CopyOnWriteArrayList<String>()
    val puts = CopyOnWriteArrayList<String>()
    val requests = CopyOnWriteArrayList<String>()

    val url: String get() = "http://${server.hostName}:${server.port}"

    fun start(): SettingsBackend {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                requests += "${request.method} $path"
                val body = request.body.readUtf8()
                return when {
                    request.method == "PUT" && path == "/api/config" -> put(body)
                    path == "/api/config" -> ok(config.toString())
                    path == "/api/orchestrator/models" -> ok(fixture("orchestrator_models.json"))
                    path == "/api/orchestrator/voice/models" -> ok(fixture("voice_models.json"))
                    path.startsWith("/api/config/voice/google/models") -> ok(fixture("google_models_vertex.json"))
                    path.startsWith("/api/config/harnesses") -> harnesses?.let { ok(it) } ?: MockResponse().setResponseCode(404)
                    path == "/api/config/providers" -> ok(fixture("providers.json"))
                    path == "/api/config/harness/qwen/models" -> ok(fixture("qwen_models.json"))
                    path == "/api/mcp/servers" -> ok(mcp)
                    path == "/api/skills" -> ok(fixture("skills.json"))
                    path == "/api/agents" -> ok(fixture("agents.json"))
                    path == "/api/auth/status" -> ok(auth)
                    path.startsWith("/api/accounts") || path.startsWith("/api/env") -> accountsRoute(request.method.orEmpty(), path, body)
                    path == "/api/auth/credentials" -> ok("""{"authenticated":true,"auth_url":null,"headless":true}""")
                    path.matches(Regex("/api/sessions/[^/]+/config")) -> {
                        if (request.method == "PUT") { puts += body; sessionConfig = mergeSession(body) }
                        ok(sessionConfig)
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        return this
    }

    private fun put(body: String): MockResponse {
        puts += body
        failNextPut?.let { (code, detail) ->
            failNextPut = null
            return MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody("""{"detail":${kotlinx.serialization.json.JsonPrimitive(detail)}}""")
        }
        val patch = json.parseToJsonElement(body) as JsonObject
        val merged = LinkedHashMap<String, kotlinx.serialization.json.JsonElement>(config + patch)
        // Like the server: harness_model merges per provider; harness_options per key, null deletes it.
        (patch["harness_model"] as? JsonObject)?.let { p -> merged["harness_model"] = JsonObject(((config["harness_model"] as? JsonObject) ?: JsonObject(emptyMap())) + p) }
        (patch["harness_options"] as? JsonObject)?.let { p ->
            val all = LinkedHashMap((config["harness_options"] as? JsonObject) ?: JsonObject(emptyMap()))
            for ((prov, opts) in p) {
                val cur = LinkedHashMap((all[prov] as? JsonObject) ?: JsonObject(emptyMap()))
                for ((k, v) in opts as JsonObject) if (v is kotlinx.serialization.json.JsonNull) cur.remove(k) else cur[k] = v
                all[prov] = JsonObject(cur)
            }
            merged["harness_options"] = JsonObject(all)
        }
        config = JsonObject(merged)
        return ok(config.toString())
    }

    /** A tiny stand-in for `api/routes/accounts.py`. */
    private fun accountsRoute(method: String, path: String, body: String): MockResponse {
        if (method != "GET") accountWrites += "$method $path $body"
        val flow = """{"id":"f1","service":"claude","method":"token","status":"waiting","url":"https://claude.com/cai/oauth/authorize?code=true&state=S","needs_code":true,"code_label":"Code","message":"Open the link, sign in, then paste the code here."}"""
        return when {
            path == "/api/accounts" -> ok(accounts)
            path == "/api/env" && method == "GET" -> ok(envKeys)
            path == "/api/env" -> ok("""{"applies":"now","note":""}""")
            path.endsWith("/reveal") -> ok("""{"name":"OPENAI_API_KEY","value":"sk-full-secret-value"}""")
            path.startsWith("/api/env/") -> ok("""{"applies":"now","note":""}""")
            path.endsWith("/login/code") -> ok(flow.replace("\"waiting\"", "\"succeeded\"").replace("Open the link, sign in, then paste the code here.", "Signed in."))
            path.endsWith("/login") && method == "POST" -> ok(flow)
            path.endsWith("/login") && method == "GET" -> loginFlow?.let { ok(it) } ?: MockResponse().setResponseCode(404)
            path.endsWith("/login") && method == "DELETE" -> ok(flow.replace("\"waiting\"", "\"cancelled\""))
            path.endsWith("/credentials") -> if (body.contains("accessToken")) ok("""{"message":"Credentials saved.","service":${serviceJson("claude")}}""")
                else MockResponse().setResponseCode(400).setHeader("Content-Type", "application/json").setBody("""{"detail":"Invalid credentials: the file has no claudeAiOauth.accessToken."}""")
            path.endsWith("/logout") -> ok("""{"message":"Signed out.","service":${serviceJson("claude").replace("\"signed_in\"", "\"signed_out\"")}}""")
            path.endsWith("/verify") -> ok(JsonObject((json.parseToJsonElement(serviceJson("openai")) as JsonObject) +
                ("verified" to json.parseToJsonElement("""{"ok":false,"message":"The provider rejected the key (401)."}"""))).toString())
            path.matches(Regex("/api/accounts/[^/]+")) -> ok(serviceJson(path.substringAfterLast('/')))
            else -> MockResponse().setResponseCode(404)
        }
    }

    private fun serviceJson(id: String): String =
        ((json.parseToJsonElement(accounts) as JsonObject)["services"] as kotlinx.serialization.json.JsonArray)
            .first { ((it as JsonObject)["id"] as kotlinx.serialization.json.JsonPrimitive).content == id }.toString()

    private fun mergeSession(body: String): String {
        val cur = json.parseToJsonElement(sessionConfig) as JsonObject
        return JsonObject(cur + (json.parseToJsonElement(body) as JsonObject)).toString()
    }

    private fun ok(body: String) = MockResponse().setHeader("Content-Type", "application/json").setBody(body)

    fun shutdown() = runCatching { server.shutdown() }
}


/** A [DevicePlatform] with settable answers. */
class FakePlatform(
    override var sdkInt: Int = 36,
    var granted: MutableSet<AppPermission> = mutableSetOf(AppPermission.MICROPHONE, AppPermission.NOTIFICATIONS, AppPermission.NEARBY_DEVICES),
) : DevicePlatform {
    override val deviceName = "POCO X7"
    override val appVersion = AppVersion("0.1.0", 1, debuggable = true)
    override val packageName = "com.assistant.archie"
    val blocked = mutableSetOf<AppPermission>()
    val outputs = MutableStateFlow(setOf(AudioOutput.AUTO, AudioOutput.LOUDSPEAKER, AudioOutput.EARPIECE))
    var speaker: Float? = 0.8f
    var batteryUnrestricted = false
    var notificationsOn = true
    override var isXiaomiFamily = true
    var assistant: Boolean? = false

    override fun isGranted(permission: AppPermission) = sdkInt < permission.minSdk || permission in granted
    override fun isBlocked(permission: AppPermission) = permission in blocked
    override fun setBlocked(permission: AppPermission, blocked: Boolean) { if (blocked) this.blocked += permission else this.blocked -= permission }
    override fun availableOutputs(): Flow<Set<AudioOutput>> = outputs
    override fun speakerLevel() = speaker
    override fun setSpeakerLevel(level: Float) { speaker = level }
    override fun isIgnoringBatteryOptimizations() = batteryUnrestricted
    override fun notificationsEnabled() = notificationsOn
    override fun isDefaultAssistant() = assistant
    override fun appDetailsIntent() = Intent("app-details")
    override fun batteryOptimizationIntent() = Intent("battery")
    override fun autostartIntents() = listOf(Intent("autostart"))
    override fun notificationSettingsIntent() = Intent("notifications")
    override fun assistantSettingsIntent() = Intent("assistant")
}

/** A [ConnectionControl] that records calls; [status] is settable. */
class FakeConnection(initial: ConnectionStatus) : ConnectionControl {
    val state = MutableStateFlow(initial)
    override val status: StateFlow<ConnectionStatus> = state
    val changes = CopyOnWriteArrayList<String>()
    val calls = CopyOnWriteArrayList<String>()
    var probeResult: ApiResult<*> = ApiResult.Ok(Unit)
    val trusted = CopyOnWriteArrayList<Pair<String, CertificateInfo>>()
    override suspend fun changeServer(url: String) { changes += url; state.value = state.value.copy(serverUrl = url) }
    override fun connect() { calls += "connect" }
    override fun disconnect() { calls += "disconnect" }
    override fun scan() { calls += "scan" }
    override suspend fun probe(url: String): ApiResult<*> = probeResult
    override suspend fun trust(hostPort: String, certificate: CertificateInfo) { trusted += hostPort to certificate }
}

/** A settings feature over [SettingsBackend] with fakes for everything Android. */
class Harness(
    val backend: SettingsBackend = SettingsBackend().start(),
    prefs: Preferences = emptyPreferences(),
    val platform: FakePlatform = FakePlatform(),
    phase: ConnectionStatus.Phase = ConnectionStatus.Phase.CONNECTED,
    sessions: SessionControl = SessionControl.None,
) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val settings = SettingsStore(MemoryDataStore(prefs), scope)
    val api = ArchieApi(HttpStack()) { backend.url }
    val repo = ServerConfigRepository(api, scope)
    val connection = FakeConnection(ConnectionStatus(serverUrl = "ws://192.168.0.200:80", serverLabel = "jetson", phase = phase))
    val appearance = MemoryAppearanceStore()
    val feature = SettingsFeature(SettingsDeps(settings, repo, api, connection, platform, appearance, scope, sessions))

    init {
        runBlocking { settings.awaitLoaded() }
    }

    fun close() {
        scope.cancel()
        backend.shutdown()
    }
}

/** Polls [cond] until true (real time; the HTTP is real). */
fun eventually(timeoutMs: Long = 10_000, message: () -> String = { "condition not met" }, cond: () -> Boolean) {
    val end = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < end) {
        if (cond()) return
        Thread.sleep(20)
    }
    throw AssertionError(message())
}
