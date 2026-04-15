// Compat shim for MessageList — iOS 12 / Safari 12 safe version.
//
// Problem: -webkit-overflow-scrolling: touch uses a native momentum scroller.
// Setting el.scrollTop programmatically while momentum is active is unreliable:
// the native scroller can ignore the assignment or lock scrollTop at 0,
// breaking all subsequent scroll events.
//
// Fix: before any programmatic scrollTop assignment, briefly set
// -webkit-overflow-scrolling to 'auto' (stops the native animation),
// assign scrollTop, then restore 'touch' on the next frame.

import { useEffect, useRef, useCallback, useState } from "react";
import type { ChatMessage } from "@/types";
import { Message } from "@/components/Message";

interface Props {
  messages: ChatMessage[];
  isActive?: boolean;
  hasMoreMessages?: boolean;
  onLoadMore?: () => Promise<void>;
}

const NEAR_BOTTOM_THRESHOLD = 150;
const LOAD_MORE_THRESHOLD = 80;

// Safely set scrollTop on iOS: stop momentum first, then restore it.
function iosScrollTo(el: HTMLElement, top: number) {
  // Cast needed — -webkit-overflow-scrolling is not in the TS types
  const style = el.style as CSSStyleDeclaration & { webkitOverflowScrolling: string };
  style.webkitOverflowScrolling = 'auto';
  el.scrollTop = top;
  requestAnimationFrame(() => {
    style.webkitOverflowScrolling = 'touch';
  });
}

// Hide/show the element around a prepend to mask the scroll jump.
// overflow-anchor:none (the main app's solution) is not supported in Safari 12.
function hideForFrame(el: HTMLElement) {
  el.style.visibility = 'hidden';
  requestAnimationFrame(() => {
    el.style.visibility = '';
  });
}

export function MessageList({ messages, isActive, hasMoreMessages, onLoadMore }: Props) {
  const parentRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);
  const isNearBottomRef = useRef(true);
  const wasActiveRef = useRef(isActive);
  const isLoadingRef = useRef(false);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const prependAnchorRef = useRef<number | null>(null);

  const scrollToBottom = useCallback(() => {
    const el = parentRef.current;
    if (el && el.clientHeight > 0) {
      iosScrollTo(el, el.scrollHeight);
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

    if (el.scrollTop <= LOAD_MORE_THRESHOLD && hasMoreMessages && onLoadMore && !isLoadingRef.current) {
      isLoadingRef.current = true;
      prependAnchorRef.current = el.querySelectorAll('.message').length;
      // Hide before React re-renders with prepended messages to mask the scroll jump.
      // (overflow-anchor:none doesn't work in Safari 12)
      hideForFrame(el);
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

  const handleScrollRef = useRef(handleScroll);
  handleScrollRef.current = handleScroll;

  useEffect(() => {
    if (isActive && !wasActiveRef.current) {
      requestAnimationFrame(() => {
        if (isNearBottomRef.current) {
          scrollToBottom();
        } else {
          handleScrollRef.current();
        }
      });
    }
    wasActiveRef.current = isActive;
  }, [isActive, scrollToBottom]);

  useEffect(() => {
    if (messages.length !== prevCountRef.current) {
      if (prependAnchorRef.current !== null) {
        const el = parentRef.current;
        if (el) {
          // Keep hidden through the scroll restoration in case the frame
          // from hideForFrame() already fired before React finished rendering.
          hideForFrame(el);
          const allMsgs = el.querySelectorAll('.message');
          const addedCount = allMsgs.length - prependAnchorRef.current;
          const firstOldMsg = allMsgs[addedCount] as HTMLElement | undefined;
          if (firstOldMsg) {
            iosScrollTo(el, firstOldMsg.offsetTop - 16);
          }
        }
        prependAnchorRef.current = null;
      } else if (isNearBottomRef.current) {
        scrollToBottom();
      }
    }
    prevCountRef.current = messages.length;
  }, [messages.length, scrollToBottom]);

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
