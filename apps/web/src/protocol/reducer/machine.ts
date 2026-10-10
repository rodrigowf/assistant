/**
 * The conversation state machine: connection manager (spec 12 §3.3–§3.6, §5.2, §5.6) + the pure
 * conversation reducer (§4.3, §4.7, §5.1, §5.5). One class instance runs one step on a
 * copy-on-write draft (./draft.ts) and is then discarded.
 *
 * The method names and the order of operations follow the spec pseudo-code line by line
 * (including the 2026-10-03 clarifications: the `dispatchedFromTray` swallow before the superseded
 * rule, the SEQ-5 `error` exception, and BG-1 skipping the notice after a user entry). Comments
 * mark the places that go beyond it (each also listed in the W-05 report):
 * - D2 history `prepend` keeps `promptSinceTurnEnd`, `pendingSplit` and the open voice entry
 *   untouched (an older page is not a new prompt), and shifts `speechAnchor` by the prepended count.
 * - D3 `agent_session_closed` for this conversation's own `localId` stops it like `session_stopped`
 *   (§3.7 onWatcherEvent; FOCUS-2 kind check); `error{session_closed}` does the same and emits that
 *   close as a `watcher` effect. Either way the session directory then closes the view (OPEN-3).
 * - D5 a start error that fails the pending `start` releases the held `preStart` frames (lossless,
 *   L-2) instead of leaving them for a `session_started` that will not come.
 */
import { classifyUserLine, normalizeOutput } from '../history/classify';
import { canResume, checkpointFromResumeState, isDuplicateSeq, mergeResumeState } from '../resume/checkpoint';
import {
  busy,
  seqCapable,
  type AssistantEntry,
  type Block,
  type Conversation,
  type Entry,
  type LiveStatus,
  type NoticeEntry,
  type NoticeKind,
  type PermissionBlock,
  type PermissionResponder,
  type QueuedPrompt,
  type Provider,
  type SessionKind,
  type SessionStatus,
  type StreamBlock,
  type ToolBlock,
  type ToolResultData,
  type UserEntry,
} from '../types';
import type { StartMessage, VoiceStartMessage } from '../wire/client';
import { coerceFrame, isPlainObject } from '../wire/decode';
import type { MessagePreview, MessagesPage, ServerFrame, SessionStartedFrame } from '../wire/server';
import { Draft, type Loc, type Mutable } from './draft';
import type { ConversationInput, Effect, HistoryMode, StepResult } from './io';

const TURN_FAILURE_AGENT = ['send_failed', 'upstream_wedged', 'turn_timeout', 'command_failed', 'compact_failed'];
const TURN_FAILURE_ORCH = [
  'api_error',
  'provider_error',
  'send_failed',
  'send_audio_failed',
  'invalid_audio',
  'inject_text_failed',
  'compact_failed',
];
const ORCH_NO_IDLE_AFTER = ['send_failed', 'send_audio_failed', 'compact_failed'];
/** §4.4.4 "Start / connection": banner, never an entry. */
const START_ERRORS = ['start_timeout', 'start_failed', 'orchestrator_active', 'orchestrator_stopping'];
/** §4.4.4 "Voice": owned by the voice controller (§7.7); the reducer ignores them. */
const VOICE_ERRORS = [
  'voice_event_failed',
  'voice_audio_failed',
  'voice_restart_failed',
  'voice_config_busy',
  'not_voice_session',
  'cannot_switch_voice',
  'voice_relay_failed',
];
const DEFAULT_CONTEXT_WINDOW = 200000;
const IN_TURN_LIVE: readonly LiveStatus[] = ['streaming', 'tool_use', 'thinking'];

const VOICE_DELTAS = [
  'response.output_audio_transcript.delta',
  'response.audio_transcript.delta',
  'response.output_text.delta',
  'response.text.delta',
];
const VOICE_TRANSCRIPT_DONE = ['response.output_audio_transcript.done', 'response.audio_transcript.done'];
const VOICE_TEXT_DONE = ['response.output_text.done', 'response.text.done'];

function str(x: unknown): string {
  return typeof x === 'string' ? x : '';
}

function hasSeq(f: ServerFrame): f is ServerFrame & { seq: number; stream_id: string } {
  return typeof f.seq === 'number' && typeof f.stream_id === 'string' && f.stream_id !== '';
}

function inputObject(x: unknown): Record<string, unknown> {
  return isPlainObject(x) ? x : {};
}

