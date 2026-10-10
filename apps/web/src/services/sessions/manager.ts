/**
 * Session directory and lifecycle (spec 12 §3.7, §6; spec 13 §3.4 `poolSync`, §4.4).
 *
 * Load-bearing rules implemented here:
 * - **P-1** (Rodrigo): only an *explicit* close (`closeSession`, `deleteSession`, rewind) sends
 *   `POST /api/sessions/{local_id}/close`. Teardown (`stopServices`), page unload, backgrounding
 *   and lost connections never send `close` or `stop`: no `pagehide` / `beforeunload` /
 *   `unload` handler is installed at all, and `dispose()` only closes sockets.
 * - **OPEN-1 / P-6 / FOCUS-1**: the server's pool is the open set. Every pool row is a tab; rows
 *   opened by pool sync or watcher events become background tabs (unseen badge) and never take
 *   focus. Only direct user actions pass `focus: true`.
 * - **OPEN-2**: views opened from the pool, and every view once subscribed, only reattach
 *   (`start{reattach}`, the reducer's `conv.reattach`): a device that missed a close cannot revive
 *   the conversation.
 * - **OPEN-3 / FOCUS-2**: a conversation that left the pool (`agent_session_closed`, checked
 *   against `is_orchestrator`; `error{session_closed}`; a pool read without its row) closes its
 *   view at once, active or not, without a `close` request.
 * - **OPEN-4**: the pool is re-read on start, on every orchestrator socket open, on every
 *   visibility → visible and after an agent view re-subscribes (the ST-2 read).
 */
import {
  busy,
  type AgentSessionClosedFrame,
  type AgentSessionOpenedFrame,
  type LiveStatus,
  type OrchestratorSwitchFrame,
  type Provider,
} from '@/protocol';
import { Emitter, generateUUID } from '@/platform';
import {
  activateTab,
  catalogStore,
  clearSessionRegistry,
  finishTabRestore,
  findTab,
  getSessionEntry,
  hydrateTabs,
  listSessionEntries,
  markTabUnseen,
  openTab,
  patchTab,
  registerSession,
  rekeySession,
  rekeyTab,
  removeSession,
  removeTab,
  setCatalogItems,
  showSnackbar,
  tabsStore,
  tabTitle,
  updateSessionItems,
} from '@/stores';
import { getEnv } from '../env';
import { api } from '../http/endpoints';
import { isApiError } from '../http/errors';
import type { PoolSession } from '../http/types';
import { probeBackendCapabilities, probeCast } from '../capabilitiesProbe';
import { cancelScheduledRefreshes, refreshSessionList, scheduleListRefresh, scheduleVisualsRefresh, refreshVisuals } from '../catalogService';
import { cancelContentRefreshes, onContentFrame, resyncContent } from '../contentChanges';
import type { ReconnectPolicy } from '../ws/reconnect';
import { ArchieSocket, CHAT_WS_PATH } from '../ws/socket';
import { ArchieRuntime } from './ArchieRuntime';
import type { RuntimeHooks } from './ConversationRuntime';
import { OrchestratorChannel, type AgentTurnFrame, type WatcherFrame } from './orchestratorChannel';
import { SessionRuntime, truncateWithRetry } from './SessionRuntime';

export type AnyRuntime = SessionRuntime | ArchieRuntime;

interface OrchestratorRef {
  localId: string;
  sdkId: string | null;
}

let channel: OrchestratorChannel | null = null;
let orchestratorRef: OrchestratorRef | null = null;
let policy: ReconnectPolicy | undefined;
let started = false;
const unsubscribers: (() => void)[] = [];

// ───────────────────────── registry access ─────────────────────────

export function getSessionRuntime(localId: string): AnyRuntime | undefined {
  return getSessionEntry(localId)?.runtime as AnyRuntime | undefined;
}

export function listRuntimes(): AnyRuntime[] {
  return listSessionEntries().map((e) => e.runtime as AnyRuntime);
}

/** The live (non read-only) Archie runtime, if one is open. */
export function getArchieRuntime(): ArchieRuntime | undefined {
  return listRuntimes().find((r): r is ArchieRuntime => r instanceof ArchieRuntime && !r.readOnly);
}

