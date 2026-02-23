import { useEffect, useRef, useCallback } from "react";
import type { ChatMessage } from "../types";
import { Message } from "./Message";

interface Props {
  messages: ChatMessage[];
  isActive?: boolean;
}

const NEAR_BOTTOM_THRESHOLD = 150;

export function MessageList({ messages, isActive }: Props) {
  const parentRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);
  const isNearBottomRef = useRef(true);
  const wasActiveRef = useRef(isActive);

  const scrollToBottom = useCallback(() => {
    const el = parentRef.current;
    if (el && el.clientHeight > 0) {
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
    }
  }, []);

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distanceFromBottom <= NEAR_BOTTOM_THRESHOLD;
  }, []);

  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [handleScroll]);

  // Scroll to bottom when tab becomes active
  useEffect(() => {
    if (isActive && !wasActiveRef.current) {
      requestAnimationFrame(() => scrollToBottom());
    }
    wasActiveRef.current = isActive;
  }, [isActive, scrollToBottom]);

  // Auto-scroll when new messages arrive (only if near bottom)
  useEffect(() => {
    if (messages.length !== prevCountRef.current) {
      if (isNearBottomRef.current) {
        scrollToBottom();
      }
    }
    prevCountRef.current = messages.length;
  }, [messages.length, scrollToBottom]);

  // Scroll when last message content changes (streaming)
  const lastMsg = messages[messages.length - 1];
  const lastMsgBlocks = lastMsg?.blocks.length ?? 0;
  useEffect(() => {
    if (isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [lastMsgBlocks, scrollToBottom]);

  if (messages.length === 0) {
    return (
      <div className="message-list empty" ref={parentRef}>
        <div className="empty-state">
          <p className="empty-title">Start a conversation</p>
          <p className="empty-hint">Send a message to begin</p>
        </div>
      </div>
    );
  }

  return (
    <div className="message-list" ref={parentRef}>
      <div className="message-list-inner">
        {messages.map((msg) => (
          <Message key={msg.id} message={msg} />
        ))}
      </div>
    </div>
  );
}
