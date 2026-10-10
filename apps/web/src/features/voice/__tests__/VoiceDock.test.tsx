/**
 * Voice dock (W-12): every state renders the approved wording and controls; the VAD counter is
 * in the dock (fixes W-4); errors show their recovery hint; Active elsewhere keeps the composer
 * (fixes W-1); the slot swaps the composer for the dock while this device has voice.
 */
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openSession, startServices, type ArchieRuntime } from '@/services';
import { expectNoAxeViolations } from '@/test/axe';
import { getVoiceController, installVoiceEngine, OFF_SNAPSHOT, uninstallVoiceEngine, type VoiceSnapshot } from '@/voice';
import { FakeWebSocket, setupServices, teardownServices } from '../../../services/__tests__/fakes';
import { dockText, formatElapsed, statusWord, vadSeconds } from '../copy';
import { ActiveElsewhereView, VoiceDockView, type VoiceDockActions } from '../VoiceDock';
import { VoiceAction, VoiceSlot } from '../VoiceSlot';

function actions(): VoiceDockActions & { calls: string[] } {
  const calls: string[] = [];
  const f = (n: string) => () => {
    calls.push(n);
  };
  return {
    calls,
    onEnd: f('end'),
    onToggleMic: f('mic'),
    onToggleSpeaker: f('speaker'),
    onInterrupt: f('interrupt'),
    onRetry: f('retry'),
    onReconnect: f('reconnect'),
    onDismiss: f('dismiss'),
  };
}

const snap = (over: Partial<VoiceSnapshot>): VoiceSnapshot => ({ ...OFF_SNAPSHOT, status: 'active', ...over });

