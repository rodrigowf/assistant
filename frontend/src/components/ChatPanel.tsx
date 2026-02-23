import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import { VoiceButton, MicMutedIcon } from "./VoiceButton";
import type { ChatMessage, SessionStatus, ConnectionState, VoiceStatus } from "../types";

function AudioLevelIndicator({ level, label }: { level: number; label: string }) {
  const height = Math.min(Math.max(level * 3, 0), 1) * 100;
  return (
    <div className="audio-level" title={label}>
      <div className="audio-level-bar" style={{ height: `${height}%` }} />
    </div>
  );
}

interface Props {
  messages: ChatMessage[];
  status: SessionStatus;
  connectionState: ConnectionState;
  cost: number;
  turns: number;
  error: string | null;
  onSend: (text: string) => void;
  onInterrupt: () => void;
  isActive?: boolean;
  // Voice mode props (orchestrator only)
  isOrchestrator?: boolean;
  voiceStatus?: VoiceStatus;
  onVoiceStart?: () => void;
  onVoiceStop?: () => void;
  isMuted?: boolean;
  onMuteToggle?: () => void;
  micLevel?: number;
  speakerLevel?: number;
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
  isActive,
  isOrchestrator,
  voiceStatus,
  onVoiceStart,
  onVoiceStop,
  isMuted,
  onMuteToggle,
  micLevel,
  speakerLevel,
}: Props) {
  const isStreaming = status === "streaming" || status === "thinking" || status === "tool_use";
  const voiceActive = voiceStatus && voiceStatus !== "off" && voiceStatus !== "error";

  return (
    <main className="chat-panel">
      <MessageList messages={messages} isActive={isActive} />
      {error && (
        <div className="error-banner">{error}</div>
      )}
      {/* Hide text input when voice is active */}
      {!voiceActive && (
        <div className="chat-input-bar">
          <ChatInput
            onSend={onSend}
            onInterrupt={onInterrupt}
            disabled={status === "disconnected" || status === "connecting"}
            streaming={isStreaming}
          />
        </div>
      )}
      {isOrchestrator && voiceStatus !== undefined && onVoiceStart && onVoiceStop && (
        <div className="voice-bar-container">
          <div className="voice-bar">
            <VoiceButton
              status={voiceStatus}
              onStart={onVoiceStart}
              onStop={onVoiceStop}
            />
            {voiceActive && onMuteToggle && (
              <>
                <button
                  className={`voice-mute-btn ${isMuted ? "muted" : ""}`}
                  onClick={onMuteToggle}
                  title={isMuted ? "Unmute microphone" : "Mute microphone"}
                  aria-label={isMuted ? "Unmute microphone" : "Mute microphone"}
                >
                  {isMuted ? <MicMutedIcon /> : <MicIcon />}
                </button>
                <AudioLevelIndicator level={micLevel ?? 0} label="Mic" />
                <AudioLevelIndicator level={speakerLevel ?? 0} label="Speaker" />
              </>
            )}
            {voiceActive && (
              <span className="voice-status-label">
                {voiceStatus === "active" && (isMuted ? "Muted" : "Listening…")}
                {voiceStatus === "speaking" && "Speaking…"}
                {voiceStatus === "thinking" && "Thinking…"}
                {voiceStatus === "tool_use" && "Using tool…"}
                {voiceStatus === "connecting" && "Connecting…"}
              </span>
            )}
          </div>
        </div>
      )}
      <div className="status-bar-container">
        <StatusBar
          status={status}
          connectionState={connectionState}
          cost={cost}
          turns={turns}
        />
      </div>
    </main>
  );
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
    </svg>
  );
}
