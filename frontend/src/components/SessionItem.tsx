import type { SessionInfo } from "../types";

interface Props {
  session: SessionInfo;
  active: boolean;
  tabOpen: boolean;
  tabStatus?: string;
  onClick: () => void;
  onDelete: () => void;
}

export function SessionItem({ session, active, tabOpen, tabStatus, onClick, onDelete }: Props) {
  const timeAgo = formatRelative(session.last_activity);

  const className = [
    "session-item",
    active ? "active" : "",
    tabOpen && !active ? "tab-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={className} onClick={onClick}>
      <div className="session-title">
        {tabOpen && tabStatus && (
          <span className={`session-tab-indicator ${tabStatus}`} />
        )}
        {session.title || "Untitled"}
      </div>
      <div className="session-meta">
        {session.is_orchestrator && (
          <span className="session-type-label">orchestrator</span>
        )}
        <span className="session-time">{timeAgo}</span>
        <span className="session-count">{session.message_count} msgs</span>
      </div>
      <button
        className="session-delete"
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        title="Delete session"
      >
        ×
      </button>
    </div>
  );
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