export function getOrchestratorRef(): OrchestratorRef | null {
  return orchestratorRef;
}

function findBySdk(sdkId: string): AnyRuntime | undefined {
  return listRuntimes().find((r) => r.conv.ref.sdkId === sdkId);
}

function getChannel(): OrchestratorChannel {
  if (!channel)
    channel = new OrchestratorChannel(onWatcherEvent, policy, onOrchestratorSwitch, {
      onTurn: (f) => turnEvents.emit('turn', f),
      onContent: onContentFrame,
      onOpen: () => void syncPool().catch(() => undefined), // OPEN-4
      onReopen: () => void resyncContent(),
    });
  return channel;
}

const turnEvents = new Emitter<{ turn: AgentTurnFrame }>();

/**
 * `agent_turn_started/finished` from the watcher socket (§3.7): every agent turn on the server,
 * whichever device or the orchestrator started it. The app's notifier subscribes. Returns an
 * unsubscribe.
 */
export function onAgentTurn(fn: (frame: AgentTurnFrame) => void): () => void {
  return turnEvents.on('turn', fn);
}

// ───────────────────────── runtime hooks ─────────────────────────

/** A `GET /api/sessions/pool/live` answer and the views the server had to have when it was sent. */
interface PoolRead {
  rows: PoolSession[];
  /** Only these may be closed for a missing row: a view whose start is answered meanwhile may be missing. */
  known: AnyRuntime[];
}

let poolRead: Promise<PoolRead> | null = null;

/** One pool read, shared by concurrent callers. */
function readPool(): Promise<PoolRead> {
  if (!poolRead) {
    const known = listRuntimes().filter((r) => !r.readOnly && r.conv.reattach);
    poolRead = api.sessions
      .poolLive()
      .then((rows) => {
        setCatalogItems('pool', rows);
        return { rows, known };
      })
      .finally(() => {
        poolRead = null;
      });
  }
  return poolRead;
}

/** `GET /api/sessions/pool/live`, shared by concurrent callers. */
export function fetchPool(): Promise<PoolSession[]> {
  return readPool().then((r) => r.rows);
}

export const runtimeHooks: RuntimeHooks = {
  onRekey(_rt, oldId, newId) {
    rekeySession(oldId, newId);
    rekeyTab(oldId, newId);
  },
  onSdkId(rt, sdkId) {
    patchTab(rt.localId, { sdkId });
    scheduleListRefresh();
  },
  onTurnEnded(rt) {
    scheduleListRefresh();
    scheduleVisualsRefresh();
    markTabUnseen(rt.localId);
    const archie = getArchieRuntime();
    if (rt instanceof ArchieRuntime) rt.afterTurnEnded();
    else if (archie) archie.clearAgentApprovals(rt.localId);
  },
  onWatcher: (frame) => onWatcherEvent(frame),
  onNested(localId, event) {
    const rt = getSessionRuntime(localId);
    if (rt instanceof SessionRuntime && !rt.readOnly) rt.step({ type: 'frame', frame: event }); // I-11 makes double delivery harmless
  },
  async lookupSdkId(localId) {
    try {
      const rows = await fetchPool();
      return rows.find((r) => r.local_id === localId)?.sdk_session_id ?? null;
    } catch {
      return null;
    }
  },
  onResubscribed(rt) {
    if (rt.kind !== 'agent') return; // the orchestrator row always says idle (ST-2, G-15)
    syncPool() // ST-2 and OPEN-4 in one read
      .then((rows) => {
        const row = rows.find((r) => r.local_id === rt.localId);
        if (row && !rt.isDisposed) rt.step({ type: 'pool_status', status: row.status as LiveStatus });
      })
      .catch(() => undefined);
  },
};

// ───────────────────────── open ─────────────────────────

export interface OpenSessionOptions {
  kind: 'agent' | 'archie';
  /** Minted when absent (UUID with the non-crypto fallback, inv02 F-20). */
  localId?: string;
  sdkId?: string | null;
  provider?: Provider | null;
  liveStatus?: LiveStatus | null;
  /** true only for direct user actions (FOCUS-1). */
  focus: boolean;
  /** Opened from the server's pool (sync or a watcher event), not by a user action: reattach only (OPEN-2). */
  fromPool?: boolean;
  /** H-3: REST only. */
  readOnly?: boolean;
  titleHint?: string;
  voiceActive?: boolean;
}

