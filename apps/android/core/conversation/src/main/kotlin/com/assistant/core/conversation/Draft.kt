package com.assistant.core.conversation

import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionStatus
import kotlinx.collections.immutable.PersistentList
import kotlinx.collections.immutable.persistentListOf
import kotlinx.collections.immutable.toPersistentList
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Mutable working copy of a [ConversationState] for one reducer step. Collections are persistent
 * builders, so [build] shares structure with the input state. The method names follow the spec 12
 * §4.3 pseudo-code one to one; the only representation change is that object references (open
 * voice user, pending split, block identity) are ids here.
 */
internal class Draft(s: ConversationState) {
    var ref = s.ref
    var entries: PersistentList.Builder<Entry> = s.entries.builder()
    /** The real timeline while a history page is converted into a scratch list (§5.1 prepend). */
    var kept: PersistentList.Builder<Entry>? = null
    val tools = s.tools.builder()
    val perms = s.perms.builder()
    val orphans = s.orphanResults.builder()
    val unattributed = s.unattributed.builder()
    val queue = s.queue.builder()
    val dispatchedFromTray = s.dispatchedFromTray.builder()

    var status = s.status
    var inTurn = s.inTurn
    var turnDepth = s.turnDepth
    var turnIsLocal = s.turnIsLocal
    var turnHasContent = s.turnHasContent
    var localTurnsPending = s.localTurnsPending
    var promptSinceTurnEnd = s.promptSinceTurnEnd
    var compactPending = s.compactPending
    val pendingInjects = s.pendingInjects.builder()
    val agentApprovals = s.agentApprovals.builder()
    var voiceActive = s.voiceActive
    var openVoiceUserId = s.openVoiceUserId
    var speechAnchor = s.speechAnchor

    var stall = s.stall
    var termination = s.termination
    var connectionBanner = s.connectionBanner
    var gapPossible = s.gapPossible

    var checkpoint = s.checkpoint
    var counters = s.counters
    var history = s.history

    var pendingSplit = s.pendingSplit
    var expectStopAck = s.expectStopAck
    var turnNeedsReconcile = s.turnNeedsReconcile

    var connection = s.connection
    var userStart = s.userStart
    var awaitingSessionStarted = s.awaitingSessionStarted
    var stoppingRetried = s.stoppingRetried
    var startRequest = s.startRequest
    val preStart = s.preStart.builder()
    var reloading = s.reloading
    val reloadBuffer = s.reloadBuffer.builder()

    var nextId = s.nextId
    /** Set while converting a REST page: ids derive from the absolute history index. */
    var historyIds: HistoryIdSource? = null

    val effects = ArrayList<ConversationEffect>()

    fun build() = ConversationState(
        ref = ref,
        entries = entries.build(),
        tools = tools.build(),
        perms = perms.build(),
        orphanResults = orphans.build(),
        unattributed = unattributed.build(),
        queue = queue.build(),
        dispatchedFromTray = dispatchedFromTray.build(),
        status = status,
        inTurn = inTurn,
        turnDepth = turnDepth,
        turnIsLocal = turnIsLocal,
        turnHasContent = turnHasContent,
        localTurnsPending = localTurnsPending,
        promptSinceTurnEnd = promptSinceTurnEnd,
        compactPending = compactPending,
        pendingInjects = pendingInjects.build(),
        agentApprovals = agentApprovals.build(),
        voiceActive = voiceActive,
        openVoiceUserId = openVoiceUserId,
        speechAnchor = speechAnchor,
        stall = stall,
        termination = termination,
        connectionBanner = connectionBanner,
        gapPossible = gapPossible,
        checkpoint = checkpoint,
        counters = counters,
        history = history,
        pendingSplit = pendingSplit,
        expectStopAck = expectStopAck,
        turnNeedsReconcile = turnNeedsReconcile,
        connection = connection,
        userStart = userStart,
        awaitingSessionStarted = awaitingSessionStarted,
        stoppingRetried = stoppingRetried,
        startRequest = startRequest,
        preStart = preStart.build(),
        reloading = reloading,
        reloadBuffer = reloadBuffer.build(),
        nextId = nextId,
    )

    val isAgent get() = ref.kind == SessionKind.AGENT
    val isOrchestrator get() = ref.kind == SessionKind.ORCHESTRATOR

