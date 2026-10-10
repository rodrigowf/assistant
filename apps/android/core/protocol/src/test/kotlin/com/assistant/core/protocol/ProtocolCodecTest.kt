package com.assistant.core.protocol

import com.assistant.core.model.AudioFormat
import com.assistant.core.model.ConnectionInfo
import com.assistant.core.model.VoiceConfig
import com.assistant.core.model.VoiceConnectionType
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.math.BigDecimal

class ProtocolCodecTest {
    private val json = Json

    private fun obj(text: String) = json.parseToJsonElement(text).jsonObject

    /** The nested implementations of a sealed frame interface (no kotlin-reflect on the classpath). */
    private fun subtypes(root: Class<*>): Set<Class<*>> =
        root.declaredClasses.filter { root.isAssignableFrom(it) && it != root }.toSet()

    // ───────────────────────── every frame type round-trips ─────────────────────────

    private val connectionInfo = ConnectionInfo(
        VoiceConnectionType.WEBSOCKET, "wss://dashscope/realtime", null, null,
        AudioFormat(24_000, "pcm"), AudioFormat(24_000, "pcm"), "qwen3.5-omni-plus-realtime", "Aiden", "backend",
    )

    private val modelInfo = OrchestratorModelInfoDto(
        model = "gpt-audio-mini", provider = "openai", maxTokens = 4096, supportsAudio = true,
        modelInfo = ModelInfoDto("openai", "gpt-audio-mini", "GPT Audio Mini", true, false, true, 4096, 128_000),
    )

    private val tree = obj("""{"type":"response.done","response":{"output":[{"content":[{"a":[1,2.5,{"deep":null}]}]}],"usage":{"x":true}}}""")

    /** One instance of every [ServerFrame] subtype, with every field set. */
    private val samples: List<ServerFrame> = listOf(
        ServerFrame.SessionStarted(
            "L1", 200_000, ResumeState("L1:1759500000000", 17), true, "J1", true, modelInfo, true,
            "qwen", "qwen3.5-omni-plus-realtime", "Aiden", "en", false, tree, connectionInfo, "relay down",
        ),
        ServerFrame.Status("retrying", "upstream silent for 240s, retrying"),
        ServerFrame.SessionStopped(),
        ServerFrame.SessionTerminated("subprocess_crashed", "exit 1", "sdk-1"),
        ServerFrame.Error("send_failed", "boom"),
        ServerFrame.UserMessage("second", queued = true),
        ServerFrame.UserMessage("[shared text]\nBuy milk", source = "shared_inject"),
        ServerFrame.UserMessage("", source = "voice_message"),
        ServerFrame.TextDelta("Hel", 1, "L1:1"),
        ServerFrame.TextComplete("Hello", 2, "L1:1"),
        ServerFrame.ThinkingDelta("Hm", 3, "L1:1"),
        ServerFrame.ThinkingComplete("Hmm.", 4, "L1:1"),
        ServerFrame.ToolUse("t1", "Bash", obj("""{"command":"ls","nested":{"a":[1,2]}}"""), 5, "L1:1"),
        ServerFrame.ToolResult("t1", JsonPrimitive("a.txt"), false, 6, "L1:1"),
        ServerFrame.ToolResult("fc_1", obj("""{"provider":"claude","voice":"Puck"}"""), true),
        ServerFrame.ToolExecuting("c1", "list_agent_sessions"),
        ServerFrame.ToolProgress("c1", "list_agent_sessions", 5.0, "Still executing list_agent_sessions..."),
        ServerFrame.TurnComplete(0.0123, obj("""{"input_tokens":10,"output_tokens":50}"""), 1010, 50, 1, "sdk-1", false, "ok", 7, "L1:1"),
        ServerFrame.CompactComplete("manual", null, 9000, 1200),
        ServerFrame.CompactComplete("auto", "Summary."),
        ServerFrame.SessionStalled(120.4, "Bash", "t1", 5, "L1:1"),
        ServerFrame.PermissionRequest("r1", "ExitPlanMode", obj("""{"plan":"1. a"}"""), 8, "L1:1"),
        ServerFrame.PermissionResolved("r1", "deny", "user", "keep planning", 9, "L1:1"),
        ServerFrame.ModelChanged(modelInfo),
        ServerFrame.ModelInfo(modelInfo),
        ServerFrame.ModelsList(listOf(modelInfo.modelInfo!!)),
        ServerFrame.NestedSessionEvent("A1", "permission_request", obj("""{"type":"permission_request","request_id":"r1","tool_name":"ExitPlanMode","tool_input":{},"seq":3,"stream_id":"A1:1"}""")),
        ServerFrame.AgentSessionOpened("A1", "sdk-9", false),
        ServerFrame.AgentSessionClosed("O1", true),
        ServerFrame.OrchestratorSwitch("past-1", "Lamps", true, "O1"),
        ServerFrame.AgentTurnStarted("A1", "S1", "claude"),
        ServerFrame.AgentTurnFinished("A1", "S1", "codex", "Energy", "error", "Half done", "Credit balance is too low"),
        ServerFrame.VisualizationChanged(
            listOf(ContentChange("dash/index.html", ContentChange.Kind.MODIFIED), ContentChange("old.html", ContentChange.Kind.DELETED)),
            listOf(ContentChange("dash/data.json", ContentChange.Kind.MODIFIED)),
        ),
        ServerFrame.MemoryChanged(listOf(ContentChange("archie/specs/12-client-protocol.md", ContentChange.Kind.CREATED))),
        ServerFrame.AudioUpload("AAAA", "webm", "hi", 3),
        ServerFrame.Ping(),
        ServerFrame.VoiceEvent(tree),
        ServerFrame.VoiceAudioOut("AAEC"),
        ServerFrame.VoiceCommand(obj("""{"type":"response.create"}""")),
        ServerFrame.VoiceConnectionError("no key"),
        ServerFrame.VoiceOwnerActive(true, "O1"),
        ServerFrame.VoiceEnding("user_stop", "O1"),
        ServerFrame.VoiceEnded("agent_end", "O1"),
        ServerFrame.VoiceStopped(),
        ServerFrame.Unknown("unknown", obj("""{"type":"unknown","x":1}""")),
    )

