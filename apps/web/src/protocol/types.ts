/**
 * Domain model of the client data layer (spec 12 §2). Plain, serializable data: no Maps, no
 * class instances, no cross-object references (spec 13 §3.2). The reducer treats every value as
 * immutable and shares unchanged entries and blocks between states (structural sharing).
 *
 * Deviations from the spec 12 §2.3 sketch, all representational:
 * - `tools` / `perms` maps are not stored; lookups scan `entries` (I-11 guarantees uniqueness).
 * - `orphanResults` is an ordered array (insertion order is part of the fixture contract and a
 *   plain object would reorder integer-like keys).
 * - `openVoiceUser` and `pendingSplit` hold ids / snapshots instead of object references, and a
 *   block's `continuationOf` is the already-shown prefix text.
 * - Every entry and block carries a stable client `id` (React keys), minted from `nextId`.
 */
import type { ClientMessage } from './wire/client';
import type { ServerFrame } from './wire/server';

export type SessionKind = 'agent' | 'orchestrator';
/**
 * A session harness id from the backend's registry (`claude`, `qwen`, `gemini`, `codex`,
 * `modelstudio`, …). Open-ended on purpose: a new harness needs no web edit; labels come from
 * `@/stores` `providerLabel`.
 */
export type Provider = string;

/** `GET /api/sessions/pool/live` row status (`manager/types.py:15-23`). */
export type LiveStatus = 'idle' | 'streaming' | 'tool_use' | 'thinking' | 'interrupted' | 'disconnected';

/** spec 12 §2.2 */
export interface SessionRef {
  readonly localId: string;
  readonly sdkId: string | null;
  readonly kind: SessionKind;
  readonly provider: Provider | null;
  readonly live: boolean;
  readonly liveStatus: LiveStatus | null;
}

/** spec 12 §2.5 */
export type SessionStatus =
  | 'connecting'
  | 'idle'
  | 'processing'
  | 'streaming'
  | 'thinking'
  | 'tool_use'
  | 'retrying'
  | 'compacting'
  | 'stopped'
  | 'terminated';

/** spec 12 §3.3 */
export type ConnState = 'offline' | 'connecting' | 'open' | 'subscribed' | 'failed';

export type BlockScope = 'turn' | 'voice';
export type BlockOrigin = 'live' | 'history';

export interface TextBlock {
  readonly id: string;
  readonly type: 'text';
  readonly text: string;
  readonly streaming: boolean;
  readonly scope: BlockScope;
  readonly origin: BlockOrigin;
  /** Closed because another block was appended after it (I-4); used by the overlap dedupe (§5.4). */
  readonly implicitlyClosed?: boolean;
  /** The prefix already shown by the block this one continues (I-5, split by an interleaved entry). */
  readonly continuationOf?: string;
}

export interface ThinkingBlock {
  readonly id: string;
  readonly type: 'thinking';
  readonly text: string;
  readonly streaming: boolean;
  readonly scope: BlockScope;
  readonly origin: BlockOrigin;
  readonly implicitlyClosed?: boolean;
  readonly continuationOf?: string;
}

export type ToolStatus = 'running' | 'done' | 'error' | 'no_result';

export interface ToolProgress {
  readonly elapsed_seconds: number;
  readonly message: string;
}

export interface ToolBlock {
  readonly id: string;
  readonly type: 'tool';
  readonly tool_use_id: string;
  readonly tool_name: string;
  readonly tool_input: Readonly<Record<string, unknown>>;
  readonly status: ToolStatus;
  /** `null` until a result arrives (normalised to a string, R-3). */
  readonly output: string | null;
  /** Attached by position to the single running card (R-4). */
  readonly inferred?: boolean;
  readonly executing?: boolean;
  readonly progress?: ToolProgress;
  readonly scope: BlockScope;
  readonly origin: BlockOrigin;
}

export type PermissionState = 'pending' | 'allowed' | 'denied';
export type PermissionResponder = 'user' | 'orchestrator' | 'system';

export interface PermissionBlock {
  readonly id: string;
  readonly type: 'permission';
  readonly request_id: string;
  readonly tool_name: string;
  readonly tool_input: Readonly<Record<string, unknown>>;
  readonly state: PermissionState;
  readonly responder: PermissionResponder | null;
  readonly message: string | null;
  readonly scope: BlockScope;
  readonly origin: BlockOrigin;
}

export type Block = TextBlock | ThinkingBlock | ToolBlock | PermissionBlock;
export type StreamBlock = TextBlock | ThinkingBlock;

export type UserOrigin = 'local' | 'echo' | 'voice' | 'audio' | 'inject' | 'history';

export interface UserEntry {
  readonly id: string;
  readonly kind: 'user';
  readonly text: string;
  readonly origin: UserOrigin;
  /** `pending`: a local inject awaiting its echo (§7.9). */
  readonly state: 'sent' | 'pending';
  /** true only while a Gemini transcript is still growing (§4.7). */
  readonly streaming?: boolean;
}

export interface AssistantEntry {
  readonly id: string;
  readonly kind: 'assistant';
  readonly blocks: readonly Block[];
}

