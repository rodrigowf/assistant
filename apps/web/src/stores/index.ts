/**
 * `@/stores` (W-06): Zustand 5 vanilla stores read through typed hooks (spec 13 §3.3, §3.9).
 * Services update the stores without React; components select narrow slices. Selectors that
 * return new objects or arrays must be wrapped in `useShallow` (re-exported here).
 */
import { useCallback } from 'react';
import { useStore } from 'zustand';
import { createStore } from 'zustand/vanilla';
import { deriveTitle, initialConversation } from '@/protocol';
import { capabilitiesStore, type CapabilitiesState } from './capabilities';
import { catalogStore, type CatalogState } from './catalog';
import { connectionStore, type ConnectionState } from './connection';
import { contentChangesStore, type ContentArea, type ContentStamp } from './contentChanges';
import { prefsStore, type Prefs } from './prefs';
import { providerLabel } from './providerLabels';
import { serverConfigStore, type ServerConfigState } from './serverConfig';
import { getSessionEntry, sessionRegistryVersion } from './sessionRegistry';
import type { SessionState } from './sessionStore';
import { snackbarStore, type SnackbarState } from './snackbar';
import { tabsStore, type Tab, type TabsState } from './tabs';

export { useShallow } from 'zustand/react/shallow';

export * from './capabilities';
export * from './catalog';
export * from './connection';
export * from './contentChanges';
export * from './liveStatus';
export * from './prefs';
export * from './providerLabels';
export * from './scheduler';
export * from './serverConfig';
export * from './sessionRegistry';
export * from './sessionStore';
export * from './snackbar';
export * from './tabs';

/** Returned for a `local_id` with no open session (never mutated). */
const placeholderSession = createStore<SessionState>(() => ({
  localId: '',
  conv: initialConversation({ localId: '', kind: 'agent' }),
  draft: '',
  scrollAnchor: null,
  loadingOlder: false,
  historyError: null,
  readOnly: true,
  modelInfo: null,
  models: null,
  hidden: true,
}));

/** Re-renders when sessions are added, removed or re-keyed. */
export function useSessionRegistryVersion(): number {
  return useStore(sessionRegistryVersion, (s) => s.version);
}

/** A narrow slice of one session (`useSession(id, s => s.conv.status)`). */
export function useSession<T>(localId: string, selector: (s: SessionState) => T): T {
  useSessionRegistryVersion(); // pick up the store once the session is registered
  const store = getSessionEntry(localId)?.handle.store ?? placeholderSession;
  return useStore(store, selector);
}

export function useTabs<T>(selector: (s: TabsState) => T): T {
  return useStore(tabsStore, selector);
}

export function useCatalog<T>(selector: (s: CatalogState) => T): T {
  return useStore(catalogStore, selector);
}

/** The change stamp of one visualization / memory file (`null` until it changes). */
export function useContentStamp(area: ContentArea, path: string): ContentStamp | null {
  return useStore(contentChangesStore, (s) => s[area][path] ?? null);
}

/** Bumped when the watcher socket reopens after a drop (spec 12 VZ-6). */
export function useContentResyncEpoch(): number {
  return useStore(contentChangesStore, (s) => s.resyncEpoch);
}

export function useServerConfig<T>(selector: (s: ServerConfigState) => T): T {
  return useStore(serverConfigStore, selector);
}

/** `providerLabel` bound to the harness registry in the store (re-renders when it loads). */
export function useProviderLabel(): (id: string | null | undefined) => string | null {
  const harnesses = useServerConfig((s) => s.harnesses);
  const providers = useServerConfig((s) => s.providers);
  return useCallback((id: string | null | undefined) => providerLabel(id, harnesses, providers), [harnesses, providers]);
}

export function usePrefs<T>(selector: (s: Prefs) => T): T {
  return useStore(prefsStore, selector);
}

export function useCapabilities<T>(selector: (s: CapabilitiesState) => T): T {
  return useStore(capabilitiesStore, selector);
}

export function useSnackbar<T>(selector: (s: SnackbarState) => T): T {
  return useStore(snackbarStore, selector);
}

export function useConnection<T>(selector: (s: ConnectionState) => T): T {
  return useStore(connectionStore, selector);
}

const KIND_PLACEHOLDER: Record<Tab['kind'], string> = {
  archie: 'Archie',
  agent: 'New agent session',
  memory: 'Memory',
  visual: 'Visual',
};

function basename(path: string): string {
  const parts = path.split('/').filter(Boolean);
  const last = parts[parts.length - 1] ?? path;
  if (last === 'index.html' && parts.length > 1) return parts[parts.length - 2] as string;
  return last.replace(/\.(md|html)$/i, '');
}

/**
 * Tab title, derived from the session list (**[LOAD-BEARING]** inv02 §7 #9, MC-2;
 * frontend/src/components/TabBar.tsx titles from the sidebar list): sdk id match, then local id,
 * then the tab's hint / a placeholder. Doc tabs: the visual's list title or the file name.
 */
export function tabTitle(tab: Tab, catalog: CatalogState = catalogStore.getState()): string {
  if (tab.kind === 'memory') return tab.titleHint ?? basename(tab.path ?? '');
  if (tab.kind === 'visual') {
    const v = catalog.visuals.items.find((x) => x.path === tab.path);
    return v?.title ?? tab.titleHint ?? basename(tab.path ?? '');
  }
  const placeholder = tab.titleHint ?? KIND_PLACEHOLDER[tab.kind];
  return deriveTitle(catalog.sessions.items, { sdkId: tab.sdkId ?? null, localId: tab.localId ?? tab.id }, placeholder);
}

export function useTabTitle(tabId: string): string {
  const tab = useTabs((s) => s.tabs.find((t) => t.id === tabId));
  const sessions = useStore(catalogStore, (s) => s.sessions);
  const visuals = useStore(catalogStore, (s) => s.visuals);
  if (!tab) return '';
  return tabTitle(tab, { ...catalogStore.getState(), sessions, visuals });
}

