import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import { VoiceButton } from "./VoiceButton";
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
  onInterrupt: () => void;
  isActive?: boolean;
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
  isMicMuted,
  onMicMuteToggle,
  isAssistantMuted,
  onAssistantMuteToggle,
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
            {/* Show start button when voice is off */}
            {!voiceActive && (
              <VoiceButton
                status={voiceStatus}
                onStart={onVoiceStart}
                onStop={onVoiceStop}
              />
            )}
            {/* Show new pill controls when voice is active */}
            {voiceActive && onMicMuteToggle && onAssistantMuteToggle && (
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
            {voiceActive && (
              <span className="voice-status-label">
                {voiceStatus === "active" && (isMicMuted ? "Muted" : "Listening…")}
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