describe('VoiceDockView states', () => {
  it.each([
    ['active', 'Listening', 'Speak any time'],
    ['speaking', 'Speaking', 'Tap the orb to interrupt'],
    ['thinking', 'Thinking', 'Working on it'],
    ['tool_use', 'Using tools', 'Running a tool'],
    ['ending', 'Ending…', 'Closing the voice connection'],
  ] as const)('%s → "%s"', async (status, title, detail) => {
    const a = actions();
    const { container } = render(<VoiceDockView snapshot={snap({ status })} now={0} {...a} />);
    expect(screen.getByText(title)).toBeTruthy();
    expect(screen.getByText(detail)).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Voice controls' })).toBeTruthy();
    await expectNoAxeViolations(container);
  });

  it('live controls: mic mute, speaker mute, End; tapping the orb interrupts while speaking', async () => {
    const a = actions();
    const user = userEvent.setup();
    render(<VoiceDockView snapshot={snap({ status: 'speaking' })} now={0} {...a} />);
    await user.click(screen.getByRole('button', { name: 'Mute microphone' }));
    await user.click(screen.getByRole('button', { name: 'Mute speaker' }));
    await user.click(screen.getByRole('button', { name: 'Interrupt Archie' }));
    await user.click(screen.getByRole('button', { name: 'End voice' }));
    expect(a.calls).toEqual(['mic', 'speaker', 'interrupt', 'end']);
  });

  it('muted toggles are pressed and say so', () => {
    render(<VoiceDockView snapshot={snap({ micMuted: true, speakerMuted: true })} now={0} {...actions()} />);
    expect(screen.getByRole('button', { name: 'Unmute microphone' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'Unmute speaker' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('Muted')).toBeTruthy();
  });

  it('connecting: Preparing conversation while summarising; End cancels; mic disabled', () => {
    render(<VoiceDockView snapshot={snap({ status: 'connecting', phase: 'summarizing' })} now={0} {...actions()} />);
    expect(screen.getByText('Preparing conversation…')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel voice' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Mute microphone' }).getAttribute('aria-disabled')).toBe('true');
  });

  it('W-4: "Hearing you · Ns" shows in the dock after 3 s of listening, counting on', () => {
    const vad = { state: 'listening', durationMs: 2000, at: 1000 };
    const { rerender } = render(<VoiceDockView snapshot={snap({ vad })} now={1500} {...actions()} />);
    expect(screen.getByText('Speak any time')).toBeTruthy();
    rerender(<VoiceDockView snapshot={snap({ vad })} now={4200} {...actions()} />);
    expect(screen.getByText('Hearing you · 5s')).toBeTruthy();
    expect(vadSeconds(snap({ vad: { ...vad, state: 'thinking' } }), 9000)).toBeNull();
  });

  it('banners replace the supporting line', () => {
    render(<VoiceDockView snapshot={snap({ banner: { kind: 'reconnect_warning', timeLeftS: 30 } })} now={0} {...actions()} />);
    expect(screen.getByText('Pausing in ~30s to reconnect…')).toBeTruthy();
  });

  it('P-2 reconnecting: elapsed timer from the drop, status role, End still works', async () => {
    const a = actions();
    const user = userEvent.setup();
    const { container } = render(<VoiceDockView snapshot={snap({ link: 'lost', linkLostAt: 1000 })} now={8_400} {...a} />);
    const st = screen.getByRole('status', { name: 'Voice reconnecting' });
    expect(st.textContent).toContain('Reconnecting…');
    expect(st.textContent).toContain('0:07');
    expect(st.textContent).toContain('you’ll hear a tone when it’s back');
    await user.click(screen.getByRole('button', { name: 'End voice' }));
    expect(a.calls).toEqual(['end']);
    await expectNoAxeViolations(container);
  });

  it('P-2 reconnected: "Back after 0:09"', () => {
    render(<VoiceDockView snapshot={snap({ link: 'restored', linkRecoveredMs: 9_200 })} now={0} {...actions()} />);
    expect(screen.getByText('Reconnected')).toBeTruthy();
    expect(screen.getByText('Back after 0:09')).toBeTruthy();
  });

  it('P-2 failed: alert with End and Reconnect', async () => {
    const a = actions();
    const user = userEvent.setup();
    const { container } = render(
      <VoiceDockView snapshot={snap({ status: 'error', link: 'failed', error: { message: 'Couldn’t reconnect', hint: null, category: null, docUrl: null } })} now={0} {...a} />,
    );
    expect(screen.getByRole('alert').textContent).toContain('Couldn’t reconnect');
    await user.click(screen.getByRole('button', { name: 'Reconnect' }));
    await user.click(screen.getByRole('button', { name: 'End voice' }));
    expect(a.calls).toEqual(['reconnect', 'dismiss']);
    await expectNoAxeViolations(container);
  });

  it('typed voice_error: message, recovery hint and the provider doc link; Retry and Close (fixes inv02 §6.4)', async () => {
    const a = actions();
    const user = userEvent.setup();
    render(
      <VoiceDockView
        snapshot={snap({ status: 'error', error: { message: 'Quota exceeded', hint: 'Top up the DashScope account', category: 'billing', docUrl: 'https://help.example' } })}
        now={0}
        {...a}
      />,
    );
    expect(screen.getByText('Quota exceeded')).toBeTruthy();
    expect(screen.getByText(/Top up the DashScope account/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Details' }).getAttribute('href')).toBe('https://help.example');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await user.click(screen.getByRole('button', { name: 'Close voice' }));
    expect(a.calls).toEqual(['retry', 'dismiss']);
  });

  it('Active elsewhere: read-only status; VT-2 copy; Take over only when offered', async () => {
    const onTakeOver = vi.fn();
    const { rerender, container } = render(<ActiveElsewhereView onTakeOver={onTakeOver} />);
    expect(screen.getByText('Voice active on another device')).toBeTruthy();
    expect(screen.getByText('Transcripts mirror here')).toBeTruthy();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Take over' }));
    expect(onTakeOver).toHaveBeenCalled();
    await expectNoAxeViolations(container);
    rerender(<ActiveElsewhereView />);
    expect(screen.getByText('Transcripts mirror here')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Take over' })).toBeNull();
  });
});

describe('copy helpers', () => {
  it('formatElapsed and the app bar status word', () => {
    expect(formatElapsed(0)).toBe('0:00');
    expect(formatElapsed(67_900)).toBe('1:07');
    expect(statusWord(snap({ link: 'lost' }))).toBe('Reconnecting…');
    expect(statusWord(snap({ status: 'tool_use' }))).toBe('Using tools');
    expect(statusWord(snap({ status: 'off' }))).toBe('');
    expect(dockText(snap({ status: 'connecting', phase: 'preparing' }), 0).title).toBe('Connecting…');
  });
});

describe('VoiceSlot on a live Archie runtime', () => {
  const ORCH = '/api/orchestrator/chat';
  beforeEach(() => {
    setupServices();
    installVoiceEngine();
  });
  afterEach(() => {
    uninstallVoiceEngine();
    teardownServices();
  });

  function archie(): { rt: ArchieRuntime; ws: FakeWebSocket } {
    startServices({ skipInitialSync: true });
    const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
    const ws = FakeWebSocket.last(ORCH);
    ws.open();
    ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
    return { rt, ws };
  }

  it('the engine attaches a controller to the Archie runtime as soon as it opens', () => {
    archie();
    expect(getVoiceController('O1')).toBeDefined();
  });

  it('W-1: voice on another device → Active elsewhere ABOVE the composer, which stays usable', () => {
    const { ws } = archie();
    render(<VoiceSlot localId="O1" composer={<span data-testid="composer">composer</span>} />);
    act(() => {
      ws.emit({ type: 'session_started', session_id: 'O1', voice: true, voice_initiator: false, voice_provider: 'qwen' });
      ws.emit({ type: 'voice_owner_active', active: true, owner_local_id: 'O1' });
      ws.emit({ type: 'voice_event', event: { type: 'response.created' } });
    });
    expect(screen.getByText('Voice active on another device')).toBeTruthy();
    expect(screen.getByTestId('composer')).toBeTruthy();
    expect(screen.queryByRole('group', { name: 'Voice controls' })).toBeNull();
    act(() => {
      ws.emit({ type: 'voice_ended', reason: 'user_stop' });
    });
    expect(screen.queryByText('Voice active on another device')).toBeNull();
    expect(screen.getByTestId('composer')).toBeTruthy();
  });

  it('own voice: the dock replaces the composer; the compact app bar shows the speaker toggle', () => {
    archie();
    render(
      <>
        <VoiceAction localId="O1" />
        <VoiceSlot localId="O1" composer={<span data-testid="composer">composer</span>} />
      </>,
    );
    expect(screen.queryByRole('button', { name: 'Speaker on' })).toBeNull();
    act(() => {
      getVoiceController('O1')?.start();
    });
    expect(screen.getByRole('group', { name: 'Voice controls' })).toBeTruthy();
    expect(screen.queryByTestId('composer')).toBeNull();
    expect(screen.getByText('Connecting…')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Speaker on' })).toBeTruthy();
    expect(FakeWebSocket.last(ORCH).types()).toEqual(['start', 'voice_start']);
  });

  it('agent sessions keep the plain composer', () => {
    render(<VoiceSlot localId="nope" composer={<span data-testid="composer">composer</span>} />);
    expect(screen.getByTestId('composer')).toBeTruthy();
  });
});
