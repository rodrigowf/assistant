/**
 * Session directory: P-1 (no close/stop from unload, visibility, teardown; explicit close does
 * close), P-6 / FOCUS-1 (background opens never steal focus), the server's pool as the open set
 * (OPEN-1..4: reattach-only starts, closed elsewhere = gone, reconcile on every read), watcher
 * events with `is_orchestrator` (FOCUS-2), the single orchestrator socket (T-6/T-7), Archie
 * attach, delete (§6.8), rewind order (§6.5), agent approvals (§6.9).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { activateTab, getSessionEntry, setCatalogItems, snackbarStore, tabsStore } from '@/stores';
import {
  ArchieRuntime,
  closeSession,
  closeTab,
  deleteSession,
  forkSession,
  getArchieRuntime,
  getOrchestratorRef,
  getSessionRuntime,
  onAgentTurn,
  openArchie,
  openSession,
  replaceRunningArchie,
  respondAgentPermission,
  rewindSession,
  SessionActionError,
  SessionRuntime,
  setSwitchVoiceHandler,
  startServices,
  stopServices,
  syncPool,
} from '@/services';
import { FakeWebSocket, flushPromises, jsonResponse, setupServices, teardownServices, type Harness } from './fakes';

const CHAT = '/api/sessions/chat';
const ORCH = '/api/orchestrator/chat';
let h: Harness;

beforeEach(() => {
  vi.useFakeTimers();
  h = setupServices();
  h.fetch.on('POST', /^\/api\/sessions\/[^/]+\/close$/, () => new Response(null, { status: 204 }));
});
afterEach(() => {
  teardownServices();
  vi.useRealTimers();
});

const pool = (rows: Record<string, unknown>[]) =>
  rows.map((r) => ({ sdk_session_id: null, status: 'idle', cost: 0, turns: 0, title: null, is_orchestrator: false, ...r }));

function subscribe(ws: FakeWebSocket, id: string, extra: Record<string, unknown> = {}): void {
  if (ws.readyState !== 1) ws.open();
  ws.emit({ type: 'session_started', session_id: id, ...extra });
}

function closeCalls(): string[] {
  return h.fetch.requests.filter((r) => r.method === 'POST' && r.path.endsWith('/close')).map((r) => r.path);
}

describe('P-1: closing the browser, losing the connection or backgrounding never closes a session', () => {
  it('pagehide / beforeunload / unload / visibility hidden / teardown send no stop and no POST close', async () => {
    startServices({ skipInitialSync: true });
    const a = openSession({ kind: 'agent', localId: 'A1', focus: true });
    subscribe(FakeWebSocket.last(CHAT), 'A1');
    FakeWebSocket.last(ORCH).open();
    const archie = openSession({ kind: 'archie', localId: 'O1', focus: false });
    subscribe(FakeWebSocket.last(ORCH), 'O1');
    a.send('hello');

    window.dispatchEvent(new Event('pagehide'));
    window.dispatchEvent(new Event('beforeunload'));
    window.dispatchEvent(new Event('unload'));
    h.visibility.set(true);
    vi.advanceTimersByTime(60_000);
    FakeWebSocket.last(CHAT).drop(); // lost connection
    vi.advanceTimersByTime(60_000);
    archie.dispose(); // runtime teardown
    stopServices(); // app teardown
    await flushPromises();

    expect(closeCalls()).toEqual([]);
    const sentTypes = FakeWebSocket.allSent().map((m) => m.type);
    expect(sentTypes).not.toContain('stop');
    expect(sentTypes).not.toContain('voice_stop');
    expect(sentTypes).toEqual(['start', 'start', 'send']);
    expect(FakeWebSocket.instances.every((w) => w.closedByClient || w.readyState === 3)).toBe(true);
  });

  it('an explicit close closes the session for everybody: POST close with the local id, tab removed, no stop frame', async () => {
    const rt = openSession({ kind: 'agent', localId: 'A1', focus: true }) as SessionRuntime;
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'A1');
    rt.setDraft('unsent');
    await closeSession('A1');
    expect(closeCalls()).toEqual(['/api/sessions/A1/close']);
    expect(ws.types()).toEqual(['start']);
    expect(ws.closedByClient).toBe(true);
    expect(tabsStore.getState().tabs).toEqual([]);
    expect(getSessionEntry('A1')).toBeUndefined();
    vi.advanceTimersByTime(1000);
    expect(window.sessionStorage.getItem('draft:A1')).toBeNull();
  });

  it('closing a doc tab sends nothing; closing a read-only view sends nothing', async () => {
    h.fetch.on('GET', /^\/api\/sessions\/old\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    openSession({ kind: 'agent', sdkId: 'old', localId: 'R1', focus: true, readOnly: true });
    expect(FakeWebSocket.all(CHAT)).toHaveLength(0); // H-3: REST only
    await closeTab('R1');
    expect(closeCalls()).toEqual([]);
  });
});

describe('P-6 / FOCUS-1: background opens never steal focus', () => {
  it('pool sync re-derives live tabs in the background (inv02 F-23) and records the orchestrator', async () => {
    h.fetch.on(
      'GET',
      '/api/sessions/pool/live',
      pool([
        { local_id: 'O1', sdk_session_id: 'O1', is_orchestrator: true, title: 'Orchestrator' },
        { local_id: 'A1', sdk_session_id: 'sdk-a', status: 'streaming', title: 'Refactor' },
      ]),
    );
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    openSession({ kind: 'agent', localId: 'MINE', focus: true });
    await syncPool();
    const s = tabsStore.getState();
    expect(s.activeId).toBe('MINE');
    expect(s.tabs.map((t) => [t.id, t.kind, t.unseen])).toEqual([
      ['O1', 'archie', true],
      ['MINE', 'agent', false],
      ['A1', 'agent', true],
    ]);
    expect(getOrchestratorRef()).toEqual({ localId: 'O1', sdkId: 'O1' });
    const a1 = getSessionRuntime('A1') as SessionRuntime;
    expect(a1.conv.status).toBe('streaming'); // ST-2 from pool/live
    expect(a1.handle.store.getState().hidden).toBe(true);
    // OPEN-2: a pool row only ever reattaches; the user's own new session creates
    const [mine, a1ws] = FakeWebSocket.all(CHAT) as [FakeWebSocket, FakeWebSocket];
    mine.open();
    a1ws.open();
    expect(mine.messages()).toEqual([{ type: 'start', local_id: 'MINE' }]);
    expect(a1ws.messages()).toEqual([{ type: 'start', local_id: 'A1', resume_sdk_id: 'sdk-a', reattach: true }]);
    activateTab('A1'); // OPEN-1: a row with no view of its own here is simply open: a click shows it
    expect(tabsStore.getState().activeId).toBe('A1');
  });

  it('agent_session_opened on the watcher socket adds a badged background tab + snackbar; Open activates it', () => {
    startServices({ skipInitialSync: true });
    openSession({ kind: 'agent', localId: 'MINE', focus: true });
    const watcher = FakeWebSocket.last(ORCH);
    watcher.open();
    expect(watcher.sent).toEqual([]); // T-7: the watcher never sends start
    watcher.emit({ type: 'agent_session_opened', session_id: 'BG1', sdk_session_id: null, is_orchestrator: false });
    expect(tabsStore.getState().activeId).toBe('MINE');
    expect(tabsStore.getState().tabs.find((t) => t.id === 'BG1')).toMatchObject({ unseen: true });
    const snack = snackbarStore.getState().queue[0];
    expect(snack?.message).toBe('Archie opened Agent BG1');
    snack?.action?.run();
    expect(tabsStore.getState().activeId).toBe('BG1');
  });

  it('OPEN-3: agent_session_closed closes the view at once, active or not, with no close request; focus moves (FOCUS-2)', () => {
    startServices({ skipInitialSync: true });
    const watcher = FakeWebSocket.last(ORCH);
    watcher.open();
    openSession({ kind: 'agent', localId: 'MINE', focus: true });
    subscribe(FakeWebSocket.last(CHAT), 'MINE');
    watcher.emit({ type: 'agent_session_opened', session_id: 'BG1', is_orchestrator: false });
    watcher.emit({ type: 'agent_session_opened', session_id: 'BG2', is_orchestrator: false });
    watcher.emit({ type: 'agent_session_closed', session_id: 'MINE', is_orchestrator: true }); // wrong kind: ignored
    expect(getSessionRuntime('MINE')).toBeDefined();
    watcher.emit({ type: 'agent_session_closed', session_id: 'BG2', is_orchestrator: false }); // in the background
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['MINE', 'BG1']);
    expect(tabsStore.getState().activeId).toBe('MINE');
    watcher.emit({ type: 'agent_session_closed', session_id: 'MINE', is_orchestrator: false }); // the one on screen
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['BG1']);
    expect(tabsStore.getState().activeId).toBe('BG1'); // as after an explicit close
    expect(getSessionRuntime('MINE')).toBeUndefined();
    expect(snackbarStore.getState().queue.map((q) => q.message).pop()).toBe('New agent session was closed elsewhere');
    expect(closeCalls()).toEqual([]);
  });

  it('OPEN-4: closed while this device was away: the pool read when visible again closes the view; the resync reattaches', async () => {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', '/api/sessions/pool/live', pool([{ local_id: 'A1' }]));
    openSession({ kind: 'agent', localId: 'A1', focus: true });
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'A1');
    await flushPromises();
    h.visibility.set(true);
    h.fetch.on('GET', '/api/sessions/pool/live', pool([])); // another device closed it meanwhile
    h.visibility.set(false);
    expect(ws.messages()[1]).toEqual({ type: 'start', local_id: 'A1', reattach: true }); // can never re-create it
    await flushPromises();
    expect(getSessionRuntime('A1')).toBeUndefined();
    expect(tabsStore.getState().tabs).toEqual([]);
    expect(closeCalls()).toEqual([]);
  });

  it('OPEN-3: error{session_closed}, the answer to a reattach start, closes the view', () => {
    startServices({ skipInitialSync: true });
    openSession({ kind: 'agent', localId: 'A1', focus: true });
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'A1');
    ws.drop();
    vi.advanceTimersByTime(1000);
    const again = FakeWebSocket.last(CHAT);
    again.open();
    expect(again.messages()).toEqual([{ type: 'start', local_id: 'A1', reattach: true }]);
    again.emit({ type: 'error', error: 'session_closed', detail: 'This conversation is not open.' });
    expect(getSessionRuntime('A1')).toBeUndefined();
    expect(tabsStore.getState().tabs).toEqual([]);
    expect(closeCalls()).toEqual([]);
  });

  it('OPEN-2: a user action creates (History open: no reattach); a pool read sent before its session_started never closes it', async () => {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    const rt = openSession({ kind: 'agent', sdkId: 'past', focus: true }) as SessionRuntime;
    const ws = FakeWebSocket.last(CHAT);
    ws.open();
    expect(ws.messages()).toEqual([{ type: 'start', local_id: rt.localId, resume_sdk_id: 'past' }]);
    let answer: (r: Response) => void = () => undefined;
    h.fetch.on('GET', '/api/sessions/pool/live', () => new Promise<Response>((r) => (answer = r)));
    const read = syncPool();
    ws.emit({ type: 'session_started', session_id: rt.localId }); // created while the read was out
    answer(jsonResponse([]));
    await read;
    expect(getSessionRuntime(rt.localId)).toBe(rt);
  });

  it('Save and Restart: the close it makes itself does not close the view; its start creates (no reattach)', async () => {
    startServices({ skipInitialSync: true });
    const watcher = FakeWebSocket.last(ORCH);
    watcher.open();
    const rt = openSession({ kind: 'agent', localId: 'A1', focus: true }) as SessionRuntime;
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'A1');
    const done = rt.restart();
    watcher.emit({ type: 'agent_session_closed', session_id: 'A1', is_orchestrator: false }); // our own close, echoed
    await done;
    expect(getSessionRuntime('A1')).toBe(rt);
    expect(ws.messages().pop()).toEqual({ type: 'start', local_id: 'A1' });
  });

  it('a turn ending in a hidden tab marks it unseen, never activates it', () => {
    openSession({ kind: 'agent', localId: 'MINE', focus: true });
    const bg = openSession({ kind: 'agent', localId: 'BG', focus: false }) as SessionRuntime;
    activateTab('BG');
    activateTab('MINE');
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'BG');
    ws.emit({ type: 'user_message', text: 'remote prompt' });
    ws.emit({ type: 'status', status: 'processing' });
    ws.emit({ type: 'turn_complete', session_id: 'sdk-bg' });
    expect(bg.conv.ref.sdkId).toBe('sdk-bg');
    expect(tabsStore.getState().activeId).toBe('MINE');
    expect(tabsStore.getState().tabs.find((t) => t.id === 'BG')?.unseen).toBe(true);
  });
});

describe('Archie on the single orchestrator socket (T-6, T-7)', () => {
  it('attaches to the open watcher socket, then subscribes on it; voice bridge sends on the same socket', () => {
    startServices({ skipInitialSync: true });
    const watcher = FakeWebSocket.last(ORCH);
    watcher.open();
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    expect(FakeWebSocket.all(ORCH)).toHaveLength(1);
    expect(watcher.messages()).toEqual([{ type: 'start', local_id: 'O1' }]);
    subscribe(watcher, 'O1', { jsonl_id: 'O1', model_info: { model: 'gpt-audio-mini', supports_audio: true } });
    expect(rt.conv.ref.sdkId).toBe('O1'); // ID-4
    expect(rt.modelInfo?.model).toBe('gpt-audio-mini');
    const seen: string[] = [];
    rt.setVoiceHooks({ startMessage: () => ({ type: 'voice_start', local_id: 'O1' }), onFrame: (f) => seen.push(f.type) });
    rt.sendVoice({ type: 'voice_stop' });
    watcher.emit({ type: 'voice_audio_out', audio: 'AAAA' });
    watcher.emit({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
    expect(seen).toEqual(['voice_audio_out', 'voice_owner_active']);
    expect(rt.conv.voiceActive).toBe(true);
    // voice owner re-arms with voice_start instead of start on reconnect (T-11, V-8)
    watcher.drop();
    vi.advanceTimersByTime(1000);
    const again = FakeWebSocket.last(ORCH);
    again.open();
    expect(again.messages()).toEqual([{ type: 'voice_start', local_id: 'O1', reattach: true }]); // OPEN-2
  });

  it('OPEN-3: Archie closed elsewhere: its view closes (voice with it), the ref is cleared, nothing is sent', () => {
    startServices({ skipInitialSync: true });
    openSession({ kind: 'agent', localId: 'A1', focus: false });
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    const ws = FakeWebSocket.last(ORCH);
    subscribe(ws, 'O1');
    const voice = { startMessage: () => null, onFrame: vi.fn() };
    rt.setVoiceHooks(voice);
    ws.emit({ type: 'agent_session_opened', session_id: 'O1', sdk_session_id: 'O1', is_orchestrator: true });
    expect(getOrchestratorRef()).toEqual({ localId: 'O1', sdkId: 'O1' });
    ws.emit({ type: 'agent_session_closed', session_id: 'O1', is_orchestrator: true });
    expect(rt.isDisposed).toBe(true); // the voice registry disposes its controller with it
    expect(getArchieRuntime()).toBeUndefined();
    expect(getOrchestratorRef()).toBeNull();
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['A1']);
    expect(tabsStore.getState().activeId).toBe('A1');
    expect(ws.types()).toEqual(['start']);
    expect(closeCalls()).toEqual([]);
  });

  it('OPEN-4 on the orchestrator socket: every open re-reads the pool; the reopen reattaches, Archie gone meanwhile closes', async () => {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', '/api/sessions/pool/live', pool([{ local_id: 'O1', sdk_session_id: 'O1', is_orchestrator: true }]));
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    subscribe(FakeWebSocket.last(ORCH), 'O1', { jsonl_id: 'O1' });
    await flushPromises();
    expect(getArchieRuntime()).toBe(rt); // still in the pool
    h.fetch.on('GET', '/api/sessions/pool/live', pool([])); // closed on another device while this socket is down
    FakeWebSocket.last(ORCH).drop();
    vi.advanceTimersByTime(1000);
    const again = FakeWebSocket.last(ORCH);
    const reads = h.fetch.calls('GET', '/api/sessions/pool/live').length;
    again.open();
    expect(again.messages()).toEqual([{ type: 'start', local_id: 'O1', resume_sdk_id: 'O1', reattach: true }]);
    await flushPromises();
    expect(h.fetch.calls('GET', '/api/sessions/pool/live').length).toBe(reads + 1);
    expect(getArchieRuntime()).toBeUndefined();
    expect(tabsStore.getState().tabs).toEqual([]); // the empty workspace: "New Archie conversation"
  });

  it('openArchie attaches to the running orchestrator from pool/live (G-15); a different resume is a conflict', async () => {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', '/api/sessions/pool/live', pool([{ local_id: 'RUN', sdk_session_id: 'RUN', is_orchestrator: true }]));
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    const r = await openArchie({ focus: true });
    expect(r.conflict).toBe(false);
    expect(r.runtime?.localId).toBe('RUN');
    expect(tabsStore.getState().activeId).toBe('RUN');
    const c = await openArchie({ focus: true, resumeSdkId: 'PAST' });
    expect(c).toMatchObject({ conflict: true, running: { localId: 'RUN', sdkId: 'RUN' } });
    const ws = FakeWebSocket.last(ORCH);
    ws.open();
    expect(ws.messages()[0]).toEqual({ type: 'start', local_id: 'RUN', resume_sdk_id: 'RUN', reattach: true }); // opened from the pool
    const fresh = await replaceRunningArchie('PAST');
    expect(closeCalls()).toEqual(['/api/sessions/RUN/close']);
    expect(fresh.conv.ref.sdkId).toBe('PAST');
    expect(getArchieRuntime()).toBe(fresh);
  });

  it('inject is kept until session_started, then sent once (§6.15)', () => {
    startServices({ skipInitialSync: true });
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    rt.inject('[shared text]\nhello');
    const ws = FakeWebSocket.last(ORCH);
    ws.open();
    expect(ws.types()).toEqual(['start']);
    subscribe(ws, 'O1');
    expect(ws.messages()[1]).toEqual({ type: 'inject_text', text: '[shared text]\nhello' });
    rt.sendAudio('UklG', 'wav');
    rt.setModel('gpt-audio');
    rt.requestModels();
    rt.interrupt();
    rt.compact();
    expect(ws.types().slice(2)).toEqual(['send_audio', 'set_model', 'get_models', 'interrupt', 'compact']);
    ws.emit({ type: 'status', status: 'streaming' });
    ws.emit({ type: 'status', status: 'idle' });
    expect(ws.types().slice(-1)).toEqual(['get_model']); // §6.16 re-read the model after an audio turn
    ws.emit({ type: 'models_list', models: [{ model_id: 'gpt-audio' }] });
    expect(rt.handle.store.getState().models).toEqual([{ model_id: 'gpt-audio' }]);
  });

  it('agent approvals: answered over REST when the agent view is not open (§6.9)', async () => {
    h.fetch.on('POST', '/api/sessions/AG/permission', { ok: true });
    startServices({ skipInitialSync: true });
    const archie = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    subscribe(FakeWebSocket.last(ORCH), 'O1');
    FakeWebSocket.last(ORCH).emit({ type: 'nested_session_event', session_id: 'AG', event_type: 'permission_request', event_data: { type: 'permission_request', request_id: 'r1', tool_name: 'Bash', tool_input: {} } });
    expect(archie.conv.agentApprovals).toHaveLength(1);
    const chatSockets = FakeWebSocket.all(CHAT).length;
    await respondAgentPermission('AG', 'r1', 'allow');
    const posts = h.fetch.requests.filter((r) => r.method === 'POST' && r.path === '/api/sessions/AG/permission');
    expect(posts.map((r) => r.body)).toEqual([{ request_id: 'r1', decision: 'allow' }]);
    expect(FakeWebSocket.all(CHAT).length).toBe(chatSockets); // no transient socket
  });

  it('agent approvals: 409 (answered elsewhere) is success; a gone session rejects with a clear message', async () => {
    h.fetch.on('POST', '/api/sessions/AG/permission', () => jsonResponse({ detail: 'No pending permission request' }, 409));
    h.fetch.on('POST', '/api/sessions/GONE/permission', () => jsonResponse({ detail: "No live pool session with local_id='GONE'" }, 404));
    startServices({ skipInitialSync: true });
    await expect(respondAgentPermission('AG', 'r1', 'deny')).resolves.toBeUndefined();
    await expect(respondAgentPermission('GONE', 'r1', 'allow')).rejects.toThrow('no longer running');
  });

  it('agent approvals: a server without the REST route falls back to a transient chat socket (T-8)', async () => {
    startServices({ skipInitialSync: true });
    const archie = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    const ows = FakeWebSocket.last(ORCH);
    subscribe(ows, 'O1');
    ows.emit({ type: 'nested_session_event', session_id: 'AG', event_type: 'permission_request', event_data: { type: 'permission_request', request_id: 'r1', tool_name: 'ExitPlanMode', tool_input: {} } });
    expect(archie.conv.agentApprovals).toHaveLength(1);
    await respondAgentPermission('AG', 'r1', 'allow'); // unrouted → 404 "Not Found"
    const transient = FakeWebSocket.last(CHAT);
    transient.open();
    expect(transient.messages()).toEqual([{ type: 'permission_response', session_id: 'AG', request_id: 'r1', decision: 'allow' }]);
    vi.advanceTimersByTime(1000);
    expect(transient.closedByClient).toBe(true);
    // with the agent view open, its own socket is used and its turn end clears the approvals (PM-5)
    const ag = openSession({ kind: 'agent', localId: 'AG', focus: false }) as SessionRuntime;
    const aws = FakeWebSocket.last(CHAT);
    subscribe(aws, 'AG');
    ows.emit({ type: 'nested_session_event', session_id: 'AG', event_type: 'permission_request', event_data: { type: 'permission_request', request_id: 'r2', tool_name: 'ExitPlanMode', tool_input: {} } });
    expect(ag.conv.entries.length).toBe(1); // routed into the open agent view
    await respondAgentPermission('AG', 'r2', 'deny');
    expect(aws.messages()[1]).toEqual({ type: 'permission_response', session_id: 'AG', request_id: 'r2', decision: 'deny' });
    aws.emit({ type: 'status', status: 'processing' });
    aws.emit({ type: 'turn_complete', session_id: 'sdk-ag' });
    expect(archie.conv.agentApprovals).toEqual([]);
  });
});

describe('explicit actions', () => {
  it('delete closes every open view of the session and the pool entry BEFORE DELETE (§6.8, W-8)', async () => {
    h.fetch.on('GET', '/api/sessions/pool/live', pool([{ local_id: 'L-other', sdk_session_id: 'sdk-d' }]));
    h.fetch.on('DELETE', '/api/sessions/sdk-d', () => new Response(null, { status: 204 }));
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    openSession({ kind: 'agent', localId: 'V1', sdkId: 'sdk-d', focus: true });
    await deleteSession('sdk-d');
    const order = h.fetch.requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.path}`);
    expect(order).toEqual(['POST /api/sessions/V1/close', 'POST /api/sessions/L-other/close', 'DELETE /api/sessions/sdk-d']);
    expect(tabsStore.getState().tabs).toEqual([]);
  });

  it('delete surfaces the backend detail verbatim; 404 is tolerated', async () => {
    h.fetch.on('DELETE', '/api/sessions/gone', () => jsonResponse({ detail: "Session 'gone' not found" }, 404));
    await expect(deleteSession('gone')).resolves.toBeUndefined();
    h.fetch.on('DELETE', '/api/sessions/bad', () => jsonResponse({ detail: 'Trash directory is not writable' }, 500));
    await expect(deleteSession('bad')).rejects.toThrow('Trash directory is not writable');
  });

  it('rewind: close → truncate (409 retried) → reopen in place with a new local id (inv02 F-09)', async () => {
    const history = {
      messages: [
        { role: 'user', text: 'one', blocks: [{ type: 'text', text: 'one' }] },
        { role: 'assistant', text: 'a1', blocks: [{ type: 'text', text: 'a1' }] },
        { role: 'user', text: 'two', blocks: [{ type: 'text', text: 'two' }] },
        { role: 'assistant', text: 'a2', blocks: [{ type: 'text', text: 'a2' }] },
      ],
      total_count: 4,
      has_more: false,
      start_index: 0,
    };
    h.fetch.on('GET', /^\/api\/sessions\/sdk-r\/messages/, history);
    let truncates = 0;
    h.fetch.on('POST', '/api/sessions/sdk-r/truncate', () =>
      ++truncates < 2 ? jsonResponse({ detail: 'Session is currently open. Close the tab before rewinding.' }, 409) : jsonResponse({ session_id: 'sdk-r' }),
    );
    const rt = openSession({ kind: 'agent', localId: 'R1', sdkId: 'sdk-r', focus: true }) as SessionRuntime;
    subscribe(FakeWebSocket.last(CHAT), 'R1');
    await flushPromises();
    const first = rt.conv.entries[0];
    expect(first?.kind).toBe('user');
    const p = rewindSession('R1', (first as { id: string }).id);
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    const next = await p;
    const writes = h.fetch.requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.path} ${JSON.stringify(r.body ?? null)}`);
    expect(writes).toEqual([
      'POST /api/sessions/R1/close null',
      'POST /api/sessions/sdk-r/truncate {"drop_last_n":3}',
      'POST /api/sessions/sdk-r/truncate {"drop_last_n":3}',
    ]);
    expect(next.localId).not.toBe('R1');
    expect(next.conv.ref.sdkId).toBe('sdk-r');
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual([next.localId]);
    expect(tabsStore.getState().activeId).toBe(next.localId);
  });

  it('rewind refuses while busy or before the first reply; fork opens the copy focused', async () => {
    const rt = openSession({ kind: 'agent', localId: 'N1', focus: true }) as SessionRuntime;
    await expect(rewindSession('N1', 'x')).rejects.toBeInstanceOf(SessionActionError);
    h.fetch.on('GET', /^\/api\/sessions\/sdk-f\/messages/, {
      messages: [
        { role: 'user', text: 'q', blocks: [{ type: 'text', text: 'q' }] },
        { role: 'assistant', text: 'a', blocks: [{ type: 'text', text: 'a' }] },
      ],
      total_count: 2,
      has_more: false,
      start_index: 0,
    });
    h.fetch.on('POST', '/api/sessions/sdk-f/fork', () => jsonResponse({ session_id: 'sdk-copy' }, 201));
    const src = openSession({ kind: 'agent', localId: 'F1', sdkId: 'sdk-f', focus: true }) as SessionRuntime;
    subscribe(FakeWebSocket.last(CHAT), 'F1');
    await flushPromises();
    const last = src.conv.entries[src.conv.entries.length - 1] as { id: string };
    const forked = await forkSession('F1', last.id);
    expect(h.fetch.calls('POST', '/api/sessions/sdk-f/fork')[0]?.body).toEqual({ drop_last_n: 0 });
    expect(forked.conv.ref.sdkId).toBe('sdk-copy');
    expect(tabsStore.getState().activeId).toBe(forked.localId);
    src.send('working');
    await expect(rewindSession('F1', last.id)).rejects.toThrow('Stop the current reply first');
    expect(rt.isDisposed).toBe(false);
  });

  it('a terminated session closes with its reason in the notice (§6.13, OPEN-3)', () => {
    startServices({ skipInitialSync: true });
    const watcher = FakeWebSocket.last(ORCH);
    watcher.open();
    openSession({ kind: 'agent', localId: 'T1', focus: true });
    const ws = FakeWebSocket.last(CHAT);
    subscribe(ws, 'T1');
    ws.emit({ type: 'session_terminated', reason: 'subprocess_crashed', detail: 'exit code 1', sdk_session_id: 'sdk-t' });
    ws.emit({ type: 'session_stopped' });
    watcher.emit({ type: 'agent_session_closed', session_id: 'T1', is_orchestrator: false });
    expect(getSessionRuntime('T1')).toBeUndefined();
    expect(snackbarStore.getState().queue.map((q) => q.message).pop()).toBe('New agent session crashed: exit code 1');
    expect(closeCalls()).toEqual([]);
  });
});

describe('provider of a past session', () => {
  it('opening a history row takes its harness from the catalog, whatever the id (codex, modelstudio, new ones)', () => {
    setCatalogItems('sessions', [
      { session_id: 'sdk-c', started_at: '', last_activity: '', title: 'Codex job', message_count: 2, is_orchestrator: false, provider: 'codex', local_id: null },
      { session_id: 'sdk-n', started_at: '', last_activity: '', title: 'New harness', message_count: 2, is_orchestrator: false, provider: 'aider', local_id: null },
    ]);
    const c = openSession({ kind: 'agent', sdkId: 'sdk-c', focus: true }) as SessionRuntime;
    expect(c.conv.ref.provider).toBe('codex');
    expect(tabsStore.getState().tabs.find((t) => t.id === c.localId)?.provider).toBe('codex');
    const n = openSession({ kind: 'agent', sdkId: 'sdk-n', focus: false }) as SessionRuntime;
    expect(n.conv.ref.provider).toBe('aider');
    // unknown session: Claude by default for the runtime, no provider tag
    const u = openSession({ kind: 'agent', sdkId: 'sdk-unknown', focus: false }) as SessionRuntime;
    expect(u.conv.ref.provider).toBe('claude');
    expect(tabsStore.getState().tabs.find((t) => t.id === u.localId)?.provider).toBeNull();
  });
});

describe('startServices', () => {
  it('initial sync, probes, visibility resync with pool status (ST-2), hidden flags follow the active tab', async () => {
    h.fetch.on('GET', '/api/sessions', [{ session_id: 'sdk-q', title: 'Qwen work', provider: 'qwen', local_id: 'Q1' }]);
    h.fetch.on('GET', '/api/sessions/pool/live', pool([{ local_id: 'Q1', sdk_session_id: 'sdk-q', status: 'idle' }]));
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    h.fetch.on('GET', '/api/visualizations/cast', { available: true });
    startServices();
    startServices(); // idempotent
    await flushPromises();
    const q = getSessionRuntime('Q1') as SessionRuntime;
    expect(q.conv.ref.provider).toBe('qwen');
    expect(tabsStore.getState().activeId).toBeNull(); // nothing focused by a sync
    const mine = openSession({ kind: 'agent', localId: 'M', focus: true }) as SessionRuntime;
    expect(q.handle.store.getState().hidden).toBe(true);
    activateTab('Q1');
    expect(q.handle.store.getState().hidden).toBe(false);
    expect(mine.handle.store.getState().hidden).toBe(true);
    const ws = FakeWebSocket.all(CHAT)[0] as FakeWebSocket;
    subscribe(ws, 'Q1');
    await flushPromises();
    q.send('long job');
    ws.emit({ type: 'status', status: 'processing' });
    expect(q.conv.inTurn).toBe(true);
    // background, the turn ends meanwhile; on visible: pool sync + start re-sent; idle ends the turn (ST-2)
    h.visibility.set(true);
    h.visibility.set(false);
    expect(ws.types().filter((t) => t === 'start')).toHaveLength(2);
    ws.emit({ type: 'session_started', session_id: 'Q1' });
    await flushPromises();
    expect(q.conv.inTurn).toBe(false);
    expect(h.fetch.calls('GET', '/api/visualizations/cast').length).toBeGreaterThanOrEqual(2);
  });
});

describe('§6.11a orchestrator_switch (agent-initiated switch)', () => {
  const SWITCH = { type: 'orchestrator_switch', sdk_session_id: 'PAST', title: 'Trip planning', from_session_id: 'O1' };

  function liveArchie(): { rt: ArchieRuntime; ws: FakeWebSocket } {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    const ws = FakeWebSocket.last(ORCH);
    subscribe(ws, 'O1');
    return { rt, ws };
  }

  /** What the server sends after `switch_conversation` (voice part included). */
  function serverSwitch(ws: FakeWebSocket, voice: boolean): void {
    if (voice) {
      ws.emit({ type: 'voice_ending', reason: 'switch', session_id: 'O1' });
      ws.emit({ type: 'voice_ended', reason: 'switch', session_id: 'O1' });
    }
    ws.emit({ type: 'agent_session_closed', session_id: 'O1', is_orchestrator: true }); // OPEN-3
    ws.emit({ ...SWITCH, voice });
  }

  afterEach(() => {
    setSwitchVoiceHandler(null);
  });

  it('SW-1: handled at the channel level with the old view attached: old view dropped locally, past conversation resumed focused', () => {
    const { rt, ws } = liveArchie();
    openSession({ kind: 'agent', localId: 'A1', focus: true });
    const voice = vi.fn();
    setSwitchVoiceHandler(voice);
    serverSwitch(ws, false);
    expect(rt.isDisposed).toBe(true);
    expect(getSessionRuntime('O1')).toBeUndefined();
    const fresh = getArchieRuntime() as ArchieRuntime;
    expect(fresh.localId).not.toBe('O1');
    expect(fresh.conv.ref.sdkId).toBe('PAST');
    const tabs = tabsStore.getState();
    expect(tabs.tabs.map((t) => t.id)).toEqual([fresh.localId, 'A1']);
    expect(tabs.activeId).toBe(fresh.localId);
    // same socket (T-6); no conflict dialog, no REST close: the server already stopped it
    expect(FakeWebSocket.all(ORCH)).toHaveLength(1);
    expect(ws.messages().slice(-1)).toEqual([{ type: 'start', local_id: fresh.localId, resume_sdk_id: 'PAST' }]);
    expect(closeCalls()).toEqual([]);
    expect(getOrchestratorRef()).toBeNull();
    expect(voice).not.toHaveBeenCalled(); // voice:false
    expect(snackbarStore.getState().queue.map((s) => s.message)).toEqual(['Switched to Trip planning']);
  });

  it('SW-2: voice:true hands the resumed view to the voice handler (which tells the user)', () => {
    const { ws } = liveArchie();
    const voice = vi.fn();
    setSwitchVoiceHandler(voice);
    serverSwitch(ws, true);
    const fresh = getArchieRuntime() as ArchieRuntime;
    expect(voice).toHaveBeenCalledWith(fresh.localId, 'Trip planning');
    expect(snackbarStore.getState().queue).toEqual([]);
  });

  it('SW-1: acts at most once per (sdk_session_id, from_session_id)', () => {
    const { ws } = liveArchie();
    const voice = vi.fn();
    setSwitchVoiceHandler(voice);
    serverSwitch(ws, true);
    const fresh = getArchieRuntime() as ArchieRuntime;
    ws.emit({ ...SWITCH, voice: true });
    expect(getArchieRuntime()).toBe(fresh);
    expect(voice).toHaveBeenCalledTimes(1);
    expect(ws.types().filter((t) => t === 'start')).toHaveLength(2);
  });

  it('a read-only view of the target conversation is replaced by the live one', () => {
    const { ws } = liveArchie();
    openSession({ kind: 'archie', localId: 'R1', sdkId: 'PAST', focus: false, readOnly: true });
    serverSwitch(ws, false);
    const fresh = getArchieRuntime() as ArchieRuntime;
    expect(getSessionRuntime('R1')).toBeUndefined();
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual([fresh.localId]);
  });
});

