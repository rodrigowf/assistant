import { useEffect, useRef, useCallback, useState } from "react";
import type { ChatMessage } from "../types";
import { Message } from "./Message";

interface Props {
  messages: ChatMessage[];
  isActive?: boolean;
  hasMoreMessages?: boolean;
  onLoadMore?: () => Promise<void>;
}

const NEAR_BOTTOM_THRESHOLD = 150;
const LOAD_MORE_THRESHOLD = 80; // px from top to trigger load

export function MessageList({ messages, isActive, hasMoreMessages, onLoadMore }: Props) {
  const parentRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);
  const isNearBottomRef = useRef(true);
  const wasActiveRef = useRef(isActive);
  const isLoadingRef = useRef(false);
  const [showScrollButton, setShowScrollButton] = useState(false);
  // Tracks how many messages existed before a load-more, so we can scroll to the right spot after re-render
  const prependAnchorRef = useRef<number | null>(null);

  const scrollToBottom = useCallback(() => {
    const el = parentRef.current;
    if (el && el.clientHeight > 0) {
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setShowScrollButton(false);
    }
  }, []);

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distanceFromBottom <= NEAR_BOTTOM_THRESHOLD;
    setShowScrollButton(distanceFromBottom > NEAR_BOTTOM_THRESHOLD);

    // Trigger load-more when scrolled near the top
    if (el.scrollTop <= LOAD_MORE_THRESHOLD && hasMoreMessages && onLoadMore && !isLoadingRef.current) {
      isLoadingRef.current = true;
      // Snapshot message count before load — scroll restoration happens in the messages.length effect after re-render
      prependAnchorRef.current = el.querySelectorAll('.message').length;
      onLoadMore().finally(() => {
        isLoadingRef.current = false;
      });
    }
  }, [hasMoreMessages, onLoadMore]);

  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [handleScroll]);

  // Keep a stable ref to handleScroll so the isActive effect doesn't re-run
  // every time hasMoreMessages changes (which would cause a load loop).
  const handleScrollRef = useRef(handleScroll);
  handleScrollRef.current = handleScroll;

  // When tab becomes active: scroll to bottom only if we were near the bottom,
  // otherwise re-evaluate scroll position (may trigger load-more if at top).
  useEffect(() => {
    if (isActive && !wasActiveRef.current) {
      requestAnimationFrame(() => {
        if (isNearBottomRef.current) {
          scrollToBottom();
        } else {
          // Panel was display:none — re-evaluate scroll state.
          handleScrollRef.current();
        }
      });
    }
    wasActiveRef.current = isActive;
  }, [isActive, scrollToBottom]);

  // After messages change: either restore scroll after a prepend, or auto-scroll if near bottom
  useEffect(() => {
    if (messages.length !== prevCountRef.current) {
      if (prependAnchorRef.current !== null) {
        // Messages were prepended — scroll to just above the first old message
        const el = parentRef.current;
        if (el) {
          const allMsgs = el.querySelectorAll('.message');
          const addedCount = allMsgs.length - prependAnchorRef.current;
          const firstOldMsg = allMsgs[addedCount] as HTMLElement | undefined;
          if (firstOldMsg) {
            el.scrollTop = firstOldMsg.offsetTop - 16;
          }
        }
        prependAnchorRef.current = null;
      } else if (isNearBottomRef.current) {
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
    <div className="message-list-wrapper">
      <div className="message-list" ref={parentRef}>
        {hasMoreMessages && (
          <div className="load-more-indicator">
            <span>Scroll up for older messages</span>
          </div>
        )}
        <div className="message-list-inner">
          {messages.map((msg) => (
            <Message key={msg.id} message={msg} />
          ))}
        </div>
      </div>
      {showScrollButton && (
        <button className="scroll-to-bottom-btn" onClick={scrollToBottom} aria-label="Scroll to bottom">
          ↓
        </button>
      )}
    </div>
  );
}
