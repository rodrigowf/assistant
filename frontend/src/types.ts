// Types mirroring the API models and WebSocket protocol.

export interface SessionInfo {
  session_id: string;
  started_at: string;
  last_activity: string;
  title: string;
  message_count: number;
  is_orchestrator?: boolean;
  /** Which agent backed this session — registered harness id from /api/config/providers. */
  provider?: string;
  /** Set when the session is currently live in the pool — this is the stable tab key. */
  local_id?: string;
}

export interface ContentBlock {
  type: "text" | "tool_use" | "tool_result";
  text?: string | null;
  tool_use_id?: string | null;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  output?: string | null;
  is_error?: boolean;
}

export interface MessagePreview {
  role: string;
  text: string;
  blocks: ContentBlock[];
  timestamp: string | null;
}

export interface SessionDetail extends SessionInfo {
  messages: MessagePreview[];
}

export interface PaginatedMessages {
  messages: MessagePreview[];
  total_count: number;
  has_more: boolean;
  start_index: number;
}

// WebSocket event types (server → client)

export interface VoiceConnectionInfoPayload {
  connection_type: "webrtc" | "websocket";
  endpoint: string;
  ephemeral_token: string | null;
  expires_at: number | null;
  audio_in_format: { sample_rate: number; encoding: string };
  audio_out_format: { sample_rate: number; encoding: string };
  model: string;
  voice: string;
  audio_relay?: "backend";
}

/** Increment B (voice subsystem refactor): Silero VAD state surfaced
 *  to the UI alongside the existing ``input_audio_buffer.speech_*``
 *  events. ``listening`` = Silero in speech_started; ``thinking`` =
 *  speech_stopped just fired; ``idle`` = no manual VAD active. */
export type VadState = "idle" | "listening" | "thinking";

/** Typed voice-provider error categories. Mirrors
 *  ``orchestrator.voice_errors.VoiceErrorCategory`` — string values are
 *  the wire contract; don't rename without coordinating the backend
 *  classifier + Android parser. */
export type VoiceErrorCategory =
  | "quota_exceeded"
  | "rate_limit"
  | "auth"
  | "model_unavailable"
  | "context_full"
  | "network"
  | "provider_internal"
  | "unknown";

/** Payload of a backend ``voice_error`` event. Shape is the typed
 *  envelope built by ``VoiceError.to_event()`` in
 *  ``orchestrator/voice_errors.py``.
 *
 *  - ``recoverable=false`` means the backend already short-circuited
 *    reconnect; clients should render a terminal banner.
 *  - ``recoverable=true`` means a reconnect attempt is in flight; the
 *    banner should auto-clear on ``session_started`` of the new
 *    connection. */
export interface VoiceErrorEnvelope {
  category: VoiceErrorCategory;
  message: string;
  recoverable: boolean;
  recovery_hint: string | null;
  provider_doc_url: string | null;
  raw_close_code: number | null;
  raw_close_reason: string | null;
  provider: string;
}

/**
 * Resume-protocol metadata attached to live events.
 *
 * Backend stamps every broadcast payload with ``seq`` (monotonic per
 * receive-loop) and ``stream_id`` (changes when the SDK subprocess
 * reconnects).  Frontend persists the last-seen pair so a reconnecting
 * WS can ask the backend to replay events newer than that seq.  Older
 * backends without the protocol simply omit the fields and the client
 * treats those sessions as non-resumable.
 */
export interface ResumeProtocolFields {
  seq?: number;
  stream_id?: string;
}

/**
 * Resume-state snapshot the backend sends in ``session_started`` so a
 * fresh subscriber can immediately start tracking seqs from this point.
 * ``next_seq`` is the seq the NEXT dispatched event will use.
 */
export interface ResumeState {
  stream_id: string;
  next_seq: number;
}

