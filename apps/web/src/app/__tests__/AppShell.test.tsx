/**
 * W-07 shell: all tabs stay mounted (spec 13 §4.4), derived tab titles (inv02 §7 #9), P-1
 * explicit close / no implicit close, P-6 background opens, P-7 keyboard, and the drawer /
 * switcher / list-pane behaviours per size class (spec 13 §4.2).
 */
import { act, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { getSessionRuntime, onWatcherEvent, openSession, type SessionInfo } from '@/services';
import { activateTab, moveTab, openTab, prefsStore, setCatalogItems, tabsStore, updateSessionItems } from '@/stores';
import { expectNoAxeViolations } from '@/test/axe';
import { renderUi } from '@/test/render';
import { App } from '../App';
import { routeStore } from '../navigation/route';
import { shellStore } from '../shell/shellState';
import { FakeWebSocket, flushPromises, setupApp, setWidth, SIZES, teardownApp } from './testUtils';
import type { Harness } from '../../services/__tests__/fakes';

const CHAT = '/api/sessions/chat';
let h: Harness;

function session(p: Partial<SessionInfo> & { session_id: string; title: string }): SessionInfo {
  return {
    started_at: '2026-10-03T08:00:00Z',
    last_activity: '2026-10-03T08:30:00Z',
    message_count: 2,
    is_orchestrator: false,
    provider: 'claude',
    local_id: null,
    ...p,
  };
}

function subscribe(ws: FakeWebSocket, id: string): void {
  if (ws.readyState !== 1) ws.open();
  ws.emit({ type: 'session_started', session_id: id });
}

function closeCalls(): string[] {
  return h.fetch.requests.filter((r) => r.method === 'POST' && r.path.endsWith('/close')).map((r) => r.path);
}

function panel(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-panel-id="${id}"]`);
  if (!el) throw new Error(`no panel ${id}`);
  return el;
}

function closeX(container: HTMLElement, id: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-tab-wrap="${id}"] [data-tab-close]`);
  if (!el) throw new Error(`no close for ${id}`);
  return el;
}

/** Three tabs: two agent sessions and a visual (with an iframe). */
function openThree(): void {
  openSession({ kind: 'agent', localId: 'A1', focus: true, titleHint: 'First' });
  openSession({ kind: 'agent', localId: 'A2', focus: true, titleHint: 'Second' });
  openTab({ id: 'viz:energy/index.html', kind: 'visual', path: 'energy/index.html', url: 'about:blank', titleHint: 'Energy' }, { focus: true });
}

beforeEach(() => {
  h = setupApp(SIZES.expanded);
});
afterEach(() => {
  teardownApp();
});

/* ------------------------------------------------------------------ §4.4 */