    @Test
    fun everyServerFrameTypeIsSampled() {
        val sampled = samples.map { it.javaClass }.toSet()
        val all = subtypes(ServerFrame::class.java)
        assertEquals("missing samples: ${all - sampled}", all, sampled)
    }

    @Test
    fun serverFramesRoundTrip() {
        for (f in samples) {
            val encoded = ProtocolCodec.encodeServer(f)
            assertEquals(f.type, (encoded["type"] as JsonPrimitive).content)
            assertEquals("round trip of ${f.type}", f, ProtocolCodec.decodeServer(encoded))
            // binary (UTF-8 bytes) and text frames decode the same (T-1)
            assertEquals(f, ProtocolCodec.decodeServer(encoded.toString().toByteArray(Charsets.UTF_8)))
            assertEquals(f, ProtocolCodec.decodeServer(encoded.toString()))
        }
    }

    @Test
    fun voiceEventTreesStayRecursive() {
        // inv04 B1: the old client flattened nested objects into a shallow map.
        val f = ProtocolCodec.decodeServer("""{"type":"voice_event","event":$tree}""") as ServerFrame.VoiceEvent
        assertEquals(tree, f.event)
        val inner = f.event["response"]!!.jsonObject["output"]!!.jsonArray[0].jsonObject["content"]!!.jsonArray[0]
        assertEquals(JsonNull, inner.jsonObject["a"]!!.jsonArray[2].jsonObject["deep"])
    }

    @Test
    fun nestedSessionEventDecodesItsPayload() {
        val f = samples.filterIsInstance<ServerFrame.NestedSessionEvent>().single()
        val inner = f.decodedEvent() as ServerFrame.PermissionRequest
        assertEquals("r1", inner.requestId)
        assertEquals(3L, inner.seq)
        // event_data without its own type falls back to event_type
        val noType = f.copy(eventType = "permission_resolved", eventData = obj("""{"request_id":"r1","decision":"allow"}"""))
        assertEquals("allow", (noType.decodedEvent() as ServerFrame.PermissionResolved).decision)
    }

    // ───────────────────────── T-3: malformed frames never reach the reducer ─────────────────────────