    // ───────────────────────── ids ─────────────────────────

    fun newEntryId(): String = historyIds?.entryId() ?: "e${nextId++}"

    fun newBlockId(): String = historyIds?.blockId() ?: "b${nextId++}"

    // ───────────────────────── lookups ─────────────────────────

    fun last(): Entry? = entries.lastOrNull()

    private fun indexOfEntry(list: List<Entry>, id: String): Int {
        for (i in list.indices.reversed()) if (list[i].id == id) return i
        return -1
    }

    /** The list and index holding entry [id]: the current (possibly scratch) list first, then [kept]. */
    private fun locate(id: String): Pair<MutableList<Entry>, Int>? {
        val i = indexOfEntry(entries, id)
        if (i >= 0) return entries to i
        val k = kept ?: return null
        val j = indexOfEntry(k, id)
        return if (j >= 0) k to j else null
    }

    fun tool(toolUseId: String): ToolBlock? {
        val entryId = tools[toolUseId] ?: return null
        val (list, i) = locate(entryId) ?: return null
        return (list[i] as? AssistantEntry)?.blocks?.firstOrNull { it is ToolBlock && it.toolUseId == toolUseId } as? ToolBlock
    }

    fun updateTool(toolUseId: String, f: (ToolBlock) -> ToolBlock): Boolean {
        val entryId = tools[toolUseId] ?: return false
        val (list, i) = locate(entryId) ?: return false
        val e = list[i] as? AssistantEntry ?: return false
        val bi = e.blocks.indexOfFirst { it is ToolBlock && it.toolUseId == toolUseId }
        if (bi < 0) return false
        list[i] = e.copy(blocks = e.blocks.set(bi, f(e.blocks[bi] as ToolBlock)))
        return true
    }

    fun updatePermission(requestId: String, f: (PermissionBlock) -> PermissionBlock): Boolean {
        val entryId = perms[requestId] ?: return false
        val (list, i) = locate(entryId) ?: return false
        val e = list[i] as? AssistantEntry ?: return false
        val bi = e.blocks.indexOfFirst { it is PermissionBlock && it.requestId == requestId }
        if (bi < 0) return false
        list[i] = e.copy(blocks = e.blocks.set(bi, f(e.blocks[bi] as PermissionBlock)))
        return true
    }

    /** Updates the block with [blockId] anywhere (used for R-4 inference, where the id may be empty). */
    fun updateBlockById(blockId: String, f: (Block) -> Block) {
        for (list in listOfNotNull(entries, kept)) {
            for (i in list.indices.reversed()) {
                val e = list[i] as? AssistantEntry ?: continue
                val bi = e.blocks.indexOfFirst { it.id == blockId }
                if (bi >= 0) {
                    list[i] = e.copy(blocks = e.blocks.set(bi, f(e.blocks[bi])))
                    return
                }
            }
        }
    }

    /** Applies [f] to every block of every assistant entry; entries whose blocks did not change are kept as is. */
    inline fun mapAllBlocks(f: (Block) -> Block) {
        for (i in entries.indices) {
            val e = entries[i] as? AssistantEntry ?: continue
            var nb: PersistentList.Builder<Block>? = null                      // allocated on the first change only
            for (j in e.blocks.indices) {
                val b = e.blocks[j]
                val n = f(b)
                if (n != b) {
                    if (nb == null) nb = e.blocks.builder()
                    nb[j] = n
                }
            }
            if (nb != null) entries[i] = e.copy(blocks = nb.build())
        }
    }

    fun allToolBlocks(): List<ToolBlock> {
        val out = ArrayList<ToolBlock>()
        for (list in listOfNotNull(kept, entries)) {
            for (e in list) if (e is AssistantEntry) for (b in e.blocks) if (b is ToolBlock) out += b
        }
        return out
    }

    // ───────────────────────── timeline helpers (§4.3) ─────────────────────────

    /** I-1: the last entry if it is an assistant run, else a new run appended. Returns its index. */
    fun tail(): Int {
        val e = last()
        if (e is AssistantEntry) return entries.lastIndex
        finalizeOpenVoiceUser()
        entries.add(AssistantEntry(newEntryId()))
        return entries.lastIndex
    }