function providerFor(sdkId: string | null | undefined): Provider | null {
  if (!sdkId) return null;
  const p = catalogStore.getState().sessions.items.find((s) => s.session_id === sdkId)?.provider;
  return typeof p === 'string' && p ? p : null;
}

/** Open a conversation view (or return the open one). Never steals focus unless `focus`. */
export function openSession(o: OpenSessionOptions): AnyRuntime {
  if (o.localId) {
    const open = getSessionRuntime(o.localId);
    if (open) {
      if (o.focus) activateTab(open.localId);
      return open;
    }
  }
  if (o.sdkId && !o.localId) {
    const open = findBySdk(o.sdkId);
    if (open && (o.kind === 'archie') === (open.kind === 'orchestrator')) {
      if (o.focus) activateTab(open.localId);
      return open;
    }
  }
  const localId = o.localId ?? generateUUID();
  const activeId = tabsStore.getState().activeId;
  const hidden = !o.focus && activeId !== localId;
  let rt: AnyRuntime;
  if (o.kind === 'archie') {
    rt = new ArchieRuntime(
      { localId, sdkId: o.sdkId ?? null, reattach: o.fromPool, voiceActive: o.voiceActive, hidden, readOnly: o.readOnly },
      runtimeHooks,
      getChannel(),
    );
  } else {
    rt = new SessionRuntime(
      {
        localId,
        sdkId: o.sdkId ?? null,
        provider: o.provider ?? providerFor(o.sdkId) ?? 'claude',
        liveStatus: o.liveStatus ?? null,
        reattach: o.fromPool,
        readOnly: o.readOnly,
        hidden,
        reconnectPolicy: policy,
      },
      runtimeHooks,
    );
  }
  registerSession(localId, { handle: rt.handle, runtime: rt });
  openTab(
    {
      id: localId,
      kind: o.kind,
      localId,
      sdkId: o.sdkId ?? null,
      provider: o.kind === 'agent' ? (o.provider ?? providerFor(o.sdkId)) : null,
      readOnly: o.readOnly === true,
      ...(o.titleHint ? { titleHint: o.titleHint } : {}),
    },
    { focus: o.focus },
  );
  rt.open();
  return rt;
}

export type OpenArchieResult = { runtime: ArchieRuntime; conflict: false } | { runtime: null; conflict: true; running: OrchestratorRef };

/**
 * §6.11 new / attach orchestrator. One Archie per app (inv02 F-25). Resuming a past
 * conversation while another is live returns `conflict` (the W-11 dialog: open the running one,
 * stop it and start the requested one, or cancel); `readOnly` views a past one instead (H-3).
 */
export async function openArchie(o: { focus: boolean; resumeSdkId?: string | null } = { focus: true }): Promise<OpenArchieResult> {
  const open = getArchieRuntime();
  if (open && (!o.resumeSdkId || open.conv.ref.sdkId === o.resumeSdkId)) {
    if (o.focus) activateTab(open.localId);
    return { runtime: open, conflict: false };
  }
  try {
    await syncPool();
  } catch {
    // offline: fall through with the last known ref
  }
  const running = orchestratorRef;
  if (running && o.resumeSdkId && running.sdkId !== o.resumeSdkId) return { runtime: null, conflict: true, running };
  if (open) return { runtime: null, conflict: true, running: { localId: open.localId, sdkId: open.conv.ref.sdkId } };
  const rt = openSession({
    kind: 'archie',
    localId: running?.localId,
    sdkId: running ? running.sdkId : (o.resumeSdkId ?? null),
    focus: o.focus,
  }) as ArchieRuntime;
  return { runtime: rt, conflict: false };
}

