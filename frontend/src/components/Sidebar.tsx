import type { SessionInfo } from "../types";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { SessionItem } from "./SessionItem";

interface Props {
  sessions: SessionInfo[];
  onDelete: (id: string) => void;
  onNew: () => void;
  onNewOrchestrator: () => void;
  onSelectOrchestrator: (id: string, title: string) => void;
}

export function Sidebar({ sessions, onDelete, onNew, onNewOrchestrator, onSelectOrchestrator }: Props) {
  const { tabs, activeTabId, openTab, switchTab, isTabOpen } = useTabsContext();

  const handleSelect = (id: string) => {
    if (isTabOpen(id)) {
      switchTab(id);
    } else {
      const session = sessions.find((s) => s.session_id === id);
      if (session?.is_orchestrator) {
        // Route through the orchestrator confirmation flow
        onSelectOrchestrator(id, session.title || "Untitled");
      } else {
        openTab(id, session?.title || "Untitled");
      }
    }
  };

  // Build a map of open tab statuses for sidebar indicators
  const tabStatusMap = new Map<string, string>();
  for (const tab of tabs) {
    tabStatusMap.set(tab.sessionId, getTabStatusIcon(tab));
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h2 className="sidebar-title">Sessions</h2>
        <div className="sidebar-header-actions">
          <button
            className="new-orchestrator-btn"
            onClick={onNewOrchestrator}
            title="New orchestrator"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
            </svg>
          </button>
          <button className="new-session-btn" onClick={onNew} title="New session">
            +
          </button>
        </div>
      </div>
      <div className="session-list">
        {sessions.map((s) => (
          <SessionItem
            key={s.session_id}
            session={s}
            active={s.session_id === activeTabId}
            tabOpen={isTabOpen(s.session_id)}
            tabStatus={tabStatusMap.get(s.session_id)}
            onClick={() => handleSelect(s.session_id)}
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