describe('all tabs stay mounted (spec 13 §4.4, inv02 §7 #1)', () => {
  it('keeps panel and iframe DOM identity across tab switches, reorders and window-class changes', async () => {
    openThree();
    const { getByRole } = renderUi(<App services={false} />);
    const main = getByRole('main', { name: 'Workspace' });
    const p1 = panel('A1');
    const p2 = panel('A2');
    const pv = panel('viz:energy/index.html');
    // the viewer is a lazy chunk: its iframe mounts once the chunk is in, then never moves
    await waitFor(() => expect(pv.querySelector('iframe')).toBeTruthy());
    const iframe = pv.querySelector('iframe');
    expect(iframe).toBeTruthy();
    expect(iframe?.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-popups allow-forms allow-modals');
    expect(pv.hidden).toBe(false);
    expect(p1.hidden).toBe(true);

    act(() => {
      activateTab('A1');
    });
    expect(panel('A1')).toBe(p1);
    expect(p1.hidden).toBe(false);
    expect(pv.hidden).toBe(true);
    expect(pv.querySelector('iframe')).toBe(iframe);

    // reordering the strip never moves a panel (moving an iframe reloads it)
    const order = (): string[] => Array.from(document.querySelectorAll('[data-panel-id]')).map((e) => e.getAttribute('data-panel-id') ?? '');
    const before = order();
    act(() => {
      moveTab('viz:energy/index.html', 0);
    });
    expect(order()).toEqual(before);
    expect(pv.querySelector('iframe')).toBe(iframe);

    for (const w of [SIZES.compact, SIZES.medium, SIZES.expanded, 1024, SIZES.compact]) {
      setWidth(w);
      expect(document.querySelector('main')).toBe(main);
      expect(panel('A1')).toBe(p1);
      expect(panel('A2')).toBe(p2);
      expect(panel('viz:energy/index.html')).toBe(pv);
      expect(pv.querySelector('iframe')).toBe(iframe);
    }
    // runtimes live outside React: still the same, never disposed
    expect(getSessionRuntime('A1')?.isDisposed).toBe(false);
  });

  it('a hidden tab keeps a live status indicator (one per tab) while its panel is frozen', async () => {
    openSession({ kind: 'agent', localId: 'V1', focus: true, titleHint: 'Visible' });
    const bg = openSession({ kind: 'agent', localId: 'H1', focus: false, titleHint: 'Hidden' });
    subscribe(FakeWebSocket.all(CHAT)[1] as FakeWebSocket, 'H1');
    renderUi(<App services={false} />);
    expect(panel('H1').hidden).toBe(true);
    act(() => {
      bg.send('work please');
    });
    await waitFor(() => {
      const st = document.querySelectorAll('[data-tab-wrap="H1"] [data-status]');
      expect(st).toHaveLength(1);
      expect(st[0]?.getAttribute('data-status')).toBe('working');
    });
  });

  it('shows the empty workspace (greeting + new actions) when no tab is open', () => {
    const { getByRole } = renderUi(<App services={false} />);
    expect(getByRole('heading', { name: /What are we doing\?/ })).toBeTruthy();
    expect(getByRole('button', { name: 'New Archie conversation' })).toBeTruthy();
    expect(getByRole('button', { name: 'New agent session' })).toBeTruthy();
  });
});

/* ------------------------------------------------------------------ §7 #9 */

describe('tab titles are derived from the session list (inv02 §7 #9)', () => {
  it('sdk id match → local id match → hint; renames in the list re-title the tab', async () => {
    setCatalogItems('sessions', [
      session({ session_id: 'sdk-1', title: 'Refactor utils' }),
      session({ session_id: 'sdk-2', title: 'Garden plan', local_id: 'L2' }),
    ]);
    openSession({ kind: 'agent', localId: 'L1', sdkId: 'sdk-1', focus: true, titleHint: 'Agent l1' });
    openSession({ kind: 'agent', localId: 'L2', focus: false, titleHint: 'Agent l2' });
    openSession({ kind: 'agent', localId: 'L3', focus: false, titleHint: 'Agent l3' });
    const { getByRole } = renderUi(<App services={false} />);
    const strip = getByRole('tablist', { name: 'Open sessions' });
    expect(within(strip).getByRole('tab', { name: 'Refactor utils' })).toBeTruthy();
    expect(within(strip).getByRole('tab', { name: 'Garden plan' })).toBeTruthy();
    expect(within(strip).getByRole('tab', { name: 'Agent l3' })).toBeTruthy();

    act(() => {
      updateSessionItems((items) => items.map((s) => (s.session_id === 'sdk-1' ? { ...s, title: 'Utils, renamed' } : s)));
    });
    await waitFor(() => {
      expect(within(strip).getByRole('tab', { name: 'Utils, renamed' })).toBeTruthy();
    });
  });
});

/* ------------------------------------------------------------------ IA §1 vocabulary */

describe('IA §1: the UI never says "Orchestrator"', () => {
  it('a generic Archie title shows "New conversation"; a real title is kept', async () => {
    setCatalogItems('sessions', [
      session({ session_id: 'o-1', title: 'Orchestrator', is_orchestrator: true }),
      session({ session_id: 'o-2', title: 'Movie night plan', is_orchestrator: true }),
    ]);
    openSession({ kind: 'archie', localId: 'O1', sdkId: 'o-1', focus: true });
    const { getByRole, container } = renderUi(<App services={false} />);
    expect(getByRole('tab', { name: 'New conversation' })).toBeTruthy();
    expect(container.textContent).not.toMatch(/orchestrator/i);
    expect(getByRole('button', { name: /Movie night plan/ })).toBeTruthy();
    setWidth(SIZES.compact);
    expect(getByRole('button', { name: /Switch session/ }).textContent).toMatch(/New conversation/);
    fireEvent.click(getByRole('button', { name: 'Open navigation' }));
    await waitFor(() => {
      expect(document.body.textContent).not.toMatch(/orchestrator/i);
    });
  });
});

/* ------------------------------------------------------------------ P-1 */

describe('P-1: an explicit close closes for everybody; nothing else closes', () => {
  it('closes an idle agent tab at once: POST …/close, tab and panel gone', async () => {
    openThree();
    const { container } = renderUi(<App services={false} />);
    fireEvent.click(closeX(container, 'A1'));
    await waitFor(() => {
      expect(closeCalls()).toEqual(['/api/sessions/A1/close']);
    });
    expect(tabsStore.getState().tabs.map((t) => t.id)).not.toContain('A1');
    expect(document.querySelector('[data-panel-id="A1"]')).toBeNull();
  });

  it('asks before closing a running session; Cancel keeps it, Close closes it', async () => {
    const rt = openSession({ kind: 'agent', localId: 'R1', focus: true, titleHint: 'Busy one' });
    subscribe(FakeWebSocket.last(CHAT), 'R1');
    rt.send('hello');
    const { container, getByRole, queryByRole } = renderUi(<App services={false} />);
    fireEvent.click(closeX(container, 'R1'));
    const dialog = getByRole('alertdialog', { name: 'Close this session?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await flushPromises();
    expect(closeCalls()).toEqual([]);
    expect(queryByRole('alertdialog')).toBeNull();

    fireEvent.click(closeX(container, 'R1'));
    fireEvent.click(within(getByRole('alertdialog')).getByRole('button', { name: 'Close session' }));
    await waitFor(() => {
      expect(closeCalls()).toEqual(['/api/sessions/R1/close']);
    });
  });

  it('always asks before stopping Archie (closes it on every device)', async () => {
    openSession({ kind: 'archie', localId: 'O1', focus: true });
    const { container, getByRole } = renderUi(<App services={false} />);
    fireEvent.click(closeX(container, 'O1'));
    const dialog = getByRole('alertdialog', { name: 'Stop Archie on all devices?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Stop Archie' }));
    await waitFor(() => {
      expect(closeCalls()).toEqual(['/api/sessions/O1/close']);
    });
  });

  it('closing a doc tab sends nothing to the server', async () => {
    openThree();
    const { container } = renderUi(<App services={false} />);
    fireEvent.click(closeX(container, 'viz:energy/index.html'));
    await flushPromises();
    expect(closeCalls()).toEqual([]);
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['A1', 'A2']);
  });

  it('unmounting the app, resizing, hiding panels and unload events never close or stop', async () => {
    openThree();
    const a = getSessionRuntime('A1');
    subscribe(FakeWebSocket.last(CHAT), 'A2');
    const { unmount } = renderUi(<App services={false} />);
    setWidth(SIZES.compact);
    setWidth(SIZES.medium);
    act(() => {
      activateTab('A1');
    });
    window.dispatchEvent(new Event('pagehide'));
    window.dispatchEvent(new Event('beforeunload'));
    unmount();
    await flushPromises();
    expect(closeCalls()).toEqual([]);
    expect(FakeWebSocket.allSent().map((m) => m.type)).not.toContain('stop');
    expect(a?.isDisposed).toBe(false);
  });
});

/* ------------------------------------------------------------------ P-6 */

describe('P-6: background opens never steal focus', () => {
  it('a session opened by Archie / another device becomes a badged background tab plus a snackbar', async () => {
    openSession({ kind: 'agent', localId: 'A1', focus: true, titleHint: 'Mine' });
    const { getByRole, findByText } = renderUi(<App services={false} />);
    act(() => {
      onWatcherEvent({ type: 'agent_session_opened', session_id: 'BG1', sdk_session_id: null } as never);
    });
    expect(tabsStore.getState().activeId).toBe('A1');
    const bg = getByRole('tab', { name: 'Agent BG1' });
    expect(bg.getAttribute('aria-selected')).toBe('false');
    expect(bg.getAttribute('aria-describedby')).toBeTruthy();
    expect(document.getElementById(bg.getAttribute('aria-describedby') ?? '')?.textContent).toMatch(/New activity/);
    expect(panel('BG1').hidden).toBe(true);
    // one status indicator per tab; unseen is a badge on the icon, not a second dot
    const wrap = document.querySelector('[data-tab-wrap="BG1"]') as HTMLElement;
    expect(wrap.querySelectorAll('[data-status]')).toHaveLength(1);
    expect(wrap.querySelector('[data-unseen]')).toBeTruthy();
    expect(await findByText('Archie opened Agent BG1')).toBeTruthy();

    // Looking at it clears the badge
    fireEvent.click(bg);
    expect(tabsStore.getState().tabs.find((t) => t.id === 'BG1')?.unseen).toBe(false);
    expect(document.querySelector('[data-tab-wrap="BG1"] [data-unseen]')).toBeNull();
  });
});

/* ------------------------------------------------------------------ P-7 */

describe('P-7 keyboard shortcuts', () => {
  const key = (k: string, code: string, extra: Partial<KeyboardEventInit> = {}) => {
    fireEvent.keyDown(document.body, { key: k, code, ctrlKey: true, altKey: true, ...extra });
  };

  it('Ctrl+Alt+→/←, Ctrl+Alt+1…9 switch tabs; Ctrl+Alt+W closes (with confirm when running)', async () => {
    openThree();
    renderUi(<App services={false} />);
    expect(tabsStore.getState().activeId).toBe('viz:energy/index.html');
    key('ArrowRight', 'ArrowRight');
    expect(tabsStore.getState().activeId).toBe('A1');
    key('ArrowLeft', 'ArrowLeft');
    expect(tabsStore.getState().activeId).toBe('viz:energy/index.html');
    key('2', 'Digit2');
    expect(tabsStore.getState().activeId).toBe('A2');
    key('9', 'Digit9');
    expect(tabsStore.getState().activeId).toBe('viz:energy/index.html');
    key('w', 'KeyW');
    await flushPromises();
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['A1', 'A2']);
    expect(closeCalls()).toEqual([]); // a doc tab: nothing to the server

    const rt = getSessionRuntime('A2');
    subscribe(FakeWebSocket.all(CHAT)[1] as FakeWebSocket, 'A2');
    act(() => {
      activateTab('A2');
      rt?.send('busy');
    });
    key('w', 'KeyW');
    expect(shellStore.getState().confirmCloseId).toBe('A2');
  });

  it('Ctrl+Alt+Shift+N opens a focused new agent tab; shortcuts are ignored while an overlay is open', () => {
    renderUi(<App services={false} />);
    key('N', 'KeyN', { shiftKey: true });
    const s = tabsStore.getState();
    expect(s.tabs).toHaveLength(1);
    expect(s.tabs[0]?.kind).toBe('agent');
    expect(s.activeId).toBe(s.tabs[0]?.id);

    act(() => {
      shellStore.setState({ renameId: null, confirmCloseId: null });
    });
    setWidth(SIZES.compact);
    act(() => {
      shellStore.setState({ drawerOpen: true });
    });
    key('N', 'KeyN', { shiftKey: true });
    expect(tabsStore.getState().tabs).toHaveLength(1);
  });
});