export type NoticeKind = 'error' | 'interrupted' | 'compaction' | 'background' | 'command';

export interface NoticeEntry {
  readonly id: string;
  readonly kind: 'notice';
  readonly notice: NoticeKind;
  readonly text: string;
  readonly data?: Readonly<Record<string, unknown>>;
}

export type Entry = UserEntry | AssistantEntry | NoticeEntry;

/** A tool result as delivered to the reducer (normalised, R-3). */
export interface ToolResultData {
  readonly output: string;
  readonly is_error: boolean;
  readonly origin: 'live' | 'history' | 'reconcile';
  readonly inferred?: boolean;
}

/** A result whose `tool_use` is not known yet (R-2). */
export interface OrphanResult extends ToolResultData {
  readonly tool_use_id: string;
}

export interface QueuedPrompt {
  readonly text: string;
  readonly owner: 'local' | 'remote';
}

export interface StallInfo {
  readonly elapsed_seconds: number;
  readonly last_tool_name: string | null;
  readonly last_tool_use_id: string | null;
}

export interface TerminationInfo {
  readonly reason: string;
  readonly detail: string | null;
  readonly sdk_session_id: string | null;
}

/** Transport / start problems: a banner, never an entry (I-15). */
export interface ConnectionBanner {
  readonly code: string;
  readonly detail: string | null;
}

export interface Checkpoint {
  readonly stream_id: string;
  readonly seq: number;
}

export interface Counters {
  readonly cost: number;
  readonly turns: number;
  readonly contextTokens: number | null;
  readonly contextWindow: number | null;
}

export interface HistoryState {
  readonly loaded: boolean;
  readonly startIndex: number;
  readonly totalCount: number;
  readonly hasMore: boolean;
}

/** A streaming block that an interleaved entry closed; a continuation may follow (I-5). */
export interface PendingSplit {
  readonly id: string;
  readonly type: 'text' | 'thinking';
  readonly scope: BlockScope;
  readonly text: string;
}

/** An agent permission request seen through `nested_session_event` (§6.9, orchestrator view). */
export interface AgentApproval {
  readonly localId: string;
  readonly request_id: string;
  readonly tool_name: string;
  readonly tool_input: Readonly<Record<string, unknown>>;
}

/** spec 12 §2.3 */
export interface Conversation {
  readonly ref: SessionRef;
  readonly entries: readonly Entry[];
  readonly orphanResults: readonly OrphanResult[];
  readonly unattributed: readonly ToolResultData[];
  readonly queue: readonly QueuedPrompt[];
  readonly dispatchedFromTray: readonly string[];

  readonly status: SessionStatus;
  readonly inTurn: boolean;
  readonly turnDepth: number;
  readonly turnIsLocal: boolean;
  readonly turnHasContent: boolean;
  readonly localTurnsPending: number;
  readonly promptSinceTurnEnd: boolean;
  readonly compactPending: boolean;
  readonly pendingInjects: readonly string[];
  readonly voiceActive: boolean;
  readonly openVoiceUserId: string | null;
  readonly speechAnchor: number | null;

  readonly stall: StallInfo | null;
  readonly termination: TerminationInfo | null;
  readonly connectionBanner: ConnectionBanner | null;
  readonly gapPossible: boolean;
  readonly agentApprovals: readonly AgentApproval[];

  readonly checkpoint: Checkpoint | null;
  readonly counters: Counters;
  readonly history: HistoryState;

  // reducer bookkeeping
  readonly pendingSplit: PendingSplit | null;
  readonly expectStopAck: boolean;
  /** An inferred result was applied in the current turn (R-7 trigger). */
  readonly turnInferred: boolean;
  readonly nextId: number;

  // connection-manager bookkeeping (§3.6, §5.2)
  readonly conn: ConnState;
  readonly awaitingSessionStarted: boolean;
  readonly startRequest: ClientMessage | null;
  readonly preStart: readonly ServerFrame[];
  readonly reloading: boolean;
  readonly reloadBuffer: readonly ServerFrame[];
  /** `error{orchestrator_stopping}` was already retried once since the last `start` (T-12). */
  readonly stoppingRetried: boolean;
  /**
   * The conversation is in the server's open set as far as this view knows (opened from the pool,
   * or subscribed here): every `start` carries `reattach` (OPEN-2) and a close by the server
   * closes the view (OPEN-3). False while a user action's `start` creates it, and after
   * `local_stop` (this client is closing or replacing it itself).
   */
  readonly reattach: boolean;
}

export const BUSY_STATUSES: readonly SessionStatus[] = [
  'processing',
  'streaming',
  'thinking',
  'tool_use',
  'retrying',
  'compacting',
];

/** `busy(status)` (spec 12 §2.5). */
export function busy(status: SessionStatus): boolean {
  return BUSY_STATUSES.indexOf(status) >= 0;
}

/** Only Claude agent sessions carry `seq` / `stream_id` (spec 12 §2.2). */
export function seqCapable(ref: SessionRef): boolean {
  return ref.kind === 'agent' && ref.provider === 'claude';
}
