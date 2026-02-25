/**
 * VoiceControls — pill-shaped voice control buttons for orchestrator mode.
 *
 * Three buttons:
 *   1. Stop Session (red) — ends the voice session
 *   2. Mute My Voice — toggles user mic mute, shows volume meter
 *   3. Mute Assistant — toggles assistant audio mute, shows volume indicator
 */

import type { VoiceStatus } from "../types";

interface Props {
  status: VoiceStatus;
  onStop: () => void;
  // Mic mute
  isMicMuted: boolean;
  onMicMuteToggle: () => void;
  micLevel: number;
  // Assistant mute
  isAssistantMuted: boolean;
  onAssistantMuteToggle: () => void;
  speakerLevel: number;
}

export function VoiceControls({
  status,
  onStop,
  isMicMuted,
  onMicMuteToggle,
  micLevel,
  isAssistantMuted,
  onAssistantMuteToggle,
  speakerLevel,
}: Props) {
  const isActive = status !== "off" && status !== "error" && status !== "connecting";

  return (
    <div className="voice-controls">
      {/* Stop Session Button */}
      <button
        className="voice-ctrl voice-ctrl--stop"
        onClick={onStop}
        title="Stop voice session"
        aria-label="Stop voice session"
      >
        <StopIcon />
      </button>

      {/* Mute Assistant Button */}
      <button
        className={`voice-ctrl voice-ctrl--assistant ${isAssistantMuted ? "muted" : ""}`}
        onClick={onAssistantMuteToggle}
        title={isAssistantMuted ? "Unmute assistant" : "Mute assistant"}
        aria-label={isAssistantMuted ? "Unmute assistant" : "Mute assistant"}
        disabled={!isActive}
      >
        {isAssistantMuted ? <SpeakerMutedIcon /> : <SpeakerIcon />}
        {!isAssistantMuted && isActive && (
          <VolumeDots level={speakerLevel} />
        )}
      </button>

      {/* Mute My Voice Button */}
      <button
        className={`voice-ctrl voice-ctrl--mic ${isMicMuted ? "muted" : ""}`}
        onClick={onMicMuteToggle}
        title={isMicMuted ? "Unmute microphone" : "Mute microphone"}
        aria-label={isMicMuted ? "Unmute microphone" : "Mute microphone"}
        disabled={!isActive}
      >
        {isMicMuted ? <MicMutedIcon /> : <MicIcon />}
        {!isMicMuted && isActive && (
          <VolumeBars level={micLevel} />
        )}
      </button>
    </div>
  );
}

/** Volume bars indicator (for mic) — vertical bars that animate with level */
function VolumeBars({ level }: { level: number }) {
  // Normalize level (0-1) to bar heights
  const normalizedLevel = Math.min(Math.max(level * 3, 0), 1);
  const bar1Height = Math.min(normalizedLevel * 0.6 + 0.2, 1);
  const bar2Height = Math.min(normalizedLevel * 0.8 + 0.15, 1);
  const bar3Height = Math.min(normalizedLevel + 0.1, 1);
  const bar4Height = Math.min(normalizedLevel * 0.7 + 0.2, 1);

  return (
    <div className="volume-bars">
      <div className="volume-bar" style={{ height: `${bar1Height * 100}%` }} />
      <div className="volume-bar" style={{ height: `${bar2Height * 100}%` }} />
      <div className="volume-bar" style={{ height: `${bar3Height * 100}%` }} />
      <div className="volume-bar" style={{ height: `${bar4Height * 100}%` }} />
    </div>
  );
}

/** Volume dots indicator (for speaker) — dots that light up with level */
function VolumeDots({ level }: { level: number }) {
  const normalizedLevel = Math.min(Math.max(level * 3, 0), 1);
  const activeDots = Math.ceil(normalizedLevel * 3);

  return (
    <div className="volume-dots">
      <div className={`volume-dot ${activeDots >= 1 ? "active" : ""}`} />
      <div className={`volume-dot ${activeDots >= 2 ? "active" : ""}`} />
      <div className={`volume-dot ${activeDots >= 3 ? "active" : ""}`} />
    </div>
  );
}

function StopIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <polyline points="8,10 12,14 16,10" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
    </svg>
  );
}

function MicMutedIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05l1.23 1.23c.56-.98.9-2.09.9-3.28zm-4.02.17c0-.06.02-.11.02-.17V5c0-1.66-1.34-3-3-3S9 3.34 9 5v.18l5.98 5.99zM4.27 3L3 4.27l6.01 6.01V11c0 1.66 1.33 3 2.99 3 .22 0 .44-.03.65-.08l1.66 1.66c-.71.33-1.5.52-2.31.52-2.76 0-5.3-2.1-5.3-5.1H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c.91-.13 1.77-.45 2.54-.9L19.73 21 21 19.73 4.27 3z" />
    </svg>
  );
}

function SpeakerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 9v6h4l5 5V4L7 9H3z" />
    </svg>
  );
}

function SpeakerMutedIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
    </svg>
  );
}