    fun openBlock(thinking: Boolean, scope: BlockScope): StreamedBlock? {
        val e = last() as? AssistantEntry ?: return null
        val b = e.blocks.lastOrNull() as? StreamedBlock ?: return null
        return if ((b is ThinkingBlock) == thinking && b.scope == scope && b.streaming) b else null
    }

    /** Replaces the last block of the last entry (which must be an assistant run). */
    fun replaceLastBlock(f: (Block) -> Block) {
        val e = entries.last() as AssistantEntry
        entries[entries.lastIndex] = e.copy(blocks = e.blocks.set(e.blocks.lastIndex, f(e.blocks.last())))
    }

    fun closeOpenBlock() {
        val e = last() as? AssistantEntry ?: return
        val b = e.blocks.lastOrNull() as? StreamedBlock ?: return
        if (b.streaming) replaceLastBlock { (it as StreamedBlock).with(streaming = false, implicitlyClosed = true) }
    }

    /** Appends [b] to the tail run; returns the id of the run's entry. */
    fun pushBlock(b: Block): String {
        closeOpenBlock()
        val t = tail()
        val ps = pendingSplit
        if (ps != null && !(b is StreamedBlock && (b is ThinkingBlock) == ps.thinking && b.scope == ps.scope)) {
            pendingSplit = null
        }
        val e = entries[t] as AssistantEntry
        entries[t] = e.copy(blocks = e.blocks.add(b))
        return e.id
    }

    /** Every non-assistant entry goes through here (I-2). */
    fun appendEntry(x: Entry) {
        if (x.id != openVoiceUserId) finalizeOpenVoiceUser()
        val e = last()
        if (e is AssistantEntry) {
            val b = e.blocks.lastOrNull()
            if (b is StreamedBlock && b.streaming) {
                replaceLastBlock { (it as StreamedBlock).with(streaming = false) }
                pendingSplit = SplitRef(b.id, b is ThinkingBlock, b.scope, b.text)
            }
        }
        entries.add(x)
        if (x is UserEntry) promptSinceTurnEnd = true
    }

    fun appendNoticeOnce(kind: NoticeKind) {
        val e = last()
        if (e is NoticeEntry && e.notice == kind) return
        appendEntry(NoticeEntry(newEntryId(), kind, ""))
    }

    fun currentScope(): BlockScope =
        if (isOrchestrator && voiceActive && !inTurn) BlockScope.VOICE else BlockScope.TURN

    fun clearStall() { stall = null }

    /**
     * BG-1: an orchestrator turn this client did not start gets a "background" notice before its run,
     * unless the entry right before the new run is a user entry (an O-3 echo from another device, an
     * inject or a voice transcript is a visible prompt, not background).
     */
    fun maybeBackgroundNotice() {
        if (!isOrchestrator || !inTurn) return
        if (!turnIsLocal && !turnHasContent && last() !is UserEntry) {
            appendEntry(NoticeEntry(newEntryId(), NoticeKind.BACKGROUND, ""))
        }
        turnHasContent = true
    }

    /** TL-3: content without a turn start (observer attached mid-turn, replayed tail, command output). */
    fun ensureAgentTurn() {
        if (isAgent && !inTurn) { inTurn = true; promptSinceTurnEnd = false }
    }

    fun setAgentStatus(s: SessionStatus) { if (isAgent && inTurn) status = s }

    fun finalizeOpenVoiceUser() {
        val id = openVoiceUserId ?: return
        openVoiceUserId = null
        for (list in listOfNotNull(entries, kept)) {
            val i = list.indexOfLast { it.id == id }
            if (i >= 0) {
                val u = list[i] as UserEntry
                list[i] = u.copy(streaming = false)
                return
            }
        }
    }

    // ───────────────────────── streamed text / thinking ─────────────────────────

    fun onDelta(thinking: Boolean, text: String, scope: BlockScope = BlockScope.TURN) {
        clearStall()
        if (scope == BlockScope.TURN) {
            ensureAgentTurn()
            setAgentStatus(if (thinking) SessionStatus.THINKING else SessionStatus.STREAMING)
        }
        val b = openBlock(thinking, scope)
        if (b != null) {
            replaceLastBlock { (it as StreamedBlock).with(text = b.text + text) }
            return
        }
        if (scope == BlockScope.TURN) maybeBackgroundNotice()
        val ps = pendingSplit
        val cont = if (ps != null && ps.thinking == thinking && ps.scope == scope) ps else null
        pendingSplit = null
        pushBlock(streamedBlock(thinking, newBlockId(), text, streaming = true, scope = scope, continuationPrefix = cont?.text))
    }

