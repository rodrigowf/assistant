/**
 * VoiceController: one named test per load-bearing rule (inv02 §7 #11), the fixed bugs
 * (W-1 passive wedge, W-3 voice tool cards, W-4 VAD counter, W-10 stop-before-start) and the
 * spec 12 §7 signaling rules. P-2 reconnect behaviour is in reconnect.test.ts.
 */
import { describe, expect, it } from 'vitest';
import { CONNECTION_INFO_TIMEOUT_MS, ENDING_TIMEOUT_MS } from '../core/VoiceController';
import { RELAY_INFO, SESSION_UPDATE, WEBRTC_INFO, flushPromises, rig } from './harness';

describe('owner start (spec 12 §7.3)', () => {
  it('W-10 / P-1: start sends voice_start on the shared socket, never `stop` first, no voice_* fields (V-1)', () => {
    const r = rig();
    r.c.start();
    expect(r.port.sent).toEqual([{ type: 'voice_start', local_id: 'O1', resume_sdk_id: 'O1' }]);
    expect(r.port.types()).not.toContain('stop');
    expect(r.c.snapshot).toMatchObject({ status: 'connecting', phase: 'starting', remoteActive: false });
  });

  it('start is allowed only from off and error', () => {
    const r = rig();
    r.c.start();
    r.c.start();
    expect(r.port.startRequests).toBe(1);
  });

  it('voice_status summarizing shows the preparing phase', () => {
    const r = rig();
    r.c.start();
    r.frame({ type: 'voice_event', event: { type: 'voice_status', status: 'summarizing' } });
    expect(r.c.snapshot.phase).toBe('summarizing');
  });

  it('LOAD-BEARING: waits up to 30 s for session_started (the summariser takes 10–20 s), then errors', () => {
    const r = rig();
    r.c.start();
    r.clock.advance(CONNECTION_INFO_TIMEOUT_MS - 1);
    expect(r.c.snapshot.status).toBe('connecting');
    r.clock.advance(1);
    expect(r.c.snapshot.status).toBe('error');
    expect(r.c.snapshot.error?.message).toMatch(/did not start/);
    expect(r.port.types()).toContain('voice_stop');
    expect(r.port.localEnds).toBe(1);
  });

  it('a session_started at 20 s is in time', () => {
    const r = rig();
    r.c.start();
    r.clock.advance(20_000);
    r.answer();
    r.t().ready();
    expect(r.c.snapshot.status).toBe('active');
    r.clock.advance(60_000);
    expect(r.c.snapshot.status).toBe('active');
  });

  it('voice_connection_error in session_started is an error state showing it', () => {
    const r = rig();
    r.c.start();
    r.started({ voice_connection_error: 'OPENAI_API_KEY not configured' });
    expect(r.c.snapshot.status).toBe('error');
    expect(r.c.snapshot.error?.message).toBe('OPENAI_API_KEY not configured');
    expect(r.transports).toHaveLength(0);
  });

  it('a top-level voice_connection_error frame while connecting is an error', () => {
    const r = rig();
    r.c.start();
    r.frame({ type: 'voice_connection_error', detail: 'token mint failed' });
    expect(r.c.snapshot).toMatchObject({ status: 'error', error: { message: 'token mint failed' } });
  });

  it('a start error frame (orchestrator_stopping) fails the start with the verbatim detail', () => {
    const r = rig();
    r.c.start();
    r.frame({ type: 'error', error: 'orchestrator_stopping', detail: 'Previous voice session is still ending. Please retry.' });
    expect(r.c.snapshot.error?.message).toBe('Previous voice session is still ending. Please retry.');
  });

  it('V-3 fallback: no connection info → fetch it, then open the transport', async () => {
    let asked = 0;
    const r = rig({
      fetchConnectionInfo: () => {
        asked += 1;
        return Promise.resolve(WEBRTC_INFO);
      },
    });
    r.c.start();
    r.started();
    await flushPromises();
    expect(asked).toBe(1);
    expect(r.transports).toHaveLength(1);
    expect(r.t().opts.info.token).toBe('ek_test');
  });

  it('no connection info and no fallback → error', () => {
    const r = rig();
    r.c.start();
    r.started();
    expect(r.c.snapshot.status).toBe('error');
  });

  it('spec 13 §2.5 / P-8: an unsupported transport (relay on compat) stops voice and explains', () => {
    const r = rig({ unsupported: (k) => (k === 'websocket' ? 'Qwen and Gemini voice need a newer browser.' : null) });
    r.c.start();
    r.answer(RELAY_INFO, { voice_provider: 'qwen' });
    expect(r.transports).toHaveLength(0);
    expect(r.port.types()).toContain('voice_stop');
    expect(r.c.snapshot).toMatchObject({ status: 'error', error: { message: 'Qwen and Gemini voice need a newer browser.' } });
    expect(r.c.snapshot.error?.hint).toMatch(/Settings/);
  });

  it('transport setup failure before the call is established is an error (mic denied)', async () => {
    const r = rig();
    r.c.start();
    r.started({ voice_connection_info: WEBRTC_INFO });
    const t = r.t();
    t.events.failed('Microphone access was denied.');
    await flushPromises();
    expect(r.c.snapshot).toMatchObject({ status: 'error', error: { message: 'Microphone access was denied.' } });
    expect(t.closed).toBe(true);
    expect(r.cues.log).toEqual([]); // not a P-2 drop: the call never came up
  });
});