    @Test
    fun malformedFramesDecodeToNull() {
        for (bad in listOf("", "not json", "[1,2]", "42", "\"text\"", "{}", """{"type":5}""", """{"type":null}""", "{\"type\":")) {
            assertNull("'$bad' must be dropped", ProtocolCodec.decodeServer(bad))
        }
        assertNull(ProtocolCodec.decodeServer(byteArrayOf(0xC3.toByte(), 0x28)))   // invalid UTF-8 is replaced, then not JSON
    }

    @Test
    fun wrongTypedFieldsReadAsNull() {
        val f = ProtocolCodec.decodeServer(
            """{"type":"tool_use","tool_use_id":7,"tool_name":"Bash","tool_input":"ls","seq":"5","stream_id":3}""",
        ) as ServerFrame.ToolUse
        assertNull(f.toolUseId); assertNull(f.toolInput); assertNull(f.seq); assertNull(f.streamId)
        assertEquals("Bash", f.toolName)
        val tc = ProtocolCodec.decodeServer("""{"type":"turn_complete","cost":null,"input_tokens":12.0,"num_turns":"x"}""") as ServerFrame.TurnComplete
        assertNull(tc.cost); assertEquals(12L, tc.inputTokens); assertNull(tc.numTurns)
        // a voice_event without an object event is kept whole as Unknown, not dropped silently
        assertTrue(ProtocolCodec.decodeServer("""{"type":"voice_event","event":"x"}""") is ServerFrame.Unknown)
    }

    @Test
    fun unknownTypesAreKeptWhole() {
        val raw = """{"type":"brand_new","payload":{"a":1},"seq":3,"stream_id":"s"}"""
        val f = ProtocolCodec.decodeServer(raw) as ServerFrame.Unknown
        assertEquals("brand_new", f.type)
        assertEquals(3L, f.seq)
        assertEquals(obj(raw), ProtocolCodec.encodeServer(f))
    }

    @Test
    fun nonAsciiSurvivesBinaryFrames() {
        val text = "Tarô — 日本語 ✓"
        val bytes = """{"type":"text_delta","text":"$text"}""".toByteArray(Charsets.UTF_8)
        assertEquals(text, (ProtocolCodec.decodeServer(bytes) as ServerFrame.TextDelta).text)
    }

    // ───────────────────────── recorded / backend-shaped frames decode without loss ─────────────────────────

    /** Every frame of every shared fixture: decodes to a typed frame, and no non-null field is lost. */
    @Test
    fun fixtureFramesDecodeWithoutLoss() {
        val dir = File(System.getProperty("archie.protocolFixtures") ?: error("archie.protocolFixtures not set"))
        val files = dir.listFiles { f -> f.name.endsWith(".json") }.orEmpty()
        assertTrue("no fixtures found in $dir", files.isNotEmpty())
        var frames = 0
        for (file in files) {
            val fixture = json.parseToJsonElement(file.readText()).jsonObject
            val events = fixture["events"]?.jsonArray ?: continue
            for (e in events) {
                frames++
                val f = ProtocolCodec.decodeServer(e)
                assertNotNull("${file.name}: $e", f)
                assertFalse("${file.name}: ${f!!.type} decoded as Unknown", f is ServerFrame.Unknown)
                assertNoLoss("${file.name}/${f.type}", e.jsonObject, ProtocolCodec.encodeServer(f))
            }
        }
        assertTrue(frames > 200)
    }

