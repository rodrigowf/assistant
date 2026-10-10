package com.assistant.core.conversation

import com.assistant.core.model.ConnectionState
import com.assistant.core.model.SessionStatus
import com.assistant.core.protocol.ServerFrame
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull

/** spec 12 §4.3 `reduce(f)`: one server frame, already deduplicated by the connection manager. */
internal fun Draft.reduceFrame(f: ServerFrame) {
    when (f) {
        is ServerFrame.TextDelta -> onDelta(thinking = false, text = f.text)
        is ServerFrame.TextComplete -> onComplete(thinking = false, text = f.text)
        is ServerFrame.ThinkingDelta -> onDelta(thinking = true, text = f.text)
        is ServerFrame.ThinkingComplete -> onComplete(thinking = true, text = f.text)
        is ServerFrame.ToolUse -> onToolUse(f.toolUseId, f.toolName, f.toolInput)
        is ServerFrame.ToolResult -> onToolResult(f.toolUseId, f.output, f.isError)
        is ServerFrame.ToolExecuting -> onToolExecuting(f.toolUseId)
        is ServerFrame.ToolProgress -> onToolProgress(f.toolUseId, f.elapsedSeconds, f.message)
        is ServerFrame.PermissionRequest -> onPermissionRequest(f.requestId, f.toolName, f.toolInput)
        is ServerFrame.PermissionResolved -> onPermissionResolved(f.requestId, f.decision, f.responder, f.message)
        is ServerFrame.UserMessage -> onUserMessage(f)
        is ServerFrame.Status -> f.status?.let { onStatus(it) }
        is ServerFrame.TurnComplete -> onTurnComplete(f)
        is ServerFrame.CompactComplete -> onCompactComplete(f)
        is ServerFrame.SessionStalled -> if (inTurn || status.busy) {
            stall = Stall(f.elapsedSeconds, f.lastToolName, f.lastToolUseId)
        }
        is ServerFrame.Error -> onError(f.error, f.detail)
        is ServerFrame.SessionTerminated -> {
            termination = Termination(f.reason, f.detail, f.sdkSessionId)
            queue.clear()
            status = SessionStatus.TERMINATED
            endTurn("terminated"); endVoice()
            checkpoint = null
        }
        is ServerFrame.SessionStopped -> {
            if (expectStopAck) { expectStopAck = false; return }               // our own stop / close
            closedByServer()
        }
        is ServerFrame.VoiceEvent -> onVoiceEvent(f.event)
        is ServerFrame.VoiceOwnerActive -> if (f.active) voiceActive = true else endVoice()
        is ServerFrame.VoiceEnded, is ServerFrame.VoiceStopped -> endVoice()
        is ServerFrame.SessionStarted -> if (f.voice == true) voiceActive = true
        is ServerFrame.NestedSessionEvent -> onNestedSessionEvent(f)
        is ServerFrame.AgentSessionClosed -> {
            // OPEN-3: this conversation itself left the pool (FOCUS-2: the flag must match its kind)
            if (f.sessionId == ref.localId && f.isOrchestrator == isOrchestrator) closedByServer()
        }
        is ServerFrame.ModelChanged -> f.modelInfo?.modelInfo?.contextWindow?.let { counters = counters.copy(contextWindow = it) }
        is ServerFrame.ModelInfo -> f.modelInfo?.modelInfo?.contextWindow?.let { counters = counters.copy(contextWindow = it) }
        // Routed or consumed outside the reducer: agent_session_opened, voice_ending, voice_command, voice_audio_out,
        // voice_connection_error, models_list, audio_upload, ping, unknown.
        else -> Unit
    }
}