describe('V-4 command queue and V-5 session.update', () => {
  it('session.update is the first provider frame, before earlier voice_commands; flushed in order on dc open', () => {
    const r = rig();
    r.c.start();
    r.started({ voice_connection_info: WEBRTC_INFO });
    // a command arrives before the session_update is known… then session_started brings it
    r.frame({ type: 'voice_command', command: { type: 'response.create' } });
    const t = r.t();
    expect(t.sentEvents).toEqual([]);
    expect(r.c.queuedFrames).toEqual([{ type: 'response.create' }]);
    t.ready();
    expect(t.sentEvents).toEqual([{ type: 'response.create' }]);
    // with a session_update: it goes first
    const r2 = rig();
    r2.c.start();
    r2.answer();
    r2.frame({ type: 'voice_command', command: { type: 'conversation.item.create', n: 1 } });
    r2.frame({ type: 'voice_command', command: { type: 'response.create', n: 2 } });
    r2.t().ready();
    expect(r2.t().sentEvents).toEqual([SESSION_UPDATE, { type: 'conversation.item.create', n: 1 }, { type: 'response.create', n: 2 }]);
  });

  it('V-4 never drops: 2 000 queued commands all arrive, in FIFO order', () => {
    const r = rig();
    r.c.start();
    r.answer();
    for (let i = 0; i < 2000; i++) r.frame({ type: 'voice_command', command: { type: 'x', i } });
    expect(r.c.queuedFrames).toHaveLength(2001);
    r.t().ready();
    const sent = r.t().sentEvents;
    expect(sent).toHaveLength(2001);
    expect(sent[0]).toEqual(SESSION_UPDATE);
    expect(sent.slice(1).map((e) => e.i)).toEqual(Array.from({ length: 2000 }, (_, i) => i));
    expect(r.c.queuedFrames).toHaveLength(0);
  });

  it('after ready, commands go straight to the data channel', () => {
    const r = rig();
    const t = r.live();
    r.frame({ type: 'voice_command', command: { type: 'response.create' } });
    expect(t.sentEvents[t.sentEvents.length - 1]).toEqual({ type: 'response.create' });
    expect(r.c.queuedFrames).toHaveLength(0);
  });

  it('V-5: session.update is not forwarded for WS providers (the backend sent it upstream)', () => {
    const r = rig();
    r.live('websocket');
    expect(r.port.voiceEvents()).not.toContainEqual(SESSION_UPDATE);
  });

  it('V-5: a WS provider ignores voice_command (the backend executes it)', () => {
    const r = rig();
    r.live('websocket');
    r.port.clear();
    r.frame({ type: 'voice_command', command: { type: 'response.create' } });
    expect(r.port.sent).toEqual([]);
  });

  it('the queue is cleared on teardown', () => {
    const r = rig();
    r.c.start();
    r.answer();
    r.frame({ type: 'voice_command', command: { type: 'x' } });
    r.c.stop();
    r.frame({ type: 'voice_ended', reason: 'user_stop' });
    expect(r.c.queuedFrames).toHaveLength(0);
  });
});