    /** Backend payloads not covered by the fixtures (orchestrator session_started, watcher, voice). */
    @Test
    fun backendShapedFramesDecodeWithoutLoss() {
        val raw = listOf(
            """{"type":"session_started","session_id":"O1","jsonl_id":"J1","voice":true,"model_info":{"model":"gpt-audio-mini","provider":"openai","max_tokens":4096,"supports_audio":true,"model_info":{"provider":"openai","model_id":"gpt-audio-mini","display_name":"GPT Audio Mini","supports_audio":true,"supports_vision":false,"supports_tools":true,"max_tokens":4096,"context_window":128000}},"voice_provider":"openai","voice_model":"gpt-realtime","voice_name":"cedar","voice_transcription_language":"","voice_initiator":true,"voice_recording_enabled":false,"voice_session_update":{"type":"session.update","session":{"tools":[{"name":"x"}]}},"voice_connection_info":{"connection_type":"webrtc","endpoint":"https://api.openai.com/v1/realtime/calls?model=gpt-realtime","ephemeral_token":"ek_1","expires_at":1759500000,"audio_in_format":{"sample_rate":24000,"encoding":"pcm16"},"audio_out_format":{"sample_rate":24000,"encoding":"pcm16"},"model":"gpt-realtime","voice":"cedar"}}""",
            """{"type":"session_started","session_id":"L1","context_window":200000,"resume_state":{"stream_id":"L1:1759500000000","next_seq":17},"replay_overflow":true}""",
            """{"type":"status","status":"retrying","detail":"upstream silent for 240s, retrying"}""",
            """{"type":"tool_executing","tool_use_id":"c1","tool_name":"x"}""",
            """{"type":"model_changed","model_info":{"model":"m","provider":"anthropic","max_tokens":1,"supports_audio":false,"model_info":null}}""",
            """{"type":"models_list","models":[{"provider":"anthropic","model_id":"claude","display_name":"C","supports_audio":false,"supports_vision":true,"supports_tools":true,"max_tokens":8192,"context_window":200000}]}""",
            """{"type":"agent_session_opened","session_id":"A1","sdk_session_id":"S1","is_orchestrator":false}""",
            """{"type":"agent_session_closed","session_id":"A1","is_orchestrator":false}""",
            // backend/orchestrator/tools/agent_sessions.py switch_conversation (spec 12 §6.11a)
            """{"type":"orchestrator_switch","sdk_session_id":"past-1","title":"Lamps","voice":true,"from_session_id":"O1"}""",
            // backend/api/pool.py send(): turn watcher events (spec 12 §3.7)
            """{"type":"agent_turn_started","session_id":"A1","sdk_session_id":"S1","provider":"claude"}""",
            """{"type":"agent_turn_finished","session_id":"A1","sdk_session_id":"S1","provider":"claude","title":"Energy","status":"ok","preview":"All 12 tests pass.","error":null}""",
            """{"type":"voice_command","command":{"type":"conversation.item.create","item":{"type":"function_call_output","call_id":"c","output":"{}"}}}""",
            """{"type":"voice_audio_out","audio":"AAEC"}""",
            """{"type":"voice_connection_error","detail":"x"}""",
            """{"type":"audio_upload","audio":"AA","format":"webm","text":"","size_bytes":2}""",
            """{"type":"ping"}""",
        )
        for (r in raw) {
            val o = obj(r)
            val f = ProtocolCodec.decodeServer(o)!!
            assertFalse(f is ServerFrame.Unknown)
            assertNoLoss(f.type, o, ProtocolCodec.encodeServer(f))
        }
    }

    /** Every non-null value of [input] is present and equal (numbers numerically) in [output]. */
    private fun assertNoLoss(where: String, input: JsonObject, output: JsonObject) {
        for ((k, v) in input) {
            if (v is JsonNull) continue
            val o = output[k] ?: throw AssertionError("$where: field '$k' lost")
            assertJsonEquals("$where.$k", v, o)
        }
    }

    private fun assertJsonEquals(where: String, a: JsonElement, b: JsonElement) {
        when {
            a is JsonObject && b is JsonObject -> {
                for (k in a.keys) if (a[k] !is JsonNull) assertJsonEquals("$where.$k", a[k]!!, b[k] ?: throw AssertionError("$where.$k lost"))
            }
            a is JsonArray && b is JsonArray -> {
                assertEquals(where, a.size, b.size)
                a.indices.forEach { assertJsonEquals("$where[$it]", a[it], b[it]) }
            }
            a is JsonPrimitive && b is JsonPrimitive && !a.isString && !b.isString && a.content != "true" && a.content != "false" ->
                assertEquals(where, 0, BigDecimal(a.content).compareTo(BigDecimal(b.content)))
            else -> assertEquals(where, a, b)
        }
    }

    // ───────────────────────── client frames ─────────────────────────

