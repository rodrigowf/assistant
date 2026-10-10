package com.assistant.archie.feature.chat

import com.assistant.core.conversation.ConversationState
import com.assistant.core.data.ApprovalAnswer
import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ConversationRepository
import com.assistant.core.data.CutResult
import com.assistant.core.data.UploadRepository
import com.assistant.core.model.SessionRef
import com.assistant.core.model.UploadResult
import com.assistant.core.network.ApiResult
import com.assistant.core.network.SendResult
import com.assistant.core.network.UploadSource
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.filter

/**
 * Everything the conversation screen asks of the data layer, for one view. Production:
 * [RepositoryChatBackend] over B-03's [ConversationRepository]; tests: a fake that runs the real
 * reducer. Keeping this seam narrow is what lets the screen be tested without sockets.
 */
interface ChatBackend {
    val key: ConversationKey
    val state: StateFlow<ConversationState?>
    val events: Flow<ConversationEvent>

    /** §6.1/§6.2 (and deny-with-feedback while a permission is pending, §6.9). `null` = no view. */
    fun send(text: String): SendResult?

    /** Agent slash command (`command` frame). */
    fun command(text: String): SendResult?
    fun interrupt()
    fun compact()
    fun respondToPermission(requestId: String, allow: Boolean): SendResult?

    /**
     * Orchestrator "Agent approvals" (PM-5, §6.9): on the agent's own socket when its view is open
     * here, else over REST. A `Failed` answer re-enables the card.
     */
    suspend fun respondToAgentApproval(agentLocalId: String, requestId: String, allow: Boolean): ApprovalAnswer
    fun loadOlder()
    fun reload()
    fun dismissBanner()

    /** Connection banner Retry: reconnect now (§6.13, retry-after-start-failure rule). */
    fun retry()
    suspend fun rewind(entryId: String): CutResult
    suspend fun fork(entryId: String): CutResult


    /** §6.15: upload then inject the share line into the Archie conversation. */
    suspend fun upload(source: UploadSource, onProgress: (Long, Long) -> Unit): ApiResult<UploadResult>
    fun inject(text: String)

    /** The user looked at this view (agent socket LRU). */
    fun touch()
}

/** Where "Continue in a new view" goes; decided from the view's kind, never from the screen (bug 5). */
/** The production backend over the process-scoped repositories (B-03). */
class RepositoryChatBackend(
    override val key: ConversationKey,
    private val repo: ConversationRepository,
    private val uploads: UploadRepository,
) : ChatBackend {
    override val state: StateFlow<ConversationState?> = repo.state(key)
    override val events: Flow<ConversationEvent> = repo.events.filter { it.key == key }

    override fun send(text: String) = repo.send(key, text)
    override fun command(text: String) = repo.command(key, text)
    override fun interrupt() { repo.interrupt(key) }
    override fun compact() { repo.compact(key) }
    override fun respondToPermission(requestId: String, allow: Boolean) = repo.respondToPermission(key, requestId, allow)

    override suspend fun respondToAgentApproval(agentLocalId: String, requestId: String, allow: Boolean) =
        repo.answerAgentApproval(agentLocalId, requestId, allow)

    override fun loadOlder() = repo.loadOlder(key)
    override fun reload() { repo.reload(key) }
    override fun dismissBanner() { repo.dismissBanner(key) }
    override fun retry() = repo.retry(key)
    override suspend fun rewind(entryId: String) = repo.rewind(key, entryId)
    override suspend fun fork(entryId: String) = repo.fork(key, entryId)

    override suspend fun upload(source: UploadSource, onProgress: (Long, Long) -> Unit) = uploads.upload(source, onProgress)
    override fun inject(text: String) = repo.inject(text)
    override fun touch() = repo.touch(key)

    companion object {
        /** A fork opened by the caller (focused, user-initiated, §6.5). */
        fun forkedRef(r: CutResult): SessionRef? = (r as? CutResult.Done)?.ref
    }
}
