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

  // Buffer new messages while the user is scrolled up, so the view isn't
  // pulled while they're reading. `displayedMessages` is the snapshot we
  // actually render; `messages` is the latest from upstream. When the user
  // returns to the bottom, the buffer is flushed in one pass.
  const [displayedMessages, setDisplayedMessages] = useState(messages);
  const isFrozenRef = useRef(false);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const bufferedCount = Math.max(0, messages.length - displayedMessages.length);

  // Sync displayed messages with upstream when not frozen. Always sync on
  // prepend (load-more) — that's a history insert at the top, not a new
  // message at the bottom, so it shouldn't be buffered.
  useEffect(() => {
    if (!isFrozenRef.current || prependAnchorRef.current !== null) {
      setDisplayedMessages(messages);
    }
  }, [messages]);

  const scrollToBottom = useCallback(() => {
    const el = parentRef.current;
    if (el && el.clientHeight > 0) {
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setShowScrollButton(false);
    }
  }, []);

  // Release the freeze and let the auto-scroll effects pull us to the new bottom.
  const flushBuffer = useCallback(() => {
    isFrozenRef.current = false;
    isNearBottomRef.current = true;
    setShowScrollButton(false);
    setDisplayedMessages(messagesRef.current);
  }, []);

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nearBottom = distanceFromBottom <= NEAR_BOTTOM_THRESHOLD;
    isNearBottomRef.current = nearBottom;
    setShowScrollButton(!nearBottom);

    if (nearBottom) {
      // Back at the bottom — release the freeze and flush any buffered messages.
      if (isFrozenRef.current) flushBuffer();
    } else {
      // Scrolled away from the bottom — freeze so incoming messages buffer
      // instead of extending the rendered list and shifting the viewport.
      isFrozenRef.current = true;
    }

    // Trigger load-more when scrolled near the top
    if (el.scrollTop <= LOAD_MORE_THRESHOLD && hasMoreMessages && onLoadMore && !isLoadingRef.current) {
      isLoadingRef.current = true;
      // Snapshot message count before load — scroll restoration happens in the messages.length effect after re-render
      prependAnchorRef.current = el.querySelectorAll('.message').length;
      onLoadMore().finally(() => {
        isLoadingRef.current = false;
      });
    }
  }, [hasMoreMessages, onLoadMore, flushBuffer]);

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

  // After displayed messages change: either restore scroll after a prepend, or auto-scroll if near bottom
  useEffect(() => {
    if (displayedMessages.length !== prevCountRef.current) {
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
    prevCountRef.current = displayedMessages.length;
  }, [displayedMessages.length, scrollToBottom]);

  // Scroll when the last displayed message's content changes (streaming)
  const lastMsg = displayedMessages[displayedMessages.length - 1];
  const lastMsgBlocks = lastMsg?.blocks.length ?? 0;
  useEffect(() => {
    if (isNearBottomRef.current) {
      scrollToBottom();
    }
  }, [lastMsgBlocks, scrollToBottom]);

  if (displayedMessages.length === 0) {
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
          {displayedMessages.map((msg) => (
            <Message key={msg.id} message={msg} />
          ))}
        </div>
      </div>
      {showScrollButton && (
        <button
          className="scroll-to-bottom-btn"
          onClick={flushBuffer}
          aria-label={bufferedCount > 0 ? `Show ${bufferedCount} new message${bufferedCount === 1 ? '' : 's'}` : 'Scroll to bottom'}
        >
          {bufferedCount > 0 ? `↓ ${bufferedCount} new` : '↓'}
        </button>
      )}
    </div>
  );
}
