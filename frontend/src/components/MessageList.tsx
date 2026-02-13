import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";
import { Message } from "./Message";

interface Props {
  messages: ChatMessage[];
}

export function MessageList({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="message-list empty">
        <div className="empty-state">
          <p className="empty-title">Start a conversation</p>
          <p className="empty-hint">Send a message to begin</p>
        </div>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((msg) => (
        <Message key={msg.id} message={msg} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
