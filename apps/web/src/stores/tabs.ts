/**
 * Ordered tab list and the active tab (spec 13 §3.3, §4.2).
 *
 * - Chat tabs (`archie`, `agent`) are keyed by `local_id`; doc tabs by `memory:<path>` /
 *   `viz:<path>`.
 * - Persistence (`localStorage` `tabs:v1`): the order and the doc tabs only. Chat tabs are
 *   re-derived from `GET /api/sessions/pool/live` on load (inv02 F-23); the saved order places
 *   them back where they were.
 * - FOCUS-1 / P-6: `openTab(..., {focus: false})` adds a background tab with an unseen badge and
 *   never changes `activeId`. Only direct user actions pass `focus: true`.
 * - Titles are not stored here: they are derived from the session list (§7 #9, `useTabTitle`).
 */
import { createStore } from 'zustand/vanilla';
import { localStore, sessionStore as sessionStorageSafe } from '@/platform';
import type { Provider } from '@/protocol';

export type TabKind = 'archie' | 'agent' | 'memory' | 'visual';

export interface Tab {
  readonly id: string;
  readonly kind: TabKind;
  /** Chat tabs: the conversation's `local_id` (== id). */
  readonly localId?: string;
  /** Chat tabs: `resumeSdkId` (null until the first turn of a new agent session). */
  readonly sdkId?: string | null;
  readonly provider?: Provider | null;
  /** Doc tabs: memory path or visualization path; visual tabs also carry the URL. */
  readonly path?: string;
  readonly url?: string;
  /** Fallback title until the session list knows the session. */
  readonly titleHint?: string;
  /** Opened in the background and not looked at yet (P-6 badge). */
  readonly unseen: boolean;
  /** H-3 read-only view. */
  readonly readOnly?: boolean;
}

export interface TabsState {
  readonly tabs: readonly Tab[];
  readonly activeId: string | null;
}

export type NewTab = Omit<Tab, 'unseen'> & Partial<Pick<Tab, 'unseen'>>;

export const TABS_STORAGE_KEY = 'tabs:v1';
/** The active tab, per browser tab (spec 12 §8.2: sessionStorage). */
export const ACTIVE_TAB_STORAGE_KEY = 'tabs:active';

interface Persisted {
  order: string[];
  docs: Tab[];
  activeId: string | null;
}

export const tabsStore = createStore<TabsState>(() => ({ tabs: [], activeId: null }));

/** The order saved before the last reload: where re-derived chat tabs are put back. */
let restoreOrder: string[] = [];
/** Tabs restored after a reload that have not come back yet (no "unseen" badge for them). */
let pendingRestore = new Set<string>();
/** The active tab before the reload, re-activated when it comes back (a restore, not a focus steal). */
let pendingActive: string | null = null;
/** `hydrateTabs` found a saved state: this load is a restore. */
let restoring = false;

const isChat = (t: Tab): boolean => t.kind === 'archie' || t.kind === 'agent';

/** Archie is pinned first (spec 13 §4.2). */
function sortPinned(tabs: Tab[]): Tab[] {
  const archie = tabs.filter((t) => t.kind === 'archie');
  return archie.length ? archie.concat(tabs.filter((t) => t.kind !== 'archie')) : tabs;
}

function persist(): void {
  const s = tabsStore.getState();
  const docs = s.tabs.filter((t) => !isChat(t));
  const data: Persisted = { order: s.tabs.map((t) => t.id), docs, activeId: s.activeId };
  localStore.setJSON(TABS_STORAGE_KEY, data);
  if (s.activeId) sessionStorageSafe.set(ACTIVE_TAB_STORAGE_KEY, s.activeId);
  else sessionStorageSafe.remove(ACTIVE_TAB_STORAGE_KEY);
}

function set(tabs: Tab[], activeId: string | null): void {
  tabsStore.setState({ tabs: sortPinned(tabs), activeId });
  persist();
}

/**
 * Restore after a page reload: doc tabs now, chat tabs when pool sync re-derives them (they come
 * back at their saved position, **without** an unseen badge). The active tab comes from
 * sessionStorage (per browser tab), falling back to the saved one.
 */
export function hydrateTabs(): void {
  const data = localStore.getJSON<Persisted | null>(TABS_STORAGE_KEY, null);
  if (!data || !Array.isArray(data.order)) return;
  restoreOrder = data.order.filter((x) => typeof x === 'string');
  restoring = restoreOrder.length > 0;
  pendingRestore = new Set(restoreOrder);
  const docs = (Array.isArray(data.docs) ? data.docs : []).filter((t) => t && typeof t.id === 'string' && !isChat(t));
  for (const d of docs) pendingRestore.delete(d.id);
  const saved = sessionStorageSafe.get(ACTIVE_TAB_STORAGE_KEY) ?? (typeof data.activeId === 'string' ? data.activeId : null);
  const savedIsDoc = !!saved && docs.some((t) => t.id === saved);
  pendingActive = saved && !savedIsDoc && restoreOrder.indexOf(saved) >= 0 ? saved : null;
  const active = savedIsDoc ? saved : pendingActive ? null : (docs[0]?.id ?? null);
  tabsStore.setState({ tabs: sortPinned(docs.map((t) => ({ ...t, unseen: false }))), activeId: active });
}

/**
 * The restore window is over (the first pool sync finished): tabs that did not come back are
 * forgotten. If this load restored a saved state and nothing is active (the saved active tab
 * did not come back), the first tab becomes active. A first visit activates nothing (FOCUS-1).
 */
