/**
 * One open agent session (spec 13 §3.4): one chat WebSocket per session (G-6, T-5), the `start`
 * handshake on every open (with `resume_from` from the in-memory checkpoint, T-10), the cold
 * open of §5.2, `replay_overflow` → REST reload (SEQ-6), pagination, send/queue, interrupt,
 * compact, permission responses, Save-and-Restart and the rewind/fork cut.
 *
 * LOAD-BEARING inv02 F-01 (frontend/src/hooks/useChatInstance.ts:852-921): `start` re-sent on
 * every socket open and on visibility; the history-init runs once per `local_id` and never
 * re-runs when `resumeSdkId` arrives after the first turn (§7 #4, H-2) — here the init is the
 * constructor's `open()`, which nothing calls again.
 */
import {
  computeDropLastN,
  countPromptLines,
  initialConversation,
  mergeLines,
  promptsNeeded,
  toIndexedLines,
  type ClientMessage,
  type DropLastNResult,
  type IndexedLine,
  type LiveStatus,
  type Provider,
  type ServerFrame,
} from '@/protocol';
import { createSessionStore } from '@/stores';
import { api } from '../http/endpoints';
import { isApiError } from '../http/errors';
import { Reconnector, type ReconnectPolicy } from '../ws/reconnect';
import { ArchieSocket, CHAT_WS_PATH } from '../ws/socket';
import { ConversationRuntime, EMPTY_PAGE, type RuntimeHooks } from './ConversationRuntime';

export interface SessionRuntimeOptions {
  localId: string;
  sdkId?: string | null;
  provider?: Provider | null;
  /** `pool/live` status at open (ST-2). */
  liveStatus?: LiveStatus | null;
  /** Opened from the server's pool, not by a user action: every `start` reattaches (OPEN-2). */
  reattach?: boolean;
  /** H-3: REST only, no WebSocket. */
  readOnly?: boolean;
  hidden?: boolean;
  reconnectPolicy?: ReconnectPolicy;
}

export const TRUNCATE_RETRIES = 3;
export const TRUNCATE_RETRY_MS = 500;
const TAIL_PAGE = 200;

export class SessionRuntime extends ConversationRuntime {
  private readonly socket: ArchieSocket;
  private readonly reconnector: Reconnector | null;
  private coldOpenPending = false;
  readonly readOnly: boolean;

  constructor(opts: SessionRuntimeOptions, hooks: RuntimeHooks) {
    const conv = initialConversation({
      localId: opts.localId,
      kind: 'agent',
      sdkId: opts.sdkId ?? null,
      provider: opts.provider ?? 'claude',
      liveStatus: opts.liveStatus ?? null,
      reattach: opts.reattach,
    });
    const handle = createSessionStore({ localId: opts.localId, conv, readOnly: opts.readOnly, hidden: opts.hidden });
    super(conv, handle, hooks);
    this.readOnly = opts.readOnly === true;
    this.socket = new ArchieSocket(CHAT_WS_PATH, {
      onOpen: () => this.step({ type: 'socket_open' }),
      onFrame: (f) => this.onFrame(f),
      onClose: () => this.onClose(),
    });
    this.reconnector = this.readOnly
      ? null
      : new Reconnector(
          {
            isOpen: () => this.socket.isOpen,
            isConnecting: () => this.socket.isConnecting,
            connect: () => this.socket.connect(),
            resync: () => {
              this.needsPoolStatus = true;
              this.resendStart();
            },
          },
          opts.reconnectPolicy,
        );
  }

  /** §5.2 cold open. Called once by the session manager (H-2: never re-run). */
  open(): void {
    if (this.readOnly) {
      if (this.conv.ref.sdkId) void this.reload();
      return;
    }
    if (this.conv.ref.sdkId) {
      // H-1: subscribe first, then fetch; frames are held raw until the page is applied
      this.step({ type: 'begin_reload' });
      this.coldOpenPending = true;
    } else {
      // a brand-new session has no history: the entries are built from this stream (T-10)
      this.step({ type: 'history_page', mode: 'replace', response: EMPTY_PAGE });
    }
    this.socket.connect();
  }

  private onFrame(f: ServerFrame): void {
    this.step({ type: 'frame', frame: f });
    if (this.coldOpenPending && !this.disposed && (f.type === 'session_started' || f.type === 'error')) this.startColdFetch();
  }

  private startColdFetch(): void {
    this.coldOpenPending = false;
    void this.runReload();
  }

