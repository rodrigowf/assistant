/**
 * Stores (spec 13 §3.3): frame coalescing (one notify per frame), frozen-while-hidden snapshots
 * with one catch-up on show, draft persistence, tabs (FOCUS-1, Archie pinned, persistence,
 * re-key), derived titles, prefs, snackbar, capabilities, registry, hooks.
 */
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initialConversation, reduceConversation, type Conversation } from '@/protocol';
import {
  activateTab,
  capabilitiesStore,
  currentProviderLabel,
  providerLabel,
  serverConfigStore,
  useProviderLabel,
  catalogStore,
  clearSessionRegistry,
  clearSnackbars,
  connectionStore,
  createFrameScheduler,
  createManualScheduler,
  createSessionStore,
  cycleTab,
  DEFAULT_PREFS,
  dismissSnackbar,
  draftKey,
  findTab,
  getFrameScheduler,
  getSessionEntry,
  hydrateTabs,
  listSessionIds,
  markBackendOffline,
  markBackendOnline,
  markTabUnseen,
  modelAcceptsAudio,
  moveTab,
  openTab,
  patchCapabilities,
  patchTab,
  prefsStore,
  registerSession,
  rekeySession,
  rekeyTab,
  reloadPrefs,
  removeSession,
  removeTab,
  resetCatalog,
  resetConnection,
  resetTabs,
  sanitizePrefs,
  setCatalogItems,
  setFrameScheduler,
  setPref,
  showSnackbar,
  snackbarStore,
  TABS_STORAGE_KEY,
  tabsStore,
  tabTitle,
  useCapabilities,
  useCatalog,
  useConnection,
  usePrefs,
  useServerConfig,
  useSession,
  useSnackbar,
  useTabs,
  useTabTitle,
  type ManualScheduler,
  type Tab,
} from '@/stores';

let sched: ManualScheduler;
beforeEach(() => {
  sched = createManualScheduler();
  setFrameScheduler(sched);
  resetTabs();
  resetCatalog();
  resetConnection();
  clearSnackbars();
  window.sessionStorage.clear();
  window.localStorage.clear();
});
afterEach(() => {
  clearSessionRegistry();
  setFrameScheduler(null);
  vi.useRealTimers();
});

function conv(localId = 'L1'): Conversation {
  return initialConversation({ localId, kind: 'agent', provider: 'claude', subscribed: true });
}

function grow(c: Conversation, n: number): Conversation[] {
  const out: Conversation[] = [];
  let s = reduceConversation(c, { type: 'local_send', text: 'go' });
  for (let i = 0; i < n; i++) {
    s = reduceConversation(s, { type: 'frame', frame: { type: 'text_delta', text: `${i} ` } });
    out.push(s);
  }
  return out;
}