    fun onComplete(thinking: Boolean, text: String, scope: BlockScope = BlockScope.TURN) {
        clearStall()
        if (scope == BlockScope.TURN) {
            ensureAgentTurn()
            setAgentStatus(if (thinking) SessionStatus.THINKING else SessionStatus.STREAMING)
        }
        val b = openBlock(thinking, scope)
        if (b != null) {
            if (isHistoryDuplicate(thinking, text, b)) { removeBlock(b); return }
            var full = text
            val prefix = b.continuationPrefix
            if (prefix != null && full.startsWith(prefix)) full = full.substring(prefix.length)
            replaceLastBlock { (it as StreamedBlock).with(text = full, streaming = false) }
            return
        }
        if (isDuplicateComplete(thinking, text)) return
        var full = text
        val ps = pendingSplit
        if (ps != null && ps.thinking == thinking && ps.scope == scope) {
            if (full.startsWith(ps.text)) full = full.substring(ps.text.length)
            pendingSplit = null
            if (full.isEmpty()) return
        }
        if (scope == BlockScope.TURN) maybeBackgroundNotice()
        pushBlock(streamedBlock(thinking, newBlockId(), full, streaming = false, scope = scope))
    }

    /** §5.4: a `*_complete` with no open block whose text the tail run already shows. */
    fun isDuplicateComplete(thinking: Boolean, text: String): Boolean {
        val e = last() as? AssistantEntry ?: return false
        return e.blocks.any {
            it is StreamedBlock && (it is ThinkingBlock) == thinking && it.text == text &&
                (it.origin == BlockOrigin.HISTORY || it.implicitlyClosed)
        }
    }

    /** §5.4: the open block's final text already came from REST. */
    fun isHistoryDuplicate(thinking: Boolean, text: String, open: Block): Boolean {
        val e = last() as? AssistantEntry ?: return false
        return e.blocks.any {
            it.id != open.id && it is StreamedBlock && (it is ThinkingBlock) == thinking &&
                it.origin == BlockOrigin.HISTORY && it.text == text
        }
    }

    /** Only used by the §5.4 dedupe; [b] is always in the tail run. */
    fun removeBlock(b: Block) {
        val t = entries.lastIndex
        val e = entries[t] as AssistantEntry
        val nb = e.blocks.removeAll { it.id == b.id }
        if (nb.isEmpty()) entries.removeAt(t) else entries[t] = e.copy(blocks = nb)   // I-10
    }

    // ───────────────────────── tools (§4.3, §4.5) ─────────────────────────

    fun onToolUse(id: String?, name: String?, input: JsonObject?) {
        clearStall()
        val scope = currentScope()
        if (scope == BlockScope.TURN) { ensureAgentTurn(); setAgentStatus(SessionStatus.TOOL_USE) }
        if (!id.isNullOrEmpty() && tools.containsKey(id)) {                       // I-11
            updateTool(id) { tb ->
                var n = tb
                if (n.toolName.isEmpty() && name != null) n = n.copy(toolName = name)
                if (input != null && input.isNotEmpty()) n = n.copy(toolInput = input)
                n
            }
            return
        }
        if (scope == BlockScope.TURN) maybeBackgroundNotice()
        val tb = ToolBlock(newBlockId(), id ?: "", name ?: "", input ?: EMPTY_OBJECT, scope = scope)
        val entryId = pushBlock(tb)
        if (!id.isNullOrEmpty()) {
            tools[id] = entryId
            orphans.remove(id)?.let { r -> updateTool(id) { applyResult(it, r, inferred = false) } }   // R-2
        }
    }

    fun onToolResult(id: String?, output: JsonElement?, isError: Boolean?) {
        clearStall()
        deliverResult(id ?: "", PendingResult(normalizeOutput(output), isError == true, ResultOrigin.LIVE))
    }

