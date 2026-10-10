/**
 * Where the floating voice controls show (W-12 `VoiceOverlay`, spec 13 §4.2): while this device
 * has a voice call on an Archie conversation, on every view except that conversation itself —
 * another tab (agent session, memory document, visual, a read-only Archie view) or a screen over
 * the workspace (Settings; History, Memory, Visuals and the document screens on compact). On the
 * Archie conversation the dock in its composer slot is the control, so nothing floats there.
 *
 * The overlay floats in the workspace's box (`<main>`), above the active panel's composer
 * (`[data-conversation-dock]`) when one is visible. Its state text focuses the Archie tab.
 * "Active elsewhere" (voice on another device) never floats: it is not this device's microphone.
 */
import { useCallback, useEffect, useMemo, type RefObject } from 'react';
import { useTabs } from '@/stores';
import { preloadVoiceOverlay, useLiveVoiceId, VoiceOverlay } from '../slots';
import type { WindowClass } from '../useWindowClass';
import { focusTab } from './actions';

/** Should the overlay show? `liveId`: the Archie tab with voice here; `covered`: a screen is over the workspace. */
export function voiceOverlayVisible(liveId: string | null, activeId: string | null, covered: boolean): boolean {
  return liveId !== null && (covered || liveId !== activeId);
}

/** The visible composer area of the active conversation panel, if any. */
export function visibleConversationDock(workspace: HTMLElement | null): Element | null {
  if (!workspace) return null;
  const docks = workspace.querySelectorAll('[data-conversation-dock]');
  for (let i = 0; i < docks.length; i += 1) {
    const el = docks[i] as Element;
    if (el.getClientRects().length > 0 && !el.closest('[hidden]')) return el;
  }
  return null;
}

export interface VoiceOverlayHostProps {
  readonly wc: WindowClass;
  /** A screen is over the workspace. */
  readonly covered: boolean;
  /** The open screens (keys), so a new screen's iframe is watched at once. */
  readonly screenKey: string;
  readonly workspaceRef: RefObject<HTMLElement | null>;
}

export function VoiceOverlayHost({ wc, covered, screenKey, workspaceRef }: VoiceOverlayHostProps) {
  const tabs = useTabs((s) => s.tabs);
  const activeId = useTabs((s) => s.activeId);
  const archieIds = useMemo(() => tabs.filter((t) => t.kind === 'archie' && !t.readOnly).map((t) => t.id), [tabs]);
  const liveId = useLiveVoiceId(archieIds);
  const getAvoid = useCallback(() => visibleConversationDock(workspaceRef.current), [workspaceRef]);
  const open = useCallback(() => {
    if (liveId) focusTab(liveId);
  }, [liveId]);
  // The overlay is a lazy chunk: fetch it as soon as a call starts here.
  useEffect(() => {
    if (liveId) preloadVoiceOverlay();
  }, [liveId]);
  if (!voiceOverlayVisible(liveId, activeId, covered) || !liveId) return null;
  return (
    <VoiceOverlay
      localId={liveId}
      regionRef={workspaceRef}
      {...(covered ? null : { getAvoid })}
      layoutKey={`${activeId ?? ''}|${screenKey}|${wc}`}
      compact={wc === 'compact'}
      onOpenConversation={open}
    />
  );
}
