import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import { VoiceControls } from "./VoiceControls";
import type { ChatMessage, SessionStatus, ConnectionState, VoiceStatus } from "../types";

interface Props {
  messages: ChatMessage[];
  status: SessionStatus;
  connectionState: ConnectionState;
  cost: number;
  turns: number;
  error: string | null;
  onSend: (text: string) => void;
  onSendAudio?: (audioBase64: string, format: string) => void;
  onInterrupt: () => void;
  onCompact?: () => void;
  contextUsage?: number;
  isActive?: boolean;
  hasMoreMessages?: boolean;
  onLoadMore?: () => Promise<void>;
  // Voice mode props (orchestrator only)
  isOrchestrator?: boolean;
  voiceStatus?: VoiceStatus;
  onVoiceStart?: () => void;
  onVoiceStop?: () => void;
  isMicMuted?: boolean;
  onMicMuteToggle?: () => void;
  isAssistantMuted?: boolean;
  onAssistantMuteToggle?: () => void;
  micLevel?: number;
  speakerLevel?: number;
  /** Voice error message (e.g. session expired). */
  voiceError?: string | null;
  /** Whether the current model supports audio input */
  supportsAudio?: boolean;
}

export function ChatPanel({
  messages,
  status,
  connectionState,
  cost,
  turns,
  error,
  onSend,
  onSendAudio,
  onInterrupt,
  onCompact,
  contextUsage,
  isActive,
  hasMoreMessages,
  onLoadMore,
  isOrchestrator,
  voiceStatus,
  onVoiceStart,
  onVoiceStop,
  isMicMuted,
  onMicMuteToggle,
  isAssistantMuted,
  onAssistantMuteToggle,
  micLevel,
  speakerLevel,
  voiceError,
  supportsAudio,
}: Props) {
  const isStreaming = status === "streaming" || status === "thinking" || status === "tool_use";
  const voiceActive = voiceStatus && voiceStatus !== "off" && voiceStatus !== "error";

  return (
    <main className="chat-panel">
      <MessageList messages={messages} isActive={isActive} hasMoreMessages={hasMoreMessages} onLoadMore={onLoadMore} />
      {error && (
        <div className="error-banner">{error}</div>
      )}
      {/* Hide text input when voice is active */}
      {!voiceActive && (
        <div className="chat-input-bar">
          <ChatInput
            onSend={onSend}
            onSendAudio={onSendAudio}
            onInterrupt={onInterrupt}
            onCompact={onCompact}
            contextUsage={contextUsage}
            disabled={status === "disconnected" || status === "connecting"}
            streaming={isStreaming}
            supportsAudio={supportsAudio}
            voiceStatus={isOrchestrator ? voiceStatus : undefined}
            onVoiceStart={isOrchestrator ? onVoiceStart : undefined}
            onVoiceStop={isOrchestrator ? onVoiceStop : undefined}
          />
          {isOrchestrator && voiceStatus === "error" && voiceError && (
            <span className="voice-error-message">{voiceError}</span>
          )}
        </div>
      )}
      {/* Voice active controls */}
      {isOrchestrator && voiceActive && voiceStatus !== undefined && onVoiceStart && onVoiceStop && (
        <div className="voice-bar-container">
          <div className="voice-bar">
            {onMicMuteToggle && onAssistantMuteToggle && (
              <VoiceControls
                status={voiceStatus}
                onStop={onVoiceStop}
                isMicMuted={isMicMuted ?? false}
                onMicMuteToggle={onMicMuteToggle}
                micLevel={micLevel ?? 0}
                isAssistantMuted={isAssistantMuted ?? false}
                onAssistantMuteToggle={onAssistantMuteToggle}
                speakerLevel={speakerLevel ?? 0}
              />
            )}
            <span className="voice-status-label">
              <span className={`voice-status-dot ${
                voiceStatus === "active" ? (isMicMuted ? "muted" : "listening") :
                voiceStatus === "speaking" ? "speaking" :
                voiceStatus === "thinking" ? "thinking" :
                voiceStatus === "tool_use" ? "tool-use" :
                "connecting"
              }`} />
              {voiceStatus === "active" && (isMicMuted ? "Muted" : "Listening…")}
              {voiceStatus === "speaking" && "Speaking…"}
              {voiceStatus === "thinking" && "Thinking…"}
              {voiceStatus === "tool_use" && "Using tool…"}
              {voiceStatus === "connecting" && "Connecting…"}
            </span>
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