describe('WebRTC data channel mirroring (§7.3)', () => {
  it('inbound events are mirrored as voice_event and fed to the reducer; outbound ones are mirrored', () => {
    const r = rig();
    const t = r.live();
    r.port.clear();
    t.events.providerEvent({ type: 'response.created' });
    expect(r.port.sent).toEqual([{ type: 'voice_event', event: { type: 'response.created' } }]);
    expect(r.port.fed).toEqual([{ type: 'response.created' }]);
    r.port.clear();
    r.frame({ type: 'voice_command', command: { type: 'response.create' } });
    expect(r.port.sent).toEqual([{ type: 'voice_event', event: { type: 'response.create' } }]);
  });
});

describe('WS relay readiness (§7.3)', () => {
  it('mic chunks are sent only after voice_status ready; queued control frames flush as voice_event', () => {
    const r = rig();
    r.c.start();
    r.answer(RELAY_INFO, { voice_provider: 'qwen' });
    const t = r.t();
    expect(r.c.snapshot.phase).toBe('preparing');
    t.ready();
    r.port.clear();
    t.events.audioChunk('AAAA');
    expect(r.port.sent).toEqual([]);
    expect(r.c.snapshot.status).toBe('connecting');
    r.frame({ type: 'voice_event', event: { type: 'voice_status', status: 'ready' } });
    expect(r.c.snapshot.status).toBe('active');
    t.events.audioChunk('BBBB');
    expect(r.port.sent).toEqual([{ type: 'voice_audio_in', audio: 'BBBB' }]);
  });

  it('voice_audio_out plays on the owner and marks speaking', () => {
    const r = rig();
    const t = r.live('websocket');
    r.frame({ type: 'voice_audio_out', audio: 'UENN' });
    expect(t.played).toEqual(['UENN']);
    expect(r.c.snapshot.status).toBe('speaking');
  });
});

describe('barge-in (§7.4)', () => {
  it('LOAD-BEARING V-9: WS speech_started flushes playback and cancels only while a response is in flight', () => {
    const r = rig();
    const t = r.live('websocket');
    r.port.clear();
    r.frame({ type: 'voice_event', event: { type: 'input_audio_buffer.speech_started' } });
    expect(t.flushes).toBe(1);
    expect(r.port.voiceEvents()).toEqual([]); // no stray cancel: DashScope would close the socket
    r.frame({ type: 'voice_event', event: { type: 'response.created' } });
    r.frame({ type: 'voice_event', event: { type: 'input_audio_buffer.speech_started' } });
    expect(t.flushes).toBe(2);
    expect(r.port.voiceEvents()).toEqual([{ type: 'response.cancel' }]);
    r.frame({ type: 'voice_event', event: { type: 'input_audio_buffer.speech_started' } });
    expect(r.port.voiceEvents()).toEqual([{ type: 'response.cancel' }]); // one cancel per response
    r.frame({ type: 'voice_event', event: { type: 'response.done' } });
    r.frame({ type: 'voice_event', event: { type: 'input_audio_buffer.speech_started' } });
    expect(r.port.voiceEvents()).toEqual([{ type: 'response.cancel' }]);
  });

  it('V-10 Gemini: interrupted flushes playback, sends no cancel', () => {
    const r = rig();
    const t = r.live('websocket');
    r.port.clear();
    r.frame({ type: 'voice_event', event: { serverContent: { interrupted: true } } });
    expect(t.flushes).toBe(1);
    expect(r.port.sent).toEqual([]);
  });

  it('V-11 WebRTC: speech_started sends nothing (the provider handles it)', () => {
    const r = rig();
    const t = r.live();
    t.events.providerEvent({ type: 'response.created' });
    const before = t.sentEvents.length;
    t.events.providerEvent({ type: 'input_audio_buffer.speech_started' });
    expect(t.sentEvents.length).toBe(before);
  });

  it('tapping the orb interrupts only while a response is in flight', () => {
    const r = rig();
    const t = r.live();
    r.c.interrupt();
    expect(t.sentEvents.map((e) => e.type)).toEqual(['session.update']);
    t.events.providerEvent({ type: 'response.created' });
    r.c.interrupt();
    expect(t.sentEvents.map((e) => e.type)).toEqual(['session.update', 'response.cancel', 'output_audio_buffer.clear']);
  });
});