describe('§3.7 agent turn watcher events (device notifications)', () => {
  const FINISHED = { type: 'agent_turn_finished', session_id: 'A1', sdk_session_id: 'S1', provider: 'claude', title: 'Energy', status: 'ok', preview: 'Done', error: null };

  it('reach onAgentTurn from the passive watcher socket and never open or focus a view (FOCUS-1)', () => {
    startServices({ skipInitialSync: true });
    const seen: string[] = [];
    const off = onAgentTurn((f) => seen.push(`${f.type}:${f.session_id}`));
    const ws = FakeWebSocket.last(ORCH);
    ws.open();
    ws.emit({ type: 'agent_turn_started', session_id: 'A1', sdk_session_id: 'S1', provider: 'claude' });
    ws.emit(FINISHED);
    off();
    ws.emit(FINISHED);
    expect(seen).toEqual(['agent_turn_started:A1', 'agent_turn_finished:A1']);
    expect(getSessionRuntime('A1')).toBeUndefined();
    expect(tabsStore.getState().activeId).toBeNull();
  });

  it('reach onAgentTurn with Archie attached, without touching its conversation', () => {
    startServices({ skipInitialSync: true });
    h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    const ws = FakeWebSocket.last(ORCH);
    subscribe(ws, 'O1');
    const before = rt.conv.entries.length;
    const seen: string[] = [];
    const off = onAgentTurn((f) => seen.push(f.type));
    ws.emit(FINISHED);
    off();
    expect(seen).toEqual(['agent_turn_finished']);
    expect(rt.conv.entries.length).toBe(before);
  });
});
