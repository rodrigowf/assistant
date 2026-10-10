/**
 * SLOT REGISTRY (W-07). Every destination and workspace part that a later work package builds is
 * imported by the shell **only from this file**, under the entry-point names of spec 13 §3.9.
 * Each export below is a clearly-marked PLACEHOLDER: when the owning package lands, replace the
 * placeholder with a re-export from its feature entry, e.g.
 *
 *   export { ConversationPanel } from '@/features/conversation';   // W-09
 *
 * and delete the placeholder. The props are the §3.9 contracts, so the shell does not change.
 *
 * | Slot                 | Owner | Used by                                       |
 * |----------------------|-------|-----------------------------------------------|
 * | ConversationPanel    | W-09  | PanelHost (chat tabs); composes the composer  | wired
 * | Composer             | W-11  | inside ConversationPanel                       | wired
 * | SessionMenu          | W-11  | workspace top bar / compact app bar (⋮)        | wired
 * | NewMenuItems         | W-11  | rail "＋ New" menu                             | wired
 * | HistoryPane          | W-14  | list pane (Chats), drawer body, History screen | wired
 * | MemoryPane           | W-14  | list pane (Memory), Memory screen              | wired
 * | MemoryDocument       | W-14  | PanelHost (memory tabs), compact doc screen    | wired
 * | VisualsPane          | W-14  | list pane (Visuals), Visuals screen            | wired
 * | VisualViewer         | W-14  | PanelHost (visual tabs), compact doc screen    | wired
 * | SettingsScreen       | W-13  | Settings screen                                | wired
 * | AuthGate             | W-13  | App root                                       | wired
 * | VoiceAction          | W-12  | compact app bar trailing voice/speaker state   | wired
 * | VoiceSlot            | W-12  | composer slot: VoiceDock / Active elsewhere    | wired
 * | VoiceOverlay         | W-12  | VoiceOverlayHost: floating controls in a call  | wired
 */
import { useCallback, useMemo, type ReactNode } from 'react';
import { Composer, startVoice } from '@/features/composer';
import { ConversationPanel as ConversationView } from '@/features/conversation';
import { AuthGate as AuthGateView } from '@/features/auth';
import { NewMenu, SessionActionsHost, SessionMenu as SessionMenuView } from '@/features/session-actions';
import { AppearanceEffects, openSessionSettings, SessionSettingsHost, SettingsScreen as SettingsView, type SettingsPageId } from '@/features/settings';
import { installVoice, VoiceSlot } from '@/features/voice';
import { HistoryPane as HistoryView, type HistoryVariant, type OpenNowItem } from '@/features/history';
import { MemoryDocument as MemoryDocumentView, MemoryPane as MemoryView } from '@/features/memory';
import { VisualsPane as VisualsView } from '@/features/visuals';
import { useProviderLabel, useTabs } from '@/stores';
import { navigate } from '../navigation/route';
import { ArchieMark } from '../shell/ArchieMark';
import { focusTab, newAgent, openDocument, openFromHistory, requestCloseTab, requestRename } from '../shell/actions';
import { closeShellOverlays } from '../shell/shellState';
import { useWindowClass } from '../useWindowClass';
import { useTitledTabs } from '../workspace/SessionTabStrip';
import { StatusGlyph, TabLeading } from '../workspace/TabParts';
import { useTabSummaries } from '../workspace/tabSummary';

export { Composer } from '@/features/composer';
export { VoiceAction, VoiceOverlay, preloadVoiceOverlay, useLiveVoiceId, VOICE_OVERLAY_SELECTOR } from '@/features/voice';

// W-12: voice controllers follow the Archie runtimes; the composer's Voice button starts voice.
installVoice();
export { SlotNote } from './SlotNote';

/* ------------------------------------------------------------- W-09 + W-11 */

/** W-09's conversation view with W-11's composer in its slot (W-12's VoiceDock takes it over while voice is active). */
export function ConversationPanel({ localId, hidden }: { localId: string; hidden: boolean }) {
  const composer = useMemo(() => <VoiceSlot localId={localId} composer={<Composer localId={localId} />} />, [localId]);
  const onStartVoice = useMemo(() => () => startVoice(localId), [localId]);
  return <ConversationView localId={localId} hidden={hidden} composer={composer} onStartVoice={onStartVoice} />;
}

/** W-11 ⋮ session menu; Rename and Close run the shell's flows (W-07); Session settings opens W-13's sheet. */
export function SessionMenu({ localId }: { localId: string }) {
  return <SessionMenuView localId={localId} onRename={requestRename} onClose={requestCloseTab} onSessionSettings={openSessionSettings} />;
}