/** §6.11 conflict: stop the running orchestrator (explicit close, P-1) and start/resume ours. */
export async function replaceRunningArchie(resumeSdkId: string | null, focus = true): Promise<ArchieRuntime> {
  const open = getArchieRuntime();
  const runningId = orchestratorRef?.localId ?? open?.localId;
  if (open && open.localId !== runningId) await closeSession(open.localId);
  if (runningId) await closeSession(runningId);
  orchestratorRef = null;
  return openSession({ kind: 'archie', sdkId: resumeSdkId, focus }) as ArchieRuntime;
}

// ───────────────────────── pool sync and watcher (§3.7) ─────────────────────────

/**
 * OPEN-4: `GET /api/sessions/pool/live` is the open set. Views it no longer has close (OPEN-3),
 * rows with no view here open as background tabs (OPEN-1, FOCUS-1), the orchestrator ref follows.
 */
export async function syncPool(): Promise<PoolSession[]> {
  const { rows, known } = await readPool();
  for (const rt of known)
    if (!rows.some((r) => r.local_id === rt.localId && (r.is_orchestrator === true) === (rt.kind === 'orchestrator'))) closedByServer(rt);
  let orch: OrchestratorRef | null = null;
  for (const row of rows) {
    if (row.is_orchestrator) {
      orch = { localId: row.local_id, sdkId: row.sdk_session_id };
      openFromPool(row.local_id, row.sdk_session_id, true);
    } else openFromPool(row.local_id, row.sdk_session_id, false, { liveStatus: row.status as LiveStatus, ...(row.title ? { titleHint: row.title } : {}) });
  }
  orchestratorRef = orch;
  return rows;
}

/** OPEN-1: a pool conversation with no view here opens in the background (one Archie per app). */
function openFromPool(localId: string, sdkId: string | null, isOrch: boolean, extra: Partial<OpenSessionOptions> = {}): boolean {
  if (getSessionRuntime(localId) || (isOrch && getArchieRuntime())) return false;
  openSession({ ...extra, kind: isOrch ? 'archie' : 'agent', localId, sdkId, focus: false, fromPool: true });
  return true;
}

function placeholderTitle(localId: string): string {
  return `Agent ${localId.slice(0, 8)}`;
}

/** Watcher events (`agent_session_opened/closed`), from the channel or the Archie conversation. */
export function onWatcherEvent(f: WatcherFrame): void {
  if (f.type === 'agent_session_opened') onOpened(f);
  else onClosed(f);
  scheduleListRefreshNow();
}

function onOpened(f: AgentSessionOpenedFrame): void {
  const sdkId = f.sdk_session_id ?? null;
  if (f.is_orchestrator === true) {
    orchestratorRef = { localId: f.session_id, sdkId };
    openFromPool(f.session_id, sdkId, true);
    return;
  }
  const known = sdkId ? catalogStore.getState().sessions.items.find((s) => s.session_id === sdkId)?.title : undefined;
  const title = known ?? placeholderTitle(f.session_id);
  if (!openFromPool(f.session_id, sdkId, false, { titleHint: title })) return;
  const id = f.session_id;
  // P-6: a badge plus a snackbar; never a focus change (fixes inv02 §6.1)
  showSnackbar(`Archie opened ${title}`, { action: { label: 'Open', run: () => activateTab(id) } });
}

function onClosed(f: AgentSessionClosedFrame): void {
  const isOrch = f.is_orchestrator === true;
  if (isOrch && orchestratorRef?.localId === f.session_id) orchestratorRef = null;
  const rt = getSessionRuntime(f.session_id);
  if (rt && (rt.kind === 'orchestrator') === isOrch) closedByServer(rt); // FOCUS-2
}

/**
 * OPEN-3: the conversation left the server's pool, so its view closes now, active or not, with no
 * `close` request; focus moves as after an explicit close. Not a view whose own `start` is still
 * creating it, or that this client is closing or replacing itself (`conv.reattach` is false then).
 */
function closedByServer(rt: AnyRuntime): void {
  if (getSessionRuntime(rt.localId) !== rt || rt.readOnly || !rt.conv.reattach) return;
  const active = tabsStore.getState().activeId === rt.localId;
  const title = titleOf(rt.localId);
  const why = rt.conv.termination?.detail;
  dropView(rt.localId);
  // Archie: no notice, a switch (§6.11a) announces itself right after
  if (active && rt.kind === 'agent') showSnackbar(why ? `${title} ended: ${why}` : `${title} was closed elsewhere`);
}

