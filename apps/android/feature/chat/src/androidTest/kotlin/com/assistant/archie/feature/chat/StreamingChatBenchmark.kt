package com.assistant.archie.feature.chat

import android.content.pm.ApplicationInfo
import android.os.Handler
import android.os.HandlerThread
import android.os.ParcelFileDescriptor
import android.util.Log
import android.view.Choreographer
import android.view.FrameMetrics
import android.view.Window
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.assistant.archie.feature.chat.ui.ConversationScreen
import com.assistant.archie.feature.chat.ui.DefaultToolCardRenderer
import com.assistant.core.conversation.ConversationInput
import com.assistant.core.conversation.ConversationReducer
import com.assistant.core.conversation.ConversationState
import com.assistant.core.conversation.HistoryState
import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.CutResult
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.ConnectionState
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.SessionStatus
import com.assistant.core.model.UploadResult
import com.assistant.core.network.ApiResult
import com.assistant.core.network.SendResult
import com.assistant.core.network.UploadSource
import com.assistant.core.protocol.ProtocolCodec
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeFalse
import org.junit.Test
import org.junit.runner.RunWith
import java.util.Collections
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Spec 14 §3.6 streaming budget on the POCO_X7 AVD: replays a ~2,000-token assistant turn (8 KB of the
 * B-02 corpus: headings, lists, code, tables) with 12 interleaved tool calls through reducer →
 * ViewModel (33 ms sampling) → LazyColumn, at ~60 tokens/s, in a real Activity on the real
 * Choreographer. Per-frame CPU work comes from FrameMetrics (UI thread + RenderThread, no GPU: the
 * macrobenchmark `frameDurationCpuMs` analogue; the emulator's GPU is the host's translated GL).
 *
 * Like a macrobenchmark it runs one warm-up pass (logged as `cold`), then measures a second pass on a
 * fresh conversation. Budget: P95 ≤ 16 ms, no frame > 48 ms. Non-debuggable run:
 *   flock /tmp/archie-locks/gradle.lock ./gradlew :feature:chat:connectedReleaseAndroidTest -PchatBenchmark
 */
@RunWith(AndroidJUnit4::class)
class StreamingChatBenchmark {
    private class Result(val cpu: List<Double>, val gfx: String, val maxVsyncGap: Double, val tools: Int, val seconds: Double)

    @Test
    fun streamingTurnWithTwelveToolsMeetsFrameBudget() {
        val answer = InstrumentationRegistry.getInstrumentation().context.assets.open("40-syn-long.md")
            .bufferedReader().readText().let { full -> full.substring(0, full.indexOf('\n', 8_000).coerceAtLeast(8_000)) }
        val scenario = ActivityScenario.launch(ComponentActivity::class.java)
        val cold = replay(scenario, answer)
        log("cold", cold)
        val warm = replay(scenario, answer)
        log("warm", warm)
        scenario.close()

        assertEquals(12, warm.tools)
        // The budget is defined for the non-debuggable `benchmark` build (spec 14 §3.6); a debuggable
        // ART runs Compose several times slower, so a debug run only logs its numbers.
        assumeFalse("frame budget applies to the non-debuggable run (-PchatBenchmark)", debuggable())
        val c = warm.cpu.sorted()
        val p95 = c[(c.size * 95 / 100).coerceAtMost(c.size - 1)]
        assertTrue("enough frames measured (${c.size})", c.size > 100)
        assertTrue("P95 CPU frame time %.2f ms ≤ 16 ms".format(p95), p95 <= 16.0)
        assertTrue("no CPU frame over 48 ms (max %.2f ms)".format(c.last()), c.last() <= 48.0)
    }

    private fun replay(scenario: ActivityScenario<ComponentActivity>, answer: String): Result {
        val backend = BenchBackend()
        val vm = ConversationViewModel(backend, PresenceChatVoice(), DefaultToolCardRenderer::describe)
        scenario.onActivity { it.setContent { ArchieTheme { ConversationScreen(vm) } } }
        Thread.sleep(1_000)
        backend.apply(ConversationInput.LocalSend("Explain the architecture, step by step."))
        backend.frames("""{"type":"status","status":"processing"}""")
        Thread.sleep(300)

        val pkg = InstrumentationRegistry.getInstrumentation().targetContext.packageName
        shell("dumpsys gfxinfo $pkg reset")
        val recording = AtomicBoolean(false)
        val cpu = Collections.synchronizedList(ArrayList<Double>())
        val metricsThread = HandlerThread("frame-metrics").apply { start() }
        val listener = Window.OnFrameMetricsAvailableListener { _, m, _ ->
            if (recording.get()) cpu += CPU_METRICS.sumOf { m.getMetric(it) } / 1e6
        }
        val vsyncs = Collections.synchronizedList(ArrayList<Long>())
        val ticking = AtomicBoolean(true)
        scenario.onActivity { a ->
            a.window.addOnFrameMetricsAvailableListener(listener, Handler(metricsThread.looper))
            val ch = Choreographer.getInstance()
            ch.postFrameCallback(object : Choreographer.FrameCallback {
                override fun doFrame(t: Long) { if (recording.get()) vsyncs += t; if (ticking.get()) ch.postFrameCallback(this) }
            })
        }

        // ~4 chars per token, 16-char deltas every 66 ms ≈ 60 tokens/s; 12 tools, results 6 deltas later.
        val chunks = answer.chunked(16)
        val toolEvery = chunks.size / 13
        var tools = 0
        val pending = ArrayDeque<Pair<Int, Int>>()
        val started = System.nanoTime()
        recording.set(true)
        chunks.forEachIndexed { i, chunk ->
            backend.frames("""{"type":"text_delta","text":${JsonPrimitive(chunk)}}""")
            if (i > 0 && i % toolEvery == 0 && tools < 12) {
                tools++
                backend.frames("""{"type":"tool_use","tool_use_id":"t$tools","tool_name":"${TOOLS[tools % TOOLS.size]}","tool_input":{"file_path":"/src/mod$tools.kt","command":"make t$tools"}}""")
                pending += tools to i + 6
            }
            while (pending.isNotEmpty() && pending.first().second <= i) {
                val (t, _) = pending.removeFirst()
                backend.frames("""{"type":"tool_result","tool_use_id":"t$t","output":${JsonPrimitive((1..20).joinToString("\n") { "line $it of tool $t output" })},"is_error":false}""")
            }
            Thread.sleep(66)
        }
        pending.forEach { (t, _) -> backend.frames("""{"type":"tool_result","tool_use_id":"t$t","output":"done","is_error":false}""") }
        backend.frames("""{"type":"turn_complete"}""")
        Thread.sleep(500)
        recording.set(false); ticking.set(false)
        val seconds = (System.nanoTime() - started) / 1e9
        scenario.onActivity { it.window.removeOnFrameMetricsAvailableListener(listener) }
        metricsThread.quitSafely()
        val gaps = synchronized(vsyncs) { vsyncs.zipWithNext { a, b -> (b - a) / 1e6 } }
        return Result(synchronized(cpu) { cpu.toList() }, shell("dumpsys gfxinfo $pkg"), gaps.maxOrNull() ?: 0.0, tools, seconds)
    }

