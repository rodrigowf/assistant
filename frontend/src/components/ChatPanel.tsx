import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import type { ChatMessage, SessionStatus, ConnectionState } from "../types";

interface Props {
  messages: ChatMessage[];
  status: SessionStatus;
  connectionState: ConnectionState;
  cost: number;
  turns: number;
  error: string | null;
  onSend: (text: string) => void;
  onInterrupt: () => void;
}

export function ChatPanel({
  messages,
  status,
  connectionState,
  cost,
  turns,
  error,
  onSend,
  onInterrupt,
}: Props) {
  const isStreaming = status === "streaming" || status === "thinking" || status === "tool_use";

  return (
    <main className="chat-panel">
      <MessageList messages={messages} />
      {error && (
        <div className="error-banner">{error}</div>
      )}
      <ChatInput
        onSend={onSend}
        onInterrupt={onInterrupt}
        disabled={status === "disconnected" || status === "connecting"}
        streaming={isStreaming}
      />
      <StatusBar
        status={status}
        connectionState={connectionState}
        cost={cost}
        turns={turns}
      />
    </main>
  );
}
