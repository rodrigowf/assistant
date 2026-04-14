import type { RecordingState } from "../hooks/useAudioRecorder";

interface Props {
  state: RecordingState;
  duration: number;
  onStart: () => void;
  onStop: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

/**
 * Button for recording audio for turn-based voice input.
 * Shows mic icon when idle, recording indicator when recording.
 */
export function VoiceRecordButton({
  state,
  duration,
  onStart,
  onStop,
  onCancel,
  disabled,
}: Props) {
  if (state === "processing") {
    return (
      <button className="voice-record-btn processing" disabled title="Processing...">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="25" strokeDashoffset="0">
            <animateTransform
              attributeName="transform"
              type="rotate"
              from="0 8 8"
              to="360 8 8"
              dur="1s"
              repeatCount="indefinite"
            />
          </circle>
        </svg>
      </button>
    );
  }

  if (state === "recording") {
    return (
      <div className="voice-record-active">
        <button
          className="voice-record-btn recording"
          onClick={onStop}
          title="Stop recording"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <rect x="4" y="4" width="8" height="8" rx="1" />
          </svg>
        </button>
        <span className="voice-record-duration">{formatDuration(duration)}</span>
        <button
          className="voice-record-cancel"
          onClick={onCancel}
          title="Cancel recording"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
            <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
          </svg>
        </button>
      </div>
    );
  }

  // Idle state
  return (
    <button
      className="voice-record-btn"
      onClick={onStart}
      disabled={disabled}
      title="Record voice message"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
        <path d="M8 1a2 2 0 0 1 2 2v5a2 2 0 1 1-4 0V3a2 2 0 0 1 2-2z" />
        <path d="M4 7a1 1 0 0 0-2 0 6 6 0 0 0 5 5.91V14H5a1 1 0 0 0 0 2h6a1 1 0 0 0 0-2H9v-1.09A6 6 0 0 0 14 7a1 1 0 0 0-2 0 4 4 0 0 1-8 0z" />
      </svg>
    </button>
  );
}
