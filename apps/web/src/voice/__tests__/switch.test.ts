/**
 * §6.11a agent-initiated switch, voice side (SW-2): the resumed Archie view starts voice with no
 * tap when audio is already unlocked on the page (the tap that started the previous call), and
 * otherwise opens with its normal voice button plus a snackbar whose Start voice is a tap.
 * Runs the real registry and the voice feature's handler over the fake orchestrator socket.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { continueVoiceAfterSwitch } from '@/features/voice';
import { resetCapabilitiesCache } from '@/platform';
import { ArchieRuntime, getArchieRuntime, openSession, setSwitchVoiceHandler, startServices } from '@/services';
import { snackbarStore } from '@/stores';
import { FakeWebSocket, setupServices, teardownServices, type Harness } from '../../services/__tests__/fakes';
import { setSharedAudioContext, type AudioContextLike } from '../audio/context';
import { getVoiceController, installVoiceEngine, startVoiceWithoutGesture, uninstallVoiceEngine, VOICE_NEEDS_TAP } from '../registry';

const ORCH = '/api/orchestrator/chat';
let h: Harness;
const restore: (() => void)[] = [];

function stub(target: object, key: string, value: unknown): void {
  const had = Object.getOwnPropertyDescriptor(target, key);
  Object.defineProperty(target, key, { value, configurable: true, writable: true });
  restore.push(() => {
    if (had) Object.defineProperty(target, key, had);
    else Reflect.deleteProperty(target, key);
  });
}

/** A context that a tap unlocked earlier (`running`) or one that still needs a tap. */
function audioContext(state: 'running' | 'suspended'): AudioContextLike {
  return { state, resume: () => Promise.resolve() } as unknown as AudioContextLike;
}

beforeEach(() => {
  vi.useFakeTimers();
  h = setupServices();
  h.fetch.on('GET', /\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
  // a device where OpenAI (WebRTC) voice can run
  stub(window, 'isSecureContext', true);
  stub(window, 'RTCPeerConnection', function RTCPeerConnection() {});
  stub(navigator, 'mediaDevices', { getUserMedia: () => Promise.reject(new Error('not in tests')) });
  resetCapabilitiesCache();
  installVoiceEngine();
  setSwitchVoiceHandler(continueVoiceAfterSwitch);
});
afterEach(() => {
  setSwitchVoiceHandler(null);
  uninstallVoiceEngine();
  setSharedAudioContext(null);
  for (const r of restore.splice(0).reverse()) r();
  resetCapabilitiesCache();
  teardownServices();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function switchFromLiveVoice(): FakeWebSocket {
  startServices({ skipInitialSync: true });
  openSession({ kind: 'archie', localId: 'O1', focus: true });
  const ws = FakeWebSocket.last(ORCH);
  ws.open();
  ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
  ws.emit({ type: 'voice_ending', reason: 'switch', session_id: 'O1' });
  ws.emit({ type: 'voice_ended', reason: 'switch', session_id: 'O1' });
  ws.emit({ type: 'agent_session_closed', session_id: 'O1', is_orchestrator: true });
  ws.emit({ type: 'orchestrator_switch', sdk_session_id: 'PAST', title: 'Trip planning', voice: true, from_session_id: 'O1' });
  return ws;
}

describe('§6.11a SW-2: voice continues on the resumed conversation', () => {
  it('audio already unlocked: start, then voice_start for the new view with resume_sdk_id (§7.3), no tap', () => {
    setSharedAudioContext(audioContext('running'));
    const ws = switchFromLiveVoice();
    const fresh = getArchieRuntime() as ArchieRuntime;
    expect(fresh.conv.ref.sdkId).toBe('PAST');
    expect(ws.messages().slice(1)).toEqual([
      { type: 'start', local_id: fresh.localId, resume_sdk_id: 'PAST' },
      { type: 'voice_start', local_id: fresh.localId, resume_sdk_id: 'PAST' },
    ]);
    expect(ws.types()).not.toContain('stop');
    expect(getVoiceController(fresh.localId)?.snapshot.status).toBe('connecting');
    expect(snackbarStore.getState().queue.map((s) => [s.message, s.action?.label])).toEqual([['Switched to Trip planning', undefined]]);
  });

  it('audio not unlocked (autoplay rules): the view opens without voice; the snackbar Start voice tap starts it', () => {
    setSharedAudioContext(audioContext('suspended'));
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
    const ws = switchFromLiveVoice();
    const fresh = getArchieRuntime() as ArchieRuntime;
    expect(ws.types().slice(1)).toEqual(['start']);
    expect(getVoiceController(fresh.localId)?.snapshot.status).toBe('off');
    const snack = snackbarStore.getState().queue[0];
    expect(snack).toMatchObject({ message: 'Switched to Trip planning', action: { label: 'Start voice' } });
    snack?.action?.run();
    expect(ws.messages().slice(-1)).toEqual([{ type: 'voice_start', local_id: fresh.localId, resume_sdk_id: 'PAST' }]);
  });

  it('startVoiceWithoutGesture without unlocked audio or without a live view says why it did not start', () => {
    startServices({ skipInitialSync: true });
    const rt = openSession({ kind: 'archie', localId: 'O2', sdkId: 'PAST', focus: true }) as ArchieRuntime;
    expect(startVoiceWithoutGesture(rt.localId)).toBe(VOICE_NEEDS_TAP);
    expect(startVoiceWithoutGesture('nope')).toBe('Open Archie to start voice');
  });
});

describe('OPEN-3: Archie closed elsewhere during a call', () => {
  it('its view closes and the voice controller is disposed with it (local media released); nothing is sent', () => {
    setSharedAudioContext(audioContext('running'));
    startServices({ skipInitialSync: true });
    openSession({ kind: 'archie', localId: 'O1', focus: true });
    const ws = FakeWebSocket.last(ORCH);
    ws.open();
    ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
    const c = getVoiceController('O1');
    c?.start();
    ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1', voice: true, voice_initiator: true });
    expect(c?.snapshot.status).toBe('connecting');
    const dispose = vi.spyOn(c as NonNullable<typeof c>, 'dispose');
    ws.emit({ type: 'agent_session_closed', session_id: 'O1', is_orchestrator: true });
    expect(getArchieRuntime()).toBeUndefined();
    expect(dispose).toHaveBeenCalled();
    expect(ws.types()).toEqual(['start', 'voice_start']);
  });
});