  private onClose(): void {
    this.step({ type: 'socket_closed' });
    if (this.coldOpenPending) this.startColdFetch(); // show history even when the socket cannot connect
    this.needsPoolStatus = true;
    this.reconnector?.scheduleReconnect();
  }

  protected transportSend(msg: ClientMessage): boolean {
    return this.socket.send(msg);
  }

  protected onSubscribed(): void {
    this.reconnector?.markHealthy(); // T-13
  }

  protected closeTransport(): void {
    this.reconnector?.stop();
    this.socket.close();
  }

  resendStart(): void {
    this.step({ type: 'resend_start' });
  }

  /** Connection banner "Retry": reconnect now (resets the backoff) or re-send `start`. */
  retry(): void {
    if (this.readOnly) return;
    if (this.socket.isOpen) this.retryHandshake();
    else this.reconnector?.reconnectNow();
  }

  get socketOpen(): boolean {
    return this.socket.isOpen;
  }

  // ───────────────────────── user actions (§6) ─────────────────────────

  /** §6.1. While busy the prompt goes to the tray and queues server-side; a pending permission is denied with the text as feedback (§6.9). */
  send(text: string): void {
    if (this.readOnly || !text.trim()) return;
    this.step({ type: 'local_send', text });
    this.sendWhenSubscribed({ type: 'send', text });
  }

  /** §6.3 */
  interrupt(): void {
    if (this.readOnly) return;
    this.step({ type: 'local_interrupt' });
    this.transportSend({ type: 'interrupt' });
  }

  /** §6.4 */
  compact(): void {
    if (this.readOnly) return;
    this.step({ type: 'local_compact' });
    this.sendWhenSubscribed({ type: 'compact' });
  }

  /** Slash command passthrough (`command` on the chat WS). */
  command(text: string): void {
    if (!this.readOnly) this.sendWhenSubscribed({ type: 'command', text });
  }

  /** §6.9 approve / deny (deny with feedback = just `send`). */
  respondPermission(requestId: string, decision: 'allow' | 'deny', message?: string): boolean {
    const msg: ClientMessage = { type: 'permission_response', request_id: requestId, decision };
    if (message) msg.message = message;
    return this.transportSend(msg);
  }

  /** Answer an agent permission for this session from the orchestrator view (§6.9). */
  respondPermissionFor(localId: string, requestId: string, decision: 'allow' | 'deny'): boolean {
    return this.transportSend({ type: 'permission_response', session_id: localId, request_id: requestId, decision });
  }

  /**
   * §6.14 Save and Restart: the backend applies session config only on a new pool entry.
   * close → `start` with the same `local_id` and `resume_sdk_id`. Entries are kept.
   */
  async restart(): Promise<void> {
    if (this.readOnly) return;
    this.step({ type: 'local_stop' });
    try {
      await api.sessions.close(this.localId);
    } catch {
      // errors ignored: the start below re-creates or re-attaches
    }
    if (this.disposed) return;
    if (this.socket.isOpen) this.resendStart();
    else this.reconnector?.reconnectNow();
  }

  /**
   * §6.5 `computeDropLastN` with its REST tail listing: pages of 200 from the end until k + 1
   * prompt lines are known or the file starts.
   */
  async resolveDropLastN(targetId: string): Promise<DropLastNResult> {
    const sdk = this.conv.ref.sdkId;
    if (!sdk) return { ok: false, reason: 'no sdk id' };
    const entries = this.conv.entries;
    const need = promptsNeeded(entries, targetId);
    let lines: IndexedLine[] = [];
    let before: number | undefined;
    for (;;) {
      const page = await api.sessions.messages(sdk, { limit: TAIL_PAGE, before });
      lines = mergeLines(toIndexedLines(page), lines);
      if (countPromptLines(lines) >= need || !page.has_more || page.start_index <= 0) break;
      before = page.start_index;
    }
    return computeDropLastN(entries, targetId, lines);
  }
}

/** `POST …/truncate` with the 409 retry of §6.5 (the pool close may still be settling). */
export async function truncateWithRetry(sdkId: string, n: number, sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      await api.sessions.truncate(sdkId, n);
      return;
    } catch (err) {
      if (isApiError(err, 409) && attempt < TRUNCATE_RETRIES) {
        await sleep(TRUNCATE_RETRY_MS);
        continue;
      }
      throw err;
    }
  }
}
