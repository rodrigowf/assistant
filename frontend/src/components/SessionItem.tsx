import type { SessionInfo } from "../types";

interface Props {
  session: SessionInfo;
  active: boolean;
  onClick: () => void;
  onDelete: () => void;
}

export function SessionItem({ session, active, onClick, onDelete }: Props) {
  const timeAgo = formatRelative(session.last_activity);

  return (
    <div
      className={`session-item ${active ? "active" : ""}`}
      onClick={onClick}
    >
      <div className="session-title">{session.title || "Untitled"}</div>
      <div className="session-meta">
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
