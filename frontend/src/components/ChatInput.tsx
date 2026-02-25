import { useRef, useCallback, type KeyboardEvent, type ChangeEvent } from "react";

interface Props {
  onSend: (text: string) => void;
  onInterrupt: () => void;
  disabled: boolean;
  streaming: boolean;
  /** Number of active MCPs (shows badge if > 0) */
  activeMcpCount?: number;
  /** Called when user clicks the MCP settings button */
  onMcpSettings?: () => void;
}

export function ChatInput({ onSend, onInterrupt, disabled, streaming, activeMcpCount, onMcpSettings }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleInput = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, []);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        const text = textareaRef.current?.value.trim();
        if (text) {
          onSend(text);
          if (textareaRef.current) {
            textareaRef.current.value = "";
            textareaRef.current.style.height = "auto";
          }
        }
      }
    },
    [onSend]
  );

  return (
    <div className="chat-input">
      <textarea
        ref={textareaRef}
        placeholder={streaming ? "Waiting for response..." : "Send a message..."}
        disabled={disabled && !streaming}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        rows={1}
      />
      {streaming ? (
        <button className="interrupt-btn" onClick={onInterrupt} title="Interrupt">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <rect x="3" y="3" width="10" height="10" rx="1" />
          </svg>
        </button>
      ) : (
        <button
          className="send-btn"
          onClick={() => {
            const text = textareaRef.current?.value.trim();
            if (text) {
              onSend(text);
              if (textareaRef.current) {
                textareaRef.current.value = "";
                textareaRef.current.style.height = "auto";
              }
            }
          }}
          title="Send"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M1 1l14 7-14 7V9l10-1-10-1V1z" />
          </svg>
        </button>
      )}
      {onMcpSettings && (
        <button
          className="send-btn mcp-btn"
          onClick={onMcpSettings}
          title="MCP Server Settings"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
          {activeMcpCount !== undefined && activeMcpCount > 0 && (
            <span className="mcp-badge">{activeMcpCount}</span>
          )}
        </button>
      )}
    </div>
  );
}