function sameJson(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

function isStream(b: Block): b is StreamBlock {
  return b.type === 'text' || b.type === 'thinking';
}

// ───────────────────────────── initial state ─────────────────────────────

export interface InitialConversationOptions {
  localId: string;
  kind: SessionKind;
  sdkId?: string | null;
  provider?: Provider | null;
  live?: boolean;
  /** `pool/live` status at open: `streaming|tool_use|thinking` ⇒ in turn (ST-2, §5.2). */
  liveStatus?: LiveStatus | null;
  /** Voice is live on some device (orchestrator). */
  voiceActive?: boolean;
  /** The socket is already subscribed (tests and the fixture runner): status `idle`, conn `subscribed`. */
  subscribed?: boolean;
  /** Opened from the server's pool, not by a user action: every `start` reattaches (OPEN-2). Default: `subscribed`. */
  reattach?: boolean;
}

/** spec 12 §2.3 initial values. */
export function initialConversation(o: InitialConversationOptions): Conversation {
  const liveStatus = o.liveStatus ?? null;
  const midTurn = o.kind === 'agent' && liveStatus !== null && IN_TURN_LIVE.indexOf(liveStatus) >= 0;
  let status: SessionStatus = o.subscribed ? 'idle' : 'connecting';
  if (midTurn) status = liveStatus as SessionStatus;
  return {
    ref: {
      localId: o.localId,
      sdkId: o.sdkId ?? null,
      kind: o.kind,
      provider: o.provider ?? null,
      live: o.live ?? liveStatus !== null,
      liveStatus,
    },
    entries: [],
    orphanResults: [],
    unattributed: [],
    queue: [],
    dispatchedFromTray: [],
    status,
    inTurn: midTurn,
    turnDepth: 0,
    turnIsLocal: false,
    turnHasContent: false,
    localTurnsPending: 0,
    promptSinceTurnEnd: false,
    compactPending: false,
    pendingInjects: [],
    voiceActive: o.voiceActive === true,
    openVoiceUserId: null,
    speechAnchor: null,
    stall: null,
    termination: null,
    connectionBanner: null,
    gapPossible: false,
    agentApprovals: [],
    checkpoint: null,
    counters: { cost: 0, turns: 0, contextTokens: null, contextWindow: null },
    history: { loaded: false, startIndex: 0, totalCount: 0, hasMore: false },
    pendingSplit: null,
    expectStopAck: false,
    turnInferred: false,
    nextId: 1,
    conn: o.subscribed ? 'subscribed' : 'offline',
    awaitingSessionStarted: false,
    startRequest: null,
    preStart: [],
    reloading: false,
    reloadBuffer: [],
    stoppingRetried: false,
    reattach: o.reattach ?? o.subscribed === true,
  };
}

/** One pure step: the next state plus the effects to run (./io.ts). */
export function stepConversation(state: Conversation, input: ConversationInput): StepResult<Conversation> {
  const m = new Machine(state);
  m.input(input);
  return { state: m.finish(), effects: m.effects };
}

/** `stepConversation(state, input).state` (spec 13 §3.9 name). */
export function reduceConversation(state: Conversation, input: ConversationInput): Conversation {
  return stepConversation(state, input).state;
}

// ───────────────────────────── the machine ─────────────────────────────

class Machine extends Draft {
  readonly effects: Effect[] = [];
  /** Converting a history page into the scratch list (§5.1): no live-only side effects. */
  private scratch = false;

  finish(): Conversation {
    return this.finishState();
  }

  input(x: ConversationInput): void {
    switch (x.type) {
      case 'frame':
        return this.onFrame(x.frame);
      case 'history_page':
        return this.onHistoryPage(x.mode, x.response);
      case 'datachannel_event':
        return this.onVoiceEvent(x.event);
      case 'local_send':
        return this.localSend(x.text);
      case 'local_send_audio':
        this.appendEntry(this.userEntry(x.text ?? '', 'audio', 'sent'));
        this.s.localTurnsPending += 1;
        return;
      case 'local_inject':
        return this.localInject(x.text);
      case 'local_interrupt':
        if (this.s.ref.kind === 'agent' && this.s.queue.some((q) => q.owner === 'local'))
          this.s.queue = this.s.queue.filter((q) => q.owner !== 'local');
        return;
      case 'local_compact':
        this.s.compactPending = true;
        if (this.s.ref.kind === 'agent') this.s.status = 'compacting';
        else this.s.localTurnsPending += 1;
        return;
      case 'local_stop':
        this.s.expectStopAck = true;
        this.s.reattach = false; // we close or replace it ourselves: the next start is ours (OPEN-2)
        return;
      case 'voice_local_end':
        return this.endVoice();
      case 'socket_open':
        this.s.conn = 'open';
        return this.sendStart(x.start);
      case 'socket_closed':
        return this.onSocketClosed();
      case 'resend_start':
        if (this.s.conn === 'open' || this.s.conn === 'subscribed') this.sendStart(x.start);
        return;
      case 'begin_reload':
        this.s.reloading = true;
        return;
      case 'reload_failed':
        if (!this.s.reloading) return;
        this.s.reloading = false;
        this.s.gapPossible = true;
        return this.flushReload();
      case 'pool_status':
        return this.onPoolStatus(x.status);
      case 'sdk_id':
        if (!this.s.ref.sdkId && x.sdkId) this.s.ref = { ...this.s.ref, sdkId: x.sdkId };
        return;
      case 'dismiss_banner':
        this.s.connectionBanner = null;
        return;
      case 'clear_agent_approvals':
        if (this.s.agentApprovals.some((a) => a.localId === x.localId))
          this.s.agentApprovals = this.s.agentApprovals.filter((a) => a.localId !== x.localId);
        return;
    }
  }

  // ═════════════════════════ connection manager (§3.3–§3.6) ═════════════════════════

  private sendStart(override?: VoiceStartMessage): void {
    const ref = this.s.ref;
    let msg: StartMessage | VoiceStartMessage;
    if (override) {
      msg = override;
    } else {
      const m: StartMessage = { type: 'start', local_id: ref.localId };
      if (ref.sdkId) m.resume_sdk_id = ref.sdkId;
      const cp = this.s.checkpoint;
      if (cp && canResume(this.s as Conversation)) m.resume_from = { stream_id: cp.stream_id, seq: cp.seq }; // T-10
      msg = m;
    }
    if (this.s.reattach) msg = { ...msg, reattach: true }; // OPEN-2: never re-create a closed conversation
    this.s.startRequest = msg;
    this.s.awaitingSessionStarted = true;
    this.s.preStart = [];
    this.effects.push({ type: 'send', message: msg });
  }

  private onSocketClosed(): void {
    this.s.conn = 'offline';
    this.s.connectionBanner = { code: 'disconnected', detail: null }; // banner only (A-8.2, I-15)
    const ref = this.s.ref;
    if (this.s.inTurn && (ref.kind === 'orchestrator' || !seqCapable(ref))) this.s.gapPossible = true; // §3.3, SEQ-7
  }

  private onFrame(f: ServerFrame): void {
    if (f.type === 'voice_audio_out') return; // L-3: audio frames never enter the conversation
    if (this.s.reloading) {
      this.s.reloadBuffer = this.s.reloadBuffer.concat([f]);
      return;
    }
    // SEQ-5 exception: an `error` (a failed start, T-12) bypasses the hold; nothing would release it.
    if (this.s.awaitingSessionStarted && f.type !== 'session_started' && f.type !== 'error') {
      this.s.preStart = this.s.preStart.concat([f]);
      return;
    }
    if (f.type === 'session_started') return this.onSessionStarted(f);
    this.dispatch(f);
  }

  private dispatch(f: ServerFrame): void {
    if (hasSeq(f) && f.type !== 'session_stalled') {
      if (isDuplicateSeq(this.s.checkpoint, f.stream_id, f.seq)) return; // SEQ-1 duplicate
      this.s.checkpoint = { stream_id: f.stream_id, seq: f.seq };
    }
    this.reduce(f);
  }

  private onSessionStarted(f: SessionStartedFrame): void {
    this.s.awaitingSessionStarted = false;
    this.s.stoppingRetried = false;
    this.s.reattach = true; // in the server's open set now: later starts only reattach (OPEN-2)
    this.applySessionStartedFields(f);
    this.reduce(f);
    const sr = this.s.startRequest;
    const rf = sr && sr.type === 'start' ? sr.resume_from : undefined;
    const rs = f.resume_state;
    const held = this.s.preStart;
    this.s.preStart = [];
    if (f.replay_overflow === true) {
      this.s.checkpoint = rs ? checkpointFromResumeState(rs) : null;
      this.s.reloadBuffer = held.concat(this.s.reloadBuffer); // applied after the reload (SEQ-6)
      this.startReload();
    } else if (rf && rs && rs.stream_id === rf.stream_id) {
      for (const g of held) if (!hasSeq(g)) this.dispatch(g); // seq-stamped ones WILL be replayed (SEQ-5)
    } else {
      for (const g of held) this.dispatch(g);
      if (rs) this.s.checkpoint = mergeResumeState(this.s.checkpoint, rs);
    }
    const st = this.s.status;
    if (!this.s.inTurn && (st === 'connecting' || st === 'stopped' || st === 'terminated')) this.s.status = 'idle';
    if (this.s.termination !== null) this.s.termination = null;
  }

  /** T-12 `session_started` fields + ID-1. */
  private applySessionStartedFields(f: SessionStartedFrame): void {
    this.s.conn = 'subscribed';
    this.s.connectionBanner = null;
    let ref = this.s.ref;
    if (f.session_id && f.session_id !== ref.localId) ref = { ...ref, localId: f.session_id }; // ID-1
    if (ref.kind === 'orchestrator' && f.jsonl_id && f.jsonl_id !== ref.sdkId) ref = { ...ref, sdkId: f.jsonl_id }; // O-3
    this.s.ref = ref;
    const cw = ref.kind === 'agent' ? f.context_window : f.model_info?.model_info?.context_window;
    this.setContextWindow(typeof cw === 'number' && cw > 0 ? cw : DEFAULT_CONTEXT_WINDOW);
  }

  private setContextWindow(cw: number | null): void {
    if (this.s.counters.contextWindow !== cw) this.s.counters = { ...this.s.counters, contextWindow: cw };
  }

  /** §5.6 canonical reload: hold frames until the `history_page{replace}`. */
  private startReload(): void {
    this.s.reloading = true;
    this.effects.push({ type: 'reload', needsSdkId: this.s.ref.sdkId === null });
  }

  private flushReload(): void {
    const buf = this.s.reloadBuffer;
    this.s.reloadBuffer = [];
    for (const f of buf) this.onFrame(f);
  }

  private onHistoryPage(mode: HistoryMode, resp: MessagesPage): void {
    if (mode === 'replace' && this.s.reloading) {
      this.applyHistoryPage('replace', resp);
      this.s.reloading = false;
      this.s.gapPossible = false;
      this.flushReload();
      return;
    }
    this.applyHistoryPage(mode, resp);
  }

  /** ST-2 */
  private onPoolStatus(status: LiveStatus): void {
    if (this.s.ref.kind !== 'agent') return; // the orchestrator row always says idle (G-15)
    if (IN_TURN_LIVE.indexOf(status) >= 0) {
      if (!this.s.inTurn) {
        this.s.inTurn = true;
        this.s.promptSinceTurnEnd = false;
      }
      this.s.status = status as SessionStatus;
    } else if (this.s.inTurn) {
      // idle, interrupted (Codex/Gemini/Qwen keep it after a stop), disconnected: no turn runs
      this.endTurn();
    }
  }

  // ═════════════════════════ reducer helpers (§4.3) ═════════════════════════

  private lastIndex(): number {
    return this.s.entries.length - 1;
  }

  private last(): Entry | undefined {
    return this.s.entries[this.s.entries.length - 1];
  }

  private userEntry(text: string, origin: UserEntry['origin'], state: UserEntry['state']): UserEntry {
    return { id: this.newId(), kind: 'user', text, origin, state };
  }

  private noticeEntry(notice: NoticeKind, text: string, data?: Record<string, unknown>): NoticeEntry {
    const e: Mutable<NoticeEntry> = { id: this.newId(), kind: 'notice', notice, text };
    if (data) e.data = data;
    return e;
  }

  /** I-1: the last entry if it is an assistant run, else a new run appended now. Returns its index. */
  private tail(): number {
    const e = this.last();
    if (e && e.kind === 'assistant') return this.lastIndex();
    if (!this.scratch) this.finalizeOpenVoiceUser();
    this.pushEntry({ id: this.newId(), kind: 'assistant', blocks: [] });
    return this.lastIndex();
  }

  /** Location of the open (streaming) block of `type`/`scope` at the very end, or null. */
  private openBlock(type: 'text' | 'thinking', scope: Block['scope']): Loc | null {
    const e = this.last();
    if (!e || e.kind !== 'assistant' || e.blocks.length === 0) return null;
    const j = e.blocks.length - 1;
    const b = e.blocks[j] as Block;
    return b.type === type && b.scope === scope && b.streaming ? { w: 0, i: this.lastIndex(), j } : null;
  }

  private lastBlockLoc(): Loc | null {
    const e = this.last();
    if (!e || e.kind !== 'assistant' || e.blocks.length === 0) return null;
    return { w: 0, i: this.lastIndex(), j: e.blocks.length - 1 };
  }

  private closeOpenBlock(): void {
    const loc = this.lastBlockLoc();
    if (!loc) return;
    const b = this.blockAt(loc);
    if (isStream(b) && b.streaming) {
      const m = this.blockMut(loc) as Mutable<StreamBlock>;
      m.streaming = false;
      m.implicitlyClosed = true;
    }
  }

  /** Append a block to the tail run (a new run when the last entry is not one). Returns its location. */
  private pushBlock(b: Block): Loc {
    this.closeOpenBlock();
    const i = this.tail();
    const ps = this.s.pendingSplit;
    if (!this.scratch && ps && !(ps.type === b.type && ps.scope === b.scope)) this.s.pendingSplit = null;
    const run = this.assistantMut(0, i);
    run.blocks.push(this.markFresh(b));
    return { w: 0, i, j: run.blocks.length - 1 };
  }

  /** Every non-assistant entry goes through here (I-2). */
  private appendEntry(x: Entry): void {
    if (!this.scratch && x.id !== this.s.openVoiceUserId) this.finalizeOpenVoiceUser();
    const loc = this.lastBlockLoc();
    if (!this.scratch && loc) {
      const b = this.blockAt(loc);
      if (isStream(b) && b.streaming) {
        const m = this.blockMut(loc) as Mutable<StreamBlock>;
        m.streaming = false;
        this.s.pendingSplit = { id: b.id, type: b.type, scope: b.scope, text: b.text }; // I-5
      }
    }
    this.pushEntry(x);
    if (!this.scratch && x.kind === 'user') this.s.promptSinceTurnEnd = true;
  }

  private appendNoticeOnce(notice: NoticeKind): void {
    const e = this.last();
    if (e && e.kind === 'notice' && e.notice === notice) return;
    this.appendEntry(this.noticeEntry(notice, ''));
  }

  private currentScope(): Block['scope'] {
    return this.s.ref.kind === 'orchestrator' && this.s.voiceActive && !this.s.inTurn ? 'voice' : 'turn';
  }

  private clearStall(): void {
    if (this.s.stall !== null) this.s.stall = null;
  }

  /** BG-1 (orchestrator only). */
  private maybeBackgroundNotice(): void {
    if (this.s.ref.kind !== 'orchestrator' || !this.s.inTurn) return;
    const prev = this.last(); // a visible prompt (echo from another device, inject, transcript) is not "background"
    if (!this.s.turnIsLocal && !this.s.turnHasContent && !(prev && prev.kind === 'user'))
      this.appendEntry(this.noticeEntry('background', ''));
    this.s.turnHasContent = true;
  }

  /** TL-3: content without a turn start. */
  private ensureAgentTurn(): void {
    if (this.s.ref.kind === 'agent' && !this.s.inTurn) {
      this.s.inTurn = true;
      this.s.promptSinceTurnEnd = false;
    }
  }

  private setAgentStatus(st: SessionStatus): void {
    if (this.s.ref.kind === 'agent' && this.s.inTurn) this.s.status = st;
  }

  private indexOfEntry(id: string): number {
    const list = this.s.entries;
    for (let i = list.length - 1; i >= 0; i--) if ((list[i] as Entry).id === id) return i;
    return -1;
  }

  private finalizeOpenVoiceUser(): void {
    const id = this.s.openVoiceUserId;
    if (id === null) return;
    const i = this.indexOfEntry(id);
    if (i >= 0 && (this.s.entries[i] as UserEntry).streaming) this.userMut(0, i).streaming = false;
    this.s.openVoiceUserId = null;
  }

  /** The tool block with this id, in either list (I-11: at most one). */
  private findTool(id: string): Loc | null {
    return this.findBlock((b) => b.type === 'tool' && b.tool_use_id === id);
  }

  private findPerm(rid: string): Loc | null {
    return this.findBlock((b) => b.type === 'permission' && b.request_id === rid);
  }

  private findBlock(pred: (b: Block) => boolean): Loc | null {
    for (const w of [0, 1] as const) {
      const list = this.list(w);
      for (let i = list.length - 1; i >= 0; i--) {
        const e = list[i] as Entry;
        if (e.kind !== 'assistant') continue;
        for (let j = 0; j < e.blocks.length; j++) if (pred(e.blocks[j] as Block)) return { w, i, j };
      }
    }
    return null;
  }

  // ───────────────────────── streamed text / thinking ─────────────────────────

  private onDelta(type: 'text' | 'thinking', text: string, scope: Block['scope'] = 'turn'): void {
    this.clearStall();
    if (scope === 'turn') {
      this.ensureAgentTurn();
      this.setAgentStatus(type === 'text' ? 'streaming' : 'thinking');
    }
    const loc = this.openBlock(type, scope);
    if (loc) {
      if (text !== '') {
        const b = this.blockMut(loc) as Mutable<StreamBlock>;
        b.text += text;
      }
      return;
    }
    if (scope === 'turn') this.maybeBackgroundNotice();
    const ps = this.s.pendingSplit;
    const cont = ps && ps.type === type && ps.scope === scope ? ps : null;
    this.s.pendingSplit = null;
    const nb: Mutable<StreamBlock> = {
      id: this.newId(),
      type,
      text,
      streaming: true,
      scope,
      origin: 'live',
    };
    if (cont) nb.continuationOf = cont.text;
    this.pushBlock(nb);
  }

  private onComplete(type: 'text' | 'thinking', text: string, scope: Block['scope'] = 'turn'): void {
    this.clearStall();
    if (scope === 'turn') {
      this.ensureAgentTurn();
      this.setAgentStatus(type === 'text' ? 'streaming' : 'thinking');
    }
    const loc = this.openBlock(type, scope);
    if (loc) {
      if (this.isHistoryDuplicate(type, text, loc)) return this.removeBlock(loc); // §5.4
      const open = this.blockAt(loc) as StreamBlock;
      let full = text;
      if (open.continuationOf !== undefined && full.startsWith(open.continuationOf))
        full = full.slice(open.continuationOf.length);
      const b = this.blockMut(loc) as Mutable<StreamBlock>;
      b.text = full;
      b.streaming = false;
      return;
    }
    if (this.isDuplicateComplete(type, text)) return; // §5.4
    let full = text;
    const ps = this.s.pendingSplit;
    if (ps && ps.type === type && ps.scope === scope) {
      if (full.startsWith(ps.text)) full = full.slice(ps.text.length);
      this.s.pendingSplit = null;
      if (full === '') return;
    }
    if (scope === 'turn') this.maybeBackgroundNotice();
    this.pushBlock({ id: this.newId(), type, text: full, streaming: false, scope, origin: 'live' });
  }

  /** §5.4 rule 3: a `*_complete` with no open block whose text the tail run already has. */
  private isDuplicateComplete(type: 'text' | 'thinking', text: string): boolean {
    const e = this.last();
    if (!e || e.kind !== 'assistant') return false;
    return e.blocks.some(
      (b) => b.type === type && b.text === text && (b.origin === 'history' || b.implicitlyClosed === true),
    );
  }

  /** §5.4: the open block's final text already came from REST. */
  private isHistoryDuplicate(type: 'text' | 'thinking', text: string, open: Loc): boolean {
    const e = this.last() as AssistantEntry;
    return e.blocks.some((b, j) => j !== open.j && b.type === type && b.origin === 'history' && b.text === text);
  }

  /** Only used by the §5.4 dedupe; the block is always in the tail run. */
  private removeBlock(loc: Loc): void {
    const run = this.assistantMut(0, loc.i);
    run.blocks.splice(loc.j, 1);
    if (run.blocks.length === 0) this.popEntry(); // I-10
  }

  // ───────────────────────── tools (§4.5) ─────────────────────────

  private onToolUse(id: string, name: string, input: Record<string, unknown>): void {
    this.clearStall();
    const scope = this.currentScope();
    if (scope === 'turn') {
      this.ensureAgentTurn();
      this.setAgentStatus('tool_use');
    }
    if (id) {
      const known = this.findTool(id);
      if (known) {
        // I-11: replay / history overlap updates the existing card
        const tb = this.blockAt(known) as ToolBlock;
        const fillName = !tb.tool_name && name !== '';
        const fillInput = Object.keys(input).length > 0 && !sameJson(tb.tool_input, input);
        if (fillName || fillInput) {
          const m = this.blockMut(known) as Mutable<ToolBlock>;
          if (fillName) m.tool_name = name;
          if (fillInput) m.tool_input = input;
        }
        return;
      }
    }
    if (scope === 'turn') this.maybeBackgroundNotice();
    const loc = this.pushBlock({
      id: this.newId(),
      type: 'tool',
      tool_use_id: id,
      tool_name: name,
      tool_input: input,
      status: 'running',
      output: null,
      scope,
      origin: 'live',
    });
    if (id) this.attachOrphan(id, loc);
  }

  /** R-2: an orphan attaches as soon as its card exists. */
  private attachOrphan(id: string, loc: Loc): void {
    const k = this.s.orphanResults.findIndex((o) => o.tool_use_id === id);
    if (k < 0) return;
    const o = this.s.orphanResults[k] as ToolResultData;
    this.s.orphanResults = this.s.orphanResults.filter((_, n) => n !== k);
    this.applyResult(loc, { output: o.output, is_error: o.is_error, origin: o.origin });
  }

  private onToolResult(id: string, output: unknown, isError: unknown): void {
    this.clearStall();
    this.deliverResult(id, { output: normalizeOutput(output), is_error: isError === true, origin: 'live' });
  }

  /** R-1 … R-4 */
  private deliverResult(id: string, r: ToolResultData): void {
    if (id) {
      const loc = this.findTool(id);
      if (loc) return this.applyResult(loc, r);
      const k = this.s.orphanResults.findIndex((o) => o.tool_use_id === id);
      const prev = k >= 0 ? this.s.orphanResults[k] : undefined;
      if (!prev) this.s.orphanResults = this.s.orphanResults.concat([{ tool_use_id: id, ...r }]);
      else if (r.output !== '' || prev.output === '')
        this.s.orphanResults = this.s.orphanResults.map((o, n) => (n === k ? { tool_use_id: id, ...r } : o));
      return;
    }
    if (r.origin === 'live') {
      const running: Loc[] = [];
      const list = this.s.entries;
      for (let i = 0; i < list.length; i++) {
        const e = list[i] as Entry;
        if (e.kind !== 'assistant') continue;
        e.blocks.forEach((b, j) => {
          if (b.type === 'tool' && b.status === 'running') running.push({ w: 0, i, j });
        });
      }
      if (running.length === 1) return this.applyResult(running[0] as Loc, { ...r, inferred: true });
    }
    this.s.unattributed = this.s.unattributed.concat([r]);
  }

  /** R-5 */
  private applyResult(loc: Loc, r: ToolResultData): void {
    const tb = this.blockAt(loc) as ToolBlock;
    const authoritative = r.origin === 'reconcile';
    const final = tb.status === 'done' || tb.status === 'error';
    if (!authoritative && final && tb.output && r.output === '') return; // never replace output with nothing
    if (authoritative && final && tb.output && !tb.inferred) return; // live non-empty result already there
    const status = r.is_error ? 'error' : 'done';
    const inferred = r.inferred === true;
    if (inferred) this.s.turnInferred = true;
    if (tb.output === r.output && tb.status === status && (tb.inferred === true) === inferred && !tb.executing) return;
    const m = this.blockMut(loc) as Mutable<ToolBlock>;
    m.output = r.output;
    m.status = status;
    m.inferred = inferred;
    m.executing = false;
  }

  private onToolExecuting(id: string): void {
    const loc = id ? this.findTool(id) : null;
    if (!loc) return;
    const tb = this.blockAt(loc) as ToolBlock;
    if (tb.status === 'running' && !tb.executing) (this.blockMut(loc) as Mutable<ToolBlock>).executing = true;
  }

  private onToolProgress(id: string, elapsed: number, message: string): void {
    const loc = id ? this.findTool(id) : null;
    if (!loc) return;
    if ((this.blockAt(loc) as ToolBlock).status === 'running')
      (this.blockMut(loc) as Mutable<ToolBlock>).progress = { elapsed_seconds: elapsed, message };
  }

  // ───────────────────────── permissions (§4.6) ─────────────────────────

  private onPermissionRequest(rid: string, name: string, input: Record<string, unknown>): void {
    this.clearStall();
    if (this.findPerm(rid)) return; // I-11
    this.ensureAgentTurn();
    this.pushBlock({
      id: this.newId(),
      type: 'permission',
      request_id: rid,
      tool_name: name,
      tool_input: input,
      state: 'pending',
      responder: null,
      message: null,
      scope: 'turn',
      origin: 'live',
    });
  }

  private onPermissionResolved(rid: string, decision: string, responder: unknown, message: unknown): void {
    this.clearStall();
    const loc = this.findPerm(rid);
    if (!loc) return; // we never saw the request: nothing to show
    const pb = this.blockMut(loc) as Mutable<PermissionBlock>;
    pb.state = decision === 'allow' ? 'allowed' : 'denied';
    pb.responder = typeof responder === 'string' ? (responder as PermissionResponder) : null;
    pb.message = typeof message === 'string' ? message : null; // NO entry is appended (PM-2, W-5)
  }

  // ───────────────────────── turns ─────────────────────────

  private beginTurn(): void {
    this.s.inTurn = true;
    this.s.promptSinceTurnEnd = false;
    this.s.turnHasContent = false;
    this.s.pendingSplit = null;
    this.s.turnInferred = false;
    this.s.status = this.s.ref.kind === 'agent' ? 'processing' : 'streaming';
  }

  /** I-7 for scope "turn", R-6, PM-4. */
  private endTurn(): void {
    const wasInTurn = this.s.inTurn;
    let expired = 0;
    const list = this.s.entries;
    for (let i = 0; i < list.length; i++) {
      const e = list[i] as Entry;
      if (e.kind !== 'assistant') continue;
      for (let j = 0; j < e.blocks.length; j++) {
        const b = e.blocks[j] as Block;
        if (b.scope !== 'turn') continue;
        const loc: Loc = { w: 0, i, j };
        if (isStream(b)) {
          if (b.streaming) (this.blockMut(loc) as Mutable<StreamBlock>).streaming = false;
        } else if (b.type === 'tool') {
          if (b.status === 'running') {
            (this.blockMut(loc) as Mutable<ToolBlock>).status = 'no_result';
            expired += 1;
          }
        } else if (b.state === 'pending') {
          const pb = this.blockMut(loc) as Mutable<PermissionBlock>;
          pb.state = 'denied'; // mirrors the backend drain (G-17)
          pb.responder = 'system';
          pb.message = 'stream ended';
        }
      }
    }
    this.s.pendingSplit = null;
    this.s.stall = null;
    this.s.inTurn = false;
    this.s.turnDepth = 0;
    this.s.turnHasContent = false;
    this.s.turnIsLocal = false;
    if (this.s.dispatchedFromTray.length) this.s.dispatchedFromTray = []; // §4.4.1: cleared on turn end
    if (this.s.status !== 'stopped' && this.s.status !== 'terminated')
      this.s.status = this.s.compactPending ? 'compacting' : 'idle';

    if (!wasInTurn) return;
    this.effects.push({ type: 'turn_ended' });
    const ref = this.s.ref;
    if (ref.kind === 'agent' && ref.sdkId) {
      const needsReconcile =
        expired > 0 ||
        this.s.turnInferred ||
        this.s.unattributed.length > 0 ||
        this.s.orphanResults.some((o) => o.origin === 'live');
      if (needsReconcile) this.effects.push({ type: 'reconcile' }); // R-7
    }
    this.s.turnInferred = false;
    if (ref.kind === 'agent' && !seqCapable(ref) && this.s.gapPossible && !this.s.reloading) this.startReload(); // SEQ-7
  }

  /** I-7 for scope "voice", VT-3. */
  private endVoice(): void {
    this.finalizeOpenVoiceUser();
    this.s.speechAnchor = null;
    const list = this.s.entries;
    for (let i = 0; i < list.length; i++) {
      const e = list[i] as Entry;
      if (e.kind !== 'assistant') continue;
      for (let j = 0; j < e.blocks.length; j++) {
        const b = e.blocks[j] as Block;
        if (b.scope !== 'voice') continue;
        const loc: Loc = { w: 0, i, j };
        if (isStream(b) && b.streaming) (this.blockMut(loc) as Mutable<StreamBlock>).streaming = false;
        else if (b.type === 'tool' && b.status === 'running') (this.blockMut(loc) as Mutable<ToolBlock>).status = 'no_result';
      }
    }
    this.s.voiceActive = false;
  }

  // ───────────────────────── frame dispatch ─────────────────────────

  private reduce(f: ServerFrame): void {
    const ref = this.s.ref;
    switch (f.type) {
      case 'text_delta':
        return this.onDelta('text', f.text);
      case 'text_complete':
        return this.onComplete('text', f.text);
      case 'thinking_delta':
        return this.onDelta('thinking', f.text);
      case 'thinking_complete':
        return this.onComplete('thinking', f.text);
      case 'tool_use':
        return this.onToolUse(f.tool_use_id, f.tool_name, f.tool_input);
      case 'tool_result':
        return this.onToolResult(f.tool_use_id, f.output, f.is_error);
      case 'tool_executing':
        return this.onToolExecuting(f.tool_use_id);
      case 'tool_progress':
        return this.onToolProgress(f.tool_use_id, f.elapsed_seconds, f.message);
      case 'permission_request':
        return this.onPermissionRequest(f.request_id, f.tool_name, f.tool_input);
      case 'permission_resolved':
        return this.onPermissionResolved(f.request_id, f.decision, f.responder, f.message);

      case 'user_message': {
        if (f.queued === true) {
          this.s.queue = this.s.queue.concat([{ text: f.text, owner: 'remote' }]); // I-12
          return;
        }
        if (f.source === 'shared_inject') {
          const i = this.s.pendingInjects.indexOf(f.text);
          if (i >= 0) {
            this.s.pendingInjects = this.s.pendingInjects.filter((_, k) => k !== i);
            return this.markInjectSent(f.text);
          }
          return this.appendEntry(this.userEntry(f.text, 'inject', 'sent'));
        }
        // another device's voice message (send_audio); the sender's own socket gets no echo
        if (f.source === 'voice_message') return this.appendEntry(this.userEntry(f.text, 'audio', 'sent'));
        // a pre-O-6 re-echo of a prompt already moved out of the tray is swallowed BEFORE endTurn (which clears it)
        const d = this.s.dispatchedFromTray.indexOf(f.text);
        if (d >= 0) {
          this.s.dispatchedFromTray = this.s.dispatchedFromTray.filter((_, k) => k !== d);
          return;
        }
        if (ref.kind === 'agent' && this.s.inTurn) this.endTurn(); // observer of an interrupted turn (TL-2)
        const qi = this.s.queue.findIndex((q) => q.owner === 'remote' && q.text === f.text);
        if (qi >= 0) this.s.queue = this.s.queue.filter((q, k) => !(q.owner === 'remote' && k <= qi));
        return this.appendEntry(this.userEntry(f.text, 'echo', 'sent'));
      }

      case 'status':
        return this.onStatus(f.status);

      case 'turn_complete': {
        if (ref.kind === 'agent') {
          const c = this.s.counters;
          const usageIn = isPlainObject(f.usage) ? f.usage.input_tokens : undefined;
          const it = typeof f.input_tokens === 'number' ? f.input_tokens : usageIn;
          this.s.counters = {
            cost: c.cost + (f.cost ?? 0),
            turns: c.turns + (f.num_turns ?? 1),
            contextTokens: typeof it === 'number' && it > 0 ? it : c.contextTokens,
            contextWindow: c.contextWindow,
          };
          if (f.session_id && !ref.sdkId) this.s.ref = { ...ref, sdkId: f.session_id };
          else if (!f.session_id && !ref.sdkId) this.effects.push({ type: 'learn_sdk_id' }); // ID-2
          if (f.is_error === true && typeof f.result === 'string' && f.result !== '')
            this.appendEntry(this.noticeEntry('error', f.result, { code: 'turn_error' }));
          this.endTurn();
        } else if (typeof f.input_tokens === 'number' && f.input_tokens > 0 && f.input_tokens !== this.s.counters.contextTokens) {
          this.s.counters = { ...this.s.counters, contextTokens: f.input_tokens };
        } // the orchestrator turn ends at status:idle
        return;
      }

      case 'compact_complete': {
        if (ref.kind === 'agent') {
          this.appendEntry(this.noticeEntry('compaction', str(f.summary), { trigger: f.trigger }));
        } else {
          this.appendEntry(
            this.noticeEntry('compaction', '', {
              trigger: f.trigger,
              tokens_before: f.tokens_before,
              tokens_after: f.tokens_after,
            }),
          );
          if (typeof f.tokens_after === 'number') this.s.counters = { ...this.s.counters, contextTokens: f.tokens_after };
        }
        this.s.compactPending = false;
        if (!this.s.inTurn && this.s.status === 'compacting') this.s.status = 'idle';
        return;
      }

      case 'session_stalled':
        if (this.s.inTurn || busy(this.s.status))
          this.s.stall = {
            elapsed_seconds: f.elapsed_seconds,
            last_tool_name: f.last_tool_name ?? null,
            last_tool_use_id: f.last_tool_use_id ?? null,
          };
        return;

      case 'error':
        return this.onError(f.error, f.detail ?? null);

      case 'session_terminated':
        this.s.termination = { reason: f.reason, detail: f.detail ?? null, sdk_session_id: f.sdk_session_id ?? null };
        if (this.s.queue.length) this.s.queue = [];
        this.s.status = 'terminated';
        this.endTurn();
        this.endVoice();
        this.s.checkpoint = null;
        return;

      case 'session_stopped':
        if (this.s.expectStopAck) {
          this.s.expectStopAck = false; // reply to our own stop / close
          return;
        }
        return this.stoppedByServer(); // the view closes on the watcher's agent_session_closed (OPEN-3)

      case 'voice_event':
        return this.onVoiceEvent(f.event);
      case 'voice_owner_active':
        if (f.active) this.s.voiceActive = true;
        else this.endVoice();
        return;
      case 'voice_ended':
      case 'voice_stopped':
        return this.endVoice();
      case 'session_started':
        if (f.voice === true) this.s.voiceActive = true; // the other fields: onSessionStarted (§3.6)
        return;
      case 'nested_session_event':
        return this.onNestedEvent(f.session_id, f.event_type, f.event_data);
      case 'model_changed':
      case 'model_info': {
        const cw = f.model_info?.model_info?.context_window;
        if (typeof cw === 'number') this.setContextWindow(cw);
        return;
      }
      case 'agent_session_opened':
        this.effects.push({ type: 'watcher', frame: f });
        return;
      case 'agent_session_closed':
        this.effects.push({ type: 'watcher', frame: f });
        // D3 (§3.7, FOCUS-2): this conversation itself left the pool.
        if (f.session_id === ref.localId && (f.is_orchestrator === true) === (ref.kind === 'orchestrator')) this.stoppedByServer();
        return;
      default:
        // voice_ending, voice_command, voice_audio_out, voice_connection_error, models_list,
        // orchestrator_switch (channel level, SW-1), agent_turn_started/finished (channel level,
        // device notifications), visualization_changed / memory_changed (channel level, §9.3),
        // audio_upload, ping, unknown: handled
        // outside the reducer or ignored
        return;
    }
  }

  private stoppedByServer(): void {
    if (this.s.status !== 'terminated') this.s.status = 'stopped';
    this.endTurn();
    this.endVoice();
  }

  private onNestedEvent(localId: string, eventType: string, data: Record<string, unknown>): void {
    const decoded = coerceFrame(data);
    if (decoded.ok) this.effects.push({ type: 'nested_event', localId, event: decoded.frame });
    if (this.s.ref.kind !== 'orchestrator') return;
    const rid = str(data.request_id);
    if (!rid) return;
    const has = this.s.agentApprovals.some((a) => a.localId === localId && a.request_id === rid);
    if (eventType === 'permission_request' && !has) {
      this.s.agentApprovals = this.s.agentApprovals.concat([
        { localId, request_id: rid, tool_name: str(data.tool_name), tool_input: inputObject(data.tool_input) },
      ]);
    } else if (eventType === 'permission_resolved' && has) {
      this.s.agentApprovals = this.s.agentApprovals.filter((a) => !(a.localId === localId && a.request_id === rid));
    }
  }

  private markInjectSent(text: string): void {
    const list = this.s.entries;
    for (let i = 0; i < list.length; i++) {
      const e = list[i] as Entry;
      if (e.kind === 'user' && e.origin === 'inject' && e.state === 'pending' && e.text === text) {
        this.userMut(0, i).state = 'sent';
        return;
      }
    }
  }

  private onStatus(st: string): void {
    if (this.s.ref.kind === 'agent') {
      switch (st) {
        case 'connecting':
          this.s.status = 'connecting';
          return;
        case 'processing': {
          if (this.s.inTurn) this.endTurn(); // superseded
          if (!this.s.promptSinceTurnEnd && this.s.queue.length > 0) {
            // a queued prompt was dispatched (the server is FIFO); since backend O-6 the observer
            // gets no second user_message for it
            const q = this.s.queue[0] as QueuedPrompt;
            this.s.queue = this.s.queue.slice(1);
            this.s.dispatchedFromTray = this.s.dispatchedFromTray.concat([q.text]);
            this.appendEntry(this.userEntry(q.text, q.owner === 'local' ? 'local' : 'echo', 'sent'));
          }
          return this.beginTurn();
        }
        case 'retrying':
          if (this.s.inTurn) this.s.status = 'retrying';
          return;
        case 'interrupted':
          if (this.s.queue.length) this.s.queue = []; // the server dropped every queued prompt
          if (this.s.inTurn || busy(this.s.status)) this.appendNoticeOnce('interrupted');
          return this.endTurn();
        default:
          return; // ST-1: unknown values keep the current status
      }
    }
    switch (st) {
      case 'connecting':
        this.s.status = 'connecting';
        return;
      case 'streaming': {
        const local = this.s.localTurnsPending > 0;
        if (local) this.s.localTurnsPending -= 1;
        this.s.turnDepth += 1;
        if (this.s.turnDepth === 1) {
          this.beginTurn();
          this.s.turnIsLocal = local;
        } else if (local) this.s.turnIsLocal = true;
        return;
      }
      case 'idle':
        this.s.turnDepth = Math.max(0, this.s.turnDepth - 1);
        if (this.s.turnDepth === 0) this.endTurn();
        return;
      case 'interrupted':
        if (this.s.inTurn) this.appendNoticeOnce('interrupted');
        return;
      default:
        return;
    }
  }

  private onError(code: string, detail: string | null): void {
    const ref = this.s.ref;
    if (code === 'interrupted') {
      if (this.s.inTurn) this.appendNoticeOnce('interrupted'); // orchestrator agent loop
      return;
    }
    const failures = ref.kind === 'agent' ? TURN_FAILURE_AGENT : TURN_FAILURE_ORCH;
    if (failures.indexOf(code) < 0) return this.nonTurnError(code, detail); // §4.4.4: never an entry
    this.appendEntry(this.noticeEntry('error', detail || code, { code }));
    this.s.compactPending = false;
    if (ref.kind === 'agent') return this.endTurn();
    if (code === 'invalid_audio' && this.s.localTurnsPending > 0) this.s.localTurnsPending -= 1; // no streaming will follow
    if (ORCH_NO_IDLE_AFTER.indexOf(code) >= 0) {
      this.s.turnDepth = Math.max(0, this.s.turnDepth - 1);
      if (this.s.turnDepth === 0) this.endTurn();
    } // api_error / provider_error: status:idle follows
  }

  /** §4.4.4 start / voice / protocol errors (T-12). */
  private nonTurnError(code: string, detail: string | null): void {
    if (VOICE_ERRORS.indexOf(code) >= 0) return; // the voice controller shows these (§7.7)
    if (code === 'not_started') {
      this.effects.push({ type: 'protocol_error', code, detail });
      return this.sendStart();
    }
    if (code === 'session_closed') {
      // OPEN-3: the answer to a `reattach` start, the conversation is not open on the server
      this.endStartWait();
      this.stoppedByServer();
      const ref = this.s.ref;
      this.effects.push({ type: 'watcher', frame: { type: 'agent_session_closed', session_id: ref.localId, is_orchestrator: ref.kind === 'orchestrator' } });
      return;
    }
    if (START_ERRORS.indexOf(code) < 0) {
      this.effects.push({ type: 'protocol_error', code, detail });
      return;
    }
    if (code === 'orchestrator_stopping' && !this.s.stoppingRetried) {
      this.s.stoppingRetried = true;
      this.effects.push({ type: 'retry_start', delayMs: 1000 });
      return;
    }
    this.s.connectionBanner = { code, detail };
    this.s.conn = 'failed';
    this.endStartWait();
  }

  /** No `session_started` will come for this start: keep what was held (lossless, L-2, SEQ-8). */
  private endStartWait(): void {
    if (!this.s.awaitingSessionStarted) return;
    this.s.awaitingSessionStarted = false;
    const held = this.s.preStart;
    this.s.preStart = [];
    for (const g of held) this.dispatch(g);
  }

  // ───────────────────────── local actions ─────────────────────────

  private localSend(text: string): void {
    if (this.s.ref.kind === 'agent') {
      if (busy(this.s.status) || this.s.inTurn) {
        this.s.queue = this.s.queue.concat([{ text, owner: 'local' }]); // I-12
        return;
      }
      this.appendEntry(this.userEntry(text, 'local', 'sent'));
      this.s.status = 'processing'; // optimistic; status:processing follows
      return;
    }
    this.appendEntry(this.userEntry(text, 'local', 'sent'));
    this.s.localTurnsPending += 1;
  }

  private localInject(text: string): void {
    if (this.s.voiceActive) {
      this.appendEntry(this.userEntry(text, 'inject', 'pending'));
      this.s.pendingInjects = this.s.pendingInjects.concat([text]);
    } else {
      this.appendEntry(this.userEntry(text, 'inject', 'sent'));
      this.s.localTurnsPending += 1;
    }
  }

  // ═════════════════════════ voice transcripts (§4.7) ═════════════════════════

  private onVoiceEvent(ev: Readonly<Record<string, unknown>>): void {
    const t = ev.type;
    if (typeof t === 'string') {
      if (t === 'input_audio_buffer.speech_started') {
        if (this.s.openVoiceUserId === null) this.s.speechAnchor = this.s.entries.length;
      } else if (t === 'conversation.item.input_audio_transcription.completed') {
        this.voiceUserFinal(str(ev.transcript));
      } else if (VOICE_DELTAS.indexOf(t) >= 0) {
        this.finalizeOpenVoiceUser();
        this.onDelta('text', str(ev.delta), 'voice');
      } else if (VOICE_TRANSCRIPT_DONE.indexOf(t) >= 0) {
        this.voiceAssistantDone(str(ev.transcript));
      } else if (VOICE_TEXT_DONE.indexOf(t) >= 0) {
        this.voiceAssistantDone(str(ev.text));
      } else if (t === 'response.done') {
        this.voiceTurnEnd();
      }
      return; // everything else: the voice controller, or ignored (tool cards come only from frames, W-3)
    }
    const sc = ev.serverContent;
    if (!isPlainObject(sc)) return; // Gemini toolCall, setupComplete, goAway, …: ignored here
    // Gemini order (voice_persister.py:146-195): input, output, interrupted, turnComplete
    const input = sc.inputTranscription;
    if (isPlainObject(input) && typeof input.text === 'string') this.voiceUserFragment(input.text);
    const output = sc.outputTranscription;
    if (isPlainObject(output) && typeof output.text === 'string') {
      this.finalizeOpenVoiceUser();
      if (output.text) this.onDelta('text', output.text, 'voice');
    }
    if (sc.interrupted === true) this.voiceTurnEnd();
    if (sc.turnComplete === true) this.voiceTurnEnd();
  }

  private voiceUserFinal(text: string): void {
    if (!text || !text.trim()) return;
    this.insertUserAtAnchor(this.userEntry(text, 'voice', 'sent'));
  }

  /** Gemini coalescing: raw concatenation, as the persister does. */
  private voiceUserFragment(text: string): void {
    if (!text) return;
    const openId = this.s.openVoiceUserId;
    if (openId !== null) {
      const i = this.indexOfEntry(openId);
      if (i >= 0) {
        const u = this.userMut(0, i);
        u.text += text;
        return;
      }
    }
    const e: UserEntry = { id: this.newId(), kind: 'user', text, origin: 'voice', state: 'sent', streaming: true };
    this.insertUserAtAnchor(e);
    this.s.openVoiceUserId = e.id;
  }

  /** I-9: the only positional insertion. */
  private insertUserAtAnchor(e: UserEntry): void {
    const pos = this.s.speechAnchor ?? this.s.entries.length;
    this.s.speechAnchor = null;
    if (pos >= this.s.entries.length) return this.appendEntry(e);
    if (e.id !== this.s.openVoiceUserId) this.finalizeOpenVoiceUser();
    this.insertEntry(pos, e); // the tail run stays last and stays open
  }

  private voiceAssistantDone(text: string): void {
    this.finalizeOpenVoiceUser();
    const loc = this.openBlock('text', 'voice');
    if (loc) {
      const b = this.blockMut(loc) as Mutable<StreamBlock>;
      if (text) b.text = text; // empty text keeps the content (Gemini)
      b.streaming = false;
      return;
    }
    if (text) this.pushBlock({ id: this.newId(), type: 'text', text, streaming: false, scope: 'voice', origin: 'live' });
  }

  private voiceTurnEnd(): void {
    this.finalizeOpenVoiceUser();
    const loc = this.openBlock('text', 'voice');
    if (loc) (this.blockMut(loc) as Mutable<StreamBlock>).streaming = false;
  }

  // ═════════════════════════ history (§5.1, §5.5) ═════════════════════════

  private applyHistoryPage(mode: HistoryMode, resp: MessagesPage): void {
    if (mode === 'reconcile') return this.reconcile(resp);
    if (mode === 'replace') this.resetContent();
    this.beginScratch();
    this.scratch = true;
    for (const p of Array.isArray(resp.messages) ? resp.messages : []) this.convertPreview(p);
    // TC-2: never "done" without a result; only the tail of a replace page may still be running
    const page = this.s.entries;
    for (let i = 0; i < page.length; i++) {
      const e = page[i] as Entry;
      if (e.kind !== 'assistant') continue;
      const mayStillRun = mode === 'replace' && this.s.inTurn && i === page.length - 1;
      if (mayStillRun) continue;
      e.blocks.forEach((b, j) => {
        if (b.type === 'tool' && b.status === 'running') (this.blockMut({ w: 0, i, j }) as Mutable<ToolBlock>).status = 'no_result';
      });
    }
    this.scratch = false;
    const [built] = this.endScratch();
    if (mode === 'replace') {
      this.setEntries(built);
      this.s.promptSinceTurnEnd = false;
    } else {
      // prepend; merge a run split by the page boundary (H-5)
      let pageEntries = built;
      const pageLast = pageEntries[pageEntries.length - 1];
      const head = this.s.entries[0];
      if (pageLast && head && pageLast.kind === 'assistant' && head.kind === 'assistant') {
        const run = this.assistantMut(0, 0);
        run.blocks = pageLast.blocks.concat(run.blocks);
        pageEntries = pageEntries.slice(0, -1);
      }
      if (pageEntries.length) {
        this.setEntries(pageEntries.concat(this.s.entries));
        if (this.s.speechAnchor !== null) this.s.speechAnchor += pageEntries.length; // D2
      }
    }
    this.s.history = {
      loaded: true,
      startIndex: typeof resp.start_index === 'number' ? resp.start_index : 0,
      totalCount: typeof resp.total_count === 'number' ? resp.total_count : 0,
      hasMore: resp.has_more === true,
    };
  }

  private resetContent(): void {
    this.setEntries([]);
    this.s.orphanResults = [];
    this.s.unattributed = [];
    this.s.pendingSplit = null;
    this.s.openVoiceUserId = null;
    this.s.speechAnchor = null;
  }

  private convertPreview(p: MessagePreview): void {
    const blocks = Array.isArray(p.blocks) ? (p.blocks as unknown[]).filter(isPlainObject) : [];
    const text = typeof p.text === 'string' ? p.text : '';
    if (p.role === 'user') {
      for (const b of blocks) if (b.type === 'tool_result') this.historyResult(b);
      if (blocks.length && blocks.every((b) => b.type === 'tool_result')) return; // protocol wrapper, not a turn
      if (text === '') return; // e.g. an image-only line
      const c = classifyUserLine(text);
      this.appendEntry(c.kind === 'user' ? this.userEntry(c.text, c.origin, 'sent') : this.noticeEntry(c.notice, c.text));
      return;
    }
    if (blocks.length === 0) {
      if (text) this.pushHistory('text', text); // empty = Claude thinking placeholder
      return;
    }
    for (const b of blocks) {
      const bt = str(b.text);
      if (b.type === 'text' && bt) this.pushHistory('text', bt);
      else if (b.type === 'thinking' && bt) this.pushHistory('thinking', bt);
      else if (b.type === 'tool_use') this.historyToolUse(str(b.tool_use_id), str(b.tool_name), inputObject(b.tool_input));
      else if (b.type === 'tool_result') this.historyResult(b);
    }
  }

  private historyResult(b: Record<string, unknown>): void {
    this.deliverResult(str(b.tool_use_id), {
      output: normalizeOutput(b.output),
      is_error: b.is_error === true,
      origin: 'history',
    });
  }

  private pushHistory(type: 'text' | 'thinking', text: string): void {
    this.pushBlock({ id: this.newId(), type, text, streaming: false, scope: 'turn', origin: 'history' });
  }

  private historyToolUse(id: string, name: string, input: Record<string, unknown>): void {
    if (id && this.findTool(id)) return; // I-11
    const loc = this.pushBlock({
      id: this.newId(),
      type: 'tool',
      tool_use_id: id,
      tool_name: name,
      tool_input: input,
      status: 'running',
      output: null,
      scope: 'turn',
      origin: 'history',
    });
    if (id) this.attachOrphan(id, loc);
  }

  /** §5.5: fills tool results only; never adds, removes or reorders entries. */
  private reconcile(resp: MessagesPage): void {
    for (const p of Array.isArray(resp.messages) ? resp.messages : []) {
      const blocks = Array.isArray(p.blocks) ? (p.blocks as unknown[]).filter(isPlainObject) : [];
      for (const b of blocks) {
        const id = str(b.tool_use_id);
        if (b.type !== 'tool_result' || !id) continue;
        const loc = this.findTool(id);
        if (loc)
          this.applyResult(loc, { output: normalizeOutput(b.output), is_error: b.is_error === true, origin: 'reconcile' });
      }
    }
    if (this.s.unattributed.length) this.s.unattributed = []; // superseded by the authoritative REST results
  }
}