    private val clientSamples: List<ClientFrame> = listOf(
        ClientFrame.Start("L1", "sdk-1", ResumeCursor("L1:1759500000000", 14), fork = true, mcpServers = obj("""{"x":{"type":"stdio"}}""")),
        ClientFrame.Start("L1"),
        ClientFrame.VoiceStart("O1", "J1", VoiceConfig("google", "gemini-3.1-flash-live-preview", "Puck", "", "aistudio")),
        ClientFrame.VoiceStart("O1"),
        ClientFrame.Send("hi"),
        ClientFrame.InjectText("[shared text]\nBuy milk"),
        ClientFrame.SendAudio("AAAA", "wav", "transcribe"),
        ClientFrame.Interrupt,
        ClientFrame.Command("/help"),
        ClientFrame.Compact,
        ClientFrame.PermissionResponse("r1", "deny", "no", "A1"),
        ClientFrame.Stop,
        ClientFrame.VoiceStop,
        ClientFrame.SetModel("gpt-audio"),
        ClientFrame.GetModel,
        ClientFrame.GetModels,
        ClientFrame.VoiceEvent(obj("""{"type":"response.cancel"}""")),
        ClientFrame.VoiceAudioIn("AAEC"),
        ClientFrame.VoiceRecordingChunk("assistant", "AAEC"),
        ClientFrame.VoiceRecordingEnd,
    )

    @Test
    fun everyClientFrameTypeIsSampled() {
        val sampled = clientSamples.map { it.javaClass }.toSet()
        assertEquals(subtypes(ClientFrame::class.java), sampled)
    }

    @Test
    fun clientFramesRoundTrip() {
        for (f in clientSamples) assertEquals(f, ProtocolCodec.decodeClient(ProtocolCodec.encodeClient(f)))
    }

    @Test
    fun startShapeMatchesTheBackend() {
        assertEquals(
            obj("""{"type":"start","local_id":"L1","resume_sdk_id":"sdk-1","resume_from":{"stream_id":"L1:1759500000000","seq":14}}"""),
            ProtocolCodec.encodeClientJson(ClientFrame.Start("L1", "sdk-1", ResumeCursor("L1:1759500000000", 14))),
        )
        // no resume fields at all for a brand-new session
        assertEquals(obj("""{"type":"start","local_id":"L1"}"""), ProtocolCodec.encodeClientJson(ClientFrame.Start("L1")))
        // OPEN-2: automatic starts and the voice re-arm reattach
        assertEquals(obj("""{"type":"start","local_id":"L1","reattach":true}"""), ProtocolCodec.encodeClientJson(ClientFrame.Start("L1", reattach = true)))
        assertEquals(
            obj("""{"type":"voice_start","local_id":"O1","reattach":true}"""),
            ProtocolCodec.encodeClientJson(ClientFrame.VoiceStart("O1", reattach = true)),
        )
    }

    @Test
    fun voiceStartOmitsUnsetVoiceFields() {
        // V-1: omitted fields take assistant_config.json defaults server-side; "" (auto language) is kept.
        assertEquals(
            obj("""{"type":"voice_start","local_id":"O1","voice_transcription_language":""}"""),
            ProtocolCodec.encodeClientJson(ClientFrame.VoiceStart("O1", voice = VoiceConfig(transcriptionLanguage = ""))),
        )
    }

    @Test
    fun simpleFramesHaveOnlyTheirType() {
        for (f in listOf(ClientFrame.Interrupt, ClientFrame.Compact, ClientFrame.Stop, ClientFrame.VoiceStop, ClientFrame.GetModel)) {
            assertEquals(obj("""{"type":"${f.type}"}"""), ProtocolCodec.encodeClientJson(f))
        }
    }

    @Test
    fun noPingFrameExists() {
        // T-4: the chat WS answers ping with error unknown_type; there is deliberately no client ping.
        assertTrue(subtypes(ClientFrame::class.java).none { it.simpleName.equals("Ping", ignoreCase = true) })
        assertNull(ProtocolCodec.decodeClient("""{"type":"ping"}"""))
    }

    @Test
    fun startAcceptsTheSessionIdAlias() {
        assertEquals("S9", (ProtocolCodec.decodeClient("""{"type":"start","local_id":"L","session_id":"S9"}""") as ClientFrame.Start).resumeSdkId)
    }
}
