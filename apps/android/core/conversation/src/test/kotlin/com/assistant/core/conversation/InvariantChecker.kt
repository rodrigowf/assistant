package com.assistant.core.conversation

import com.assistant.core.protocol.ServerFrame

/**
 * Structural invariants of spec 12 §4.1 that hold on every reducer output, plus the ones that hold
 * right after specific inputs. Used after every step of every fixture and of the random sequences.
 */
object InvariantChecker {

    fun check(s: ConversationState): List<String> {
        val v = ArrayList<String>()
        val entryIds = HashSet<String>()
        val blockIds = HashSet<String>()
        val toolIds = HashMap<String, String>()
        val requestIds = HashMap<String, String>()
        for (e in s.entries) {
            if (!entryIds.add(e.id)) v += "A-4.4.2: duplicate entry id ${e.id}"
            if (e !is AssistantEntry) continue
            if (e.blocks.isEmpty()) v += "I-10: empty assistant entry ${e.id}"
            for (b in e.blocks) {
                if (!blockIds.add(b.id)) v += "A-4.4.2: duplicate block id ${b.id}"
                if (b is ToolBlock && b.toolUseId.isNotEmpty()) {
                    if (toolIds.put(b.toolUseId, e.id) != null) v += "I-11: two tool cards for ${b.toolUseId}"
                    if (s.tools[b.toolUseId] != e.id) v += "index: tools[${b.toolUseId}] = ${s.tools[b.toolUseId]}, card is in ${e.id}"
                    if (s.orphanResults.containsKey(b.toolUseId)) v += "R-2: orphan for ${b.toolUseId} not attached to its card"
                }
                if (b is PermissionBlock) {
                    if (requestIds.put(b.requestId, e.id) != null) v += "I-11: two permission blocks for ${b.requestId}"
                    if (s.perms[b.requestId] != e.id) v += "index: perms[${b.requestId}] = ${s.perms[b.requestId]}, block is in ${e.id}"
                }
            }
        }
        for (k in s.tools.keys) if (k !in toolIds) v += "index: tools has $k but no card"
        for (k in s.perms.keys) if (k !in requestIds) v += "index: perms has $k but no block"
        // I-4: only the last block of the last entry may be streaming (plus nothing else open)
        s.entries.forEachIndexed { i, e ->
            if (e is AssistantEntry) e.blocks.forEachIndexed { j, b ->
                val isTailBlock = i == s.entries.lastIndex && j == e.blocks.lastIndex
                if (b is StreamedBlock && b.streaming && !isTailBlock) v += "I-4: open block ${b.id} is not the tail block"
            }
            if (e is UserEntry && e.streaming && e.id != s.openVoiceUserId) v += "§4.7: streaming user entry ${e.id} is not the open voice transcript"
        }
        // R-5: a card never returns to running once it has output
        for (e in s.entries) if (e is AssistantEntry) for (b in e.blocks) {
            if (b is ToolBlock && b.status == ToolStatus.RUNNING && b.output != null) v += "R-5: running card ${b.toolUseId} has output"
        }
        return v
    }

    /** I-7 after inputs that end a turn and/or voice; I-15 for transport inputs. */
    fun checkStep(before: ConversationState, input: ConversationInput?, after: ConversationState): List<String> {
        val v = ArrayList<String>()
        val frame = (input as? ConversationInput.Frame)?.frame
        // Only single-frame steps are checked: a frame held behind a pending start (SEQ-5) or a reload
        // (§5.6) is not applied yet, and session_started / a completed reload apply several held frames
        // in one step (content may legitimately follow the turn end). Our own stop ack is swallowed.
        val startErrorReleasingHeld = frame is ServerFrame.Error && frame.error in ConversationReducer.START_ERRORS &&
            before.awaitingSessionStarted && before.preStart.isNotEmpty()                     // SEQ-8
        val multiFrame = frame is ServerFrame.SessionStarted || input is ConversationInput.HistoryPage ||
            input is ConversationInput.ReloadFailed || startErrorReleasingHeld
        val held = before.awaitingSessionStarted || before.reloading || multiFrame
        val turnEnded = !held && before.inTurn && !after.inTurn
        val voiceEnded = !held && before.voiceActive && !after.voiceActive
        val stoppedOrTerminated = !held &&
            ((frame is ServerFrame.SessionStopped && !before.expectStopAck) || frame is ServerFrame.SessionTerminated)
        if (turnEnded || stoppedOrTerminated) v += openContent(after, BlockScope.TURN, "I-7 (endTurn)")
        if (voiceEnded || stoppedOrTerminated) v += openContent(after, BlockScope.VOICE, "I-7 (endVoice)")
        val transport = input is ConversationInput.SocketClosed || input is ConversationInput.SocketOpened ||
            input is ConversationInput.Resync || input is ConversationInput.DismissBanner ||
            (frame is ServerFrame.Error && frame.error in ConversationReducer.START_ERRORS && !startErrorReleasingHeld)
        if (transport && before.entries != after.entries) v += "I-15: transport input $input changed the entries"
        // I-14: watcher frames about OTHER sessions never touch this conversation (OPEN-3 handles its own close;
        // PM-5 approvals are side state, never entries)
        val ownClose = frame is ServerFrame.AgentSessionClosed && frame.sessionId == before.ref.localId
        if ((frame is ServerFrame.AgentSessionOpened || frame is ServerFrame.AgentSessionClosed) && !ownClose && before != after) {
            v += "I-14: watcher frame for another session changed the conversation"
        }
        if (frame is ServerFrame.NestedSessionEvent && before.entries != after.entries) v += "PM-5: nested_session_event changed the timeline"
        return v
    }

    private fun openContent(s: ConversationState, scope: BlockScope, rule: String): List<String> {
        val v = ArrayList<String>()
        for (e in s.entries) if (e is AssistantEntry) for (b in e.blocks) {
            if (b.scope != scope) continue
            if (b is StreamedBlock && b.streaming) v += "$rule: block ${b.id} still streaming"
            if (b is ToolBlock && b.status == ToolStatus.RUNNING) v += "$rule: tool ${b.toolUseId} still running"
            if (b is PermissionBlock && b.state == PermissionState.PENDING && scope == BlockScope.TURN) v += "$rule: permission ${b.requestId} still pending"
        }
        return v
    }
}
