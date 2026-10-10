/**
 * Floating voice controls in the shell (VoiceOverlayHost): while this device has voice on Archie,
 * they float over every other view (another tab, a screen over the workspace) and never on the
 * Archie conversation itself, where the dock is; "Open the Archie conversation" focuses it; voice
 * on another device does not float.
 */
import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { openSession, startServices } from '@/services';
import { activateTab, prefsStore, setFrameScheduler, tabsStore } from '@/stores';
import { renderUi } from '@/test/render';
import { getVoiceController } from '@/voice';
import { App } from '../App';
import { navigate } from '../navigation/route';
import { voiceOverlayVisible } from '../shell/VoiceOverlayHost';
import { FakeWebSocket, setupApp, SIZES, teardownApp } from './testUtils';
import type { Harness } from '../../services/__tests__/fakes';

const ORCH = '/api/orchestrator/chat';
let h: Harness;

function setup(width: number = SIZES.expanded): void {
  h = setupApp(width);
  setFrameScheduler({
    schedule: (fn) => {
      let c = false;
      void Promise.resolve().then(() => {
        if (!c) fn();
      });
      return () => {
        c = true;
      };
    },
  });
  h.fetch.on('GET', /\/api\/sessions\/[^/]+\/messages/, { messages: [], total_count: 0, has_more: false, start_index: 0 });
  startServices({ skipInitialSync: true });
}

function liveArchie(): FakeWebSocket {
  openSession({ kind: 'archie', localId: 'O1', sdkId: 'O1', focus: true });
  const ws = FakeWebSocket.last(ORCH);
  ws.open();
  ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
  return ws;
}

const overlay = () => document.querySelector('[data-voice-overlay]');

describe('voiceOverlayVisible', () => {
  it('only with voice here, and not on its own Archie view unless a screen covers it', () => {
    expect(voiceOverlayVisible(null, 'A1', false)).toBe(false);
    expect(voiceOverlayVisible('O1', 'O1', false)).toBe(false);
    expect(voiceOverlayVisible('O1', 'A1', false)).toBe(true);
    expect(voiceOverlayVisible('O1', 'O1', true)).toBe(true);
    expect(voiceOverlayVisible('O1', null, false)).toBe(true);
  });
});

describe('VoiceOverlayHost', () => {
  beforeEach(() => {
    setup();
  });
  afterEach(() => {
    teardownApp();
  });

  it('floats over another tab and over screens, never on the Archie view; its state text goes back to Archie', async () => {
    liveArchie();
    openSession({ kind: 'agent', localId: 'A1', focus: false, titleHint: 'Agent' });
    renderUi(<App services={false} />);
    expect(overlay()).toBeNull();

    act(() => {
      getVoiceController('O1')?.start();
    });
    // on the Archie view: the dock is the control
    expect(overlay()).toBeNull();
    expect(screen.getAllByRole('group', { name: 'Voice controls' })).toHaveLength(1);

    act(() => {
      activateTab('A1');
    });
    // a lazy chunk: preloaded when the call started, mounted on the first other view
    expect(await screen.findByRole('region', { name: 'Voice call' })).toBeTruthy();
    expect(overlay()).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Open the Archie conversation' }));
    expect(tabsStore.getState().activeId).toBe('O1');
    expect(overlay()).toBeNull();

    act(() => {
      navigate({ name: 'settings', page: null });
    });
    expect(await screen.findByRole('region', { name: 'Voice call' })).toBeTruthy();

    // Ending… still floats on the other views (End's progress stays visible)
    act(() => {
      navigate({ name: 'workspace' });
      activateTab('A1');
      getVoiceController('O1')?.stop();
    });
    expect(overlay()?.querySelector('[data-voice="ending"]')).toBeTruthy();
  });

  it('a drag snaps the controls to a corner and saves it as the device pref', async () => {
    liveArchie();
    openSession({ kind: 'agent', localId: 'A1', focus: false, titleHint: 'Agent' });
    renderUi(<App services={false} />);
    act(() => {
      getVoiceController('O1')?.start();
      activateTab('A1');
    });
    const frame = await screen.findByRole('region', { name: 'Voice call' });
    const main = document.querySelector('main') as HTMLElement;
    main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800, right: 1000, bottom: 800, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    expect(prefsStore.getState().voiceOverlayAnchor).toBe('bottom-center');
    fireEvent.mouseDown(frame, { button: 0, clientX: 500, clientY: 760 });
    fireEvent.mouseMove(document, { clientX: 450, clientY: 700 });
    fireEvent.mouseMove(document, { clientX: 120, clientY: 90 });
    fireEvent.mouseUp(document, { clientX: 120, clientY: 90 });
    expect(prefsStore.getState().voiceOverlayAnchor).toBe('top-left');
  });

  it('voice on another device does not float', () => {
    const ws = liveArchie();
    openSession({ kind: 'agent', localId: 'A1', focus: true, titleHint: 'Agent' });
    renderUi(<App services={false} />);
    act(() => {
      ws.emit({ type: 'session_started', session_id: 'O1', voice: true, voice_initiator: false, voice_provider: 'qwen' });
      ws.emit({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
    });
    expect(overlay()).toBeNull();
  });
});
