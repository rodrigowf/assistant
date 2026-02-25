// Types mirroring the API models and WebSocket protocol.

export interface SessionInfo {
  session_id: string;
  started_at: string;
  last_activity: string;
  title: string;
  message_count: number;
  is_orchestrator?: boolean;
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

// WebSocket event types (server → client)

export type ServerEvent =
  | { type: "session_started"; session_id: string; voice?: boolean; voice_session_update?: Record<string, unknown> }
  | { type: "session_stopped" }
  | { type: "text_delta"; text: string }
  | { type: "text_complete"; text: string }
  | { type: "thinking_delta"; text: string }
  | { type: "thinking_complete"; text: string }
  | { type: "tool_use"; tool_use_id: string; tool_name: string; tool_input: Record<string, unknown> }
  | { type: "tool_executing"; tool_use_id: string; tool_name: string }
  | { type: "tool_progress"; tool_use_id: string; tool_name: string; elapsed_seconds: number; message?: string }
  | { type: "tool_result"; tool_use_id: string; output: string; is_error: boolean }
  | { type: "nested_session_event"; session_id: string; event_type: string; event_data: Record<string, unknown> }
  | { type: "turn_complete"; cost?: number | null; usage?: Record<string, unknown>; num_turns?: number; session_id?: string; is_error?: boolean; result?: string | null; input_tokens?: number; output_tokens?: number }
  | { type: "compact_complete"; trigger: string }
  | { type: "status"; status: string }
  | { type: "error"; error: string; detail?: string }
  | { type: "agent_session_opened"; session_id: string; sdk_session_id?: string }
  | { type: "agent_session_closed"; session_id: string }
  | { type: "user_message"; text: string; source?: string }
  | { type: "voice_command"; command: Record<string, unknown> };

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