export type ServerEvent =
  | ({
      type: "session_started";
      session_id: string;
      voice?: boolean;
      voice_session_update?: Record<string, unknown>;
      voice_provider?: string;
      voice_model?: string;
      voice_connection_info?: VoiceConnectionInfoPayload;
      voice_connection_error?: string;
      voice_recording_enabled?: boolean;
      /** True when this WS is the initiator of the voice session.
       * False for clients receiving session_started because another
       * device on the same orchestrator opened voice. Non-initiators
       * should mirror the voice UI state but not spin up their own
       * provider transport. Defaults to true on older backends. */
      voice_initiator?: boolean;
      /** Provider/model context window in tokens. Used by the compact-button %
       * counter. May be omitted when the backend has no opinion — frontend
       * falls back to a conservative default. */
      context_window?: number | null;
      /** Orchestrator-only — full model metadata including ``context_window``. */
      model_info?: { context_window?: number | null; [key: string]: unknown };
      /** Resume-protocol: backend's current checkpoint. Frontend stores
       * it so a future reconnect can ask for replay from this seq. */
      resume_state?: ResumeState;
      /** Resume-protocol: ``true`` when the backend couldn't replay
       * because the checkpoint was too old or referenced a stale stream.
       * Frontend falls back to a full REST refetch. */
      replay_overflow?: boolean;
    } & ResumeProtocolFields)
  | ({ type: "session_stopped" } & ResumeProtocolFields)
  | ({ type: "text_delta"; text: string } & ResumeProtocolFields)
  | ({ type: "text_complete"; text: string } & ResumeProtocolFields)
  | ({ type: "thinking_delta"; text: string } & ResumeProtocolFields)
  | ({ type: "thinking_complete"; text: string } & ResumeProtocolFields)
  | ({ type: "tool_use"; tool_use_id: string; tool_name: string; tool_input: Record<string, unknown> } & ResumeProtocolFields)
  | ({ type: "tool_executing"; tool_use_id: string; tool_name: string } & ResumeProtocolFields)
  | ({ type: "tool_progress"; tool_use_id: string; tool_name: string; elapsed_seconds: number; message?: string } & ResumeProtocolFields)
  | ({ type: "tool_result"; tool_use_id: string; output: string; is_error: boolean } & ResumeProtocolFields)
  | ({ type: "nested_session_event"; session_id: string; event_type: string; event_data: Record<string, unknown> } & ResumeProtocolFields)
  | ({ type: "turn_complete"; cost?: number | null; usage?: Record<string, unknown>; num_turns?: number; session_id?: string; is_error?: boolean; result?: string | null; input_tokens?: number; output_tokens?: number } & ResumeProtocolFields)
  | ({ type: "compact_complete"; trigger: string; summary?: string } & ResumeProtocolFields)
  | ({ type: "session_stalled"; elapsed_seconds: number; last_tool_name: string | null; last_tool_use_id: string | null } & ResumeProtocolFields)
  | ({ type: "permission_request"; request_id: string; tool_name: string; tool_input: Record<string, unknown> } & ResumeProtocolFields)
  | ({ type: "permission_resolved"; request_id: string; decision: "allow" | "deny"; responder: string; message?: string | null } & ResumeProtocolFields)
  | ({ type: "status"; status: string } & ResumeProtocolFields)
  | ({ type: "error"; error: string; detail?: string } & ResumeProtocolFields)
  | ({ type: "voice_error"; error: VoiceErrorEnvelope } & ResumeProtocolFields)
  | ({ type: "voice_vad_state"; state: VadState; duration_ms: number; silero_prob: number | null } & ResumeProtocolFields)
  | ({ type: "agent_session_opened"; session_id: string; sdk_session_id?: string } & ResumeProtocolFields)
  | ({ type: "agent_session_closed"; session_id: string } & ResumeProtocolFields)
  | ({ type: "user_message"; text: string; source?: string } & ResumeProtocolFields)
  | ({ type: "voice_command"; command: Record<string, unknown> } & ResumeProtocolFields)
  | ({ type: "voice_event"; event: RealtimeEvent } & ResumeProtocolFields)
  | ({ type: "voice_audio_out"; audio: string } & ResumeProtocolFields)
  | ({ type: "voice_ending"; reason?: string; session_id?: string } & ResumeProtocolFields)
  | ({ type: "voice_ended"; reason?: string; session_id?: string } & ResumeProtocolFields)
  | ({ type: "voice_stopped" } & ResumeProtocolFields)
  | { type: "ping" }
  | { type: "pong" };

// OpenAI Realtime API event types (subset used by voice integration)
export interface RealtimeEvent {
  type: string;
  [key: string]: unknown;
}

export type VoiceStatus =
  | "off"
  | "connecting"
  | "active"
  | "speaking"
  | "thinking"
  | "tool_use"
  | "ending"
  | "error";

// Chat state types

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  blocks: MessageBlock[];
}

export type MessageBlock =
  | { type: "text"; content: string; streaming: boolean }
  | { type: "thinking"; content: string; streaming: boolean }
  | { type: "compact"; content: string; streaming: boolean }
  | {
      type: "tool_use";
      toolUseId: string;
      toolName: string;
      toolInput: Record<string, unknown>;
      result?: string;
      isError?: boolean;
      complete: boolean;
      /** True when the tool has started executing (non-blocking status update) */
      executing?: boolean;
    };

export type SessionStatus =
  | "connecting"
  | "idle"
  | "streaming"
  | "thinking"
  | "tool_use"
  | "interrupted"
  | "disconnected";

export type ConnectionState =
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

// Tab system types

export type TabStatusIcon = "active" | "waiting" | "idle" | "error" | "loading";

export interface TabState {
  sessionId: string;         // Stable local ID (never changes)
  resumeSdkId?: string;      // SDK session ID for resuming from history
  title: string;
  status: SessionStatus;
  connectionState: ConnectionState;
  isOrchestrator?: boolean;
}

export interface TabsState {
  tabs: TabState[];
  activeTabId: string | null;
}
