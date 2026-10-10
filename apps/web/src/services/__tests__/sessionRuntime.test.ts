/**
 * SessionRuntime with a fake WebSocket and fake timers (spec 13 W-06 DoD): `start` on every open
 * and on visibility, reconnect/backoff paused while hidden, in-memory checkpoint replay (T-10),
 * `replay_overflow` → REST reload (SEQ-6), ID-1 adoption, cold-open order (H-1), outbox,
 * reconcile debounce, pagination.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getSessionEntry, tabsStore } from '@/stores';
import { legacyPolicy, openSession, SessionRuntime, type AnyRuntime } from '@/services';
import { EMPTY, FakeWebSocket, flushPromises, jsonResponse, setupServices, teardownServices, type Harness } from './fakes';

const CHAT = '/api/sessions/chat';
let h: Harness;

beforeEach(() => {
  vi.useFakeTimers();
  h = setupServices();
});
afterEach(() => {
  teardownServices();
  vi.useRealTimers();
});

function page(texts: [string, string][], start = 0, total?: number) {
  const messages = texts.map(([role, text]) => ({ role, text, blocks: [{ type: 'text', text }] }));
  return { messages, total_count: total ?? start + messages.length, has_more: start > 0, start_index: start };
}

function open(opts: Partial<Parameters<typeof openSession>[0]> = {}): SessionRuntime {
  return openSession({ kind: 'agent', focus: true, ...opts }) as SessionRuntime;
}

const started = (ws: FakeWebSocket, sessionId: string, extra: Record<string, unknown> = {}) =>
  ws.emit({ type: 'session_started', session_id: sessionId, context_window: 200000, ...extra });

describe('start handshake', () => {
  it('sends start on open, on every reconnect, and re-sends it on visibility when open (inv02 F-01 #2); the automatic ones reattach (OPEN-2)', async () => {
    const rt = open({ localId: 'L1' });
    const ws1 = FakeWebSocket.last(CHAT);
    expect(ws1.url).toBe('ws://backend.test/api/sessions/chat');
    expect(ws1.binaryType).toBe('arraybuffer');
    ws1.open();
    expect(ws1.messages()).toEqual([{ type: 'start', local_id: 'L1' }]);
    started(ws1, 'L1');
    expect(rt.conv.conn).toBe('subscribed');

    h.visibility.set(true);
    h.visibility.set(false); // visible again with an OPEN socket: re-send start (T-9)
    expect(ws1.messages()).toEqual([
      { type: 'start', local_id: 'L1' },
      { type: 'start', local_id: 'L1', reattach: true },
    ]);

    ws1.drop();
    expect(rt.conv.connectionBanner).toEqual({ code: 'disconnected', detail: null });
    vi.advanceTimersByTime(1000); // backoff attempt 0: 1 s × (0.8 + 0.4 × 0.5)
    const ws2 = FakeWebSocket.last(CHAT);
    expect(ws2).not.toBe(ws1);
    ws2.open();
    expect(ws2.messages()).toEqual([{ type: 'start', local_id: 'L1', reattach: true }]);
  });

  it('adopts session_started.session_id (ID-1) and re-keys the registry and the tab', () => {
    open({ localId: 'L1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1-server');
    expect(getSessionEntry('L1')).toBeUndefined();
    expect(getSessionEntry('L1-server')).toBeDefined();
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['L1-server']);
    expect(tabsStore.getState().activeId).toBe('L1-server');
  });

  it('start_failed shows the banner (no retry loop); Retry re-sends start on the open socket', () => {
    const rt = open({ localId: 'L1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    ws.emit({ type: 'error', error: 'start_failed', detail: 'Directory does not exist: /nope' });
    expect(rt.conv.conn).toBe('failed');
    expect(rt.conv.connectionBanner).toEqual({ code: 'start_failed', detail: 'Directory does not exist: /nope' });
    vi.advanceTimersByTime(60_000);
    expect(ws.types()).toEqual(['start']);
    rt.retry();
    expect(ws.types()).toEqual(['start', 'start']);
  });

  it('not_started re-sends start once (T-12)', () => {
    open({ localId: 'L1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    ws.emit({ type: 'error', error: 'not_started' });
    expect(ws.types()).toEqual(['start', 'start']);
  });
});

describe('reconnect and visibility (T-13, T-14)', () => {
  it('backs off 1 s, 2 s, 4 s … capped at 15 s, and resets after a successful session_started', () => {
    open({ localId: 'L1' });
    const delays: number[] = [];
    for (let i = 0; i < 6; i++) {
      FakeWebSocket.last(CHAT).drop();
      const before = FakeWebSocket.all(CHAT).length;
      let waited = 0;
      while (FakeWebSocket.all(CHAT).length === before) {
        vi.advanceTimersByTime(100);
        waited += 100;
      }
      delays.push(waited);
    }
    expect(delays).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    ws.drop();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.all(CHAT).length).toBe(7);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.all(CHAT).length).toBe(8);
  });

  it('does not reconnect while hidden; on visible it connects at once with the backoff reset', () => {
    open({ localId: 'L1' });
    FakeWebSocket.last(CHAT).open();
    h.visibility.set(true);
    FakeWebSocket.last(CHAT).drop();
    vi.advanceTimersByTime(120_000);
    expect(FakeWebSocket.all(CHAT)).toHaveLength(1);
    h.visibility.set(false);
    expect(FakeWebSocket.all(CHAT)).toHaveLength(2);
  });

  it('hiding cancels a pending reconnect timer', () => {
    open({ localId: 'L1' });
    FakeWebSocket.last(CHAT).drop();
    h.visibility.set(true);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.all(CHAT)).toHaveLength(1);
  });

  it('legacy policy: 2 s × 10 attempts, then gives up (inv02 F-01)', () => {
    const rt = new SessionRuntime({ localId: 'X', reconnectPolicy: legacyPolicy }, {
      onRekey: () => undefined,
      onSdkId: () => undefined,
      onTurnEnded: () => undefined,
      onWatcher: () => undefined,
      onNested: () => undefined,
      lookupSdkId: async () => null,
      onResubscribed: () => undefined,
    });
    rt.open();
    for (let i = 0; i < 12; i++) {
      FakeWebSocket.last(CHAT).drop();
      vi.advanceTimersByTime(2000);
    }
    expect(FakeWebSocket.all(CHAT)).toHaveLength(11);
    rt.dispose();
  });
});

describe('cold open and history (§5.2)', () => {
  it('subscribes first, fetches history after session_started, then applies held frames (H-1)', async () => {
    h.fetch.on('GET', /^\/api\/sessions\/sdk-1\/messages/, page([['user', 'hi'], ['assistant', 'hello']]));
    const rt = open({ localId: 'L1', sdkId: 'sdk-1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    expect(ws.messages()).toEqual([{ type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-1' }]); // no resume_from (T-10)
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages')).toHaveLength(0);
    started(ws, 'L1', { resume_state: { stream_id: 's1', next_seq: 5 } });
    ws.emit({ type: 'user_message', text: 'from elsewhere' });
    expect(rt.conv.reloading).toBe(true);
    await flushPromises();
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages?limit=50')).toHaveLength(1);
    expect(rt.conv.reloading).toBe(false);
    expect(rt.conv.entries.map((e) => (e.kind === 'user' ? e.text : e.kind))).toEqual(['hi', 'assistant', 'from elsewhere']);
    expect(rt.conv.checkpoint).toEqual({ stream_id: 's1', seq: 4 });
  });

  it('a new session (no sdk id) needs no history and has nothing to reload', () => {
    const rt = open({ localId: 'L1' });
    expect(rt.conv.history.loaded).toBe(true);
    expect(rt.conv.reloading).toBe(false);
    expect(h.fetch.requests).toHaveLength(0);
  });

  it('a history 404 is an empty history; other errors keep a verbatim historyError', async () => {
    h.fetch.on('GET', /^\/api\/sessions\/sdk-x\/messages/, () => jsonResponse({ detail: 'Session store unavailable' }, 500));
    const rt = open({ localId: 'L2', sdkId: 'sdk-x' });
    FakeWebSocket.last(CHAT).open();
    started(FakeWebSocket.last(CHAT), 'L2');
    await flushPromises();
    expect(rt.handle.store.getState().historyError).toBe('Session store unavailable');
    expect(rt.conv.gapPossible).toBe(true);
    expect(rt.conv.reloading).toBe(false);
  });

  it('loadOlder prepends an abutting page and reloads on a non-abutting one (§5.3)', async () => {
    h.fetch.on('GET', '/api/sessions/sdk-1/messages', (req) => {
      if (req.path.includes('before=2')) return jsonResponse(page([['user', 'old q'], ['assistant', 'old a']], 0, 4));
      return jsonResponse(page([['user', 'new q'], ['assistant', 'new a']], 2, 4));
    });
    const rt = open({ localId: 'L1', sdkId: 'sdk-1' });
    FakeWebSocket.last(CHAT).open();
    started(FakeWebSocket.last(CHAT), 'L1');
    await flushPromises();
    expect(rt.conv.history).toMatchObject({ startIndex: 2, hasMore: true });
    await rt.loadOlder();
    expect(rt.conv.entries.filter((e) => e.kind === 'user').map((e) => (e.kind === 'user' ? e.text : ''))).toEqual(['old q', 'new q']);
    expect(rt.conv.history).toMatchObject({ startIndex: 0, hasMore: false });
    await rt.loadOlder(); // nothing more
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages')).toHaveLength(2);
  });

  it('a page that does not abut triggers a canonical reload', async () => {
    let n = 0;
    h.fetch.on('GET', '/api/sessions/sdk-1/messages', (req) => {
      n++;
      if (req.path.includes('before=')) return jsonResponse(page([['user', 'x']], 0, 3)); // ends at 1, not 2
      return jsonResponse(page([['user', 'q'], ['assistant', 'a']], 2, 4));
    });
    const rt = open({ localId: 'L1', sdkId: 'sdk-1' });
    FakeWebSocket.last(CHAT).open();
    started(FakeWebSocket.last(CHAT), 'L1');
    await flushPromises();
    await rt.loadOlder();
    await flushPromises();
    expect(n).toBe(3); // cold open + older + reload
    expect(rt.conv.reloading).toBe(false);
  });
});

describe('in-memory checkpoint replay (T-10) and replay_overflow (SEQ-6)', () => {
  function subscribedNew(): { rt: SessionRuntime; ws: FakeWebSocket } {
    const rt = open({ localId: 'L1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1', { resume_state: { stream_id: 's1', next_seq: 1 } });
    return { rt, ws };
  }

  it('after an in-page reconnect, start carries resume_from = the in-memory checkpoint; replayed duplicates are dropped', () => {
    const { rt, ws } = subscribedNew();
    rt.send('hello');
    ws.emit({ type: 'status', status: 'processing' });
    ws.emit({ type: 'text_delta', text: 'Hel', seq: 1, stream_id: 's1' });
    ws.emit({ type: 'text_delta', text: 'lo', seq: 2, stream_id: 's1' });
    ws.drop();
    vi.advanceTimersByTime(1000);
    const ws2 = FakeWebSocket.last(CHAT);
    ws2.open();
    expect(ws2.messages()[0]).toEqual({ type: 'start', local_id: 'L1', resume_from: { stream_id: 's1', seq: 2 }, reattach: true });
    started(ws2, 'L1', { resume_state: { stream_id: 's1', next_seq: 4 } });
    ws2.emit({ type: 'text_delta', text: 'lo', seq: 2, stream_id: 's1' }); // duplicate (SEQ-1)
    ws2.emit({ type: 'text_delta', text: '!', seq: 3, stream_id: 's1' });
    ws2.emit({ type: 'turn_complete', session_id: 'sdk-9', seq: 4, stream_id: 's1' });
    const run = rt.conv.entries[1];
    expect(run?.kind === 'assistant' && run.blocks[0]?.type === 'text' ? run.blocks[0].text : null).toBe('Hello!');
    expect(rt.conv.ref.sdkId).toBe('sdk-9');
  });

  it('nothing is written to storage for the checkpoint (a reload rebuilds from REST, no resume_from)', () => {
    const { ws } = subscribedNew();
    ws.emit({ type: 'text_delta', text: 'x', seq: 1, stream_id: 's1' });
    const keys = Object.keys(window.sessionStorage).concat(Object.keys(window.localStorage));
    expect(keys.filter((k) => /checkpoint|resume/i.test(k))).toEqual([]);
    // "page reload": a fresh runtime for the same session sends no resume_from
    teardownServices();
    h = setupServices();
    open({ localId: 'L1', sdkId: 'sdk-9' });
    FakeWebSocket.last(CHAT).open();
    expect(FakeWebSocket.last(CHAT).messages()[0]).toEqual({ type: 'start', local_id: 'L1', resume_sdk_id: 'sdk-9' });
  });

  it('replay_overflow reloads from REST, then applies the held frames', async () => {
    const { rt, ws } = subscribedNew();
    rt.send('q');
    ws.emit({ type: 'status', status: 'processing' });
    ws.emit({ type: 'turn_complete', session_id: 'sdk-1', seq: 1, stream_id: 's1' });
    h.fetch.on('GET', /^\/api\/sessions\/sdk-1\/messages/, page([['user', 'q'], ['assistant', 'answer from REST'], ['user', 'later']]));
    ws.drop();
    vi.advanceTimersByTime(1000);
    const ws2 = FakeWebSocket.last(CHAT);
    ws2.open();
    expect(ws2.messages()[0]).toMatchObject({ resume_from: { stream_id: 's1', seq: 1 } });
    started(ws2, 'L1', { replay_overflow: true, resume_state: { stream_id: 's2', next_seq: 10 } });
    ws2.emit({ type: 'user_message', text: 'after overflow' });
    expect(rt.conv.reloading).toBe(true);
    await flushPromises();
    expect(rt.conv.reloading).toBe(false);
    expect(rt.conv.entries.map((e) => (e.kind === 'user' ? e.text : e.kind))).toEqual(['q', 'assistant', 'later', 'after overflow']);
    expect(rt.conv.checkpoint).toEqual({ stream_id: 's2', seq: 9 });
  });

  it('replay_overflow without an sdk id looks it up in pool/live first', async () => {
    const { ws } = subscribedNew();
    h.fetch.on('GET', '/api/sessions/pool/live', [{ local_id: 'L1', sdk_session_id: 'sdk-p', status: 'idle', cost: 0, turns: 1, title: null, is_orchestrator: false }]);
    h.fetch.on('GET', /^\/api\/sessions\/sdk-p\/messages/, page([['user', 'from pool']]));
    ws.drop();
    vi.advanceTimersByTime(1000);
    const ws2 = FakeWebSocket.last(CHAT);
    ws2.open();
    started(ws2, 'L1', { replay_overflow: true });
    await flushPromises();
    const rt = getSessionEntry('L1')?.runtime as AnyRuntime;
    expect(rt.conv.ref.sdkId).toBe('sdk-p');
    expect(rt.conv.entries.map((e) => e.kind)).toEqual(['user']);
  });
});

describe('user actions', () => {
  it('a send before subscribe waits in the outbox and goes out after session_started', () => {
    const rt = open({ localId: 'L1' });
    rt.send('early');
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    expect(ws.types()).toEqual(['start']);
    started(ws, 'L1');
    expect(ws.messages()).toEqual([
      { type: 'start', local_id: 'L1' },
      { type: 'send', text: 'early' },
    ]);
    expect(rt.conv.entries[0]).toMatchObject({ kind: 'user', text: 'early', origin: 'local' });
  });

  it('interrupt / compact / permission responses / command are text frames', () => {
    const rt = open({ localId: 'L1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    rt.interrupt();
    rt.compact();
    rt.respondPermission('r1', 'allow');
    rt.respondPermission('r2', 'deny', 'nope');
    rt.command('/help');
    expect(ws.messages().slice(1)).toEqual([
      { type: 'interrupt' },
      { type: 'compact' },
      { type: 'permission_response', request_id: 'r1', decision: 'allow' },
      { type: 'permission_response', request_id: 'r2', decision: 'deny', message: 'nope' },
      { type: 'command', text: '/help' },
    ]);
    expect(rt.conv.status).toBe('compacting');
  });

  it('turn end without an sdk id looks it up in pool/live (ID-2) and updates the tab', async () => {
    h.fetch.on('GET', '/api/sessions/pool/live', [{ local_id: 'L1', sdk_session_id: 'g-1', status: 'idle', cost: 0, turns: 1, title: null, is_orchestrator: false }]);
    const rt = open({ localId: 'L1', provider: 'gemini' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    rt.send('hi');
    ws.emit({ type: 'status', status: 'processing' });
    ws.emit({ type: 'turn_complete' });
    await flushPromises();
    expect(rt.conv.ref.sdkId).toBe('g-1');
    expect(tabsStore.getState().tabs[0]?.sdkId).toBe('g-1');
  });

  it('reconcile after a turn with unattributed results: one debounced REST fetch (R-7)', async () => {
    h.fetch.on('GET', /^\/api\/sessions\/sdk-1\/messages/, EMPTY);
    const rt = open({ localId: 'L1', sdkId: 'sdk-1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    await flushPromises();
    rt.send('go');
    ws.emit({ type: 'status', status: 'processing' });
    ws.emit({ type: 'tool_use', tool_use_id: 't1', tool_name: 'Bash', tool_input: {} });
    ws.emit({ type: 'tool_use', tool_use_id: 't2', tool_name: 'Bash', tool_input: {} });
    ws.emit({ type: 'tool_result', tool_use_id: '', output: 'x' });
    ws.emit({ type: 'turn_complete', session_id: 'sdk-1' });
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages')).toHaveLength(1);
    vi.advanceTimersByTime(499);
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages')).toHaveLength(1);
    vi.advanceTimersByTime(1);
    await flushPromises();
    expect(h.fetch.calls('GET', '/api/sessions/sdk-1/messages')).toHaveLength(2);
  });

  it('Save and Restart: POST close, then start again with the same local id on the open socket (§6.14)', async () => {
    h.fetch.on('POST', '/api/sessions/L1/close', () => new Response(null, { status: 204 }));
    const rt = open({ localId: 'L1', sdkId: 'sdk-1' });
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    started(ws, 'L1');
    await flushPromises();
    await rt.restart();
    ws.emit({ type: 'session_stopped' }); // our own ack: swallowed
    expect(h.fetch.calls('POST', '/api/sessions/L1/close')).toHaveLength(1);
    expect(ws.messages().filter((m) => m.type === 'start')).toHaveLength(2);
    expect(ws.types()).not.toContain('stop');
    expect(rt.conv.status).not.toBe('stopped');
  });
});
