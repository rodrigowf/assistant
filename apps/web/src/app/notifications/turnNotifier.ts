/**
 * Posts the "agent session finished" notices (turnNotices.ts decides) from the watcher socket's
 * `agent_turn_finished` (spec 12 §3.7), and routes their clicks to the session.
 *
 * Delivery: only while this page is open (a tab in the background counts; a closed tab or a frozen
 * mobile tab does not: there is no push server). Clicks: the service worker (main build) focuses
 * the page and posts `archie:notification-click`, or, with no page left, opens
 * `/?open_session=<localId>&open_sdk=<sdkId>`, read once at startup here. Without the service
 * worker the page's own Notification handles its click.
 *
 * Several Archie pages on one device: each one hears the frame, so a page posts only when no other
 * page is showing that session (viewPresence.ts); the tag collapses the duplicates otherwise.
 *
 * Every decision logs one `[notify] …` line (also in the remote console log) for hands-on checks.
 */
import { notificationPermission, onNotificationClick, showSystemNotification } from '@/platform';
import { deriveTitle, type AgentTurnFinishedFrame } from '@/protocol';
import { onAgentTurn } from '@/services';
import { catalogStore, findTab, prefsStore, tabsStore } from '@/stores';
import { routeStore } from '../navigation/route';
import { openFromNotification } from '../shell/actions';
import { NOTICE_KIND, decideTurnNotice, isViewing, type ViewState } from './turnNotices';
import { VIEWING_REFRESH_MS, ViewPresence } from './viewPresence';

export const OPEN_SESSION_PARAM = 'open_session';
export const OPEN_SDK_PARAM = 'open_sdk';

function viewState(): ViewState {
  const doc = typeof document !== 'undefined' ? document : undefined;
  let focused = false;
  try {
    focused = !!doc && typeof doc.hasFocus === 'function' && doc.hasFocus();
  } catch {
    focused = false;
  }
  return {
    visible: !!doc && doc.visibilityState === 'visible',
    focused,
    workspaceOnTop: routeStore.getState().route.name === 'workspace',
    activeId: tabsStore.getState().activeId,
  };
}

function knownTitle(f: AgentTurnFinishedFrame): string | null {
  const tab = findTab(f.session_id);
  const t = deriveTitle(catalogStore.getState().sessions.items, { sdkId: f.sdk_session_id ?? tab?.sdkId ?? null, localId: f.session_id }, tab?.titleHint ?? '');
  return t || null;
}

function iconUrl(): string {
  const base = import.meta.env.BASE_URL || '/';
  return `${base.endsWith('/') ? base : `${base}/`}icon-192.png`;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

let presence: ViewPresence | null = null;

/** The session this page is showing to the user right now, if any. */
function viewingHere(): string | null {
  const v = viewState();
  return v.activeId && isViewing(v, v.activeId) ? v.activeId : null;
}

export function handleAgentTurn(f: AgentTurnFinishedFrame): void {
  const elsewhere = presence?.viewedElsewhere(f.session_id) === true;
  const d = decideTurnNotice(f, {
    enabled: prefsStore.getState().notifyAgentTurns,
    permission: notificationPermission(),
    viewing: isViewing(viewState(), f.session_id) || elsewhere,
    knownTitle: knownTitle(f),
    icon: iconUrl(),
  });
  if (!d.post) {
    console.info(`[notify] suppressed ${f.session_id} status=${f.status} reason=${d.reason}${d.reason === 'viewing' && elsewhere ? ' (another tab)' : ''}`);
    return;
  }
  void showSystemNotification(d.notice, () => openFromNotification(d.localId, d.sdkId)).then((path) =>
    console.info(`[notify] posted ${f.session_id} status=${f.status} via=${path} title="${d.notice.title}"`),
  );
}

/** `?open_session=…` from a notification click that had to open a new window: open it, once. */
function consumeOpenParam(): void {
  if (typeof location === 'undefined' || !location.search) return;
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(location.search);
  } catch {
    return;
  }
  const localId = str(params.get(OPEN_SESSION_PARAM));
  if (!localId) return;
  const sdkId = str(params.get(OPEN_SDK_PARAM));
  params.delete(OPEN_SESSION_PARAM);
  params.delete(OPEN_SDK_PARAM);
  const rest = params.toString();
  try {
    window.history.replaceState(window.history.state, '', location.pathname + (rest ? `?${rest}` : '') + location.hash);
  } catch {
    // keep going: the session still opens
  }
  console.info(`[notify] opening ${localId} from a notification (new window)`);
  openFromNotification(localId, sdkId);
}

/** Install once, after the services started. Returns an uninstall. */
export function installTurnNotifications(): () => void {
  const p = new ViewPresence();
  presence = p;
  const sync = (): void => p.update(viewingHere());
  const offTabs = tabsStore.subscribe(sync);
  const offRoute = routeStore.subscribe(sync);
  const timer = setInterval(sync, VIEWING_REFRESH_MS);
  const events: [EventTarget, string][] =
    typeof window !== 'undefined' ? [[window, 'focus'], [window, 'blur'], [document, 'visibilitychange']] : [];
  for (const [t, e] of events) t.addEventListener(e, sync);
  sync();
  const offTurn = onAgentTurn((f) => {
    if (f.type === 'agent_turn_finished') handleAgentTurn(f);
  });
  const offClick = onNotificationClick((data) => {
    const localId = str(data.localId);
    if (data.kind !== NOTICE_KIND || !localId) return;
    console.info(`[notify] clicked ${localId}`);
    openFromNotification(localId, str(data.sdkId));
  });
  consumeOpenParam();
  return () => {
    offTurn();
    offClick();
    offTabs();
    offRoute();
    clearInterval(timer);
    for (const [t, e] of events) t.removeEventListener(e, sync);
    p.update(null);
    if (presence === p) presence = null;
  };
}
