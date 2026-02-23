import type { SessionInfo } from "../types";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { SessionItem } from "./SessionItem";
import { generateUUID } from "../utils/uuid";

interface Props {
  sessions: SessionInfo[];
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onNew: () => void;
  onNewOrchestrator: () => void;
  onSelectOrchestrator: (id: string, title: string) => void;
  isOpen?: boolean;
  onClose?: () => void;
}

export function Sidebar({ sessions, onDelete, onRename, onNew, onNewOrchestrator, onSelectOrchestrator, isOpen, onClose }: Props) {
  const { tabs, activeTabId, openTab, switchTab, findTabByResumeId } = useTabsContext();

  const handleSelect = (sdkId: string, localId?: string) => {
    // Priority 1: If we know the local_id (pool-live session), find tab directly by sessionId.
    // Priority 2: Find a tab resumed with this SDK session ID (tab.resumeSdkId === sdkId).
    // Priority 3: Find a tab whose sessionId matches (e.g., orchestrator where local_id == sdkId).
    const existingTab =
      (localId && tabs.find((t) => t.sessionId === localId)) ??
      findTabByResumeId(sdkId) ??
      tabs.find((t) => t.sessionId === sdkId);

    if (existingTab) {
      switchTab(existingTab.sessionId);
      onClose?.();
      return;
    }

    const session = sessions.find((s) => s.session_id === sdkId);
    if (session?.is_orchestrator) {
      // If an orchestrator tab is already open (e.g., reconnected from pool with a
      // different local_id), switch to it instead of trying to open a second one.
      const existingOrchestratorTab = tabs.find((t) => t.isOrchestrator);
      if (existingOrchestratorTab) {
        switchTab(existingOrchestratorTab.sessionId);
        onClose?.();
        return;
      }
      onSelectOrchestrator(sdkId, session.title || "Untitled");
    } else {
      const newLocalId = generateUUID();
      openTab(newLocalId, session?.title || "Untitled", false, sdkId);
    }
    onClose?.();
  };

  // Build indicator maps for the sidebar.
  // A session is "open" (dot shown) if any tab corresponds to it.
  // We match tabs by resumeSdkId OR by local_id (for orchestrator-opened agent tabs
  // where tab.sessionId === local_id and the session list exposes local_id).
  const sdkTabStatusMap = new Map<string, string | null>();
  const sdkTabOpenSet = new Set<string>();
  const activeTab = activeTabId ? tabs.find((t) => t.sessionId === activeTabId) : null;

  // Build a quick lookup: local_id → sdk_session_id from live sessions
  const localToSdk = new Map<string, string>();
  for (const s of sessions) {
    if (s.local_id) localToSdk.set(s.local_id, s.session_id);
  }

  for (const tab of tabs) {
    const icon = getTabStatusIcon(tab);
    if (tab.resumeSdkId) {
      sdkTabStatusMap.set(tab.resumeSdkId, icon);
      sdkTabOpenSet.add(tab.resumeSdkId);
    }
    // For pool-live tabs (local_id keyed): look up their sdk_session_id via the sessions list
    const sdkId = localToSdk.get(tab.sessionId);
    if (sdkId) {
      sdkTabStatusMap.set(sdkId, icon);
      sdkTabOpenSet.add(sdkId);
    }
  }

  return (
    <>
    {isOpen && <div className="sidebar-backdrop" onClick={onClose} />}
    <aside className={`sidebar${isOpen ? " sidebar-open" : ""}`}>
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
        {sessions.map((s) => {
          // A session item is active if the current tab matches by resumeSdkId or by local_id
          const isActive =
            activeTab?.resumeSdkId === s.session_id ||
            (!!s.local_id && activeTab?.sessionId === s.local_id);
          return (
            <SessionItem
              key={s.session_id}
              session={s}
              active={isActive}
              tabOpen={sdkTabOpenSet.has(s.session_id)}
              tabStatus={sdkTabStatusMap.get(s.session_id) ?? undefined}
              onClick={() => handleSelect(s.session_id, s.local_id)}
              onDelete={() => onDelete(s.session_id)}
              onRename={(title) => onRename(s.session_id, title)}
            />
          );
        })}
        {sessions.length === 0 && (
          <div className="sidebar-empty">No sessions yet</div>
        )}
      </div>
    </aside>
    </>
  );
}
