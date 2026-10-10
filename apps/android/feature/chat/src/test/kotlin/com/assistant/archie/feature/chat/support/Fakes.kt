package com.assistant.archie.feature.chat.support

import com.assistant.archie.feature.chat.ChatBackend
import com.assistant.archie.feature.chat.ChatVoice
import com.assistant.core.conversation.ConversationInput
import com.assistant.core.conversation.ConversationReducer
import com.assistant.core.conversation.ConversationState
import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.CutResult
import com.assistant.core.model.UploadResult
import com.assistant.core.network.ApiResult
import com.assistant.core.network.SendResult
import com.assistant.core.network.UploadSource
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** A [ChatBackend] that runs the real reducer on local actions and records what was sent. */
class FakeChatBackend(initial: ConversationState) : ChatBackend {
    override val key = ConversationKey("test")
    private val _state = MutableStateFlow<ConversationState?>(initial)
    override val state: StateFlow<ConversationState?> = _state
    val eventsFlow = MutableSharedFlow<ConversationEvent>(extraBufferCapacity = 8)
    override val events: Flow<ConversationEvent> = eventsFlow

    val sent = mutableListOf<String>()
    val commands = mutableListOf<String>()
    val permissions = mutableListOf<Pair<String, Boolean>>()
    val agentApprovals = mutableListOf<Triple<String, String, Boolean>>()
    val injected = mutableListOf<String>()
    var interrupts = 0
    var compacts = 0
    var olderRequests = 0
    var retries = 0
    var sendResult = SendResult.SENT
    var cutResult: CutResult = CutResult.Failed("no")

    fun apply(vararg inputs: ConversationInput) { _state.value = ConversationReducer.reduceAll(_state.value!!, inputs.toList()) }

    fun frames(vararg json: String) { _state.value = Frames.reduce(_state.value!!, *json) }

    fun set(s: ConversationState) { _state.value = s }

    override fun send(text: String): SendResult {
        sent += text
        apply(ConversationInput.LocalSend(text))
        return sendResult
    }

    override fun command(text: String): SendResult { commands += text; return sendResult }
    override fun interrupt() { interrupts++; apply(ConversationInput.LocalInterrupt) }
    override fun compact() { compacts++ }
    override fun respondToPermission(requestId: String, allow: Boolean): SendResult { permissions += requestId to allow; return SendResult.SENT }
    var approvalAnswer: com.assistant.core.data.ApprovalAnswer = com.assistant.core.data.ApprovalAnswer.Sent
    override suspend fun respondToAgentApproval(agentLocalId: String, requestId: String, allow: Boolean): com.assistant.core.data.ApprovalAnswer {
        agentApprovals += Triple(agentLocalId, requestId, allow); return approvalAnswer
    }
    override fun loadOlder() { olderRequests++ }
    override fun reload() = Unit
    override fun dismissBanner() = apply(ConversationInput.DismissBanner)
    override fun retry() { retries++ }
    override suspend fun rewind(entryId: String) = cutResult
    override suspend fun fork(entryId: String) = cutResult
    override suspend fun upload(source: UploadSource, onProgress: (Long, Long) -> Unit): ApiResult<UploadResult> =
        ApiResult.Ok(UploadResult(source.fileName, "/srv/uploads/${source.fileName}", "/uploads/${source.fileName}", 2048, "text/plain"))
    override fun inject(text: String) { injected += text }
    override fun touch() = Unit
}

class FakeChatVoice(initial: VoiceSessionState = VoiceSessionState()) : ChatVoice {
    override val state = MutableStateFlow(initial)
    override val level = MutableStateFlow<Float?>(null)
    override val speakerMuted = MutableStateFlow(false)
    override val remoteDevice = MutableStateFlow<String?>(null)
    override val remoteTranscriptMirrored = MutableStateFlow(true)
    override val recording = MutableStateFlow(false)
    var starts = 0
    var stops = 0
    override fun start() { starts++ }
    override fun stop() { stops++ }
    override fun toggleMute() { state.value = state.value.copy(isMuted = !state.value.isMuted) }
    override fun toggleSpeaker() { speakerMuted.value = !speakerMuted.value }
    override fun takeOver() = Unit
    override fun toggleRecording() { recording.value = !recording.value }
}