/** PM-5. The runtime also routes `eventData` to the open agent view (`routeToAgentView`). */
private fun Draft.onNestedSessionEvent(f: ServerFrame.NestedSessionEvent) {
    if (!isOrchestrator) return
    val localId = f.sessionId ?: return
    val data = f.eventData ?: return
    val rid = (data["request_id"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    if (rid.isNullOrEmpty()) return
    val i = agentApprovals.indexOfFirst { it.localId == localId && it.requestId == rid }
    when (f.eventType) {
        "permission_request" -> if (i < 0) {
            val name = (data["tool_name"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: ""
            agentApprovals.add(AgentApproval(localId, rid, name, data["tool_input"] as? JsonObject ?: Draft.EMPTY_OBJECT))
        }
        "permission_resolved" -> if (i >= 0) agentApprovals.removeAt(i)
    }
}

private fun Draft.onUserMessage(f: ServerFrame.UserMessage) {
    if (f.queued) { queue.add(QueuedPrompt(f.text, QueueOwner.REMOTE)); return }   // I-12
    if (f.source == "shared_inject") {
        val i = pendingInjects.indexOf(f.text)
        if (i >= 0) { pendingInjects.removeAt(i); markInjectSent(f.text); return }
        appendEntry(UserEntry(newEntryId(), f.text, UserOrigin.INJECT, UserState.SENT))
        return
    }
    // Another device's voice message (`send_audio`); the sender's own socket gets no echo.
    if (f.source == "voice_message") {
        appendEntry(UserEntry(newEntryId(), f.text, UserOrigin.AUDIO, UserState.SENT))
        return
    }
    // A pre-O-6 backend re-echoes a queued prompt at dispatch, after status{processing} already moved
    // it from the tray into the timeline. It is not a new prompt, so it is swallowed BEFORE the
    // "observer of an interrupted turn" rule below (which would otherwise end the just-started turn).
    val d = dispatchedFromTray.indexOf(f.text)
    if (d >= 0) { dispatchedFromTray.removeAt(d); return }
    if (isAgent && inTurn) endTurn("superseded")                             // observer of an interrupted turn
    val i = queue.indexOfFirst { it.owner == QueueOwner.REMOTE && it.text == f.text }
    if (i >= 0) {
        val keep = queue.filterIndexed { k, q -> !(q.owner == QueueOwner.REMOTE && k <= i) }
        queue.clear(); queue.addAll(keep)
    }
    appendEntry(UserEntry(newEntryId(), f.text, UserOrigin.ECHO, UserState.SENT))
}

internal fun Draft.onStatus(s: String) {
    if (isAgent) {
        when (s) {
            "connecting" -> status = SessionStatus.CONNECTING
            "processing" -> {
                if (inTurn) endTurn("superseded")
                if (!promptSinceTurnEnd && queue.isNotEmpty()) {                 // a queued prompt was dispatched (FIFO)
                    val q = queue.removeAt(0)
                    dispatchedFromTray.add(q.text)
                    val origin = if (q.owner == QueueOwner.LOCAL) UserOrigin.LOCAL else UserOrigin.ECHO
                    appendEntry(UserEntry(newEntryId(), q.text, origin, UserState.SENT))
                }
                beginTurn()
            }
            "retrying" -> if (inTurn) status = SessionStatus.RETRYING
            "interrupted" -> {
                queue.clear()                                                  // the server dropped every queued prompt
                if (inTurn || status.busy) appendNoticeOnce(NoticeKind.INTERRUPTED)
                endTurn("interrupted")
            }
            else -> Unit                                                       // ST-1
        }
    } else {
        when (s) {
            "connecting" -> status = SessionStatus.CONNECTING
            "streaming" -> {
                val local = localTurnsPending > 0
                if (local) localTurnsPending -= 1
                turnDepth += 1
                if (turnDepth == 1) { beginTurn(); turnIsLocal = local } else if (local) turnIsLocal = true
            }
            "idle" -> {
                turnDepth = maxOf(0, turnDepth - 1)
                if (turnDepth == 0) endTurn("complete")
            }
            "interrupted" -> if (inTurn) appendNoticeOnce(NoticeKind.INTERRUPTED)
            else -> Unit
        }
    }
}

private fun Draft.onTurnComplete(f: ServerFrame.TurnComplete) {
    if (isAgent) {
        counters = counters.copy(
            cost = counters.cost + (f.cost ?: 0.0),
            turns = counters.turns + (f.numTurns ?: 1),
        )
        val it = f.inputTokens ?: (f.usage?.get("input_tokens") as? JsonPrimitive)?.content?.toLongOrNull()
        if (it != null && it > 0) counters = counters.copy(contextTokens = it)
        if (!f.sessionId.isNullOrEmpty() && ref.sdkId == null) ref = ref.copy(sdkId = f.sessionId)
        if (f.isError == true && !f.result.isNullOrEmpty()) {
            appendEntry(NoticeEntry(newEntryId(), NoticeKind.ERROR, f.result!!, mapOf("code" to "turn_error")))
        }
        endTurn("complete")
        if (ref.sdkId == null) effects += ConversationEffect.LookupSdkId     // ID-2 (Gemini sends no session_id)
    } else {
        val it = f.inputTokens
        if (it != null && it > 0) counters = counters.copy(contextTokens = it)
        // the orchestrator turn ends at status:idle
    }
}

private fun Draft.onCompactComplete(f: ServerFrame.CompactComplete) {
    if (isAgent) {
        appendEntry(NoticeEntry(newEntryId(), NoticeKind.COMPACTION, f.summary ?: "", mapOf("trigger" to f.trigger)))
    } else {
        appendEntry(
            NoticeEntry(
                newEntryId(), NoticeKind.COMPACTION, "",
                mapOf("trigger" to f.trigger, "tokens_before" to f.tokensBefore?.toString(), "tokens_after" to f.tokensAfter?.toString()),
            ),
        )
        f.tokensAfter?.let { counters = counters.copy(contextTokens = it) }
    }
    compactPending = false
    if (!inTurn && status == SessionStatus.COMPACTING) status = SessionStatus.IDLE
}

internal fun Draft.onError(code: String?, detail: String?) {
    if (code == null) return
    if (code == "interrupted") { if (inTurn) appendNoticeOnce(NoticeKind.INTERRUPTED); return }   // orchestrator agent loop
    val failures = if (isAgent) ConversationReducer.TURN_FAILURE_AGENT else ConversationReducer.TURN_FAILURE_ORCH
    if (code !in failures) { connectionOrSideError(code, detail); return }   // §4.4.4: never an entry
    appendEntry(NoticeEntry(newEntryId(), NoticeKind.ERROR, if (detail.isNullOrEmpty()) code else detail, mapOf("code" to code)))
    compactPending = false
    if (isAgent) { endTurn("error"); return }
    if (code == "invalid_audio" && localTurnsPending > 0) localTurnsPending -= 1   // no streaming will follow
    if (code in ConversationReducer.ORCH_NO_IDLE_AFTER) {
        turnDepth = maxOf(0, turnDepth - 1)
        if (turnDepth == 0) endTurn("error")
    }                                                                         // api_error/provider_error: status:idle follows
}

private fun Draft.connectionOrSideError(code: String, detail: String?) {
    when (code) {
        in ConversationReducer.START_ERRORS -> onStartError(code, detail)
        "not_started" -> sendStart()                                          // T-12: re-send start (reattach)
        "session_closed" -> closedByServer()                                  // OPEN-3: the reattach found it closed
        else -> effects += ConversationEffect.SideError(code, detail)
    }
}

/** OPEN-3: the session is gone from the server. The view stops here and the repository closes it. */
private fun Draft.closedByServer() {
    if (status != SessionStatus.TERMINATED) status = SessionStatus.STOPPED
    endTurn("stopped"); endVoice()
    if (awaitingSessionStarted) {                                            // no session_started will come (L-2)
        awaitingSessionStarted = false
        val held = preStart.toList()
        preStart.clear()
        for (g in held) dispatch(g)
    }
    effects += ConversationEffect.Closed(termination)
}

/** SEQ-8: a start error ends the wait for `session_started` and releases the held frames in order (L-2). */
private fun Draft.onStartError(code: String, detail: String?) {
    if (code == "orchestrator_stopping" && !stoppingRetried) {
        stoppingRetried = true
        effects += ConversationEffect.ScheduleStartRetry(1_000)                // T-12; keep waiting
        return
    }
    connection = ConnectionState.FAILED
    connectionBanner = ConnectionBanner(code, detail)                         // never an entry (I-15)
    effects += ConversationEffect.StartError(code, detail)
    if (awaitingSessionStarted) {
        awaitingSessionStarted = false
        val held = preStart.toList()
        preStart.clear()
        for (g in held) dispatch(g)
    }
}

// ───────────────────────── voice transcripts (§4.7) ─────────────────────────

internal fun Draft.onVoiceEvent(e: JsonObject) {
    val type = (e["type"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    if (type != null) {
        when (type) {
            "input_audio_buffer.speech_started" -> if (openVoiceUserId == null) speechAnchor = entries.size
            "conversation.item.input_audio_transcription.completed" -> voiceUserFinal(e.string("transcript"))
            "response.output_audio_transcript.delta", "response.audio_transcript.delta",
            "response.output_text.delta", "response.text.delta" -> {
                finalizeOpenVoiceUser()
                val delta = e.string("delta")
                if (!delta.isNullOrEmpty()) onDelta(thinking = false, text = delta, scope = BlockScope.VOICE)
            }
            "response.output_audio_transcript.done", "response.audio_transcript.done" -> voiceAssistantDone(e.string("transcript"))
            "response.output_text.done", "response.text.done" -> voiceAssistantDone(e.string("text"))
            "response.done" -> voiceTurnEnd()
            // response.function_call_arguments.done, response.output_item.added, conversation.item.created,
            // voice_status, voice_vad_state, voice_error, error, session.*: not timeline content (W-3).
            else -> Unit
        }
        return
    }
    // Gemini (no `type`): inputTranscription, outputTranscription, interrupted, turnComplete — in that order.
    val sc = e["serverContent"] as? JsonObject ?: return
    (sc["inputTranscription"] as? JsonObject)?.string("text")?.let { voiceUserFragment(it) }
    (sc["outputTranscription"] as? JsonObject)?.let { ot ->
        finalizeOpenVoiceUser()
        val text = ot.string("text")
        if (!text.isNullOrEmpty()) onDelta(thinking = false, text = text, scope = BlockScope.VOICE)
    }
    if (sc.truthy("interrupted")) voiceTurnEnd()
    if (sc.truthy("turnComplete")) voiceTurnEnd()
}

private fun JsonObject.string(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun JsonObject.truthy(key: String): Boolean {
    val v = this[key] ?: return false
    if (v is JsonPrimitive) return v.booleanOrNull ?: (v.isString && v.content.isNotEmpty())
    return true
}

private fun Draft.voiceUserFinal(text: String?) {
    if (text.isNullOrBlank()) return
    insertUserAtAnchor(UserEntry(newEntryId(), text, UserOrigin.VOICE, UserState.SENT))
}

/** Gemini coalescing: raw concatenation, as the backend persister does. */
private fun Draft.voiceUserFragment(text: String) {
    if (text.isEmpty()) return
    val openId = openVoiceUserId
    if (openId != null) {
        val i = entries.indexOfLast { it.id == openId }
        if (i >= 0) {
            val u = entries[i] as UserEntry
            entries[i] = u.copy(text = u.text + text)
            return
        }
    }
    val e = UserEntry(newEntryId(), text, UserOrigin.VOICE, UserState.SENT, streaming = true)
    insertUserAtAnchor(e)
    openVoiceUserId = e.id
}

/** I-9: the only positional insertion. It never splits a run. */
private fun Draft.insertUserAtAnchor(e: UserEntry) {
    val pos = speechAnchor ?: entries.size
    speechAnchor = null
    if (pos >= entries.size) { appendEntry(e); return }
    if (e.id != openVoiceUserId) finalizeOpenVoiceUser()
    entries.add(pos, e)                                                       // the tail run stays last and open
}

private fun Draft.voiceAssistantDone(text: String?) {
    finalizeOpenVoiceUser()
    val b = openBlock(thinking = false, scope = BlockScope.VOICE)
    if (b != null) {
        // empty text keeps the streamed content (Gemini)
        replaceLastBlock { (it as StreamedBlock).with(text = if (text.isNullOrEmpty()) b.text else text, streaming = false) }
        return
    }
    if (!text.isNullOrEmpty()) pushBlock(TextBlock(newBlockId(), text, streaming = false, scope = BlockScope.VOICE))
}

private fun Draft.voiceTurnEnd() {
    finalizeOpenVoiceUser()
    if (openBlock(thinking = false, scope = BlockScope.VOICE) != null) {
        replaceLastBlock { (it as StreamedBlock).with(streaming = false) }
    }
}