export function finishTabRestore(): void {
  const wanted = pendingActive;
  const wasRestoring = restoring;
  restoring = false;
  pendingRestore.clear();
  pendingActive = null;
  const s = tabsStore.getState();
  if (wasRestoring && s.activeId === null && s.tabs.length) activateTab(((s.tabs.find((t) => t.id === wanted) ?? s.tabs[0]) as Tab).id);
}

/** Insert position: where the saved order had it, else the end. */
function insertIndex(tabs: readonly Tab[], id: string): number {
  const pos = restoreOrder.indexOf(id);
  if (pos < 0) return tabs.length;
  for (let i = 0; i < tabs.length; i++) {
    const p = restoreOrder.indexOf((tabs[i] as Tab).id);
    if (p < 0 || p > pos) return i;
  }
  return tabs.length;
}

export interface OpenTabOptions {
  /** true only for direct user actions (FOCUS-1). */
  focus: boolean;
}

/** Open (or update) a tab. An existing tab is focused only when `focus` is true. */
export function openTab(tab: NewTab, opts: OpenTabOptions): void {
  const s = tabsStore.getState();
  const existing = s.tabs.find((t) => t.id === tab.id);
  const restored = !existing && pendingRestore.delete(tab.id);
  let tabs: Tab[];
  if (existing) {
    tabs = s.tabs.map((t) => (t.id === tab.id ? { ...t, ...tab, unseen: opts.focus ? false : t.unseen } : t));
  } else {
    const full: Tab = {
      ...tab,
      // unseen = new activity in the background; a tab restored after a reload has none yet
      unseen: restored ? false : (tab.unseen ?? !opts.focus),
    };
    tabs = s.tabs.slice();
    tabs.splice(insertIndex(tabs, tab.id), 0, full);
  }
  let activeId = opts.focus ? tab.id : s.activeId; // FOCUS-1: never activated by a server event
  if (restored && pendingActive === tab.id) {
    pendingActive = null;
    if (activeId === null) activeId = tab.id; // restoring the pre-reload view, not a focus change
  }
  set(opts.focus ? tabs.map((t) => (t.id === tab.id ? { ...t, unseen: false } : t)) : tabs, activeId);
}

/** A direct user action: make the tab active, clear its badge. */
export function activateTab(id: string): void {
  const s = tabsStore.getState();
  if (!s.tabs.some((t) => t.id === id)) return;
  set(
    s.tabs.map((t) => (t.id === id ? { ...t, unseen: false } : t)),
    id,
  );
}

/** Remove a tab from the strip (no network: services decide whether a session is closed). */
export function removeTab(id: string): void {
  const s = tabsStore.getState();
  const i = s.tabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  const tabs = s.tabs.filter((t) => t.id !== id);
  let activeId = s.activeId;
  if (activeId === id) activeId = (tabs[i] ?? tabs[i - 1] ?? null)?.id ?? null;
  set(tabs, activeId);
}

export function patchTab(id: string, partial: Partial<Omit<Tab, 'id'>>): void {
  const s = tabsStore.getState();
  if (!s.tabs.some((t) => t.id === id)) return;
  set(
    s.tabs.map((t) => (t.id === id ? { ...t, ...partial } : t)),
    s.activeId,
  );
}

/** Badge a background tab (new content while not active). */
export function markTabUnseen(id: string): void {
  const s = tabsStore.getState();
  const t = s.tabs.find((x) => x.id === id);
  if (!t || t.unseen || s.activeId === id) return;
  patchTab(id, { unseen: true });
}

/** ID-1: re-key a chat tab when the server adopted another `local_id`. */
export function rekeyTab(oldId: string, newId: string): void {
  const s = tabsStore.getState();
  if (oldId === newId || !s.tabs.some((t) => t.id === oldId)) return;
  set(
    s.tabs.map((t) => (t.id === oldId ? { ...t, id: newId, localId: newId } : t)),
    s.activeId === oldId ? newId : s.activeId,
  );
}

/** Drag to reorder; Archie stays pinned first. */
export function moveTab(id: string, toIndex: number): void {
  const s = tabsStore.getState();
  const from = s.tabs.findIndex((t) => t.id === id);
  if (from < 0) return;
  const tabs = s.tabs.slice();
  const [t] = tabs.splice(from, 1);
  tabs.splice(Math.max(0, Math.min(toIndex, tabs.length)), 0, t as Tab);
  set(tabs, s.activeId);
}

/** Switch to the next / previous tab (keyboard). */
export function cycleTab(delta: 1 | -1): void {
  const s = tabsStore.getState();
  if (!s.tabs.length) return;
  const i = Math.max(0, s.tabs.findIndex((t) => t.id === s.activeId));
  const next = s.tabs[(i + delta + s.tabs.length) % s.tabs.length] as Tab;
  activateTab(next.id);
}

export function findTab(id: string): Tab | undefined {
  return tabsStore.getState().tabs.find((t) => t.id === id);
}

/** Tests. */
export function resetTabs(): void {
  restoreOrder = [];
  pendingRestore = new Set();
  pendingActive = null;
  restoring = false;
  sessionStorageSafe.remove(ACTIVE_TAB_STORAGE_KEY);
  tabsStore.setState({ tabs: [], activeId: null });
  localStore.remove(TABS_STORAGE_KEY);
}
