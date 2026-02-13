// Types mirroring the API models and WebSocket protocol.

export interface SessionInfo {
  session_id: string;
  started_at: string;
  last_activity: string;
  title: string;
  message_count: number;
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
  | { type: "session_started"; session_id: string }
  | { type: "session_stopped" }
  | { type: "text_delta"; text: string }
  | { type: "text_complete"; text: string }
  | { type: "thinking_delta"; text: string }
  | { type: "thinking_complete"; text: string }
  | { type: "tool_use"; tool_use_id: string; tool_name: string; tool_input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; output: string; is_error: boolean }
  | { type: "turn_complete"; cost: number | null; usage: Record<string, unknown>; num_turns: number; session_id: string; is_error: boolean; result: string | null }
  | { type: "compact_complete"; trigger: string }
  | { type: "status"; status: string }
  | { type: "error"; error: string; detail?: string };

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
