/**
 * Server → client frames of the chat WS (`/api/sessions/chat`) and the orchestrator WS
 * (`/api/orchestrator/chat`), with the backend's exact field names (inventory 01 §4.2, §5.3, §7.4;
 * `api/serializers.py`, `api/routes/chat.py`, `api/routes/orchestrator.py`, `api/pool.py`).
 *
 * Every frame may carry `seq` / `stream_id` (Claude agent sessions only, spec 12 §3.6).
 */

export interface Sequenced {
  seq?: number;
  stream_id?: string;
}

export interface ModelInfo {
  provider?: string;
  model_id?: string;
  display_name?: string;
  supports_audio?: boolean;
  supports_vision?: boolean;
  supports_tools?: boolean;
  max_tokens?: number;
  context_window?: number | null;
}

/** `orchestrator/config.py:278-286` */
export interface OrchestratorModelInfo {
  model?: string;
  provider?: string;
  max_tokens?: number;
  supports_audio?: boolean;
  model_info?: ModelInfo | null;
}

export interface ResumeState {
  stream_id: string;
  next_seq: number;
}

export interface SessionStartedFrame extends Sequenced {
  type: 'session_started';
  /** The conversation's `localId` (G-13, ID-1). */
  session_id: string;
  context_window?: number | null;
  resume_state?: ResumeState;
  replay_overflow?: boolean;
  // orchestrator
  voice?: boolean;
  voice_initiator?: boolean;
  model_info?: OrchestratorModelInfo | null;
  /** Backend O-3: the orchestrator's JSONL id. */
  jsonl_id?: string;
  voice_provider?: string;
  voice_model?: string;
  voice_name?: string;
  voice_transcription_language?: string;
  voice_recording_enabled?: boolean;
  voice_session_update?: Record<string, unknown>;
  voice_connection_info?: Record<string, unknown>;
  voice_connection_error?: string;
}

export interface StatusFrame extends Sequenced {
  type: 'status';
  status: string;
  detail?: string | null;
}

export interface UserMessageFrame extends Sequenced {
  type: 'user_message';
  text: string;
  queued?: boolean;
  source?: string;
}

export interface TextDeltaFrame extends Sequenced {
  type: 'text_delta';
  text: string;
}
export interface TextCompleteFrame extends Sequenced {
  type: 'text_complete';
  text: string;
}
export interface ThinkingDeltaFrame extends Sequenced {
  type: 'thinking_delta';
  text: string;
}
export interface ThinkingCompleteFrame extends Sequenced {
  type: 'thinking_complete';
  text: string;
}

export interface ToolUseFrame extends Sequenced {
  type: 'tool_use';
  tool_use_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
}

export interface ToolResultFrame extends Sequenced {
  type: 'tool_result';
  tool_use_id: string;
  /** A string from every agent path; may be any JSON value from Gemini voice (R-3). */
  output: unknown;
  is_error?: boolean;
}

export interface ToolExecutingFrame extends Sequenced {
  type: 'tool_executing';
  tool_use_id: string;
  tool_name?: string;
}

export interface ToolProgressFrame extends Sequenced {
  type: 'tool_progress';
  tool_use_id: string;
  tool_name?: string;
  elapsed_seconds: number;
  message: string;
}

export interface TurnCompleteFrame extends Sequenced {
  type: 'turn_complete';
  cost?: number | null;
  usage?: Record<string, unknown> | null;
  input_tokens?: number;
  output_tokens?: number;
  num_turns?: number;
  /** The `sdkId` (chat WS only). */
  session_id?: string | null;
  is_error?: boolean;
  result?: string | null;
}

export interface CompactCompleteFrame extends Sequenced {
  type: 'compact_complete';
  trigger?: string;
  summary?: string | null;
  tokens_before?: number;
  tokens_after?: number;
}

export interface SessionStalledFrame extends Sequenced {
  type: 'session_stalled';
  elapsed_seconds: number;
  last_tool_name?: string | null;
  last_tool_use_id?: string | null;
}

export interface PermissionRequestFrame extends Sequenced {
  type: 'permission_request';
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
}

