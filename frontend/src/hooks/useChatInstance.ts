import { useReducer, useCallback, useRef, useEffect, useState } from "react";
import type {
  ChatMessage,
  MessageBlock,
  MessagePreview,
  ServerEvent,
  SessionStatus,
  ConnectionState,
} from "../types";
import { useWebSocket } from "./useWebSocket";
import { getSession } from "../api/rest";

// -------------------------------------------------------------------
// State
// -------------------------------------------------------------------

interface ChatState {
  messages: ChatMessage[];
  status: SessionStatus;
  sessionId: string | null;
  cost: number;
  turns: number;
  error: string | null;
}

const INITIAL_STATE: ChatState = {
  messages: [],
  status: "disconnected",
  sessionId: null,
  cost: 0,
  turns: 0,
  error: null,
};

// -------------------------------------------------------------------
// Actions
// -------------------------------------------------------------------

type Action =
  | { type: "RESET" }
  | { type: "LOAD_HISTORY"; messages: MessagePreview[] }
  | { type: "SESSION_STARTED"; sessionId: string }
  | { type: "USER_MESSAGE"; text: string }
  | { type: "TEXT_DELTA"; text: string }
  | { type: "TEXT_COMPLETE"; text: string }
  | { type: "THINKING_DELTA"; text: string }
  | { type: "THINKING_COMPLETE"; text: string }
  | { type: "TOOL_USE"; toolUseId: string; toolName: string; toolInput: Record<string, unknown> }
  | { type: "TOOL_RESULT"; toolUseId: string; output: string; isError: boolean }
  | { type: "TURN_COMPLETE"; cost: number | null; turns: number; sessionId: string }
  | { type: "STATUS"; status: SessionStatus }
  | { type: "ERROR"; error: string };

// -------------------------------------------------------------------
// Reducer
// -------------------------------------------------------------------

let msgCounter = 0;
function nextId(): string {
  return `msg-${++msgCounter}`;
}

function ensureAssistantMessage(messages: ChatMessage[]): ChatMessage[] {
  const last = messages[messages.length - 1];
  if (last && last.role === "assistant") return messages;
  return [...messages, { id: nextId(), role: "assistant", blocks: [] }];
}

function updateLastAssistantBlock(
  messages: ChatMessage[],
  updater: (blocks: MessageBlock[]) => MessageBlock[]
): ChatMessage[] {
  const msgs = ensureAssistantMessage(messages);
  const last = { ...msgs[msgs.length - 1] };
  last.blocks = updater([...last.blocks]);
  return [...msgs.slice(0, -1), last];
}