describe('provider events → own status', () => {
  it('maps the response lifecycle to Speaking / Using tools / Thinking / Listening', () => {
    const r = rig();
    const t = r.live();
    const ev = (e: Record<string, unknown>): void => t.events.providerEvent(e);
    ev({ type: 'response.created' });
    expect(r.c.snapshot.status).toBe('speaking');
    ev({ type: 'response.output_item.added', item: { type: 'function_call' } });
    expect(r.c.snapshot.status).toBe('tool_use');
    ev({ type: 'response.function_call_arguments.done', call_id: 'c1', name: 'run_script', arguments: '{}' });
    expect(r.c.snapshot.status).toBe('thinking');
    ev({ type: 'response.done' });
    expect(r.c.snapshot.status).toBe('active');
    ev({ type: 'input_audio_buffer.speech_stopped' });
    expect(r.c.snapshot.status).toBe('thinking');
    ev({ type: 'input_audio_buffer.speech_started' });
    expect(r.c.snapshot.status).toBe('active');
  });

  it('W-3: voice tool calls never create tool cards from provider events (the controller emits none)', () => {
    const r = rig();
    const t = r.live();
    r.port.fed = [];
    t.events.providerEvent({ type: 'response.function_call_arguments.done', call_id: 'c1', name: 'x', arguments: '{}' });
    // the event only reaches the reducer, which ignores it (§4.7); tool cards come from tool_use/tool_result frames
    expect(r.port.fed).toEqual([{ type: 'response.function_call_arguments.done', call_id: 'c1', name: 'x', arguments: '{}' }]);
  });

  it('Gemini: turnComplete returns to Listening; outputTranscription is Speaking; toolCall is Using tools', () => {
    const r = rig();
    r.live('websocket');
    r.frame({ type: 'voice_event', event: { serverContent: { outputTranscription: { text: 'Hi' } } } });
    expect(r.c.snapshot.status).toBe('speaking');
    r.frame({ type: 'voice_event', event: { toolCall: { functionCalls: [{ id: 'f', name: 'x', args: {} }] } } });
    expect(r.c.snapshot.status).toBe('tool_use');
    r.frame({ type: 'voice_event', event: { serverContent: { turnComplete: true } } });
    expect(r.c.snapshot.status).toBe('active');
  });

  it('W-4: voice_vad_state is kept for the dock counter', () => {
    const r = rig();
    r.live('websocket');
    r.clock.advance(1000);
    r.frame({ type: 'voice_event', event: { type: 'voice_vad_state', state: 'listening', duration_ms: 4200, silero_prob: 0.9 } });
    expect(r.c.snapshot.vad).toEqual({ state: 'listening', durationMs: 4200, at: 1000 });
  });

  it('reconnect_warning shows the "Pausing in ~Ns" banner', () => {
    const r = rig();
    r.live('websocket');
    r.frame({ type: 'voice_event', event: { type: 'voice_status', status: 'reconnect_warning', time_left: 30 } });
    expect(r.c.snapshot.banner).toEqual({ kind: 'reconnect_warning', timeLeftS: 30 });
    r.frame({ type: 'voice_event', event: { type: 'voice_status', status: 'ready' } });
    expect(r.c.snapshot.banner).toBeNull();
  });

  it('soft errors (voice_audio_failed) are a banner; voice keeps going', () => {
    const r = rig();
    r.live('websocket');
    r.frame({ type: 'error', error: 'voice_audio_failed', detail: 'relay hiccup' });
    expect(r.c.snapshot).toMatchObject({ status: 'active', banner: { kind: 'warning', message: 'relay hiccup' } });
  });
});