// ───────────────────────── agent-initiated switch (§6.11a) ─────────────────────────

/**
 * SW-2: start voice on the resumed Archie view without a gesture, and tell the user about the
 * switch. Registered by the voice feature (services do not reach the voice engine).
 */
export type SwitchVoiceHandler = (localId: string, title: string) => void;

let switchVoiceHandler: SwitchVoiceHandler | null = null;
/** SW-1: the switches already acted on (`sdk_session_id` + `from_session_id`). */
const switchesDone = new Set<string>();

/** Returns the unregister function. */
export function setSwitchVoiceHandler(fn: SwitchVoiceHandler | null): () => void {
  switchVoiceHandler = fn;
  return () => {
    if (switchVoiceHandler === fn) switchVoiceHandler = null;
  };
}

function dropView(localId: string): void {
  removeSession(localId, true);
  removeTab(localId);
}

/**
 * §6.11a `orchestrator_switch`, from the channel (SW-1). The server already ended voice and
 * stopped the old orchestrator (OPEN-3 closed its view), so: drop the old view locally (no
 * REST close, it is gone), resume the past conversation focused with a new `local_id` (no
 * conflict dialog, nothing runs any more), and start voice on it when voice was live (SW-2).
 */
export function onOrchestratorSwitch(f: OrchestratorSwitchFrame): ArchieRuntime | null {
  const sdkId = f.sdk_session_id;
  if (!sdkId) return null;
  const from = f.from_session_id ?? '';
  const key = `${sdkId}\n${from}`;
  if (switchesDone.has(key)) return null;
  switchesDone.add(key);
  if (from) {
    const old = getSessionRuntime(from);
    if (!old || old.kind === 'orchestrator') dropView(from);
  }
  // One orchestrator per pool and the server just stopped it: any other live Archie view is stale.
  const stale = getArchieRuntime();
  if (stale) dropView(stale.localId);
  orchestratorRef = null;
  // a read-only view of the same conversation would be reused by sdk id: it goes first
  for (const r of listRuntimes()) if (r instanceof ArchieRuntime && r.readOnly && r.conv.ref.sdkId === sdkId) dropView(r.localId);
  const title = (f.title ?? '').trim();
  const rt = openSession({ kind: 'archie', localId: generateUUID(), sdkId, focus: true, ...(title ? { titleHint: title } : {}) }) as ArchieRuntime;
  const label = title || titleOf(rt.localId);
  if (f.voice === true && switchVoiceHandler) switchVoiceHandler(rt.localId, label);
  else showSnackbar(`Switched to ${label}`);
  scheduleListRefresh();
  return rt;
}

function scheduleListRefreshNow(): void {
  void refreshSessionList();
  void refreshVisuals();
}

// ───────────────────────── explicit user actions (§6) ─────────────────────────

/**
 * §6.7 close a view — the **only** path besides delete/rewind that closes a session server-side
 * (P-1: an explicit close closes it for everybody). Confirmation ("Closing stops the current
 * reply." / "Stop the orchestrator on all devices?") is the UI's job before calling this.
 */
export async function closeSession(localId: string): Promise<void> {
  const rt = getSessionRuntime(localId);
  if (!rt) {
    removeTab(localId);
    return;
  }
  const readOnly = rt.readOnly;
  if (!readOnly) rt.step({ type: 'local_stop' });
  removeSession(localId, true);
  removeTab(localId);
  if (rt instanceof ArchieRuntime && !readOnly && orchestratorRef?.localId === localId) orchestratorRef = null;
  if (!readOnly) {
    try {
      await api.sessions.close(localId);
    } catch (err) {
      getEnv().log('warn', 'close failed (ignored)', err);
    }
  }
  scheduleListRefresh();
}

/** Close any tab: doc tabs just go; chat tabs are an explicit session close. */
export function closeTab(tabId: string): Promise<void> {
  const tab = findTab(tabId);
  if (tab && (tab.kind === 'memory' || tab.kind === 'visual')) {
    removeTab(tabId);
    return Promise.resolve();
  }
  return closeSession(tabId);
}