function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "RESET":
      return INITIAL_STATE;

    case "LOAD_HISTORY": {
      const toolResults = new Map<string, { output: string; isError: boolean }>();
      for (const m of action.messages) {
        for (const b of m.blocks) {
          if (b.type === "tool_result" && b.tool_use_id) {
            toolResults.set(b.tool_use_id, {
              output: b.output || "",
              isError: b.is_error || false,
            });
          }
        }
      }

      const messages: ChatMessage[] = [];
      for (const m of action.messages) {
        const hasNonToolResult = m.blocks.some(b => b.type !== "tool_result");
        if (m.role === "user" && !hasNonToolResult && m.blocks.length > 0) {
          continue;
        }

        const blocks: MessageBlock[] = [];
        for (const b of m.blocks) {
          if (b.type === "text") {
            if (b.text) {
              blocks.push({ type: "text", content: b.text, streaming: false });
            }
          } else if (b.type === "tool_use") {
            const result = b.tool_use_id ? toolResults.get(b.tool_use_id) : undefined;
            blocks.push({
              type: "tool_use",
              toolUseId: b.tool_use_id || "",
              toolName: b.tool_name || "",
              toolInput: (b.tool_input as Record<string, unknown>) || {},
              result: result?.output,
              isError: result?.isError,
              complete: true,
            });
          }
        }

        if (blocks.length === 0 && m.text) {
          blocks.push({ type: "text", content: m.text, streaming: false });
        }

        if (blocks.length > 0) {
          messages.push({
            id: nextId(),
            role: m.role as "user" | "assistant",
            blocks,
          });
        }
      }
      return { ...state, messages };
    }

    case "SESSION_STARTED":
      return { ...state, sessionId: action.sessionId, status: "idle", error: null };

    case "USER_MESSAGE":
      return {
        ...state,
        messages: [
          ...state.messages,
          {
            id: nextId(),
            role: "user",
            blocks: [{ type: "text", content: action.text, streaming: false }],
          },
        ],
      };

    case "TEXT_DELTA":
      return {
        ...state,
        status: "streaming",
        messages: updateLastAssistantBlock(state.messages, (blocks) => {
          const last = blocks[blocks.length - 1];
          if (last?.type === "text" && last.streaming) {
            return [
              ...blocks.slice(0, -1),
              { ...last, content: last.content + action.text },
            ];
          }
          return [...blocks, { type: "text", content: action.text, streaming: true }];
        }),
      };

    case "TEXT_COMPLETE":
      return {
        ...state,
        messages: updateLastAssistantBlock(state.messages, (blocks) => {
          const last = blocks[blocks.length - 1];
          if (last?.type === "text" && last.streaming) {
            return [...blocks.slice(0, -1), { ...last, content: action.text, streaming: false }];
          }
          return [...blocks, { type: "text", content: action.text, streaming: false }];
        }),
      };

    case "THINKING_DELTA":
      return {
        ...state,
        status: "thinking",
        messages: updateLastAssistantBlock(state.messages, (blocks) => {
          const last = blocks[blocks.length - 1];
          if (last?.type === "thinking" && last.streaming) {
            return [
              ...blocks.slice(0, -1),
              { ...last, content: last.content + action.text },
            ];
          }
          return [...blocks, { type: "thinking", content: action.text, streaming: true }];
        }),
      };

    case "THINKING_COMPLETE":
      return {
        ...state,
        messages: updateLastAssistantBlock(state.messages, (blocks) => {
          const last = blocks[blocks.length - 1];
          if (last?.type === "thinking" && last.streaming) {
            return [...blocks.slice(0, -1), { ...last, content: action.text, streaming: false }];
          }
          return [...blocks, { type: "thinking", content: action.text, streaming: false }];
        }),
      };

    case "TOOL_USE":
      return {
        ...state,
        status: "tool_use",
        messages: updateLastAssistantBlock(state.messages, (blocks) => [
          ...blocks,
          {
            type: "tool_use",
            toolUseId: action.toolUseId,
            toolName: action.toolName,
            toolInput: action.toolInput,
            complete: false,
          },
        ]),
      };

    case "TOOL_RESULT":
      return {
        ...state,
        messages: updateLastAssistantBlock(state.messages, (blocks) =>
          blocks.map((b) =>
            b.type === "tool_use" && b.toolUseId === action.toolUseId
              ? { ...b, result: action.output, isError: action.isError, complete: true }
              : b
          )
        ),
      };

    case "TURN_COMPLETE":
      return {
        ...state,
        status: "idle",
        cost: state.cost + (action.cost ?? 0),
        turns: state.turns + action.turns,
        sessionId: action.sessionId || state.sessionId,
      };

    case "STATUS":
      return { ...state, status: action.status };

    case "ERROR":
      return { ...state, error: action.error };

    default:
      return state;
  }
}

// -------------------------------------------------------------------
// Hook
// -------------------------------------------------------------------

export interface ChatInstance {
  messages: ChatMessage[];
  status: SessionStatus;
  connectionState: ConnectionState;
  sessionId: string | null;
  cost: number;
  turns: number;
  error: string | null;
  send: (text: string) => void;
  command: (text: string) => void;
  interrupt: () => void;
}

interface UseChatInstanceOptions {
  /** Session ID to resume. If null, starts a new session. */
  resumeId: string | null;
  /** Called when a turn completes (to refresh session list). */
  onSessionChange?: () => void;
  /** Called when status or connection state changes (for tab status sync). */
  onStatusChange?: (status: SessionStatus, connectionState: ConnectionState) => void;
  /** Called when session is established and we have a session ID. */
  onSessionStarted?: (sessionId: string) => void;
  /** WebSocket endpoint path (default: /api/sessions/chat). */
  wsEndpoint?: string;
  /** Skip loading history from REST API (for orchestrator sessions). */
  skipHistory?: boolean;
  /** Called when pool notifies that an agent session was opened. */
  onAgentSessionOpened?: (sessionId: string) => void;
  /** Called when pool notifies that an agent session was closed. */
  onAgentSessionClosed?: (sessionId: string) => void;
}

