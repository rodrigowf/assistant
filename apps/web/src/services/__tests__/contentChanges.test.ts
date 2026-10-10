/**
 * Live content changes (spec 12 §9.3, VZ-6): `visualization_changed` / `memory_changed` reach the
 * app at the channel level (Archie attached or not), bump per-path counters, refresh the lists
 * once per burst; a reopened socket catches up from the list's `modified`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { contentChangesStore, resetContentChanges, setCatalogItems } from '@/stores';
import { openSession, startServices } from '@/services';
import { FakeWebSocket, flushPromises, setupServices, teardownServices, type Harness } from './fakes';

const ORCH = '/api/orchestrator/chat';
let h: Harness;

beforeEach(() => {
  vi.useFakeTimers();
  h = setupServices();
  resetContentChanges();
});
afterEach(() => {
  teardownServices();
  vi.useRealTimers();
});

const viz = (path: string, modified: string) => ({ path, url: `/${path}`, title: path, created: modified, modified, size: 1 });

function openWatcher(): FakeWebSocket {
  startServices({ skipInitialSync: true });
  const ws = FakeWebSocket.last(ORCH);
  ws.open();
  return ws;
}

describe('visualization_changed', () => {
  it('bumps each listed visualization and refreshes the list once per burst', async () => {
    const ws = openWatcher();
    ws.emit({
      type: 'visualization_changed',
      visualizations: [{ path: 'dash/index.html', kind: 'modified' }],
      files: [{ path: 'dash/data.json', kind: 'modified' }],
    });
    ws.emit({ type: 'visualization_changed', visualizations: [{ path: 'dash/index.html', kind: 'modified' }, { path: 'old.html', kind: 'deleted' }] });
    const s = contentChangesStore.getState().visuals;
    expect(s['dash/index.html']?.version).toBe(2);
    expect(s['old.html']).toMatchObject({ version: 1, deleted: true });
    expect(h.fetch.calls('GET', '/api/visualizations')).toHaveLength(0);
    vi.advanceTimersByTime(500);
    await flushPromises();
    expect(h.fetch.calls('GET', '/api/visualizations')).toHaveLength(1);
  });

  it('is handled at the channel level while an Archie view is attached', () => {
    const ws = openWatcher();
    openSession({ kind: 'archie', localId: 'O1', focus: true });
    ws.emit({ type: 'session_started', session_id: 'O1' });
    ws.emit({ type: 'visualization_changed', visualizations: [{ path: 'a.html', kind: 'created' }] });
    expect(contentChangesStore.getState().visuals['a.html']?.version).toBe(1);
  });

  it('ignores malformed entries', () => {
    const ws = openWatcher();
    ws.emit({ type: 'visualization_changed', visualizations: [{ kind: 'modified' }, 'x', { path: 'ok.html' }] });
    expect(Object.keys(contentChangesStore.getState().visuals)).toEqual(['ok.html']);
  });
});

describe('memory_changed', () => {
  it('bumps the files; the tree refetches only when loaded and a file appeared or went away', async () => {
    const ws = openWatcher();
    h.fetch.on('GET', '/api/memory/tree', []);
    ws.emit({ type: 'memory_changed', changes: [{ path: 'projects/x.md', kind: 'modified' }] });
    expect(contentChangesStore.getState().memory['projects/x.md']?.version).toBe(1);
    vi.advanceTimersByTime(500);
    expect(h.fetch.calls('GET', '/api/memory/tree')).toHaveLength(0); // tree never loaded

    setCatalogItems('memory', []);
    ws.emit({ type: 'memory_changed', changes: [{ path: 'projects/x.md', kind: 'modified' }] });
    vi.advanceTimersByTime(500);
    expect(h.fetch.calls('GET', '/api/memory/tree')).toHaveLength(0); // edits do not change the tree
    ws.emit({ type: 'memory_changed', changes: [{ path: 'projects/new.md', kind: 'created' }] });
    vi.advanceTimersByTime(500);
    await flushPromises();
    expect(h.fetch.calls('GET', '/api/memory/tree')).toHaveLength(1);
  });
});

describe('VZ-6: reconnect fallback', () => {
  it('a reopened socket bumps visualizations whose modified moved, marks vanished ones deleted, and bumps the resync epoch', async () => {
    const ws = openWatcher();
    setCatalogItems('visuals', [viz('a.html', '2026-10-01T00:00:00+00:00'), viz('b.html', '2026-10-01T00:00:00+00:00'), viz('c.html', '2026-10-01T00:00:00+00:00')]);
    h.fetch.on('GET', '/api/visualizations', [viz('a.html', '2026-10-09T00:00:00+00:00'), viz('c.html', '2026-10-01T00:00:00+00:00')]);
    expect(contentChangesStore.getState().resyncEpoch).toBe(0);
    ws.drop();
    vi.advanceTimersByTime(5000);
    FakeWebSocket.last(ORCH).open();
    await flushPromises();
    const s = contentChangesStore.getState();
    expect(s.resyncEpoch).toBe(1);
    expect(s.visuals['a.html']).toMatchObject({ version: 1, deleted: false });
    expect(s.visuals['b.html']).toMatchObject({ version: 1, deleted: true });
    expect(s.visuals['c.html']).toBeUndefined();
  });

  it('the first open is not a reconnect', () => {
    openWatcher();
    expect(contentChangesStore.getState().resyncEpoch).toBe(0);
  });
});