/**
 * §6.8 delete: close every open view of the session and its pool entry **before** `DELETE` (so
 * the CLI stops writing a file that moved to trash), then refresh. Fixes W-8 (tab left open).
 */
export async function deleteSession(sdkId: string): Promise<void> {
  let poolRow: PoolSession | undefined;
  try {
    poolRow = (await fetchPool()).find((r) => r.sdk_session_id === sdkId);
  } catch {
    poolRow = undefined;
  }
  const closing = listRuntimes().filter((r) => r.conv.ref.sdkId === sdkId || (poolRow && r.localId === poolRow.local_id));
  for (const r of closing) await closeSession(r.localId);
  if (poolRow && !closing.some((r) => r.localId === poolRow?.local_id)) {
    try {
      await api.sessions.close(poolRow.local_id);
    } catch {
      // errors ignored, as for a view close
    }
  }
  try {
    await api.sessions.remove(sdkId);
  } catch (err) {
    if (!isApiError(err, 404)) throw err;
  }
  updateSessionItems((items) => items.filter((s) => s.session_id !== sdkId));
  void refreshSessionList();
  void syncPool().catch(() => undefined);
}

/**
 * Replace a view in place (§6.5 rewind, §6.13 "Continue in a new view"): new `local_id`, the
 * given sdk id, same kind, same tab position, canonical cold open. Sends no close itself.
 */
export function replaceInPlace(localId: string, sdkId: string): AnyRuntime {
  const old = getSessionRuntime(localId);
  const kind = old?.kind === 'orchestrator' ? 'archie' : 'agent';
  const provider = old?.conv.ref.provider ?? null;
  const wasActive = tabsStore.getState().activeId === localId;
  const newId = generateUUID();
  if (old) removeSession(localId, false);
  rekeyTab(localId, newId);
  patchTab(newId, { sdkId });
  const rt = openSession({ kind, localId: newId, sdkId, provider, focus: wasActive });
  return rt;
}

export class SessionActionError extends Error {}

/**
 * §6.5 rewind (after the UI confirmed): compute `drop_last_n` against a fresh REST listing,
 * then close → truncate (409 retry) → reopen in place (**[LOAD-BEARING]** inv02 F-09 order).
 */
export async function rewindSession(localId: string, targetId: string): Promise<AnyRuntime> {
  const rt = getSessionRuntime(localId);
  if (!(rt instanceof SessionRuntime) || !rt.conv.ref.sdkId) throw new SessionActionError('Available after the first reply');
  if (busy(rt.conv.status) || rt.conv.inTurn) throw new SessionActionError('Stop the current reply first');
  const sdk = rt.conv.ref.sdkId;
  const res = await rt.resolveDropLastN(targetId);
  if (!res.ok) {
    void rt.reload();
    throw new SessionActionError('The conversation changed. Try again.');
  }
  if (res.n === 0) throw new SessionActionError('There is nothing after this message.');
  rt.step({ type: 'local_stop' });
  try {
    await api.sessions.close(localId);
  } catch {
    // errors ignored (§6.5)
  }
  await truncateWithRetry(sdk, res.n);
  return replaceInPlace(localId, sdk);
}

/** §6.5 fork: `n == 0` is a plain copy. Opens the fork focused (user-initiated). */
export async function forkSession(localId: string, targetId: string): Promise<AnyRuntime> {
  const rt = getSessionRuntime(localId);
  if (!rt || !rt.conv.ref.sdkId) throw new SessionActionError('Available after the first reply');
  const res = rt instanceof SessionRuntime ? await rt.resolveDropLastN(targetId) : { ok: true as const, n: 0 };
  if (!res.ok) {
    void rt.reload();
    throw new SessionActionError('The conversation changed. Try again.');
  }
  const r = await api.sessions.fork(rt.conv.ref.sdkId, res.n);
  void refreshSessionList();
  if (rt.kind === 'orchestrator') return openSession({ kind: 'archie', sdkId: r.session_id, focus: true, readOnly: true });
  return openSession({ kind: 'agent', sdkId: r.session_id, provider: rt.conv.ref.provider, focus: true });
}