/** W-11 "＋ New" items: New Archie asks first when Archie is already running (§6.11, F-25). */
export function NewMenuItems() {
  return (
    <NewMenu
      archieMark={<ArchieMark size={20} />}
      prepare={() => {
        closeShellOverlays();
        navigate({ name: 'workspace' });
      }}
      onNewAgent={newAgent}
    />
  );
}

/* ------------------------------------------------------------- W-14 */

/**
 * W-14 history list. The shell supplies the open conversations exactly as the tab strip shows
 * them (title, kind icon + unseen badge, live status) and the open / focus flows (W-07).
 */
export function HistoryPane({ variant = 'pane' }: { variant?: HistoryVariant }) {
  const { tabs, titles } = useTitledTabs();
  const activeId = useTabs((s) => s.activeId);
  const open = useMemo(() => tabs.filter((t) => t.kind === 'archie' || t.kind === 'agent'), [tabs]);
  const summaries = useTabSummaries(open);
  const providerLabelOf = useProviderLabel();
  const openNow = useMemo<OpenNowItem[]>(
    () =>
      open.map((t) => {
        const sum = summaries[t.id] ?? null;
        return {
          id: t.id,
          title: titles[t.id] ?? '',
          sdkId: t.sdkId ?? null,
          isArchie: t.kind === 'archie',
          providerLabel: t.kind === 'agent' ? providerLabelOf(t.provider) : null,
          statusLabel: sum ? sum.label : null,
          leading: <TabLeading tab={t} size={t.kind === 'archie' ? 24 : 20} unseen={t.unseen && t.id !== activeId} />,
          status: <StatusGlyph summary={sum} />,
        };
      }),
    [open, summaries, titles, activeId, providerLabelOf],
  );
  return (
    <HistoryView
      variant={variant}
      openNow={openNow}
      activeId={activeId}
      onFocusOpen={focusTab}
      onOpenSession={(s) => {
        openFromHistory(s.session_id, s.is_orchestrator);
      }}
      archieMark={<ArchieMark size={20} />}
    />
  );
}

/** W-14 memory tree: files open as tabs on medium/expanded, as a document screen on compact (IA §9.2). */
export function MemoryPane() {
  const wc = useWindowClass();
  return (
    <MemoryView
      onOpen={(path, name) => {
        openDocument('memory', path, { compact: wc === 'compact', title: name });
      }}
    />
  );
}

/** W-14 visuals list (same open rule as Memory). */
export function VisualsPane() {
  const wc = useWindowClass();
  return (
    <VisualsView
      onOpen={(v) => {
        openDocument('visual', v.path, { compact: wc === 'compact', url: v.url, title: v.title });
      }}
    />
  );
}

/** W-14 memory document; a relative link opens the other file the same way the tree does. */
export function MemoryDocument({ path, hidden }: { path: string; hidden: boolean }) {
  const wc = useWindowClass();
  const onOpenLink = useCallback(
    (target: string) => {
      openDocument('memory', target, { compact: wc === 'compact', title: target.split('/').pop() ?? target });
    },
    [wc],
  );
  return <MemoryDocumentView path={path} hidden={hidden} onOpenLink={onOpenLink} />;
}

/** W-14 visual viewer (sandboxed iframe that stays mounted while hidden; Show on TV; ⋮). */
export { VisualViewer } from '@/features/visuals';

/* ------------------------------------------------------------- W-13 */

const openSettingsPage = (page: SettingsPageId | null): void => {
  navigate({ name: 'settings', page });
};

/** W-13 settings (IA §7): two panes on Expanded, pushed pages on Compact / Medium; pages route via the hash. */
export function SettingsScreen({ page }: { page?: string | null }) {
  return <SettingsView page={page ?? null} onNavigate={openSettingsPage} />;
}

/**
 * W-13 `AuthGate`: startup sign-in check, sign-in screen over the app when the backend's Claude
 * CLI is signed out. Also mounts W-11's `SessionActionsHost` (session dialogs, busy overlay, F2),
 * W-13's session settings sheet host and the appearance effects (text size) beside the app.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  return (
    <AuthGateView>
      <AppearanceEffects />
      {children}
      <SessionActionsHost onRename={requestRename} />
      <SessionSettingsHost
        onOpenSettings={(p) => {
          closeShellOverlays();
          openSettingsPage(p);
        }}
      />
    </AuthGateView>
  );
}
