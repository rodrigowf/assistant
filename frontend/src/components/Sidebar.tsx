import type { SessionInfo } from "../types";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { SessionItem } from "./SessionItem";

interface Props {
  sessions: SessionInfo[];
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onNew: () => void;
  onNewOrchestrator: () => void;
  onSelectOrchestrator: (id: string, title: string) => void;
}

export function Sidebar({ sessions, onDelete, onRename, onNew, onNewOrchestrator, onSelectOrchestrator }: Props) {
  const { tabs, activeTabId, openTab, switchTab, findTabByResumeId } = useTabsContext();

  const handleSelect = (id: string) => {
    // Check if a tab is already open for this SDK session ID
    const existingTab = findTabByResumeId(id);
    if (existingTab) {
      switchTab(existingTab.sessionId);
    } else {
      const session = sessions.find((s) => s.session_id === id);
      if (session?.is_orchestrator) {
        // Route through the orchestrator confirmation flow
        onSelectOrchestrator(id, session.title || "Untitled");
      } else {
        // Generate a stable local_id, pass SDK session ID as resumeSdkId
        const localId = crypto.randomUUID();
        openTab(localId, session?.title || "Untitled", false, id);
      }
    }
  };

  // Build a map from SDK session ID → tab status icon for sidebar indicators
  const sdkTabStatusMap = new Map<string, string | null>();
  const sdkTabOpenSet = new Set<string>();
  const activeTab = activeTabId ? tabs.find((t) => t.sessionId === activeTabId) : null;

  for (const tab of tabs) {
    if (tab.resumeSdkId) {
      sdkTabStatusMap.set(tab.resumeSdkId, getTabStatusIcon(tab));
      sdkTabOpenSet.add(tab.resumeSdkId);
    }
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
            active={activeTab?.resumeSdkId === s.session_id}
            tabOpen={sdkTabOpenSet.has(s.session_id)}
            tabStatus={sdkTabStatusMap.get(s.session_id) ?? undefined}
            onClick={() => handleSelect(s.session_id)}
            onDelete={() => onDelete(s.session_id)}
            onRename={(title) => onRename(s.session_id, title)}
          />
        ))}
        {sessions.length === 0 && (
          <div className="sidebar-empty">No sessions yet</div>
        )}
      </div>
    </aside>
  );
}
