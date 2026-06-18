import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import { VoiceControls } from "./VoiceControls";
import { PermissionBar } from "./PermissionBar";
import type { ChatMessage, SessionStatus, ConnectionState, VoiceStatus } from "../types";
import type { StallInfo, PendingPermission, TerminationState } from "../hooks/useChatInstance";

function formatStallElapsed(s: number): string {
  if (s < 90) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m * 60);
  return rem > 0 ? `${m}m${rem}s` : `${m}m`;
}

/**
 * Human-readable headline for a TerminationState.reason.  The detail
 * field carries the technical cause (e.g. "exit code 255"); this is
 * just the framing the user sees first.
 */
function terminationHeadline(reason: TerminationState["reason"]): string {
  switch (reason) {
    case "subprocess_crashed":
      return "This session crashed";
    case "subprocess_lost":
      return "This session ended unexpectedly";
    case "unreachable":
      return "The host is unreachable";
    case "replaced":
      return "This session was replaced";
    case "closed_by_user":
      return "This session was closed";
  }
}

interface Props {
  messages: ChatMessage[];
  status: SessionStatus;
  connectionState: ConnectionState;
  cost: number;
  turns: number;
  error: string | null;
  /** Set when the backend reports the SDK is stuck on a tool. */
  stall?: StallInfo | null;
  /** Set when the backend signals the underlying session is gone
   *  (subprocess crashed, SSH transport closed).  Drives the
   *  recovery banner — distinct from generic ``error``. */
  termination?: TerminationState | null;
  /** Click handler for "open a fresh session" from the termination
   *  banner.  Receives the SDK session id so the new tab resumes
   *  from the same on-disk JSONL. */
  onRecoverFromTermination?: (sdkSessionId: string) => void;
  /** Set when the SDK is waiting for a permission decision (popup). */
  pendingPermission?: PendingPermission | null;
  onRespondToPermission?: (decision: "allow" | "deny", message?: string) => void;
  onSend: (text: string) => void;
  onSendAudio?: (audioBase64: string, format: string) => void;
  onInterrupt: () => void;
  onCompact?: () => void;
  contextUsage?: number;
  isActive?: boolean;
  hasMoreMessages?: boolean;
  onLoadMore?: () => Promise<void>;
  onRewindMessage?: (dropLastN: number) => void;
  onForkMessage?: (dropLastN: number) => void;
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
  /** Increment B (voice subsystem refactor) — Silero VAD state from
   *  the backend ``voice_vad_state`` broadcast. Used by VoiceButton
   *  to render a "listening Ns" duration indicator when stuck. */
  vadState?: import("../types").VadState;
  vadDurationMs?: number;
  /** Whether the current model supports audio input */
  supportsAudio?: boolean;
  /** Open session-specific config panel (non-orchestrator only) */
  onOpenSessionConfig?: () => void;
}

export function ChatPanel({
  messages,
  status,
  connectionState,
  cost,
  turns,
  error,
  stall,
  termination,
  onRecoverFromTermination,
  pendingPermission,
  onRespondToPermission,
  onSend,
  onSendAudio,
  onInterrupt,
  onCompact,
  contextUsage,
  isActive,
  hasMoreMessages,
  onLoadMore,
  onRewindMessage,
  onForkMessage,
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
  vadState,
  vadDurationMs,
  supportsAudio,
  onOpenSessionConfig,
}: Props) {
  const isStreaming = status === "streaming" || status === "thinking" || status === "tool_use" || status === "processing";
  const voiceActive = voiceStatus && voiceStatus !== "off" && voiceStatus !== "error";

  return (
    <main className="chat-panel">
      <MessageList
        messages={messages}
        isActive={isActive}
        hasMoreMessages={hasMoreMessages}
        onLoadMore={onLoadMore}
        onRewindMessage={onRewindMessage}
        onForkMessage={onForkMessage}
      />
      {error && (
        <div className="error-banner">{error}</div>
      )}
      {termination && (
        <div className="termination-banner" role="alert">
          <div className="termination-banner-text">
            <strong>{terminationHeadline(termination.reason)}</strong>
            {termination.detail ? <>: {termination.detail}</> : null}
          </div>
          {termination.sdkSessionId && onRecoverFromTermination && (
            <button
              type="button"
              className="termination-banner-button"
              onClick={() => onRecoverFromTermination(termination.sdkSessionId!)}
            >
              Continue in new tab
            </button>
          )}
        </div>
      )}
      {stall && isStreaming && (
        <div className="stall-banner">
          <span className="stall-banner-text">
            {stall.toolName
              ? `${stall.toolName} has been running for ${formatStallElapsed(stall.elapsedSeconds)} with no response.`
              : `No response from Claude for ${formatStallElapsed(stall.elapsedSeconds)}.`}
          </span>
          <button
            type="button"
            className="stall-banner-button"
            onClick={onInterrupt}
          >
            Interrupt
          </button>
        </div>
      )}
      {pendingPermission && onRespondToPermission && (
        <PermissionBar
          pending={pendingPermission}
          onRespond={onRespondToPermission}
        />
      )}
      <div className="status-bar-container">
        <StatusBar
          status={status}
          connectionState={connectionState}
          cost={cost}
          turns={turns}
        />
      </div>
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
            vadState={isOrchestrator ? vadState : undefined}
            vadDurationMs={isOrchestrator ? vadDurationMs : undefined}
            onOpenConfig={!isOrchestrator ? onOpenSessionConfig : undefined}
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
    </main>
  );
}
