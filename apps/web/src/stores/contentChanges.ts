/**
 * Live content changes (spec 12 §9.3): one counter per visualization / memory file, bumped when
 * the content watcher reports it changed (`visualization_changed`, `memory_changed`) or when the
 * reconnect fallback finds it newer. An open viewer reloads when its counter moves. Pure state:
 * `@/services` (`contentChanges.ts`) writes it.
 */
import { createStore } from 'zustand/vanilla';

export interface ContentStamp {
  /** Increments on every reported change. */
  readonly version: number;
  /** The last change deleted the file. */
  readonly deleted: boolean;
  /** `Date.now()` of the last change. */
  readonly at: number;
}

export interface ContentChangesState {
  /** Keyed by visualization path (relative to context/public/). */
  readonly visuals: Readonly<Record<string, ContentStamp>>;
  /** Keyed by memory path (relative to context/memory/). */
  readonly memory: Readonly<Record<string, ContentStamp>>;
  /** Bumped when the socket reopens after a drop: open documents refetch quietly (VZ-6). */
  readonly resyncEpoch: number;
}

export const contentChangesStore = createStore<ContentChangesState>(() => ({ visuals: {}, memory: {}, resyncEpoch: 0 }));

export type ContentArea = 'visuals' | 'memory';

/** Bump the counters of `paths` (`deleted` per path). No-op for an empty list. */
export function bumpContent(area: ContentArea, changes: readonly { path: string; deleted: boolean }[], now = Date.now()): void {
  if (!changes.length) return;
  contentChangesStore.setState((s) => {
    const next: Record<string, ContentStamp> = { ...s[area] };
    for (const c of changes) {
      const prev = next[c.path];
      next[c.path] = { version: (prev ? prev.version : 0) + 1, deleted: c.deleted, at: now };
    }
    return { [area]: next } as Partial<ContentChangesState>;
  });
}

export function contentVersion(area: ContentArea, path: string): number {
  return contentChangesStore.getState()[area][path]?.version ?? 0;
}

export function resetContentChanges(): void {
  contentChangesStore.setState({ visuals: {}, memory: {}, resyncEpoch: 0 });
}
