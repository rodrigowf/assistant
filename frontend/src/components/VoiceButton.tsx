/**
 * VoiceButton — pill-shaped start button for orchestrator voice mode.
 *
 * States:
 *   off        → show mic icon with "Start Voice" label (click to start)
 *   connecting → show spinner with "Connecting" label
 *   error      → show error icon with "Retry" label (click to retry)
 *
 * Note: active, speaking, thinking, tool_use states are now handled by
 * VoiceControls component which shows the stop/mute buttons.
 */

import type { VoiceStatus } from "../types";

interface Props {
  status: VoiceStatus;
  onStart: () => void;
  onStop: () => void;
}

export function VoiceButton({ status, onStart, onStop }: Props) {
  const isOff = status === "off" || status === "error";
  const isConnecting = status === "connecting";

  const label = {
    off: "Start Voice",
    connecting: "Connecting…",
    active: "Voice Active",
    speaking: "Speaking",
    thinking: "Thinking",
    tool_use: "Working",
    error: "Retry",
  }[status];

  return (
    <button
      className={`voice-start-btn voice-start-btn--${status}`}
      onClick={isOff ? onStart : onStop}
      title={label}
      aria-label={label}
      disabled={isConnecting}
    >
      {status === "off" && <MicIcon />}
      {status === "connecting" && <SpinnerIcon />}
      {status === "error" && <ErrorIcon />}
      <span className="voice-start-label">{label}</span>
    </button>
  );
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
      <circle cx="12" cy="12" r="9" strokeDasharray="28" strokeDashoffset="8" strokeLinecap="round" />
    </svg>
  );
}

function ErrorIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
    </svg>
  );
}

export function MicMutedIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05l1.23 1.23c.56-.98.9-2.09.9-3.28zm-4.02.17c0-.06.02-.11.02-.17V5c0-1.66-1.34-3-3-3S9 3.34 9 5v.18l5.98 5.99zM4.27 3L3 4.27l6.01 6.01V11c0 1.66 1.33 3 2.99 3 .22 0 .44-.03.65-.08l1.66 1.66c-.71.33-1.5.52-2.31.52-2.76 0-5.3-2.1-5.3-5.1H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c.91-.13 1.77-.45 2.54-.9L19.73 21 21 19.73 4.27 3z" />
    </svg>
  );
}