describe('session store', () => {
  it('coalesces: 50 deltas in one frame → one notify with the latest state', () => {
    const h = createSessionStore({ localId: 'L1', conv: conv() });
    const notify = vi.fn();
    h.store.subscribe(notify);
    const states = grow(conv(), 50);
    for (const s of states) h.setConv(s);
    expect(notify).not.toHaveBeenCalled();
    expect(sched.pending).toBe(1);
    sched.flush();
    expect(notify).toHaveBeenCalledTimes(1);
    expect(h.store.getState().conv).toBe(states[49]);
  });

  it('frozen while hidden: no notify at all; one catch-up render on show', () => {
    const h = createSessionStore({ localId: 'L1', conv: conv() });
    const notify = vi.fn();
    h.store.subscribe(notify);
    h.setHidden(true);
    notify.mockClear();
    const before = h.store.getState().conv;
    const states = grow(conv(), 30);
    for (const s of states) h.setConv(s);
    sched.flush();
    expect(notify).not.toHaveBeenCalled();
    expect(h.store.getState().conv).toBe(before);
    h.setHidden(false);
    expect(notify).toHaveBeenCalledTimes(1);
    expect(h.store.getState()).toMatchObject({ hidden: false, conv: states[29] });
    h.setHidden(false); // no-op
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('hiding cancels a scheduled publish; flush publishes now', () => {
    const h = createSessionStore({ localId: 'L1', conv: conv() });
    const [a] = grow(conv(), 1);
    h.setConv(a as Conversation);
    h.setHidden(true);
    sched.flush();
    expect(h.store.getState().conv).not.toBe(a);
    h.setHidden(false);
    expect(h.store.getState().conv).toBe(a);
    const [b] = grow(conv(), 2);
    h.setConv(b as Conversation);
    h.flush();
    expect(h.store.getState().conv).toBe(b);
  });

  it('draft persists in sessionStorage (debounced), moves on re-key, is forgotten on explicit close', () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem(draftKey('L1'), 'restored');
    const h = createSessionStore({ localId: 'L1', conv: conv() });
    expect(h.store.getState().draft).toBe('restored');
    h.setDraft('typing…');
    expect(window.sessionStorage.getItem('draft:L1')).toBe('restored');
    vi.advanceTimersByTime(250);
    expect(window.sessionStorage.getItem('draft:L1')).toBe('typing…');
    h.rekey('L2');
    expect(window.sessionStorage.getItem('draft:L1')).toBeNull();
    expect(window.sessionStorage.getItem('draft:L2')).toBe('typing…');
    h.setDraft('');
    vi.advanceTimersByTime(250);
    expect(window.sessionStorage.getItem('draft:L2')).toBeNull();
    h.setDraft('pending');
    h.dispose(false); // teardown flushes the pending draft
    expect(window.sessionStorage.getItem('draft:L2')).toBe('pending');
    h.setDraft('ignored after dispose');
    h.setConv(conv());
    h.patch({ loadingOlder: true });
    expect(h.store.getState().draft).toBe('pending');
    const h2 = createSessionStore({ localId: 'L3', conv: conv() });
    h2.setDraft('x');
    h2.dispose(true);
    vi.advanceTimersByTime(500);
    expect(window.sessionStorage.getItem('draft:L3')).toBeNull();
  });

  it('frame schedulers: rAF on main, 100 ms when low-end', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const low = createFrameScheduler(() => true);
    low.schedule(fn);
    vi.advanceTimersByTime(99);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
    const cancel = low.schedule(fn);
    cancel();
    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(1);
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => {
      cb(0);
      return 1;
    });
    createFrameScheduler(() => false).schedule(fn)();
    expect(fn).toHaveBeenCalledTimes(2);
    raf.mockRestore();
    setFrameScheduler(null);
    expect(getFrameScheduler()).toBeDefined();
  });
});

describe('registry', () => {
  it('registers, re-keys, removes and disposes', () => {
    const handle = createSessionStore({ localId: 'A', conv: conv('A') });
    const dispose = vi.fn();
    registerSession('A', { handle, runtime: { localId: 'A', dispose } });
    expect(listSessionIds()).toEqual(['A']);
    rekeySession('A', 'B');
    rekeySession('missing', 'C');
    expect(getSessionEntry('B')?.handle.store.getState().localId).toBe('B');
    const other = vi.fn();
    registerSession('B', { handle, runtime: { localId: 'B', dispose: other } }); // replacing disposes the previous
    expect(dispose).toHaveBeenCalledTimes(1);
    removeSession('B');
    removeSession('B');
    expect(other).toHaveBeenCalledTimes(1);
    expect(listSessionIds()).toEqual([]);
  });
});

