/**
 * Shell-level user actions (W-07). Every action here is a direct user action, so it may take
 * focus (`focus: true`); background opens from pool sync / watcher events never pass through
 * here (P-6, implemented in `@/services`).
 *
 * Closing (P-1, Rodrigo): an explicit close of a chat tab closes the session for everybody
 * (`closeTab` → `POST /api/sessions/{local_id}/close`). A running session asks first
 * (**[LOAD-BEARING]** inv02 §1.6, frontend/src/components/TabBar.tsx:74-91 ConfirmCloseModal),
 * and Archie always asks, because closing it stops the orchestrator on every device. Nothing
 * else in the shell (unmount, unload, window-class change, hiding) ever closes a session.
 */
import { requestNewArchie, requestOpenArchie } from '@/features/session-actions';
import { errorMessage, closeTab, getArchieRuntime, openSession, renameSession } from '@/services';
import { activateTab, catalogStore, findTab, openTab, showSnackbar, tabsStore, type Tab } from '@/stores';
import { navigate, type Route } from '../navigation/route';
import { liveSummary } from '../workspace/tabSummary';
import { closeShellOverlays, setShell, shellStore } from './shellState';

export type CloseConfirmKind = 'archie' | 'running';

/** Why closing `tab` needs a confirmation, or null to close at once. */
export function closeConfirmKind(tab: Tab): CloseConfirmKind | null {
  if (tab.kind === 'memory' || tab.kind === 'visual' || tab.readOnly) return null;
  const s = liveSummary(tab);
  if (s?.readOnly) return null;
  if (tab.kind === 'archie') return 'archie';
  return s?.busy ? 'running' : null;
}

/** × on a tab, middle-click, Ctrl+Alt+W, the switcher's ×, the session menu's Close. */
export function requestCloseTab(id: string): void {
  const tab = findTab(id);
  if (!tab) return;
  if (closeConfirmKind(tab)) {
    setShell({ confirmCloseId: id });
    return;
  }
  void closeTab(id);
}

export function confirmCloseTab(): void {
  const id = shellStore.getState().confirmCloseId;
  setShell({ confirmCloseId: null });
  if (id) void closeTab(id);
}

export function cancelCloseTab(): void {
  setShell({ confirmCloseId: null });
}

/**
 * The sdk id a rename goes to: the tab's own, else the session-list row matched by `local_id`
 * (the same match the derived title uses, inv02 §7 #9).
 */
export function renameTarget(tab: Tab): string | null {
  if (tab.sdkId) return tab.sdkId;
  const row = catalogStore.getState().sessions.items.find((s) => s.local_id === tab.id);
  return row ? row.session_id : null;
}

/** Rename needs the sdk id (inv02 §6.2: it used to fail silently before the first turn). */
export function requestRename(id: string): void {
  const tab = findTab(id);
  if (!tab || (tab.kind !== 'archie' && tab.kind !== 'agent')) return;
  if (tab.readOnly) return;
  if (!renameTarget(tab)) {
    showSnackbar('Rename is available after the first reply');
    return;
  }
  setShell({ renameId: id });
}

export async function commitRename(id: string, title: string): Promise<boolean> {
  const tab = findTab(id);
  const sdk = tab ? renameTarget(tab) : null;
  const t = title.trim();
  if (!sdk || !t) return false;
  try {
    await renameSession(sdk, t);
    return true;
  } catch (err) {
    showSnackbar(errorMessage(err), { tone: 'error' });
    return false;
  }
}

/**
 * New Archie conversation (Ctrl+Alt+N, the empty workspace, the switcher, the ＋ New menu).
 * Spec 12 §6.11 / inv02 F-25 via W-11: with Archie running anywhere the three-action dialog asks
 * (Open the running one / Stop it and start new / Cancel); otherwise a fresh `start{local_id}`.
 */
export function newArchie(): void {
  requestNewArchie(() => {
    closeShellOverlays();
    navigate({ name: 'workspace' });
  });
}

export function newAgent(): void {
  closeShellOverlays();
  navigate({ name: 'workspace' });
  openSession({ kind: 'agent', focus: true });
}

/**
 * Open a past conversation from the history list (interim; W-14's HistoryPane owns the list).
 * Archie (spec 12 §6.11, fixes inv02 §6.3 #11): the resume flow — focus it when it is the running
 * one, the conflict dialog when another one runs, else `start{local_id: uuid(), resume_sdk_id}`.
 * Agent sessions reopen live (`start{resume_sdk_id}`, §5.2).
 */
export function openFromHistory(sdkId: string, isArchie: boolean): void {
  closeShellOverlays();
  navigate({ name: 'workspace' });
  if (isArchie) {
    void requestOpenArchie({ mode: 'resume', sdkId });
    return;
  }
  openSession({ kind: 'agent', sdkId, focus: true });
}

/**
 * A tap on an "agent finished" notification (a user action, so FOCUS-1 allows the focus change):
 * the workspace with that session focused. Its open tab, else the live pool session by `localId`
 * (or, if it has left the pool, `start{local_id, resume_sdk_id}` reopens it from history).
 */
export function openFromNotification(localId: string, sdkId: string | null): void {
  closeShellOverlays();
  navigate({ name: 'workspace' });
  openSession({ kind: 'agent', localId, sdkId, focus: true });
}

/** Focus an open tab from a list (drawer, switcher, list pane). */
export function focusTab(id: string): void {
  closeShellOverlays();
  navigate({ name: 'workspace' });
  activateTab(id);
}

export type DocumentKind = 'memory' | 'visual';

/**
 * Open a memory document or a visual: a tab on medium/expanded (IA §9.2), a detail screen on
 * compact (spec 13 §4.2 table). W-14 calls this from its panes.
 */
export function openDocument(kind: DocumentKind, path: string, opts: { compact: boolean; url?: string; title?: string }): void {
  if (opts.compact) {
    setShell({ drawerOpen: false, switcherOpen: false });
    navigate(kind === 'memory' ? { name: 'memory', path } : { name: 'visuals', path });
    return;
  }
  closeShellOverlays();
  const id = `${kind === 'memory' ? 'memory' : 'viz'}:${path}`;
  openTab(
    {
      id,
      kind,
      path,
      ...(opts.url ? { url: opts.url } : {}),
      ...(opts.title ? { titleHint: opts.title } : {}),
    },
    { focus: true },
  );
}

/** Rail / drawer destinations. */
export function routeForDestination(d: 'chats' | 'memory' | 'visuals' | 'settings'): Route {
  switch (d) {
    case 'memory':
      return { name: 'memory', path: null };
    case 'visuals':
      return { name: 'visuals', path: null };
    case 'settings':
      return { name: 'settings', page: null };
    default:
      return { name: 'history' };
  }
}

export function hasLiveArchie(): boolean {
  return !!getArchieRuntime();
}

export function activeTab(): Tab | undefined {
  const s = tabsStore.getState();
  return s.tabs.find((t) => t.id === s.activeId);
}
