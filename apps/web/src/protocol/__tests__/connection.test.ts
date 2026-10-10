/** Connection manager rules (spec 12 §3.3–§3.6, §5.2, §5.6, T-*, SEQ-*, ID-*, ST-2). */
import { describe, expect, it } from 'vitest';
import type { Conversation } from '../index';
import { initialConversation, stepConversation } from '../index';
import { agent, f, feed, orch, page, texts, toolUseLine, userLine } from './util';

const S = 'L1:1';

describe('start (T-9, T-10, T-11)', () => {
  it('socket_open sends start with resume_sdk_id, and resume_from only when history was built in-process', () => {
    const first = feed(agent(), { type: 'socket_open' });
    const effects = first.effects;
    let conv: Conversation = first.conv;
    expect(effects).toEqual([{ type: 'send', message: { type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-1', reattach: true } }]);
    expect(conv.conn).toBe('open');
    expect(conv.awaitingSessionStarted).toBe(true);
    // a checkpoint exists, but history.loaded is false (cold-open rebuild): no resume_from (T-10)
    conv = { ...conv, checkpoint: { stream_id: S, seq: 4 } };
    expect(feed(conv, { type: 'socket_open' }).effects[0]).toEqual({
      type: 'send',
      message: { type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-1', reattach: true },
    });
    conv = { ...conv, history: { ...conv.history, loaded: true } };
    expect(feed(conv, { type: 'resend_start' }).effects[0]).toEqual({
      type: 'send',
      message: { type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-1', resume_from: { stream_id: S, seq: 4 }, reattach: true },
    });
  });

  it('OPEN-2: only a user action creates; once subscribed (or opened from the pool) every start reattaches', () => {
    const start = (c: Conversation, input: Parameters<typeof feed>[1] = { type: 'resend_start' }) => feed(c, input).effects[0];
    const mine = initialConversation({ localId: 'N1', kind: 'agent', provider: 'claude' }); // a user action (new, History, fork)
    let conv = feed(mine, { type: 'socket_open' }).conv;
    expect(conv.startRequest).toEqual({ type: 'start', local_id: 'N1' });
    conv = feed(conv, { type: 'session_started', session_id: 'N1' }).conv;
    expect(conv.reattach).toBe(true);
    expect(start(conv)).toEqual({ type: 'send', message: { type: 'start', local_id: 'N1', reattach: true } }); // visible resync, reconnect
    expect(feed(conv, { type: 'error', error: 'not_started' }).conv.startRequest).toEqual({ type: 'start', local_id: 'N1', reattach: true });
    // Save and Restart / rewind close it themselves: the next start is the user's again
    const restarting = feed(conv, { type: 'local_stop' }).conv;
    expect(restarting.reattach).toBe(false);
    expect(start(restarting)).toEqual({ type: 'send', message: { type: 'start', local_id: 'N1' } });
    // a view opened from the pool never creates, voice owner's voice_start included
    const pooled = initialConversation({ localId: 'O1', kind: 'orchestrator', sdkId: 'O1', reattach: true });
    expect(start(pooled, { type: 'socket_open', start: { type: 'voice_start', local_id: 'O1' } })).toEqual({
      type: 'send',
      message: { type: 'voice_start', local_id: 'O1', reattach: true },
    });
  });

  it('OPEN-3: error{session_closed} ends the wait (held frames kept), stops the conversation and reports its close', () => {
    const waiting = feed(agent(), { type: 'socket_open' }, { type: 'user_message', text: 'held' }).conv;
    expect(waiting.preStart).toHaveLength(1);
    const r = feed(waiting, { type: 'frame', frame: { type: 'error', error: 'session_closed', detail: 'not open' } });
    expect(r.conv.awaitingSessionStarted).toBe(false);
    expect(r.conv.status).toBe('stopped');
    expect(texts(r.conv)).toEqual(['U(echo):held']);
    expect(r.effects).toEqual([{ type: 'watcher', frame: { type: 'agent_session_closed', session_id: 'L1', is_orchestrator: false } }]);
  });

  it('never sends resume_from for Qwen/Gemini or the orchestrator (no seq), nor resume_sdk_id without an sdkId', () => {
    const q = initialConversation({ localId: 'Q', kind: 'agent', provider: 'qwen', sdkId: null });
    const withCp = { ...q, checkpoint: { stream_id: S, seq: 1 }, history: { ...q.history, loaded: true } };
    expect(feed(withCp, { type: 'socket_open' }).effects[0]).toEqual({ type: 'send', message: { type: 'start', local_id: 'Q' } });
  });

  it('voice_start replaces start for the voice owner (T-11), and resend_start is a no-op on a closed socket', () => {
    const vs = { type: 'voice_start', local_id: 'O1' } as const;
    const { conv, effects } = feed(orch({ subscribed: false }), { type: 'socket_open', start: vs });
    expect(effects).toEqual([{ type: 'send', message: vs }]);
    expect(conv.startRequest).toEqual(vs);
    expect(feed(orch({ subscribed: false }), { type: 'resend_start' }).effects).toEqual([]);
  });

  it('session_started subscribes, clears the banner, adopts the localId (ID-1) and the context window', () => {
    let conv = initialConversation({ localId: 'L1', kind: 'agent', provider: 'claude' });
    expect(conv.status).toBe('connecting');
    conv = feed(conv, { type: 'socket_open' }, { type: 'socket_closed' }, { type: 'socket_open' }).conv;
    expect(conv.connectionBanner).toEqual({ code: 'disconnected', detail: null });
    conv = feed(conv, { type: 'status', status: 'connecting' }, { type: 'session_started', session_id: 'L9', context_window: 100000 }).conv;
    expect(conv.conn).toBe('subscribed');
    expect(conv.connectionBanner).toBeNull();
    expect(conv.ref.localId).toBe('L9');
    expect(conv.status).toBe('idle');
    expect(conv.counters.contextWindow).toBe(100000);
  });

  it('orchestrator context window from model_info, fallback 200 000; jsonl_id becomes the sdkId (O-3)', () => {
    const o = initialConversation({ localId: 'O1', kind: 'orchestrator' });
    let conv = feed(o, { type: 'socket_open' }, { type: 'session_started', session_id: 'O1', voice: false, jsonl_id: 'J1' }).conv;
    expect(conv.counters.contextWindow).toBe(200000);
    expect(conv.ref.sdkId).toBe('J1');
    conv = feed(conv, { type: 'session_started', session_id: 'O1', model_info: { model: 'm', model_info: { context_window: 400000 } } }).conv;
    expect(conv.counters.contextWindow).toBe(400000);
    conv = feed(conv, { type: 'model_changed', model_info: { model_info: { context_window: 128000 } } }).conv;
    expect(conv.counters.contextWindow).toBe(128000);
    conv = feed(conv, { type: 'model_info', model_info: null }).conv;
    expect(conv.counters.contextWindow).toBe(128000);
  });
});

describe('pre-start hold and seq (SEQ-1 … SEQ-5)', () => {
  it('frames before session_started are held, then applied in order when no replay was granted', () => {
    let conv = feed(agent(), { type: 'socket_open' }).conv;
    conv = feed(conv, { type: 'user_message', text: 'hi' }, { type: 'status', status: 'processing' }, { type: 'text_delta', text: 'A', seq: 1, stream_id: S }).conv;
    expect(conv.entries).toEqual([]);
    expect(conv.preStart).toHaveLength(3);
    conv = feed(conv, { type: 'session_started', session_id: 'L1', context_window: 1, resume_state: { stream_id: S, next_seq: 1 } }).conv;
    expect(texts(conv)).toEqual(['U(echo):hi', 'A[text:A]']);
    expect(conv.checkpoint).toEqual({ stream_id: S, seq: 1 }); // max(applied 1, next_seq-1 = 0)
    expect(conv.status).toBe('streaming');
  });

  it('a granted replay discards held seq-stamped frames but keeps unsequenced ones (SEQ-5)', () => {
    let conv: Conversation = { ...agent(), checkpoint: { stream_id: S, seq: 2 }, history: { ...agent().history, loaded: true } };
    conv = feed(conv, { type: 'socket_open' }, { type: 'text_delta', text: 'X', seq: 3, stream_id: S }, { type: 'user_message', text: 'u' }).conv;
    conv = feed(conv, { type: 'session_started', session_id: 'L1', resume_state: { stream_id: S, next_seq: 4 } }).conv;
    expect(texts(conv)).toEqual(['U(echo):u']);
    expect(conv.checkpoint).toEqual({ stream_id: S, seq: 2 }); // the replay will advance it
  });

  it('a new stream replaces the checkpoint (SEQ-4); duplicates are dropped (SEQ-1); stalls are exempt (SEQ-2)', () => {
    let conv = feed(agent(), { type: 'text_delta', text: 'a', seq: 5, stream_id: S }, { type: 'text_delta', text: 'b', seq: 5, stream_id: S }).conv;
    expect(texts(conv)).toEqual(['A[text:a]']);
    conv = feed(conv, { type: 'text_delta', text: 'c', seq: 1, stream_id: 'L1:2' }).conv;
    expect(texts(conv)).toEqual(['A[text:ac]']);
    expect(conv.checkpoint).toEqual({ stream_id: 'L1:2', seq: 1 });
    conv = feed(conv, { type: 'session_stalled', elapsed_seconds: 120, seq: 1, stream_id: 'L1:2' }).conv;
    expect(conv.stall).toEqual({ elapsed_seconds: 120, last_tool_name: null, last_tool_use_id: null });
    expect(conv.checkpoint).toEqual({ stream_id: 'L1:2', seq: 1 });
  });

  it('session_started of a different stream seeds the checkpoint at next_seq - 1', () => {
    const conv = feed({ ...agent(), checkpoint: { stream_id: S, seq: 9 } }, { type: 'socket_open' }, {
      type: 'session_started',
      session_id: 'L1',
      resume_state: { stream_id: 'L1:2', next_seq: 3 },
    }).conv;
    expect(conv.checkpoint).toEqual({ stream_id: 'L1:2', seq: 2 });
  });

  it('voice_audio_out never reaches the conversation (L-3)', () => {
    const c = orch();
    expect(stepConversation(c, f({ type: 'voice_audio_out', audio: 'AAAA' })).state).toBe(c);
  });
});

describe('canonical reload and cold open (§5.2, §5.6, SEQ-6, SEQ-7)', () => {
  it('cold open: frames are held during the REST fetch, then flushed through the connection manager (H-1)', () => {
    let conv = initialConversation({ localId: 'L1', kind: 'agent', provider: 'claude', sdkId: 'sdk-1', liveStatus: 'streaming' });
    expect(conv.inTurn).toBe(true);
    expect(conv.status).toBe('streaming');
    conv = feed(conv, { type: 'begin_reload' }, { type: 'socket_open' }).conv;
    expect(conv.startRequest).toEqual({ type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-1' });
    conv = feed(
      conv,
      { type: 'session_started', session_id: 'L1', resume_state: { stream_id: S, next_seq: 3 } },
      { type: 'text_delta', text: ' more', seq: 3, stream_id: S },
    ).conv;
    expect(conv.reloadBuffer).toHaveLength(2);
    const r = stepConversation(conv, page([userLine('Q'), { role: 'assistant', text: 'Part', blocks: [{ type: 'text', text: 'Part' }] }]));
    expect(r.state.reloading).toBe(false);
    expect(r.state.history.loaded).toBe(true);
    expect(texts(r.state)).toEqual(['U(history):Q', 'A[text:Part|text: more]']);
    expect(r.state.checkpoint).toEqual({ stream_id: S, seq: 3 });
  });

  it('replay_overflow without an sdkId asks to look it up, and reload_failed keeps entries and flags the gap', () => {
    let conv = agent({ sdkId: null });
    conv = feed(conv, { type: 'text_complete', text: 'kept' }, { type: 'socket_open' }).conv;
    const r = feed(conv, { type: 'session_started', session_id: 'L1', replay_overflow: true }, { type: 'text_complete', text: 'later' });
    expect(r.effects).toContainEqual({ type: 'reload', needsSdkId: true });
    expect(r.conv.checkpoint).toBeNull();
    const done = feed(r.conv, { type: 'reload_failed' }).conv;
    expect(done.gapPossible).toBe(true);
    expect(done.reloading).toBe(false);
    expect(texts(done)).toEqual(['A[text:kept|text:later]']);
    expect(feed(done, { type: 'reload_failed' }).conv).toBe(done);
  });

  it('a socket drop mid-turn flags a gap for the orchestrator and Qwen/Gemini, not for Claude (SEQ-7)', () => {
    const busyOrch = feed(orch(), { type: 'status', status: 'streaming' }, { type: 'socket_closed' }).conv;
    expect(busyOrch.gapPossible).toBe(true);
    const claude = feed(agent(), { type: 'status', status: 'processing' }, { type: 'socket_closed' }).conv;
    expect(claude.gapPossible).toBe(false);
    expect(claude.entries).toEqual([]); // A-4.3 (b): a disconnect never touches entries
  });

  it('Qwen/Gemini: a gap reloads canonically at the next turn end; the reload clears the gap', () => {
    let conv = initialConversation({ localId: 'Q', kind: 'agent', provider: 'gemini', sdkId: 'g1', subscribed: true });
    conv = feed(conv, { type: 'status', status: 'processing' }, { type: 'socket_closed' }, { type: 'socket_open' }).conv;
    conv = feed(conv, { type: 'session_started', session_id: 'Q' }).conv;
    const r = feed(conv, { type: 'turn_complete', session_id: 'g1' });
    expect(r.effects).toContainEqual({ type: 'reload', needsSdkId: false });
    expect(r.conv.reloading).toBe(true);
    const after = feed(r.conv, page([userLine('x')])).conv;
    expect(after.gapPossible).toBe(false);
  });

  it('a prepend or reconcile page during a reload does not complete it', () => {
    const conv = feed(agent(), { type: 'begin_reload' }).conv;
    const r = feed(conv, { type: 'history_page', mode: 'reconcile', response: { messages: [], start_index: 0, total_count: 0, has_more: false } });
    expect(r.conv.reloading).toBe(true);
  });
});

describe('start errors and protocol errors (T-12, §4.4.4, I-15)', () => {
  it('start_failed: banner + failed, held frames are not lost, no entry', () => {
    let conv = feed(agent(), { type: 'socket_open' }, { type: 'text_complete', text: 'held' }).conv;
    conv = feed(conv, { type: 'error', error: 'start_failed', detail: 'boom' }).conv;
    expect(conv.connectionBanner).toEqual({ code: 'start_failed', detail: 'boom' });
    expect(conv.conn).toBe('failed');
    expect(texts(conv)).toEqual(['A[text:held]']);
    expect(feed(conv, { type: 'dismiss_banner' }).conv.connectionBanner).toBeNull();
  });

  it('SEQ-5 exception: any error bypasses the pre-start hold; only a start failure releases the held frames', () => {
    let r = feed(agent(), { type: 'socket_open' }, { type: 'text_complete', text: 'held' }, { type: 'error', error: 'invalid_json' });
    expect(r.effects.map((e) => e.type)).toEqual(['send', 'protocol_error']);
    expect(r.conv.preStart).toHaveLength(1);
    r = feed(r.conv, { type: 'error', error: 'send_failed', detail: 'x' });
    expect(texts(r.conv)).toEqual(['N(error):x']);
    expect(r.conv.awaitingSessionStarted).toBe(true);
  });

  it('orchestrator_stopping retries start once after 1 s, then fails', () => {
    let r = feed(orch(), { type: 'error', error: 'orchestrator_stopping' });
    expect(r.effects).toEqual([{ type: 'retry_start', delayMs: 1000 }]);
    r = feed(r.conv, { type: 'error', error: 'orchestrator_stopping', detail: 'still' });
    expect(r.effects).toEqual([]);
    expect(r.conv.connectionBanner).toEqual({ code: 'orchestrator_stopping', detail: 'still' });
    expect(feed(r.conv, { type: 'session_started', session_id: 'O1' }).conv.stoppingRetried).toBe(false);
  });

  it('not_started re-sends start; protocol and unknown codes are toasts; voice codes are ignored', () => {
    const r = feed(agent(), { type: 'error', error: 'not_started' });
    expect(r.effects.map((e) => e.type)).toEqual(['protocol_error', 'send']);
    const p = feed(agent(), { type: 'error', error: 'invalid_json' }, { type: 'error', error: 'brand_new_code', detail: 'x' });
    expect(p.effects).toEqual([
      { type: 'protocol_error', code: 'invalid_json', detail: null },
      { type: 'protocol_error', code: 'brand_new_code', detail: 'x' },
    ]);
    expect(p.conv.entries).toEqual([]);
    const v = orch();
    expect(feed(v, { type: 'error', error: 'voice_event_failed' }).conv).toBe(v);
  });
});

describe('identity (ID-2, ST-2) and pool events', () => {
  it('learns the sdkId from the first turn_complete, else asks to look it up (ID-2)', () => {
    const fresh = agent({ sdkId: null });
    const r = feed(fresh, { type: 'status', status: 'processing' }, { type: 'turn_complete', is_error: false });
    expect(r.effects).toContainEqual({ type: 'learn_sdk_id' });
    const learned = feed(r.conv, { type: 'sdk_id', sdkId: 'gem-7' }).conv;
    expect(learned.ref.sdkId).toBe('gem-7');
    expect(feed(learned, { type: 'sdk_id', sdkId: 'other' }).conv.ref.sdkId).toBe('gem-7');
    expect(feed(fresh, { type: 'turn_complete', session_id: 'abc' }).conv.ref.sdkId).toBe('abc');
  });

  it('pool/live status is authoritative for the turn state (ST-2); the orchestrator row is ignored', () => {
    let conv = feed(agent(), { type: 'pool_status', status: 'tool_use' }).conv;
    expect(conv.inTurn).toBe(true);
    expect(conv.status).toBe('tool_use');
    conv = feed(conv, { type: 'tool_use', tool_use_id: 't', tool_name: 'Bash', tool_input: {} }, { type: 'pool_status', status: 'idle' }).conv;
    expect(conv.inTurn).toBe(false);
    expect(texts(conv)).toEqual(['A[tool:t:no_result]']);
    const o = orch();
    expect(feed(o, { type: 'pool_status', status: 'streaming' }).conv).toBe(o);
  });

  it('ST-2: interrupted (Codex/Gemini/Qwen after a stop) and disconnected also end a stale turn', () => {
    for (const settled of ['interrupted', 'disconnected'] as const) {
      const busy = feed(agent(), { type: 'pool_status', status: 'tool_use' }).conv;
      const conv = feed(busy, { type: 'tool_use', tool_use_id: 't', tool_name: 'Bash', tool_input: {} }, { type: 'pool_status', status: settled }).conv;
      expect(conv.inTurn).toBe(false);
      expect(texts(conv)).toEqual(['A[tool:t:no_result]']);
    }
  });

  it('error{turn_timeout} (the orchestrator stopped the turn) is a turn failure notice', () => {
    const busy = feed(agent(), { type: 'pool_status', status: 'tool_use' }).conv;
    const conv = feed(busy, { type: 'error', error: 'turn_timeout', detail: 'The orchestrator stopped this turn: no progress for 1800s.' }, { type: 'status', status: 'interrupted' }).conv;
    expect(conv.inTurn).toBe(false);
    expect(conv.entries.filter((e) => e.kind === 'notice').map((e) => (e as { text: string }).text)).toEqual([
      'The orchestrator stopped this turn: no progress for 1800s.',
    ]);
  });

  it('watcher events become effects; closing this conversation stops it (FOCUS-2 kind check)', () => {
    const opened = { type: 'agent_session_opened', session_id: 'L5', sdk_session_id: 's5', is_orchestrator: false };
    const r = feed(orch(), opened, { type: 'agent_session_closed', session_id: 'L5', is_orchestrator: false });
    expect(r.effects.map((e) => e.type)).toEqual(['watcher', 'watcher']);
    expect(r.conv.status).toBe('idle');
    const self = feed(orch(), { type: 'status', status: 'streaming' }, { type: 'agent_session_closed', session_id: 'O1', is_orchestrator: true });
    expect(self.conv.status).toBe('stopped');
    expect(self.conv.inTurn).toBe(false);
    const wrongKind = feed(agent(), { type: 'agent_session_closed', session_id: 'L1', is_orchestrator: true });
    expect(wrongKind.conv.status).toBe('idle');
  });

  it('a stopped view comes back to idle on the next session_started (§2.5)', () => {
    let conv = feed(agent(), { type: 'session_stopped' }).conv;
    expect(conv.status).toBe('stopped');
    conv = feed(conv, { type: 'socket_open' }, { type: 'session_started', session_id: 'L1' }).conv;
    expect(conv.status).toBe('idle');
  });

  it('history tool still running on a cold open of a busy session keeps running; otherwise no_result (TC-2)', () => {
    const busy = initialConversation({ localId: 'L1', kind: 'agent', provider: 'claude', sdkId: 's', liveStatus: 'tool_use' });
    expect(texts(feed(busy, page([userLine('q'), toolUseLine('a')])).conv)).toEqual(['U(history):q', 'A[tool:a:running]']);
    expect(texts(feed(agent(), page([userLine('q'), toolUseLine('a')])).conv)).toEqual(['U(history):q', 'A[tool:a:no_result]']);
  });
});
