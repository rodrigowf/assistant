/**
 * Live reload of visualizations and memory documents (spec 12 §9.3).
 *
 * - `visualization_changed` / `memory_changed` arrive on the orchestrator socket (channel level,
 *   attached or not) and bump per-path counters in `contentChangesStore`; an open viewer reloads
 *   when its counter moves (the viewers debounce). The Visuals list refreshes once per burst, the
 *   memory tree only when it is loaded and a file appeared or went away.
 * - Fallback while the socket was down (VZ-6): on every reopen after the first, the list is
 *   refetched and each visualization whose `modified` moved is bumped; open memory documents
 *   refetch quietly (`resyncEpoch`) and show the cue only if their text differs.
 */
import type { ContentChange, MemoryChangedFrame, VisualizationChangedFrame } from '@/protocol';
import { bumpContent, catalogStore, contentChangesStore } from '@/stores';
import { refreshMemoryTree, refreshVisuals } from './catalogService';

/** One list refresh per burst of change frames. */
export const CONTENT_REFRESH_DEBOUNCE_MS = 400;

export type ContentFrame = VisualizationChangedFrame | MemoryChangedFrame;

function changesOf(list: unknown): ContentChange[] {
  if (!Array.isArray(list)) return [];
  const out: ContentChange[] = [];
  for (const c of list) {
    if (!c || typeof c !== 'object') continue;
    const { path, kind } = c as { path?: unknown; kind?: unknown };
    if (typeof path !== 'string' || !path) continue;
    out.push({ path, kind: kind === 'created' || kind === 'deleted' ? kind : 'modified' });
  }
  return out;
}

function later(fn: () => void): { schedule(): void; cancel(): void } {
  let t: ReturnType<typeof setTimeout> | null = null;
  return {
    schedule() {
      if (t) return;
      t = setTimeout(() => {
        t = null;
        fn();
      }, CONTENT_REFRESH_DEBOUNCE_MS);
    },
    cancel() {
      if (t) clearTimeout(t);
      t = null;
    },
  };
}

const visualsSoon = later(() => void refreshVisuals());
const memoryTreeSoon = later(() => void refreshMemoryTree());

export function onContentFrame(f: ContentFrame): void {
  if (f.type === 'visualization_changed') {
    const changes = changesOf(f.visualizations);
    bumpContent(
      'visuals',
      changes.map((c) => ({ path: c.path, deleted: c.kind === 'deleted' })),
    );
    // Titles, order and `modified` of the list; also a new page with no `visualizations` entry yet.
    visualsSoon.schedule();
    return;
  }
  const changes = changesOf(f.changes);
  bumpContent(
    'memory',
    changes.map((c) => ({ path: c.path, deleted: c.kind === 'deleted' })),
  );
  if (catalogStore.getState().memory.loadedAt > 0 && changes.some((c) => c.kind !== 'modified')) memoryTreeSoon.schedule();
}

/** The socket reopened after a drop: catch up on what its frames would have said (VZ-6). */
export async function resyncContent(): Promise<void> {
  const before = new Map<string, string>();
  for (const v of catalogStore.getState().visuals.items) before.set(v.path, v.modified);
  contentChangesStore.setState((s) => ({ resyncEpoch: s.resyncEpoch + 1 }));
  if (catalogStore.getState().memory.loadedAt > 0) void refreshMemoryTree();
  if (!before.size) return;
  await refreshVisuals();
  const moved: { path: string; deleted: boolean }[] = [];
  const after = new Set<string>();
  for (const v of catalogStore.getState().visuals.items) {
    after.add(v.path);
    const prev = before.get(v.path);
    if (prev !== undefined && prev !== v.modified) moved.push({ path: v.path, deleted: false });
  }
  before.forEach((_m, path) => {
    if (!after.has(path)) moved.push({ path, deleted: true });
  });
  bumpContent('visuals', moved);
}

export function cancelContentRefreshes(): void {
  visualsSoon.cancel();
  memoryTreeSoon.cancel();
}
