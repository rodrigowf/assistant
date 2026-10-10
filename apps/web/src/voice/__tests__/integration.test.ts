/**
 * The voice controller on a real ArchieRuntime (W-06) over the fake orchestrator socket: the
 * shared voice fixtures (spec 12 §11) run end to end with the controller attached through
 * `VoiceHooks`, so the conversation and the controller are checked together.
 *
 * - W-10: voice start sends voice_start on the single socket, never `stop`.
 * - W-3: voice tool calls complete in the transcript (and a tool still running at the end
 *   becomes no_result).
 * - Gemini transcript coalescing and empty-text turnComplete finalisation.
 * - W-1: the passive viewer fixture's `expected.controller` (own state off, remoteActive true
 *   then false, input enabled).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Conversation } from '@/protocol';
import { ArchieRuntime, openSession, startServices } from '@/services';
import { FakeWebSocket, setupServices, teardownServices } from '../../services/__tests__/fakes';
import { VoiceController } from '../core/VoiceController';
import type { ConnectionType, TransportOptions } from '../core/types';
import { portFor } from '../registry';
import { FakeCues, FakeTransport, RELAY_INFO } from './harness';

const ORCH = '/api/orchestrator/chat';
const FIXTURES = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../protocol-fixtures');

interface Fixture {
  events: Record<string, unknown>[];
  expected: {
    entries: Record<string, unknown>[];
    state: Record<string, unknown>;
    controller?: { voice_state: string; remote_active: boolean; input_enabled: boolean };
  };
}
const fixture = (name: string): Fixture => JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8')) as Fixture;

beforeEach(() => {
  vi.useFakeTimers();
  setupServices();
});
afterEach(() => {
  teardownServices();
  vi.useRealTimers();
});

function setup(): { rt: ArchieRuntime; ws: FakeWebSocket; c: VoiceController; transports: FakeTransport[] } {
  startServices({ skipInitialSync: true });
  const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
  const transports: FakeTransport[] = [];
  const c = new VoiceController({
    port: portFor(rt),
    cues: new FakeCues(),
    createTransport: (kind: ConnectionType, opts: TransportOptions) => {
      const t = new FakeTransport(kind, opts);
      transports.push(t);
      return t;
    },
  });
  rt.setVoiceHooks({ startMessage: () => c.startMessage(), onFrame: (f) => c.onFrame(f), onSocketClosed: () => c.onSocketClosed() });
  const ws = FakeWebSocket.last(ORCH);
  ws.open();
  ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
  return { rt, ws, c, transports };
}

/** Owner start on a WS-relay provider, through to `ready`. */
function ownerLive(s: ReturnType<typeof setup>): FakeTransport {
  s.c.start();
  s.ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1', voice: true, voice_initiator: true, voice_provider: 'google', voice_connection_info: RELAY_INFO });
  s.ws.emit({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
  const t = s.transports[s.transports.length - 1] as FakeTransport;
  t.ready();
  s.ws.emit({ type: 'voice_event', event: { type: 'voice_status', status: 'ready' } });
  return t;
}

function shape(conv: Conversation): Record<string, unknown>[] {
  return conv.entries.map((e) => {
    if (e.kind === 'user') return { kind: 'user', text: e.text, origin: e.origin, state: e.state };
    if (e.kind === 'assistant')
      return {
        kind: 'assistant',
        blocks: e.blocks.map((b) =>
          b.type === 'tool'
            ? { type: 'tool', tool_use_id: b.tool_use_id, tool_name: b.tool_name, tool_input: b.tool_input, status: b.status, output: b.output }
            : { type: b.type, text: 'text' in b ? b.text : undefined, streaming: 'streaming' in b ? b.streaming : undefined },
        ),
      };
    return { kind: e.kind };
  });
}

describe('voice on the Archie runtime', () => {
  it('W-10 / P-1: starting voice sends voice_start on the single orchestrator socket and never `stop`', () => {
    const s = setup();
    s.c.start();
    expect(s.ws.types()).toEqual(['start', 'voice_start']);
    expect(s.ws.messages()[1]).toEqual({ type: 'voice_start', local_id: 'O1', resume_sdk_id: 'O1', reattach: true }); // subscribed: it exists (OPEN-2)
    expect(FakeWebSocket.all(ORCH)).toHaveLength(1);
  });

  it('Gemini coalescing + empty-text turnComplete finalisation (fixture voice_transcript_coalescing_gemini)', () => {
    const s = setup();
    ownerLive(s);
    const fx = fixture('voice_transcript_coalescing_gemini');
    for (const ev of fx.events) s.ws.emit(ev);
    expect(shape(s.rt.conv)).toEqual(fx.expected.entries);
    expect(s.c.snapshot.status).toBe('active'); // turnComplete → Listening
  });

  it('W-3: voice tool calls complete in the transcript; one still running at the end becomes no_result (fixture voice_mode_tool_result_gemini)', () => {
    const s = setup();
    const t = ownerLive(s);
    const fx = fixture('voice_mode_tool_result_gemini');
    for (const ev of fx.events) s.ws.emit(ev);
    expect(shape(s.rt.conv)).toEqual(fx.expected.entries);
    expect(s.rt.conv.voiceActive).toBe(false);
    expect(s.c.snapshot.status).toBe('off');
    expect(t.closed).toBe(true);
  });

  it('a local end (5 s timeout) also finishes running voice tools (VT-3)', () => {
    const s = setup();
    ownerLive(s);
    s.ws.emit({ type: 'tool_use', tool_use_id: 't1', tool_name: 'run_script', tool_input: {} });
    s.c.stop();
    vi.advanceTimersByTime(5_000);
    const blocks = s.rt.conv.entries.flatMap((e) => (e.kind === 'assistant' ? e.blocks : []));
    expect(blocks.find((b) => b.type === 'tool')).toMatchObject({ status: 'no_result' });
    expect(s.rt.conv.voiceActive).toBe(false);
  });

  it('W-1: passive viewer fixture — transcripts mirror, own state stays off, remoteActive true then false, input enabled', () => {
    const s = setup();
    const fx = fixture('voice_passive_viewer_no_wedge');
    const seen: boolean[] = [];
    for (const ev of fx.events) {
      s.ws.emit(ev);
      seen.push(s.c.snapshot.remoteActive);
      expect(s.c.snapshot.status).toBe('off'); // never wedged into a voice state
    }
    expect(seen).toContain(true);
    expect(shape(s.rt.conv)).toEqual(fx.expected.entries);
    const exp = fx.expected.controller;
    expect(exp).toBeDefined();
    expect(s.c.snapshot.status).toBe(exp?.voice_state);
    expect(s.c.snapshot.remoteActive).toBe(exp?.remote_active);
    // input_enabled: the dock is not shown (status off), so the composer stays
    expect(s.c.snapshot.status === 'off').toBe(exp?.input_enabled);
    // nothing voice-related was ever sent from the passive device
    expect(s.ws.types().filter((t) => t.startsWith('voice_'))).toEqual([]);
  });

  it('P-2 on the runtime: a dropped socket reconnects and re-arms with voice_start (V-8), never a plain start', () => {
    const s = setup();
    ownerLive(s);
    s.ws.drop();
    expect(s.c.snapshot.link).toBe('lost');
    vi.advanceTimersByTime(1_000);
    const again = FakeWebSocket.last(ORCH);
    again.open();
    expect(again.messages()).toEqual([{ type: 'voice_start', local_id: 'O1', resume_sdk_id: 'O1', reattach: true }]); // OPEN-2
  });
});