/* ------------------------------------------------------------------ size classes */

describe('expanded: rail + standard list pane', () => {
  it('collapses and restores the list pane (remembered in prefs); rail destinations drive it', async () => {
    setCatalogItems('sessions', [session({ session_id: 's1', title: 'Weather script' })]);
    const { getByRole, queryByRole, container } = renderUi(<App services={false} />);
    expect(getByRole('navigation', { name: 'Main' })).toBeTruthy();
    expect(getByRole('complementary', { name: 'Chats' })).toBeTruthy();
    expect(getByRole('button', { name: /Weather script/ })).toBeTruthy();

    fireEvent.click(getByRole('button', { name: 'Collapse list' }));
    expect(prefsStore.getState().listPaneCollapsed).toBe(true);
    expect(queryByRole('complementary')).toBeNull();
    fireEvent.click(getByRole('button', { name: 'Show list' }));
    expect(prefsStore.getState().listPaneCollapsed).toBe(false);

    fireEvent.click(getByRole('button', { name: 'Memory' }));
    expect(getByRole('complementary', { name: 'Memory' })).toBeTruthy();
    expect(routeStore.getState().route).toEqual({ name: 'memory', path: null });
    await expectNoAxeViolations(container);
  });

  it('opening a past session from the list focuses it as a tab', () => {
    setCatalogItems('sessions', [session({ session_id: 's1', title: 'Weather script' })]);
    const { getByRole } = renderUi(<App services={false} />);
    fireEvent.click(getByRole('button', { name: /Weather script/ }));
    const s = tabsStore.getState();
    expect(s.tabs[0]?.sdkId).toBe('s1');
    expect(s.activeId).toBe(s.tabs[0]?.id);
    expect(getByRole('tab', { name: 'Weather script' }).getAttribute('aria-selected')).toBe('true');
  });

  it('Settings opens a screen beside the rail; Back closes it', async () => {
    const { getByRole, queryByRole } = renderUi(<App services={false} />);
    fireEvent.click(getByRole('button', { name: 'Settings' }));
    expect(getByRole('region', { name: 'Settings' })).toBeTruthy();
    expect(getByRole('navigation', { name: 'Main' })).toBeTruthy();
    act(() => {
      window.history.back();
    });
    await waitFor(() => {
      expect(queryByRole('region', { name: 'Settings' })).toBeNull();
    });
    expect(routeStore.getState().route).toEqual({ name: 'workspace' });
  });
});