    /** R-1 … R-4. */
    fun deliverResult(id: String, r: PendingResult) {
        if (id.isNotEmpty()) {
            if (tool(id) != null) { updateTool(id) { applyResult(it, r, inferred = false) }; return }
            val prev = orphans[id]
            if (prev == null || r.output != "" || prev.output == "") orphans[id] = r
            if (r.origin == ResultOrigin.LIVE) turnNeedsReconcile = true
            return
        }
        if (r.origin == ResultOrigin.LIVE) {
            val running = allToolBlocks().filter { it.status == ToolStatus.RUNNING }
            if (running.size == 1) {
                updateBlockById(running[0].id) { applyResult(it as ToolBlock, r, inferred = true) }
                turnNeedsReconcile = true
                return
            }
        }
        unattributed.add(r)
        if (r.origin == ResultOrigin.LIVE) turnNeedsReconcile = true
    }

    /** R-5: never downgrade; a reconcile overrides inferred or empty live results only. */
    fun applyResult(tb: ToolBlock, r: PendingResult, inferred: Boolean): ToolBlock {
        val authoritative = r.origin == ResultOrigin.RECONCILE
        val final = tb.status == ToolStatus.DONE || tb.status == ToolStatus.ERROR
        if (!authoritative && final && !tb.output.isNullOrEmpty() && r.output == "") return tb
        if (authoritative && final && !tb.output.isNullOrEmpty() && !tb.inferred) return tb
        return tb.copy(
            output = r.output,
            status = if (r.isError) ToolStatus.ERROR else ToolStatus.DONE,
            inferred = inferred,
            executing = false,
            progress = null,
        )
    }

    fun onToolExecuting(id: String?) {
        if (id.isNullOrEmpty()) return
        updateTool(id) { if (it.status == ToolStatus.RUNNING) it.copy(executing = true) else it }
    }

    fun onToolProgress(id: String?, elapsed: Double?, message: String?) {
        if (id.isNullOrEmpty()) return
        updateTool(id) { if (it.status == ToolStatus.RUNNING) it.copy(progress = ToolProgressInfo(elapsed, message)) else it }
    }

    // ───────────────────────── permissions (§4.6) ─────────────────────────

    fun onPermissionRequest(rid: String?, name: String?, input: JsonObject?) {
        clearStall()
        if (rid == null || perms.containsKey(rid)) return                        // I-11
        ensureAgentTurn()
        val pb = PermissionBlock(newBlockId(), rid, name ?: "", input ?: EMPTY_OBJECT)
        perms[rid] = pushBlock(pb)
    }

    /** PM-2: updates the block in place; never appends an entry (fixes W-5). */
    fun onPermissionResolved(rid: String?, decision: String?, responder: String?, message: String?) {
        clearStall()
        if (rid == null) return
        updatePermission(rid) {
            it.copy(
                state = if (decision == "allow") PermissionState.ALLOWED else PermissionState.DENIED,
                responder = responder,
                message = message,
            )
        }
    }

    // ───────────────────────── turns ─────────────────────────

    fun beginTurn() {
        inTurn = true; promptSinceTurnEnd = false; turnHasContent = false; pendingSplit = null
        status = if (isAgent) SessionStatus.PROCESSING else SessionStatus.STREAMING
    }

    /** I-7 for scope `turn`; PM-4; R-6. */
    fun endTurn(@Suppress("UNUSED_PARAMETER") reason: String) {
        val wasInTurn = inTurn
        var sweptLiveTool = false
        mapAllBlocks { b ->
            if (b.scope != BlockScope.TURN) return@mapAllBlocks b
            when (b) {
                is StreamedBlock -> if (b.streaming) b.with(streaming = false) else b
                is ToolBlock -> if (b.status == ToolStatus.RUNNING) {
                    if (b.origin == BlockOrigin.LIVE) sweptLiveTool = true
                    b.copy(status = ToolStatus.NO_RESULT, executing = false, progress = null)
                } else b
                is PermissionBlock -> if (b.state == PermissionState.PENDING) {
                    b.copy(state = PermissionState.DENIED, responder = "system", message = "stream ended")
                } else b
            }
        }
        pendingSplit = null; stall = null
        inTurn = false; turnDepth = 0; turnHasContent = false; turnIsLocal = false
        dispatchedFromTray.clear()
        if (status != SessionStatus.STOPPED && status != SessionStatus.TERMINATED) {
            status = if (compactPending) SessionStatus.COMPACTING else SessionStatus.IDLE
        }
        if (sweptLiveTool) turnNeedsReconcile = true
        if (wasInTurn || sweptLiveTool) {
            effects += ConversationEffect.TurnEnded
            // R-7: reconcile tool results from REST when live delivery was incomplete.
            val liveOrphans = orphans.values.any { it.origin == ResultOrigin.LIVE }
            if (isAgent && ref.sdkId != null && (turnNeedsReconcile || unattributed.isNotEmpty() || liveOrphans)) {
                effects += ConversationEffect.ScheduleReconcile
            }
            turnNeedsReconcile = false
        }
    }