describe('ending (§7.6) and errors (§7.7)', () => {
  it('End: voice_stop, Ending…, then voice_ended closes the transport → off', () => {
    const r = rig();
    const t = r.live();
    r.c.stop();
    expect(r.port.types()).toContain('voice_stop');
    expect(r.c.snapshot.status).toBe('ending');
    r.frame({ type: 'voice_ending', reason: 'user_stop', session_id: 'O1' });
    r.frame({ type: 'voice_ended', reason: 'user_stop', session_id: 'O1' });
    expect(r.c.snapshot.status).toBe('off');
    expect(t.closed).toBe(true);
    expect(r.port.localEnds).toBe(0); // the server's voice_ended reaches the reducer itself
  });

  it('LOAD-BEARING: the 5 s ending timeout forces off and delivers voice_local_end', () => {
    const r = rig();
    const t = r.live();
    r.c.stop();
    r.clock.advance(ENDING_TIMEOUT_MS - 1);
    expect(r.c.snapshot.status).toBe('ending');
    r.clock.advance(1);
    expect(r.c.snapshot.status).toBe('off');
    expect(t.closed).toBe(true);
    expect(r.port.localEnds).toBe(1);
  });

  it('agent_end: the server-side voice_ending plays the closing state like a user stop', () => {
    const r = rig();
    r.live();
    r.frame({ type: 'voice_ending', reason: 'agent_end', session_id: 'O1' });
    expect(r.c.snapshot.status).toBe('ending');
    r.frame({ type: 'voice_ended', reason: 'agent_end', session_id: 'O1' });
    r.frame({ type: 'voice_stopped' });
    r.frame({ type: 'voice_owner_active', active: false, owner_local_id: 'O1' });
    expect(r.c.snapshot).toMatchObject({ status: 'off', remoteActive: false });
  });

  it('§6.11a SW-3: reason switch is a quiet end (no Ending… state, no cue, no local end)', () => {
    const r = rig();
    const t = r.live();
    r.frame({ type: 'voice_ending', reason: 'switch', session_id: 'O1' });
    expect(r.c.snapshot.status).toBe('active');
    r.frame({ type: 'voice_ended', reason: 'switch', session_id: 'O1' });
    r.frame({ type: 'voice_owner_active', active: false, owner_local_id: 'O1' });
    expect(r.c.snapshot).toMatchObject({ status: 'off', error: null, remoteActive: false });
    expect(t.closed).toBe(true);
    expect(r.cues.log).toEqual([]);
    expect(r.port.localEnds).toBe(0);
    expect(r.port.types()).not.toContain('voice_stop');
  });

  it('legacy voice_stopped alone also ends', () => {
    const r = rig();
    r.live();
    r.frame({ type: 'voice_stopped' });
    expect(r.c.snapshot.status).toBe('off');
  });

  it('a stale voice_ended before our session_started is ignored', () => {
    const r = rig();
    r.c.start();
    r.frame({ type: 'voice_ended', reason: 'user_stop' });
    r.frame({ type: 'voice_owner_active', active: false });
    expect(r.c.snapshot.status).toBe('connecting');
  });

  it('cancel while connecting: the late answer and its owner broadcast are ignored (no "elsewhere" flash)', () => {
    const r = rig();
    r.c.start();
    r.c.stop();
    r.clock.advance(ENDING_TIMEOUT_MS);
    expect(r.c.snapshot.status).toBe('off');
    r.answer();
    expect(r.transports).toHaveLength(0);
    expect(r.c.snapshot.remoteActive).toBe(false);
  });

  it('fatal voice_error (recoverable:false): voice_stop, wait for voice_ended, then the error with its hint', () => {
    const r = rig();
    r.live('websocket');
    r.frame({
      type: 'voice_event',
      event: {
        type: 'voice_error',
        error: { category: 'billing', message: 'Quota exceeded', recoverable: false, recovery_hint: 'Top up the DashScope account', provider_doc_url: 'https://x' },
      },
    });
    expect(r.port.types()).toContain('voice_stop');
    expect(r.c.snapshot.status).toBe('ending');
    r.frame({ type: 'voice_ended', reason: 'error' }); // backend O-4
    expect(r.c.snapshot).toMatchObject({
      status: 'error',
      error: { message: 'Quota exceeded', hint: 'Top up the DashScope account', category: 'billing', docUrl: 'https://x' },
    });
  });

  it('fatal without voice_ended within 5 s: voice_local_end and the error', () => {
    const r = rig();
    r.live('websocket');
    r.frame({ type: 'voice_event', event: { type: 'error', error: { code: 'voice_relay_failed', message: 'upstream closed' } } });
    r.clock.advance(ENDING_TIMEOUT_MS);
    expect(r.c.snapshot).toMatchObject({ status: 'error', error: { message: 'upstream closed' } });
    expect(r.port.localEnds).toBe(1);
  });

  it('a recoverable voice_error is a banner with the hint; the transport stays', () => {
    const r = rig();
    const t = r.live('websocket');
    r.frame({ type: 'voice_event', event: { type: 'voice_error', error: { message: 'Rate limited', recoverable: true, recovery_hint: 'Retrying' } } });
    expect(r.c.snapshot).toMatchObject({ status: 'active', banner: { kind: 'warning', message: 'Rate limited · Retrying' } });
    expect(t.closed).toBe(false);
  });

  it('Retry from error starts again; dismiss returns to off', () => {
    const r = rig();
    r.c.start();
    r.started({ voice_connection_error: 'x' });
    r.c.dismissError();
    expect(r.c.snapshot.status).toBe('off');
    r.c.start();
    expect(r.c.snapshot.status).toBe('connecting');
  });
});

