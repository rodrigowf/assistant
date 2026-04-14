import { useRef, useCallback, type KeyboardEvent, type ChangeEvent } from "react";
import { VoiceRecordButton } from "./VoiceRecordButton";
import { useAudioRecorder } from "../hooks/useAudioRecorder";

interface Props {
  onSend: (text: string) => void;
  onSendAudio?: (audioBase64: string, format: string) => void;
  onInterrupt: () => void;
  onCompact?: () => void;
  disabled: boolean;
  streaming: boolean;
  /** Context usage percentage (0–100). Shows compact button when > 0. */
  contextUsage?: number;
  /** Whether audio recording is supported (model supports audio input) */
  supportsAudio?: boolean;
}

export function ChatInput({
  onSend,
  onSendAudio,
  onInterrupt,
  onCompact,
  disabled,
  streaming,
  contextUsage,
  supportsAudio,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleRecordingComplete = useCallback(
    (audioBase64: string, format: string) => {
      onSendAudio?.(audioBase64, format);
    },
    [onSendAudio]
  );

  const handleRecordingError = useCallback((error: string) => {
    console.error("Recording error:", error);
  }, []);

  const { state: recordingState, duration, startRecording, stopRecording, cancelRecording } =
    useAudioRecorder({
      onRecordingComplete: handleRecordingComplete,
      onError: handleRecordingError,
      maxDuration: 60,
    });

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

  const isRecording = recordingState !== "idle";

  const hasUsageData = contextUsage !== undefined && contextUsage > 0;
  const usageLabel = hasUsageData ? `${contextUsage}%` : "?";
  const usageClass = hasUsageData && contextUsage >= 80
    ? " compact-btn--warning"
    : hasUsageData && contextUsage >= 50
      ? " compact-btn--caution"
      : "";
  const usageTitle = hasUsageData
    ? `Compact conversation (${contextUsage}% context used)`
    : "Compact conversation (context usage unknown)";

  return (
    <div className="chat-input">
      <textarea
        ref={textareaRef}
        placeholder={streaming ? "Waiting for response..." : "Send a message..."}
        disabled={(disabled && !streaming) || isRecording}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        rows={1}
      />
      {onCompact && (
        <button
          className={`compact-btn${usageClass}`}
          onClick={onCompact}
          title={usageTitle}
          disabled={isRecording || streaming}
        >
          <span className="compact-btn-pct">{usageLabel}</span>
        </button>
      )}
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
          disabled={isRecording}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M1 1l14 7-14 7V9l10-1-10-1V1z" />
          </svg>
        </button>
      )}
      {supportsAudio && onSendAudio && (
        <VoiceRecordButton
          state={recordingState}
          duration={duration}
          onStart={startRecording}
          onStop={stopRecording}
          onCancel={cancelRecording}
          disabled={disabled || streaming}
        />
      )}
    </div>
  );
}
