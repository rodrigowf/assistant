import { useState } from "react";
import type { VisualizationInfo } from "../types";

interface Props {
  viz: VisualizationInfo;
  active: boolean;
  tabOpen: boolean;
  onClick: () => void;
  onRename: (title: string) => void;
}

/**
 * One HTML artifact in the sidebar. Mirrors SessionItem's inline-rename
 * interaction, minus delete/duplicate — these are files on disk, not
 * conversations, so the sidebar deliberately offers no destructive action.
 */
export function VizItem({ viz, active, tabOpen, onClick, onRename }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  // Seed the draft when entering edit mode rather than in an effect; focus and
  // selection are handled declaratively by autoFocus + onFocus on the input.
  function startEdit() {
    setDraft(viz.title || "");
    setEditing(true);
  }

  function commit() {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== viz.title) {
      onRename(trimmed);
    }
    setEditing(false);
  }

  const className = [
    "session-item",
    "viz-item",
    active ? "active" : "",
    tabOpen && !active ? "tab-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={className} onClick={editing ? undefined : onClick}>
      <div className="session-title">
        {editing ? (
          <input
            autoFocus
            className="session-rename-input"
            value={draft}
            onFocus={(e) => e.target.select()}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              else if (e.key === "Escape") setEditing(false);
            }}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          viz.title || viz.path
        )}
      </div>
      <div className="session-meta">
        <span className="session-time">{formatRelative(viz.modified)}</span>
        <span className="session-count" title={viz.path}>
          {parentLabel(viz.path)}
        </span>
      </div>
      {!editing && (
        <button
          className="session-rename"
          onClick={(e) => {
            e.stopPropagation();
            startEdit();
          }}
          title="Rename visualization"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
          </svg>
        </button>
      )}
    </div>
  );
}

/** Folder the file lives in, shown as a lightweight origin hint. */
function parentLabel(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts[parts.length - 2] : "public";
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