export interface PermissionResolvedFrame extends Sequenced {
  type: 'permission_resolved';
  request_id: string;
  decision: string;
  responder?: string | null;
  message?: string | null;
}

export interface SessionTerminatedFrame extends Sequenced {
  type: 'session_terminated';
  reason: string;
  detail?: string | null;
  sdk_session_id?: string | null;
}

export interface SessionStoppedFrame extends Sequenced {
  type: 'session_stopped';
}

export interface ErrorFrame extends Sequenced {
  type: 'error';
  /** A code string. (Inside `voice_event` the legacy `error` is an object, G-28; not this frame.) */
  error: string;
  detail?: string | null;
}

export interface ModelChangedFrame extends Sequenced {
  type: 'model_changed';
  model_info?: OrchestratorModelInfo | null;
}
export interface ModelInfoFrame extends Sequenced {
  type: 'model_info';
  model_info?: OrchestratorModelInfo | null;
}
export interface ModelsListFrame extends Sequenced {
  type: 'models_list';
  models?: ModelInfo[];
}

export interface NestedSessionEventFrame extends Sequenced {
  type: 'nested_session_event';
  /** The agent's `localId`. */
  session_id: string;
  event_type: string;
  event_data: Record<string, unknown>;
}

export interface AgentSessionOpenedFrame extends Sequenced {
  type: 'agent_session_opened';
  session_id: string;
  sdk_session_id?: string | null;
  is_orchestrator?: boolean;
}

export interface AgentSessionClosedFrame extends Sequenced {
  type: 'agent_session_closed';
  session_id: string;
  is_orchestrator?: boolean;
}

/**
 * §3.7 turn watcher events: an agent session (any harness, never the orchestrator) started or
 * ended a turn, from any device or delegated by the orchestrator. Pool watchers only (every
 * orchestrator socket); handled at the channel level (device notifications), never by a reducer.
 */
export interface AgentTurnStartedFrame extends Sequenced {
  type: 'agent_turn_started';
  /** The agent's `localId`. */
  session_id: string;
  sdk_session_id?: string | null;
  provider?: string | null;
}

export type AgentTurnStatus = 'ok' | 'error' | 'interrupted';

export interface AgentTurnFinishedFrame extends Sequenced {
  type: 'agent_turn_finished';
  /** The agent's `localId`. */
  session_id: string;
  sdk_session_id?: string | null;
  provider?: string | null;
  /** The session's title when the server knows it (custom title or first prompt). */
  title?: string | null;
  /** `interrupted` = stopped by someone: no notification. Unknown values read as `ok`. */
  status: string;
  /** One line from the turn's final assistant text (≤ 200 chars). */
  preview?: string | null;
  /** Failure detail when `status == "error"`. */
  error?: string | null;
}

/**
 * §6.11a: the orchestrator's `switch_conversation` tool moved the user into a past orchestrator
 * conversation. Sent to ONE socket (the voice owner, else the last text sender) after the server
 * ended voice and stopped the old orchestrator; handled at the channel level (SW-1).
 */
export interface OrchestratorSwitchFrame extends Sequenced {
  type: 'orchestrator_switch';
  /** The JSONL id of the past conversation to resume. */
  sdk_session_id: string;
  title?: string | null;
  /** Voice was live: start it on the resumed view (SW-2). */
  voice?: boolean | null;
  /** The old (now stopped) orchestrator's `localId`. */
  from_session_id?: string | null;
}

/** `created` | `modified` | `deleted` (advisory: an atomic save or rsync reports a create). */
export type ContentChangeKind = 'created' | 'modified' | 'deleted';

export interface ContentChange {
  path: string;
  kind: ContentChangeKind;
}

/**
 * §9.3: files under `context/public/` changed (content watcher, pushed to every orchestrator
 * socket like the watcher events). `visualizations` are list paths (`GET /api/visualizations`)
 * whose page or assets changed; `files` the raw changed paths. Handled at the channel level.
 */
export interface VisualizationChangedFrame extends Sequenced {
  type: 'visualization_changed';
  visualizations?: ContentChange[] | null;
  files?: ContentChange[] | null;
}