export function useChatInstance(options: UseChatInstanceOptions): ChatInstance {
  const { resumeId, onSessionChange, onStatusChange, onSessionStarted, wsEndpoint, skipHistory, onAgentSessionOpened, onAgentSessionClosed } = options;
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const [wsActive, setWsActive] = useState(false);
  const pendingStartRef = useRef<{ resumeId: string | null } | null>(null);

  // Stable refs for callbacks
  const onSessionChangeRef = useRef(onSessionChange);
  onSessionChangeRef.current = onSessionChange;
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  const onSessionStartedRef = useRef(onSessionStarted);
  onSessionStartedRef.current = onSessionStarted;
  const onAgentSessionOpenedRef = useRef(onAgentSessionOpened);
  onAgentSessionOpenedRef.current = onAgentSessionOpened;
  const onAgentSessionClosedRef = useRef(onAgentSessionClosed);
  onAgentSessionClosedRef.current = onAgentSessionClosed;

  // Track status changes and notify parent
  const prevStatusRef = useRef<{ status: SessionStatus; conn: ConnectionState } | null>(null);

  const handleEvent = useCallback((event: ServerEvent) => {
    switch (event.type) {
      case "session_started":
        dispatch({ type: "SESSION_STARTED", sessionId: event.session_id });
        onSessionStartedRef.current?.(event.session_id);
        break;
      case "text_delta":
        dispatch({ type: "TEXT_DELTA", text: event.text });
        break;
      case "text_complete":
        dispatch({ type: "TEXT_COMPLETE", text: event.text });
        break;
      case "thinking_delta":
        dispatch({ type: "THINKING_DELTA", text: event.text });
        break;
      case "thinking_complete":
        dispatch({ type: "THINKING_COMPLETE", text: event.text });
        break;
      case "tool_use":
        dispatch({
          type: "TOOL_USE",
          toolUseId: event.tool_use_id,
          toolName: event.tool_name,
          toolInput: event.tool_input,
        });
        break;
      case "tool_result":
        dispatch({
          type: "TOOL_RESULT",
          toolUseId: event.tool_use_id,
          output: event.output,
          isError: event.is_error,
        });
        break;
      case "turn_complete":
        dispatch({
          type: "TURN_COMPLETE",
          cost: event.cost ?? null,
          turns: event.num_turns ?? 1,
          sessionId: event.session_id ?? "",
        });
        onSessionChangeRef.current?.();
        break;
      case "status":
        dispatch({ type: "STATUS", status: event.status as SessionStatus });
        break;
      case "error":
        dispatch({ type: "ERROR", error: event.detail || event.error });
        break;
      case "session_stopped":
        dispatch({ type: "STATUS", status: "disconnected" });
        break;
      case "agent_session_opened":
        onAgentSessionOpenedRef.current?.(event.session_id);
        break;
      case "agent_session_closed":
        onAgentSessionClosedRef.current?.(event.session_id);
        break;
      case "user_message":
        dispatch({ type: "USER_MESSAGE", text: event.text });
        break;
    }
  }, []);

  const handleOpen = useCallback(() => {
    const pending = pendingStartRef.current;
    if (pending) {
      pendingStartRef.current = null;
      if (pending.resumeId) {
        wsSendRef.current?.({ type: "start", session_id: pending.resumeId });
      } else {
        wsSendRef.current?.({ type: "start" });
      }
    }
  }, []);

  const { send: wsSend, close: wsClose, connectionState } = useWebSocket(wsActive, handleEvent, handleOpen, wsEndpoint);
  const wsSendRef = useRef(wsSend);
  wsSendRef.current = wsSend;

  // Notify parent of status/connection changes
  useEffect(() => {
    const prev = prevStatusRef.current;
    if (!prev || prev.status !== state.status || prev.conn !== connectionState) {
      prevStatusRef.current = { status: state.status, conn: connectionState };
      onStatusChangeRef.current?.(state.status, connectionState);
    }
  }, [state.status, connectionState]);

  // Auto-connect on mount
  useEffect(() => {
    let cancelled = false;

    async function init() {
      dispatch({ type: "RESET" });

      if (resumeId && !skipHistory) {
        try {
          const detail = await getSession(resumeId);
          if (cancelled) return;
          dispatch({ type: "LOAD_HISTORY", messages: detail.messages });
        } catch {
          // Session may not exist anymore
        }
      }

      if (cancelled) return;
      pendingStartRef.current = { resumeId };
      setWsActive(true);
    }

    init();

    return () => {
      cancelled = true;
      wsClose();
      setWsActive(false);
    };
  }, [resumeId, wsClose]);

  const send = useCallback(
    (text: string) => {
      dispatch({ type: "USER_MESSAGE", text });
      wsSend({ type: "send", text });
    },
    [wsSend]
  );

  const command = useCallback(
    (text: string) => {
      wsSend({ type: "command", text });
    },
    [wsSend]
  );

  const interrupt = useCallback(() => {
    wsSend({ type: "interrupt" });
  }, [wsSend]);

  return {
    messages: state.messages,
    status: state.status,
    connectionState,
    sessionId: state.sessionId,
    cost: state.cost,
    turns: state.turns,
    error: state.error,
    send,
    command,
    interrupt,
  };
}
