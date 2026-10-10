/**
 * Inputs and effects of the conversation state machine.
 *
 * `stepConversation(state, input)` is pure (spec 12 L-1): everything it needs arrives as an input,
 * and everything it wants done in the outside world (send a frame, fetch a page, refresh the
 * session list) comes back as an effect for the session runtime (W-06) to execute.
 */
import type { LiveStatus } from '../types';
import type { ClientMessage, VoiceStartMessage } from '../wire/client';
import type { AgentSessionClosedFrame, AgentSessionOpenedFrame, MessagesPage, ServerFrame } from '../wire/server';

export type HistoryMode = 'replace' | 'prepend' | 'reconcile';

export type ConversationInput =
  /** A decoded server frame. Goes through the connection manager (§3.6) and then the reducer. */
  | { readonly type: 'frame'; readonly frame: ServerFrame }
  /** A REST page (§5). A `replace` while `reloading` completes the canonical reload (§5.6). */
  | { readonly type: 'history_page'; readonly mode: HistoryMode; readonly response: MessagesPage }
  /** OpenAI data-channel inbound event, voice owner only (§4.7). */
  | { readonly type: 'datachannel_event'; readonly event: Readonly<Record<string, unknown>> }
  // user actions (§4.3 local_*, §6)
  | { readonly type: 'local_send'; readonly text: string }
  /** `text`: the optional prompt sent with the clip (the composer note); `""` when none. */
  | { readonly type: 'local_send_audio'; readonly text?: string }
  | { readonly type: 'local_inject'; readonly text: string }
  | { readonly type: 'local_interrupt' }
  | { readonly type: 'local_compact' }
  /** Before sending `stop` or `POST …/close`: the next `session_stopped` is our own ack, the next `start` is ours (OPEN-2). */
  | { readonly type: 'local_stop' }
  /** The voice controller tore voice down locally (timeout, fatal error, §7.6). */
  | { readonly type: 'voice_local_end' }
  // connection (§3.3–§3.6, §5.2, §5.6)
  /** The socket opened: emits the `start` (or the given `voice_start`, T-11) to send. */
  | { readonly type: 'socket_open'; readonly start?: VoiceStartMessage }
  | { readonly type: 'socket_closed' }
  /** Re-send `start` (or the given `voice_start`) on an open socket (visibility resume, T-9). */
  | { readonly type: 'resend_start'; readonly start?: VoiceStartMessage }
  /** Hold every frame until the next `history_page{replace}` (cold open §5.2, user Reload). */
  | { readonly type: 'begin_reload' }
  /** The reload could not run (no `sdkId`, fetch failed): keep entries, flag the gap (SEQ-6). */
  | { readonly type: 'reload_failed' }
  /** `pool/live` status for this agent session on (re)subscribe (ST-2). */
  | { readonly type: 'pool_status'; readonly status: LiveStatus }
  /** `sdkId` learned from `pool/live` (ID-2). */
  | { readonly type: 'sdk_id'; readonly sdkId: string }
  | { readonly type: 'dismiss_banner' }
  /** Orchestrator view: drop approvals of an agent whose turn ended (§6.9). */
  | { readonly type: 'clear_agent_approvals'; readonly localId: string };

export type Effect =
  /** Send this message on the conversation's socket (text frame). */
  | { readonly type: 'send'; readonly message: ClientMessage }
  /** Canonical reload: `GET …/messages?limit=50` → `history_page{replace}`. `needsSdkId`: try `pool/live` first. */
  | { readonly type: 'reload'; readonly needsSdkId: boolean }
  /** R-7: `GET …/messages?limit=50` → `history_page{reconcile}` (debounce 500 ms, one at a time). */
  | { readonly type: 'reconcile' }
  /** ID-2: a turn ended without a `session_id`; look the `sdkId` up in `pool/live`. */
  | { readonly type: 'learn_sdk_id' }
  /** A turn ended (MC-2 `refreshList()`, visualization refresh, approvals cleanup). */
  | { readonly type: 'turn_ended' }
  /** `nested_session_event`: feed `event` into the open agent view `localId` (§4.3 routeToAgentView). */
  | { readonly type: 'nested_event'; readonly localId: string; readonly event: ServerFrame }
  /** Pool watcher event on the orchestrator WS (§3.7), for the session directory. */
  | { readonly type: 'watcher'; readonly frame: AgentSessionOpenedFrame | AgentSessionClosedFrame }
  /** Protocol error (§4.4.4): log + transient toast. */
  | { readonly type: 'protocol_error'; readonly code: string; readonly detail: string | null }
  /** `error{orchestrator_stopping}`: re-send `start` after the delay, once (T-12). */
  | { readonly type: 'retry_start'; readonly delayMs: number };

export interface StepResult<S> {
  readonly state: S;
  readonly effects: readonly Effect[];
}