/**
 * §6.9 answer an agent permission from the orchestrator view: on the agent's socket when its
 * view is open, else `POST /api/sessions/{localId}/permission`. A 409 (someone answered first)
 * is success: `permission_resolved` removes the card. Only a server without that route (404
 * "Not Found") falls back to a transient unsubscribed chat socket (T-8). Other errors reject.
 */
export async function respondAgentPermission(agentLocalId: string, requestId: string, decision: 'allow' | 'deny'): Promise<void> {
  const rt = getSessionRuntime(agentLocalId);
  if (rt instanceof SessionRuntime && rt.respondPermissionFor(agentLocalId, requestId, decision)) return;
  try {
    await api.sessions.permission(agentLocalId, { request_id: requestId, decision });
  } catch (err) {
    if (isApiError(err, 409)) return;
    if (isApiError(err, 404) && err.detail === 'Not Found') {
      respondOnTransientSocket(agentLocalId, requestId, decision);
      return;
    }
    if (isApiError(err, 404)) throw new SessionActionError('That agent session is no longer running');
    throw err;
  }
}

/** T-8 fallback for servers without the REST route. */
function respondOnTransientSocket(agentLocalId: string, requestId: string, decision: 'allow' | 'deny'): void {
  const sock: ArchieSocket = new ArchieSocket(CHAT_WS_PATH, {
    onOpen: () => {
      sock.send({ type: 'permission_response', session_id: agentLocalId, request_id: requestId, decision });
      setTimeout(() => sock.close(), 1000);
    },
    onFrame: () => undefined,
    onClose: () => undefined,
  });
  sock.connect();
}

/** The panel of `localId` is (not) visible: frozen snapshots while hidden (spec 13 §4.4). */
export function setSessionHidden(localId: string, hidden: boolean): void {
  getSessionEntry(localId)?.handle.setHidden(hidden);
}

/** Rendered title of a tab (derived, §7 #9). */
export function titleOf(tabId: string): string {
  const t = findTab(tabId);
  return t ? tabTitle(t) : '';
}

// ───────────────────────── lifecycle ─────────────────────────

export interface StartServicesOptions {
  reconnectPolicy?: ReconnectPolicy;
  /** Skip the startup fetches (tests). */
  skipInitialSync?: boolean;
}

function syncHiddenFlags(): void {
  const active = tabsStore.getState().activeId;
  for (const e of listSessionEntries()) e.handle.setHidden(e.runtime.localId !== active);
}

/** §3.5 onVisible at app level: pool sync, list refresh, cast probe. (Sockets resync themselves.) */
function onAppVisible(): void {
  void syncPool().catch(() => undefined);
  void refreshSessionList();
  void probeCast();
}

/** Start the app's services once: tabs, watcher socket, initial sync, probes, visibility. */
export function startServices(o: StartServicesOptions = {}): void {
  if (started) return;
  started = true;
  policy = o.reconnectPolicy;
  hydrateTabs();
  getChannel().start(); // T-7
  unsubscribers.push(getEnv().visibility.subscribe((hidden) => !hidden && onAppVisible()));
  unsubscribers.push(tabsStore.subscribe((s, prev) => s.activeId !== prev.activeId && syncHiddenFlags()));
  // NOTE (P-1): deliberately NO pagehide / beforeunload / unload handlers.
  if (!o.skipInitialSync) {
    // the list first (titles, providers), then the live tabs; the restore window closes after it
    void refreshSessionList()
      .then(() => syncPool().catch(() => undefined))
      .then(() => finishTabRestore());
    void refreshVisuals(); // visual tab titles; the Visuals pane refreshes again when it opens (§9.1)
    void probeBackendCapabilities();
  }
}

/**
 * Teardown (tests, HMR). Disposes every runtime and closes the sockets **without** closing any
 * session: no `stop` frame, no `POST …/close` (P-1).
 */
export function stopServices(): void {
  for (const u of unsubscribers.splice(0)) u();
  clearSessionRegistry();
  channel?.stop();
  channel = null;
  orchestratorRef = null;
  poolRead = null;
  switchesDone.clear();
  cancelScheduledRefreshes();
  cancelContentRefreshes();
  started = false;
}

export function servicesStarted(): boolean {
  return started;
}
