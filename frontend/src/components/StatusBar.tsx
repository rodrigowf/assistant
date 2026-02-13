import type { ConnectionState, SessionStatus } from "../types";

interface Props {
  status: SessionStatus;
  connectionState: ConnectionState;
  cost: number;
  turns: number;
}

export function StatusBar({ status, connectionState, cost, turns }: Props) {
  return (
    <div className="status-bar">
      <div className="status-left">
        <span className={`status-dot ${status}`} />
        <span className="status-text">{formatStatus(status)}</span>
      </div>
      <div className="status-center">
        <span className={`conn-state ${connectionState}`}>
          {connectionState}
        </span>
      </div>
      <div className="status-right">
        {turns > 0 && (
          <>
            <span className="stat">{turns} turn{turns !== 1 ? "s" : ""}</span>
            <span className="stat-sep" />
            <span className="stat">${cost.toFixed(4)}</span>
          </>
        )}
      </div>
    </div>
  );
}

function formatStatus(s: SessionStatus): string {
  switch (s) {
    case "connecting": return "Connecting...";
    case "streaming": return "Streaming";
    case "thinking": return "Thinking";
    case "tool_use": return "Using tool";
    case "interrupted": return "Interrupted";
    case "disconnected": return "Disconnected";
    default: return "Ready";
  }
}
