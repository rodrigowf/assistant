import { useRef, useCallback, type KeyboardEvent, type ChangeEvent } from "react";

interface Props {
  onSend: (text: string) => void;
  onInterrupt: () => void;
  disabled: boolean;
  streaming: boolean;
}

export function ChatInput({ onSend, onInterrupt, disabled, streaming }: Props) {
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
    </div>
  );
}
