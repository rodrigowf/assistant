/**
 * "Agent session finished" notices (spec 12 §3.7 `agent_turn_finished`, §8.2 device setting):
 * the pure part. Whether this device notifies about a finished turn and what the notice says.
 *
 * - Off unless the device switch is on and the browser granted permission.
 * - `interrupted` (someone pressed Stop) never notifies; `error` says "Failed: …".
 * - Suppressed while this device is visibly showing that very session: page visible and
 *   focused, the workspace on top, that tab active.
 * - One notice per session (`tag`): a newer one replaces it.
 */
import type { NotifyPermission, SystemNotice } from '@/platform';
import type { AgentTurnFinishedFrame } from '@/protocol';

export const NOTICE_TAG_PREFIX = 'archie-turn:';
export const FALLBACK_TITLE = 'Agent session';
/** `data.kind` of our notices (the service worker routes clicks by it). */
export const NOTICE_KIND = 'agent-turn';

export interface ViewState {
  /** `document.visibilityState === 'visible'`. */
  visible: boolean;
  /** `document.hasFocus()`. */
  focused: boolean;
  /** The route is the workspace (no Settings / Memory / … screen on top). */
  workspaceOnTop: boolean;
  /** The active tab's id (= `localId` for chat tabs). */
  activeId: string | null;
}

/** The user is looking at `localId` on this device right now. */
export function isViewing(v: ViewState, localId: string): boolean {
  return v.visible && v.focused && v.workspaceOnTop && v.activeId === localId;
}

export type SuppressReason = 'disabled' | 'permission' | 'interrupted' | 'viewing';

export type TurnNoticeDecision =
  | { post: true; notice: SystemNotice; localId: string; sdkId: string | null }
  | { post: false; reason: SuppressReason };

export interface TurnNoticeInputs {
  enabled: boolean;
  permission: NotifyPermission;
  viewing: boolean;
  /** The title this app already knows (session list, tab hint), used when the frame has none. */
  knownTitle?: string | null;
  icon?: string;
}

function clean(s: string | null | undefined): string | null {
  const t = (s ?? '').trim();
  return t ? t : null;
}

export function noticeTitle(f: AgentTurnFinishedFrame, knownTitle?: string | null): string {
  return clean(f.title) ?? clean(knownTitle) ?? FALLBACK_TITLE;
}

export function noticeBody(f: AgentTurnFinishedFrame): string {
  if (f.status === 'error') return `Failed: ${clean(f.error) ?? clean(f.preview) ?? 'the turn ended with an error'}`;
  return clean(f.preview) ?? 'Finished';
}

export function decideTurnNotice(f: AgentTurnFinishedFrame, i: TurnNoticeInputs): TurnNoticeDecision {
  if (!i.enabled) return { post: false, reason: 'disabled' };
  if (f.status === 'interrupted') return { post: false, reason: 'interrupted' };
  if (i.permission !== 'granted') return { post: false, reason: 'permission' };
  if (i.viewing) return { post: false, reason: 'viewing' };
  const sdkId = clean(f.sdk_session_id);
  return {
    post: true,
    localId: f.session_id,
    sdkId,
    notice: {
      title: noticeTitle(f, i.knownTitle),
      body: noticeBody(f),
      tag: NOTICE_TAG_PREFIX + f.session_id,
      ...(i.icon ? { icon: i.icon } : {}),
      data: { kind: NOTICE_KIND, localId: f.session_id, sdkId },
    },
  };
}