describe('passive viewers (§7.5; fixes W-1)', () => {
  it('session_started{voice_initiator:false} → Active elsewhere; own status stays off', () => {
    const r = rig();
    r.frame({ type: 'session_started', session_id: 'O1', voice: true, voice_initiator: false, voice_provider: 'qwen' });
    expect(r.c.snapshot).toMatchObject({ status: 'off', remoteActive: true, remoteProvider: 'qwen' });
  });

  it('W-1: mirrored provider events never change the passive status (no wedge, input stays)', () => {
    const r = rig();
    r.frame({ type: 'session_started', session_id: 'O1', voice: true, voice_initiator: false });
    for (const ev of [
      { type: 'response.created' },
      { type: 'input_audio_buffer.speech_started' },
      { type: 'voice_status', status: 'ready' },
      { type: 'voice_status', status: 'reconnecting' },
      { serverContent: { turnComplete: true } },
      { type: 'voice_error', error: { message: 'x', recoverable: false } },
    ])
      r.frame({ type: 'voice_event', event: ev });
    r.frame({ type: 'voice_ending', reason: 'user_stop' });
    expect(r.c.snapshot.status).toBe('off');
    expect(r.c.snapshot.link).toBe('ok');
    expect(r.cues.log).toEqual([]);
  });

  it('V-12: a passive device sends nothing, plays nothing, executes no voice_command', () => {
    const r = rig();
    r.frame({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
    r.frame({ type: 'voice_command', command: { type: 'response.create' } });
    r.frame({ type: 'voice_audio_out', audio: 'AAAA' });
    r.frame({ type: 'voice_event', event: { type: 'input_audio_buffer.speech_started' } });
    expect(r.port.sent).toEqual([]);
    expect(r.transports).toHaveLength(0);
  });

  it('voice_owner_active toggles remoteActive; voice_ended / voice_stopped clear it', () => {
    const r = rig();
    r.frame({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
    expect(r.c.snapshot.remoteActive).toBe(true);
    r.frame({ type: 'voice_ended', reason: 'user_stop' });
    expect(r.c.snapshot.remoteActive).toBe(false);
    r.frame({ type: 'voice_owner_active', active: true });
    r.frame({ type: 'voice_owner_active', active: false });
    expect(r.c.snapshot.remoteActive).toBe(false);
  });

  it('hooks attached after the conversation learned voice is live start passive', () => {
    const first = rig();
    first.port.voiceActive = true;
    const late = rig({ port: first.port });
    expect(late.c.snapshot).toMatchObject({ status: 'off', remoteActive: true });
  });

  it('V-13 ownership loss: the owner tears down locally without voice_stop', () => {
    const r = rig();
    const t = r.live();
    r.port.clear();
    r.frame({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
    expect(t.closed).toBe(true);
    expect(r.port.sent).toEqual([]);
    expect(r.c.snapshot).toMatchObject({ status: 'off', remoteActive: true });
    expect(r.c.startMessage()).toBeNull();
  });

  it('our own voice_owner_active (answer to our voice_start) is not an ownership loss', () => {
    const r = rig();
    r.live();
    expect(r.c.snapshot).toMatchObject({ status: 'active', remoteActive: false });
  });

  it('Take over from Active elsewhere is an owner start', () => {
    const r = rig();
    r.frame({ type: 'voice_owner_active', active: true });
    r.c.takeOver();
    expect(r.port.sent[0]).toMatchObject({ type: 'voice_start' });
    r.answer();
    r.t().ready();
    expect(r.c.snapshot).toMatchObject({ status: 'active', remoteActive: false });
  });
});

describe('resync and mutes', () => {
  it('a re-sent voice_start on a healthy call (visibility resync) keeps the transport', () => {
    const r = rig();
    const t = r.live();
    r.reopen(); // resend_start while armed
    r.answer();
    expect(r.transports).toHaveLength(1);
    expect(t.closed).toBe(false);
    expect(r.c.snapshot.status).toBe('active');
  });

  it('mic and speaker mutes reach the transport and survive into a new transport', () => {
    const r = rig();
    const t = r.live();
    r.c.setMicMuted(true);
    r.c.setSpeakerMuted(true);
    expect(t.micMuted).toBe(true);
    expect(t.speakerMuted).toBe(true);
    expect(r.c.levels()).toEqual({ mic: 0, speaker: 0 });
    r.c.setMicMuted(false);
    expect(r.c.levels().mic).toBeCloseTo(0.2);
  });

  it('dispose releases the transport and sends nothing (P-1)', () => {
    const r = rig();
    const t = r.live();
    r.port.clear();
    r.c.dispose();
    expect(t.closed).toBe(true);
    expect(r.port.sent).toEqual([]);
  });

  it('voice_start carries no resume id for a brand-new orchestrator', () => {
    const r = rig();
    r.port.sdk = null;
    r.c.start();
    expect(r.port.sent[0]).toEqual({ type: 'voice_start', local_id: 'O1' });
  });

  it('a recording-enabled WebRTC session asks the transport to record and relays chunks', () => {
    const r = rig();
    r.c.start();
    r.answer(WEBRTC_INFO, { voice_recording_enabled: true });
    const t = r.t();
    expect(t.opts.record).toBe(true);
    t.ready();
    r.port.clear();
    t.events.recordingChunk('user', 'AAAA');
    t.events.recordingEnd();
    expect(r.port.sent).toEqual([{ type: 'voice_recording_chunk', channel: 'user', audio: 'AAAA' }, { type: 'voice_recording_end' }]);
    expect(r.c.snapshot.recording).toBe(true);
  });
});
