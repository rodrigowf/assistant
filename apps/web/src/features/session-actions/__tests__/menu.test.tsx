/**
 * The ⋮ session menu per kind (fake socket), F2, the busy overlay, and the start race that ends
 * in `error{orchestrator_active}` (another device won between our pool check and our start).
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openSession, startServices, type ArchieRuntime } from '@/services';
import { openTab, setFrameScheduler, tabsStore } from '@/stores';
import { expectNoAxeViolations } from '@/test/axe';
import { patchSessionActions, requestOpenArchie, resetSessionActions, SessionActionsHost, SessionMenu } from '..';
import { FakeWebSocket, jsonResponse, setupServices, teardownServices, type Harness } from '../../../services/__tests__/fakes';

const ORCH = '/api/orchestrator/chat';
let h: Harness;

beforeEach(() => {
  h = setupServices();
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
  resetSessionActions();
  startServices({ skipInitialSync: true });
});
afterEach(() => {
  teardownServices();
});

function liveArchie(): ArchieRuntime {
  const rt = openSession({ kind: 'archie', localId: 'O1', focus: true }) as ArchieRuntime;
  const ws = FakeWebSocket.last(ORCH);
  ws.open();
  ws.emit({ type: 'session_started', session_id: 'O1', jsonl_id: 'O1' });
  return rt;
}

describe('SessionMenu per kind (IA §3, mockups "Menu and dialog")', () => {
  it('Archie: Rename allowed (P-4), no Session settings (inv02 F-18), Compact, Fork, Close, Delete', async () => {
    const user = userEvent.setup();
    liveArchie();
    const onRename = vi.fn();
    const onClose = vi.fn();
    const { container } = render(<SessionMenu localId="O1" onRename={onRename} onClose={onClose} />);
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    const menu = screen.getByRole('menu', { name: 'Session menu' });
    expect(within(menu).getAllByRole('menuitem').map((i) => i.textContent?.replace(/F2|Ctrl\+Alt\+W/g, '').trim())).toEqual([
      'Rename',
      'Compact context',
      'Fork',
      'Close',
      'Delete',
    ]);
    await expectNoAxeViolations(container);
    await expectNoAxeViolations(menu);
    await user.click(within(menu).getByRole('menuitem', { name: /^Rename/ }));
    expect(onRename).toHaveBeenCalledWith('O1');
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    await user.click(screen.getByRole('menuitem', { name: /^Close/ }));
    expect(onClose).toHaveBeenCalledWith('O1');
  });

  it('a document tab only offers Close', async () => {
    const user = userEvent.setup();
    openTab({ id: 'memory:MEMORY.md', kind: 'memory', path: 'MEMORY.md' }, { focus: true });
    render(<SessionMenu localId="memory:MEMORY.md" onClose={() => undefined} />);
    await user.click(screen.getByRole('button', { name: 'Tab menu' }));
    expect(screen.getAllByRole('menuitem').map((i) => i.textContent?.replace('Ctrl+Alt+W', ''))).toEqual(['Close']);
  });

  it('Compact is disabled while a reply runs, with the reason', async () => {
    const user = userEvent.setup();
    liveArchie();
    await act(async () => {
      FakeWebSocket.last(ORCH).emit({ type: 'status', status: 'streaming' });
    });
    render(<SessionMenu localId="O1" />);
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    const item = screen.getByRole('menuitem', { name: /^Compact context/ });
    expect(item.getAttribute('aria-disabled')).toBe('true');
    expect(item.textContent).toContain('Stop the current reply first');
  });
});

describe('SessionActionsHost', () => {
  it('F2 renames the active conversation (not while typing)', async () => {
    const user = userEvent.setup();
    liveArchie();
    const onRename = vi.fn();
    render(
      <>
        <div role="textbox" aria-label="other" contentEditable tabIndex={0} suppressContentEditableWarning />
        <SessionActionsHost onRename={onRename} />
      </>,
    );
    await user.keyboard('{F2}');
    expect(onRename).toHaveBeenCalledWith('O1');
    await user.click(screen.getByRole('textbox', { name: 'other' }));
    await user.keyboard('{F2}');
    expect(onRename).toHaveBeenCalledTimes(1);
  });

  it('shows the busy overlay while an action runs (inv02 F-12)', async () => {
    render(<SessionActionsHost />);
    await act(async () => {
      patchSessionActions({ busy: 'Forking…' });
    });
    expect(screen.getByRole('status').textContent).toContain('Forking…');
    await act(async () => {
      patchSessionActions({ busy: null });
    });
    expect(screen.queryByText('Forking…')).toBeNull();
  });
});

describe('start race: error{orchestrator_active} (spec 12 §6.11)', () => {
  it('drops our failed view (no close: it never existed) and shows the dialog for the one that won', async () => {
    render(<SessionActionsHost />);
    let rt: ArchieRuntime | null = null;
    await act(async () => {
      rt = await requestOpenArchie({ mode: 'new' });
    });
    const mine = (rt as ArchieRuntime | null)?.localId as string;
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual([mine]);
    // another device started Archie meanwhile
    h.fetch.on('GET', '/api/sessions/pool/live', () =>
      jsonResponse([{ local_id: 'WINNER', sdk_session_id: 'WINNER', status: 'idle', cost: 0, turns: 0, title: 'Orchestrator', is_orchestrator: true }]),
    );
    const ws = FakeWebSocket.last(ORCH);
    await act(async () => {
      ws.open();
      ws.emit({ type: 'error', error: 'orchestrator_active', detail: 'Another orchestrator is active (WINNER).' });
    });
    const dlg = await screen.findByRole('alertdialog', { name: 'Archie is already active' });
    expect(dlg.textContent).toContain('Only one Archie conversation runs at a time.');
    await waitFor(() => {
      expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['WINNER']); // OPEN-1: the pool's Archie is open here too
    });
    expect(h.fetch.calls('POST', `/api/sessions/${mine}/close`)).toEqual([]);
  });
});
