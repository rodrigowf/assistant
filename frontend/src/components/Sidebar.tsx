import type { SessionInfo } from "../types";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { SessionItem } from "./SessionItem";

interface Props {
  sessions: SessionInfo[];
  onDelete: (id: string) => void;
  onNew: () => void;
}

export function Sidebar({ sessions, onDelete, onNew }: Props) {
  const { tabs, activeTabId, openTab, switchTab, isTabOpen } = useTabsContext();

  const handleSelect = (id: string) => {
    if (isTabOpen(id)) {
      switchTab(id);
    } else {
      const session = sessions.find((s) => s.session_id === id);
      openTab(id, session?.title || "Untitled");
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
        <button className="new-session-btn" onClick={onNew} title="New session">
          +
        </button>
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