describe('tabs', () => {
  const agent = (id: string, extra: Partial<Tab> = {}) => ({ id, kind: 'agent' as const, localId: id, ...extra });

  it('FOCUS-1: background opens never change the active tab; user activation clears the badge', () => {
    openTab(agent('A'), { focus: true });
    openTab(agent('B'), { focus: false });
    expect(tabsStore.getState().activeId).toBe('A');
    expect(findTab('B')).toMatchObject({ unseen: true });
    openTab(agent('B'), { focus: false }); // updating keeps the badge and the focus
    expect(tabsStore.getState().activeId).toBe('A');
    activateTab('B');
    expect(findTab('B')).toMatchObject({ unseen: false });
    activateTab('nope');
    markTabUnseen('B'); // active: no badge
    markTabUnseen('A');
    expect(findTab('A')?.unseen).toBe(true);
    openTab(agent('A'), { focus: true });
    expect(findTab('A')?.unseen).toBe(false);
  });

  it('Archie is pinned first; reorder, cycle, close picks a neighbour', () => {
    openTab(agent('A'), { focus: true });
    openTab(agent('B'), { focus: true });
    openTab({ id: 'O', kind: 'archie', localId: 'O' }, { focus: false });
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['O', 'A', 'B']);
    moveTab('B', 0);
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['O', 'B', 'A']);
    moveTab('zz', 0);
    cycleTab(1);
    expect(tabsStore.getState().activeId).toBe('A');
    cycleTab(1);
    expect(tabsStore.getState().activeId).toBe('O');
    cycleTab(-1);
    expect(tabsStore.getState().activeId).toBe('A');
    removeTab('A');
    expect(tabsStore.getState().activeId).toBe('B');
    removeTab('missing');
    rekeyTab('B', 'B2');
    expect(tabsStore.getState().activeId).toBe('B2');
    patchTab('B2', { sdkId: 's' });
    patchTab('missing', { sdkId: 's' });
    expect(findTab('B2')?.sdkId).toBe('s');
    resetTabs();
    cycleTab(1);
    expect(tabsStore.getState().activeId).toBeNull();
  });

  it('persists order + doc tabs only; chat tabs come back at their saved position', () => {
    openTab(agent('A'), { focus: true });
    openTab({ id: 'memory:notes/a.md', kind: 'memory', path: 'notes/a.md' }, { focus: true });
    openTab(agent('B'), { focus: false });
    const saved = JSON.parse(window.localStorage.getItem(TABS_STORAGE_KEY) ?? '{}') as { order: string[]; docs: Tab[] };
    expect(saved.order).toEqual(['A', 'memory:notes/a.md', 'B']);
    expect(saved.docs.map((t) => t.id)).toEqual(['memory:notes/a.md']);
    tabsStore.setState({ tabs: [], activeId: null }); // "reload"
    hydrateTabs();
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['memory:notes/a.md']);
    expect(tabsStore.getState().activeId).toBe('memory:notes/a.md');
    openTab(agent('B'), { focus: false }); // re-derived from pool/live
    openTab(agent('A'), { focus: false });
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['A', 'memory:notes/a.md', 'B']);
    window.localStorage.setItem(TABS_STORAGE_KEY, '{"order":5}');
    hydrateTabs();
  });

  it('titles are derived from the session list (sdk id, then local id, then hint/placeholder)', () => {
    const a: Tab = { id: 'L1', kind: 'agent', localId: 'L1', sdkId: 'sdk-1', unseen: false };
    expect(tabTitle(a)).toBe('New agent session');
    setCatalogItems('sessions', [
      { session_id: 'sdk-1', title: 'By sdk', local_id: null } as never,
      { session_id: 'x', title: 'By local', local_id: 'L2' } as never,
    ]);
    expect(tabTitle(a)).toBe('By sdk');
    expect(tabTitle({ ...a, id: 'L2', localId: 'L2', sdkId: null })).toBe('By local');
    expect(tabTitle({ ...a, kind: 'archie', sdkId: null, localId: 'zz' })).toBe('Archie');
    expect(tabTitle({ ...a, kind: 'memory', path: 'notes/recipes.md' })).toBe('recipes');
    setCatalogItems('visuals', [{ path: 'solar/index.html', title: 'Solar', url: '', created: '', modified: '', size: 0 }]);
    expect(tabTitle({ ...a, kind: 'visual', path: 'solar/index.html' })).toBe('Solar');
    expect(tabTitle({ ...a, kind: 'visual', path: 'other/index.html' })).toBe('other');
  });
});

describe('prefs, snackbar, connection, capabilities', () => {
  it('prefs sanitize, persist and apply', () => {
    expect(sanitizePrefs({ theme: 'neon', textSize: 'large', syntaxHighlighting: 'yes' })).toMatchObject({ theme: 'dark', textSize: 'large', syntaxHighlighting: true });
    setPref('theme', 'dark');
    setPref('theme', 'dark');
    setPref('reduceMotion', true);
    setPref('remoteLogging', true);
    expect(JSON.parse(window.localStorage.getItem('prefs:v1') ?? '{}')).toMatchObject({ theme: 'dark', reduceMotion: true });
    reloadPrefs();
    expect(prefsStore.getState()).toMatchObject({ theme: 'dark', remoteLogging: true });
    setPref('remoteLogging', false);
    setPref('reduceMotion', false);
    expect(DEFAULT_PREFS.toolStepGrouping).toBe(true);
  });

  it('snackbar queue is capped; actions extend the duration', () => {
    const id = showSnackbar('one');
    for (let i = 0; i < 6; i++) showSnackbar(`n${i}`, { action: { label: 'Open', run: () => undefined } });
    expect(snackbarStore.getState().queue).toHaveLength(5);
    expect(snackbarStore.getState().queue[0]?.durationMs).toBe(8000);
    dismissSnackbar(id);
    dismissSnackbar(snackbarStore.getState().queue[0]?.id ?? 0);
    expect(snackbarStore.getState().queue).toHaveLength(4);
  });

  it('connection state transitions', () => {
    markBackendOffline('down', 5);
    markBackendOffline('down', 9);
    expect(connectionStore.getState()).toEqual({ backend: 'offline', lastError: 'down', since: 5 });
    markBackendOnline(10);
    expect(connectionStore.getState()).toEqual({ backend: 'online', lastError: null, since: 10 });
  });

  it('P-5: audio follows the current model', () => {
    expect(modelAcceptsAudio(null)).toBe(false);
    expect(modelAcceptsAudio({ supports_audio: true })).toBe(true);
    expect(modelAcceptsAudio({ model_info: { supports_audio: false } })).toBe(false);
    patchCapabilities({ audioCapableModels: ['gpt-audio-mini'] });
    expect(modelAcceptsAudio({ model: 'gpt-audio-mini' })).toBe(true);
    expect(modelAcceptsAudio({ model: 'claude' })).toBe(false);
    expect(capabilitiesStore.getState().client).not.toBeNull();
  });
});