    /** I-7 for scope `voice`; VT-3. */
    fun endVoice() {
        finalizeOpenVoiceUser(); speechAnchor = null
        mapAllBlocks { b ->
            if (b.scope != BlockScope.VOICE) return@mapAllBlocks b
            when (b) {
                is StreamedBlock -> if (b.streaming) b.with(streaming = false) else b
                is ToolBlock -> if (b.status == ToolStatus.RUNNING) {
                    b.copy(status = ToolStatus.NO_RESULT, executing = false, progress = null)
                } else b
                else -> b
            }
        }
        voiceActive = false
    }

    fun markInjectSent(text: String) {
        val i = entries.indexOfFirst { it is UserEntry && it.origin == UserOrigin.INJECT && it.state == UserState.PENDING && it.text == text }
        if (i >= 0) entries[i] = (entries[i] as UserEntry).copy(state = UserState.SENT)
    }

    /** Rebuilds the id → entry indexes from the timeline (after a history page re-arranged entries). */
    fun rebuildIndexes() {
        tools.clear(); perms.clear()
        for (e in entries) {
            if (e !is AssistantEntry) continue
            for (b in e.blocks) {
                if (b is ToolBlock && b.toolUseId.isNotEmpty() && !tools.containsKey(b.toolUseId)) tools[b.toolUseId] = e.id
                if (b is PermissionBlock && !perms.containsKey(b.requestId)) perms[b.requestId] = e.id
            }
        }
    }

    companion object {
        val EMPTY_OBJECT = JsonObject(emptyMap())
    }
}

/** Derives ids from the absolute REST index (spec 14 §3.1): `h:<sdkId>:<index>` / `h:<sdkId>:<index>:<block>`. */
internal class HistoryIdSource(private val sessionKey: String) {
    var absIndex: Int = 0
    var blockIndex: Int = 0

    fun entryId() = "h:$sessionKey:$absIndex"
    fun blockId() = "h:$sessionKey:$absIndex:$blockIndex"
}

internal fun streamedBlock(
    thinking: Boolean,
    id: String,
    text: String,
    streaming: Boolean,
    scope: BlockScope,
    origin: BlockOrigin = BlockOrigin.LIVE,
    continuationPrefix: String? = null,
): StreamedBlock =
    if (thinking) ThinkingBlock(id, text, streaming, scope, origin, continuationPrefix = continuationPrefix)
    else TextBlock(id, text, streaming, scope, origin, continuationPrefix = continuationPrefix)

internal fun StreamedBlock.with(
    text: String = this.text,
    streaming: Boolean = this.streaming,
    implicitlyClosed: Boolean = this.implicitlyClosed,
): StreamedBlock = when (this) {
    is TextBlock -> copy(text = text, streaming = streaming, implicitlyClosed = implicitlyClosed)
    is ThinkingBlock -> copy(text = text, streaming = streaming, implicitlyClosed = implicitlyClosed)
}

/**
 * `normalizeOutput` (R-3): strings unchanged, `null` → `""`, anything else serialised compactly with
 * key order preserved (the `JSON.stringify` of the web client).
 */
fun normalizeOutput(x: JsonElement?): String = when {
    x == null || x is JsonNull -> ""
    x is JsonPrimitive && x.isString -> x.content
    else -> x.toString()
}

internal fun <T> List<T>.toPersistent(): PersistentList<T> = if (isEmpty()) persistentListOf() else toPersistentList()
