import type { SessionInfo } from "../types";
import { SessionItem } from "./SessionItem";

interface Props {
  sessions: SessionInfo[];
  activeId: string | null;
  onSelect: (id: string | null) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
}

export function Sidebar({ sessions, activeId, onSelect, onDelete, onNew }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h2 className="sidebar-title">Sessions</h2>
        <button className="new-session-btn" onClick={onNew} title="New session">
          +
        </button>
      </div>
      <div className="session-list">
        {sessions.map((s) => (
          <SessionItem
            key={s.session_id}
            session={s}
            active={s.session_id === activeId}
            onClick={() => onSelect(s.session_id)}
            onDelete={() => onDelete(s.session_id)}
          />
        ))}
        {sessions.length === 0 && (
          <div className="sidebar-empty">No sessions yet</div>
        )}
      </div>
    </aside>
  );
}