/** §9.3: markdown under the memory tree changed (paths as in `GET /api/memory/tree`). */
export interface MemoryChangedFrame extends Sequenced {
  type: 'memory_changed';
  changes?: ContentChange[] | null;
}

export interface AudioUploadFrame extends Sequenced {
  type: 'audio_upload';
}

export interface PingFrame extends Sequenced {
  type: 'ping';
}

export interface VoiceEventFrame extends Sequenced {
  type: 'voice_event';
  /** Provider-native event (OpenAI-style typed, or Gemini type-less camelCase). */
  event: Record<string, unknown>;
}

export interface VoiceOwnerActiveFrame extends Sequenced {
  type: 'voice_owner_active';
  active: boolean;
  owner_local_id?: string | null;
}

export interface VoiceCommandFrame extends Sequenced {
  type: 'voice_command';
  command: Record<string, unknown>;
}

export interface VoiceAudioOutFrame extends Sequenced {
  type: 'voice_audio_out';
  audio: string;
}

export interface VoiceConnectionErrorFrame extends Sequenced {
  type: 'voice_connection_error';
  detail?: string | null;
}

export interface VoiceEndingFrame extends Sequenced {
  type: 'voice_ending';
  reason?: string;
  session_id?: string;
}
export interface VoiceEndedFrame extends Sequenced {
  type: 'voice_ended';
  reason?: string;
  session_id?: string;
}
export interface VoiceStoppedFrame extends Sequenced {
  type: 'voice_stopped';
}

/** Serializer fallback (`serializers.py:206,270`) and any type this client does not know. */
export interface UnknownFrame extends Sequenced {
  type: 'unknown';
  /** The original `type` string. */
  original_type: string;
  raw: Record<string, unknown>;
}

export type ServerFrame =
  | SessionStartedFrame
  | StatusFrame
  | UserMessageFrame
  | TextDeltaFrame
  | TextCompleteFrame
  | ThinkingDeltaFrame
  | ThinkingCompleteFrame
  | ToolUseFrame
  | ToolResultFrame
  | ToolExecutingFrame
  | ToolProgressFrame
  | TurnCompleteFrame
  | CompactCompleteFrame
  | SessionStalledFrame
  | PermissionRequestFrame
  | PermissionResolvedFrame
  | SessionTerminatedFrame
  | SessionStoppedFrame
  | ErrorFrame
  | ModelChangedFrame
  | ModelInfoFrame
  | ModelsListFrame
  | NestedSessionEventFrame
  | AgentSessionOpenedFrame
  | AgentSessionClosedFrame
  | OrchestratorSwitchFrame
  | AgentTurnStartedFrame
  | AgentTurnFinishedFrame
  | VisualizationChangedFrame
  | MemoryChangedFrame
  | AudioUploadFrame
  | PingFrame
  | VoiceEventFrame
  | VoiceOwnerActiveFrame
  | VoiceCommandFrame
  | VoiceAudioOutFrame
  | VoiceConnectionErrorFrame
  | VoiceEndingFrame
  | VoiceEndedFrame
  | VoiceStoppedFrame
  | UnknownFrame;

export type ServerFrameType = Exclude<ServerFrame['type'], 'unknown'>;

/** REST `ContentBlockResponse` (`api/models.py:52-59`; `thinking` since backend O-2). */
export interface ContentBlockPreview {
  type: 'text' | 'thinking' | 'tool_use' | 'tool_result' | string;
  text?: string | null;
  tool_use_id?: string | null;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  output?: unknown;
  is_error?: boolean | null;
}

/** REST `MessagePreviewResponse` (`api/models.py:62-66`). */
export interface MessagePreview {
  role: 'user' | 'assistant' | string;
  text?: string | null;
  blocks?: ContentBlockPreview[] | null;
  timestamp?: string | null;
}

/** REST `PaginatedMessagesResponse` (`api/models.py:73-79`). */
export interface MessagesPage {
  messages: MessagePreview[];
  total_count: number;
  has_more: boolean;
  start_index: number;
}
