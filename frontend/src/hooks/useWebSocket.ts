import { useRef, useCallback, useEffect, useState } from "react";
import type { ServerEvent, ConnectionState } from "../types";
import { ChatSocket, type EventHandler } from "../api/websocket";

/** Delay before attempting to reconnect after a drop (ms). */
const RECONNECT_DELAY_MS = 2000;
/** Max reconnect attempts before giving up. */
const MAX_RECONNECT_ATTEMPTS = 10;

interface UseWebSocketResult {
  send: (msg: Record<string, unknown>) => void;
  close: () => void;
  connectionState: ConnectionState;
}

export function useWebSocket(
  active: boolean,
  onEvent: EventHandler,
  onOpen?: () => void,
  endpoint?: string,
): UseWebSocketResult {
  const socketRef = useRef<ChatSocket | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  // Reconnection state — kept in refs to avoid re-triggering the effect.
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const activeRef = useRef(active);
  activeRef.current = active;
  // Track whether we intentionally closed (user action / unmount).
  const intentionalCloseRef = useRef(false);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const connectSocket = useCallback(
    (handlerFn: EventHandler, ep?: string) => {
      clearReconnectTimer();
      setConnectionState("connecting");

      const socket = new ChatSocket(handlerFn, ep);
      socket.connect(() => {
        reconnectAttemptsRef.current = 0;
        setConnectionState("connected");
        onOpenRef.current?.();
      });
      socketRef.current = socket;
    },
    [clearReconnectTimer],
  );

  const scheduleReconnect = useCallback(
    (handlerFn: EventHandler, ep?: string) => {
      if (!activeRef.current || intentionalCloseRef.current) return;
      if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) return;
      // Don't reconnect while the page is hidden (mobile background).
      if (document.hidden) return;

      clearReconnectTimer();
      reconnectAttemptsRef.current += 1;

      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (activeRef.current && !intentionalCloseRef.current) {
          connectSocket(handlerFn, ep);
        }
      }, RECONNECT_DELAY_MS);
    },
    [clearReconnectTimer, connectSocket],
  );

  // Stable handler that wraps onEvent and triggers reconnect on disconnect.
  const handlerRef = useRef<EventHandler | null>(null);

  useEffect(() => {
    // Build the event handler that also manages reconnection.
    const handler: EventHandler = (event: ServerEvent) => {
      if (event.type === "status" && event.status === "disconnected") {
        setConnectionState("disconnected");
        scheduleReconnect(handler, endpoint);
      } else if (event.type === "error" && event.error === "websocket_error") {
        setConnectionState("error");
        scheduleReconnect(handler, endpoint);
      }
      onEventRef.current(event);
    };
    handlerRef.current = handler;

    if (!active) {
      intentionalCloseRef.current = true;
      clearReconnectTimer();
      socketRef.current?.close();
      socketRef.current = null;
      setConnectionState("disconnected");
      return;
    }

    // Starting fresh — reset reconnect state.
    intentionalCloseRef.current = false;
    reconnectAttemptsRef.current = 0;
    connectSocket(handler, endpoint);

    // Reconnect when the page becomes visible again (mobile resume).
    const onVisibilityChange = () => {
      if (
        !document.hidden &&
        activeRef.current &&
        !intentionalCloseRef.current &&
        socketRef.current?.readyState !== WebSocket.OPEN
      ) {
        reconnectAttemptsRef.current = 0; // reset attempts on visibility
        connectSocket(handler, endpoint);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      intentionalCloseRef.current = true;
      clearReconnectTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      socketRef.current?.close();
      socketRef.current = null;
      setConnectionState("disconnected");
    };
  }, [active, endpoint, connectSocket, scheduleReconnect, clearReconnectTimer]);

  const send = useCallback((msg: Record<string, unknown>) => {
    socketRef.current?.send(msg);
  }, []);

  const close = useCallback(() => {
    intentionalCloseRef.current = true;
    clearReconnectTimer();
    socketRef.current?.close();
    socketRef.current = null;
    setConnectionState("disconnected");
  }, [clearReconnectTimer]);

  return { send, close, connectionState };
}