describe('medium: rail + list-pane overlay', () => {
  beforeEach(() => {
    setWidth(SIZES.medium);
  });

  it('has no standard list pane; ☰ opens the overlay, choosing a session closes it', () => {
    setCatalogItems('sessions', [session({ session_id: 's1', title: 'Weather script' })]);
    const { getByRole, queryByRole } = renderUi(<App services={false} />);
    expect(getByRole('navigation', { name: 'Main' })).toBeTruthy();
    expect(queryByRole('complementary')).toBeNull();
    fireEvent.click(getByRole('button', { name: 'Open list' }));
    const sheet = getByRole('dialog', { name: 'Chats' });
    fireEvent.click(within(sheet).getByRole('button', { name: /Weather script/ }));
    expect(queryByRole('dialog', { name: 'Chats' })).toBeNull();
    expect(tabsStore.getState().tabs[0]?.sdkId).toBe('s1');
  });

  it('a rail destination opens the overlay on that destination', () => {
    const { getByRole } = renderUi(<App services={false} />);
    fireEvent.click(getByRole('button', { name: 'Visuals' }));
    expect(getByRole('dialog', { name: 'Visuals' })).toBeTruthy();
  });
});

describe('compact: app bar, drawer, switcher, screens', () => {
  beforeEach(() => {
    setWidth(SIZES.compact);
  });

  it('no rail, no tab strip; ☰ opens the drawer with Memory / Visuals / Settings; Settings opens a screen', async () => {
    const { getByRole, queryByRole, container } = renderUi(<App services={false} />);
    expect(queryByRole('navigation', { name: 'Main' })).toBeNull();
    expect(queryByRole('tablist')).toBeNull();
    fireEvent.click(getByRole('button', { name: 'Open navigation' }));
    const drawer = getByRole('dialog', { name: 'Navigation' });
    expect(within(drawer).getByRole('button', { name: 'Memory' })).toBeTruthy();
    // no soft keyboard on open: focus lands on the drawer heading, not the search field
    await waitFor(() => {
      expect(document.activeElement).toBe(within(drawer).getByRole('heading', { name: 'Archie' }));
    });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Settings' }));
    await waitFor(() => {
      expect(queryByRole('dialog', { name: 'Navigation' })).toBeNull();
    });
    expect(getByRole('region', { name: 'Settings' })).toBeTruthy();
    expect(container.querySelector('main')?.getAttribute('aria-hidden')).toBe('true');
    fireEvent.click(getByRole('button', { name: 'Back' }));
    expect(queryByRole('region', { name: 'Settings' })).toBeNull();
    expect(container.querySelector('main')?.getAttribute('aria-hidden')).toBeNull();
  });

  it('the title opens the session switcher: rows focus a session, × closes it (P-1), New actions', async () => {
    openSession({ kind: 'agent', localId: 'A1', focus: true, titleHint: 'First' });
    openSession({ kind: 'agent', localId: 'A2', focus: true, titleHint: 'Second' });
    const { getByRole, queryByRole } = renderUi(<App services={false} />);
    const title = getByRole('button', { name: /Switch session/ });
    expect(title.textContent).toMatch(/Second/);
    fireEvent.click(title);
    const sheet = getByRole('dialog', { name: 'Sessions' });
    fireEvent.click(within(sheet).getByRole('button', { name: /^First/ }));
    expect(tabsStore.getState().activeId).toBe('A1');
    expect(queryByRole('dialog', { name: 'Sessions' })).toBeNull();

    fireEvent.click(getByRole('button', { name: /Switch session/ }));
    fireEvent.click(within(getByRole('dialog', { name: 'Sessions' })).getByRole('button', { name: 'Close Second' }));
    await waitFor(() => {
      expect(closeCalls()).toEqual(['/api/sessions/A2/close']);
    });
    fireEvent.click(within(getByRole('dialog', { name: 'Sessions' })).getByRole('button', { name: 'New agent session' }));
    expect(tabsStore.getState().tabs).toHaveLength(2);
    expect(queryByRole('dialog', { name: 'Sessions' })).toBeNull();
  });

  it('a memory document opens as a detail screen over the Memory screen; Back returns to the list', async () => {
    act(() => {
      routeStore.setState({ route: { name: 'memory', path: 'assistant/voice.md' } });
    });
    const { getByRole, queryByRole } = renderUi(<App services={false} />);
    expect(getByRole('region', { name: 'voice.md' })).toBeTruthy();
    act(() => {
      window.history.back();
    });
    await waitFor(() => {
      expect(queryByRole('region', { name: 'voice.md' })).toBeNull();
    });
    expect(routeStore.getState().route).toEqual({ name: 'memory', path: null });
  });

  it('rotating to expanded turns a document screen into a tab', () => {
    act(() => {
      routeStore.setState({ route: { name: 'memory', path: 'assistant/voice.md' } });
    });
    renderUi(<App services={false} />);
    setWidth(SIZES.expanded);
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['memory:assistant/voice.md']);
    expect(routeStore.getState().route).toEqual({ name: 'memory', path: null });
  });
});
