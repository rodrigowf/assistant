import { useRef, useCallback, useEffect, useState } from "react";
import type { ServerEvent, ConnectionState } from "../types";
import { ChatSocket, type EventHandler } from "../api/websocket";

interface UseWebSocketResult {
  send: (msg: Record<string, unknown>) => void;
  close: () => void;
  connectionState: ConnectionState;
}

export function useWebSocket(
  active: boolean,
  onEvent: EventHandler,
  onOpen?: () => void
): UseWebSocketResult {
  const socketRef = useRef<ChatSocket | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;

  const handler = useCallback<EventHandler>(
    (event: ServerEvent) => {
      if (event.type === "status" && event.status === "disconnected") {
        setConnectionState("disconnected");
      } else if (event.type === "error" && event.error === "websocket_error") {
        setConnectionState("error");
      }
      onEvent(event);
    },
    [onEvent]
  );

  useEffect(() => {
    if (!active) {
      socketRef.current?.close();
      socketRef.current = null;
      setConnectionState("disconnected");
      return;
    }

    setConnectionState("connecting");
    const socket = new ChatSocket(handler);
    socket.connect(() => {
      setConnectionState("connected");
      onOpenRef.current?.();
    });
    socketRef.current = socket;

    return () => {
      socket.close();
      socketRef.current = null;
      setConnectionState("disconnected");
    };
  }, [active, handler]);

  const send = useCallback((msg: Record<string, unknown>) => {
    socketRef.current?.send(msg);
  }, []);

  const close = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    setConnectionState("disconnected");
  }, []);

  return { send, close, connectionState };
}