    private fun log(pass: String, r: Result) {
        val c = r.cpu.sorted()
        val p50 = c[c.size / 2]
        val p95 = c[(c.size * 95 / 100).coerceAtMost(c.size - 1)]
        fun g(re: String) = Regex(re).find(r.gfx)?.groupValues?.get(1) ?: "?"
        val debuggable = debuggable()
        Log.i(
            TAG,
            "BENCH $pass debuggable=$debuggable seconds=%.1f tools=${r.tools} cpuFrames=${c.size} cpuP50=%.2fms cpuP95=%.2fms cpuMax=%.2fms over16=${c.count { it > 16 }} "
                .format(r.seconds, p50, p95, c.last()) +
                "| hwui total=${g("Total frames rendered: (\\d+)")} p50=${g("50th percentile: (\\d+)ms")}ms p95=${g("95th percentile: (\\d+)ms")}ms " +
                "gpuP95=${g("95th gpu percentile: (\\d+)ms")}ms maxVsyncGap=%.1fms".format(r.maxVsyncGap),
        )
    }

    private fun debuggable() =
        (InstrumentationRegistry.getInstrumentation().targetContext.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    private fun shell(cmd: String): String {
        val pfd = InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(cmd)
        return ParcelFileDescriptor.AutoCloseInputStream(pfd).bufferedReader().readText()
    }

    private companion object {
        const val TAG = "StreamingChatBenchmark"
        val TOOLS = listOf("Read", "Bash", "Grep", "Edit")
        val CPU_METRICS = listOf(
            FrameMetrics.INPUT_HANDLING_DURATION, FrameMetrics.ANIMATION_DURATION, FrameMetrics.LAYOUT_MEASURE_DURATION,
            FrameMetrics.DRAW_DURATION, FrameMetrics.SYNC_DURATION, FrameMetrics.COMMAND_ISSUE_DURATION,
            FrameMetrics.SWAP_BUFFERS_DURATION,
        )
    }
}

/** Minimal backend: the real reducer, no sockets. */
private class BenchBackend : ChatBackend {
    private val s = MutableStateFlow<ConversationState?>(
        ConversationState.initial(SessionRef("L1", "sdk-1", SessionKind.AGENT, HarnessProvider.CLAUDE))
            .copy(status = SessionStatus.IDLE, connection = ConnectionState.SUBSCRIBED, history = HistoryState(loaded = true)),
    )
    override val key = ConversationKey("bench")
    override val state = s
    override val events = MutableSharedFlow<ConversationEvent>()

    fun apply(i: ConversationInput) { s.value = ConversationReducer.reduce(s.value!!, i) }
    fun frames(json: String) = apply(ConversationInput.Frame(ProtocolCodec.decodeServer(json)!!))

    override fun send(text: String): SendResult = SendResult.SENT
    override fun command(text: String): SendResult = SendResult.SENT
    override fun interrupt() = Unit
    override fun compact() = Unit
    override fun respondToPermission(requestId: String, allow: Boolean): SendResult = SendResult.SENT
    override suspend fun respondToAgentApproval(agentLocalId: String, requestId: String, allow: Boolean) =
        com.assistant.core.data.ApprovalAnswer.Failed("benchmark")
    override fun loadOlder() = Unit
    override fun reload() = Unit
    override fun dismissBanner() = Unit
    override fun retry() = Unit
    override suspend fun rewind(entryId: String): CutResult = CutResult.Failed("")
    override suspend fun fork(entryId: String): CutResult = CutResult.Failed("")
    override suspend fun upload(source: UploadSource, onProgress: (Long, Long) -> Unit): ApiResult<UploadResult> = ApiResult.NetworkError(Exception())
    override fun inject(text: String) = Unit
    override fun touch() = Unit
}