describe('hooks', () => {
  it('useSession re-renders once per frame and not while hidden; other hooks read their stores', () => {
    const renders: string[] = [];
    function View({ id }: { id: string }) {
      const status = useSession(id, (s) => s.conv.entries.length);
      const tabs = useTabs((s) => s.tabs.length);
      const title = useTabTitle(id);
      useCatalog((s) => s.sessions.loading);
      useServerConfig((s) => s.config);
      usePrefs((s) => s.theme);
      useCapabilities((s) => s.castAvailable);
      useSnackbar((s) => s.queue.length);
      useConnection((s) => s.backend);
      renders.push(`${status}|${tabs}|${title}`);
      return null;
    }
    render(<View id="L1" />);
    expect(renders.at(-1)).toBe('0|0|');
    const handle = createSessionStore({ localId: 'L1', conv: conv() });
    act(() => {
      registerSession('L1', { handle, runtime: { localId: 'L1', dispose: () => undefined } });
      openTab({ id: 'L1', kind: 'agent', localId: 'L1' }, { focus: true });
    });
    const n = renders.length;
    const states = grow(conv(), 20);
    act(() => {
      for (const s of states) handle.setConv(s);
      sched.flush();
    });
    expect(renders.length - n).toBe(1);
    expect(renders.at(-1)).toBe('2|1|New agent session');
    act(() => handle.setHidden(true));
    const m = renders.length;
    act(() => {
      for (const s of grow(conv(), 40)) handle.setConv(s);
      sched.flush();
    });
    expect(renders.length).toBe(m);
    expect(catalogStore.getState().sessions.items).toEqual([]);
  });
});

describe('provider labels', () => {
  it('short tags for the shipped harnesses, registry labels for new ones, the id as last resort', () => {
    expect(providerLabel('claude')).toBe('Claude');
    expect(providerLabel('codex')).toBe('Codex');
    expect(providerLabel('modelstudio')).toBe('Model Studio');
    expect(providerLabel('aider', [{ id: 'aider', label: 'Aider CLI' }])).toBe('Aider CLI');
    expect(providerLabel('aider', null, [{ id: 'aider', label: 'Aider (providers)' }])).toBe('Aider (providers)');
    expect(providerLabel('aider', [{ id: 'aider', label: '' }])).toBe('aider');
    expect(providerLabel('toString')).toBe('toString');
    expect(providerLabel('')).toBeNull();
    expect(providerLabel(null)).toBeNull();
    serverConfigStore.setState({ harnesses: [{ id: 'aider', label: 'Aider CLI', catalog: null }] });
    expect(currentProviderLabel('aider')).toBe('Aider CLI');
  });

  it('useProviderLabel follows the registry in the store', () => {
    const seen: (string | null)[] = [];
    function View() {
      seen.push(useProviderLabel()('aider'));
      return null;
    }
    serverConfigStore.setState({ harnesses: null, providers: null });
    render(<View />);
    expect(seen.at(-1)).toBe('aider');
    act(() => serverConfigStore.setState({ harnesses: [{ id: 'aider', label: 'Aider CLI', catalog: null }] }));
    expect(seen.at(-1)).toBe('Aider CLI');
  });
});
