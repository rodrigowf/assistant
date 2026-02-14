import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";

export function TabBar() {
  const { tabs, activeTabId, switchTab, closeTab } = useTabsContext();

  if (tabs.length === 0) return null;

  // Sort: orchestrator tab always first
  const sorted = [...tabs].sort((a, b) => {
    if (a.isOrchestrator && !b.isOrchestrator) return -1;
    if (!a.isOrchestrator && b.isOrchestrator) return 1;
    return 0;
  });

  return (
    <div className="tab-bar">
      {sorted.map((tab) => {
        const isActive = tab.sessionId === activeTabId;
        const statusIcon = getTabStatusIcon(tab);

        return (
          <div
            key={tab.sessionId}
            className={`tab ${isActive ? "active" : ""}${tab.isOrchestrator ? " orchestrator" : ""}`}
            onClick={() => switchTab(tab.sessionId)}
          >
            {statusIcon && <span className={`tab-status ${statusIcon}`} />}
            <span className="tab-title">
              {tab.title || "New session"}
            </span>
            <button
              className="tab-close"
              onClick={(e) => {
                e.stopPropagation();
                closeTab(tab.sessionId);
              }}
              title="Close tab"
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
