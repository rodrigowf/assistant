import { useState, useRef, useEffect } from "react";
import type { SessionInfo } from "../types";

interface Props {
  session: SessionInfo;
  active: boolean;
  tabOpen: boolean;
  tabStatus?: string;
  onClick: () => void;
  onDelete: () => void;
  onRename: (title: string) => void;
}

export function SessionItem({ session, active, tabOpen, tabStatus, onClick, onDelete, onRename }: Props) {
  const timeAgo = formatRelative(session.last_activity);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(session.title || "");
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing, session.title]);

  function startEdit(e: React.MouseEvent) {
    e.stopPropagation();
    setEditing(true);
  }

  function commit() {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== session.title) {
      onRename(trimmed);
    }
    setEditing(false);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") commit();
    else if (e.key === "Escape") setEditing(false);
  }

  const className = [
    "session-item",
    active ? "active" : "",
    tabOpen && !active ? "tab-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={className} onClick={editing ? undefined : onClick}>
      <div className="session-title">
        {tabOpen && tabStatus && (
          <span className={`session-tab-indicator ${tabStatus}`} />
        )}
        {editing ? (
          <input
            ref={inputRef}
            className="session-rename-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={onKeyDown}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          session.title || "Untitled"
        )}
      </div>
      <div className="session-meta">
        {session.is_orchestrator && (
          <span className="session-type-label">orchestrator</span>
        )}
        <span className="session-time">{timeAgo}</span>
        <span className="session-count">{session.message_count} msgs</span>
      </div>
      {!editing && (
        <>
          <button
            className="session-rename"
            onClick={startEdit}
            title="Rename session"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
          </button>
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
        </>
      )}
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
