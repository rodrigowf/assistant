import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";

export function TabBar() {
  const { tabs, activeTabId, switchTab, closeTab } = useTabsContext();

  if (tabs.length === 0) return null;

  return (
    <div className="tab-bar">
      {tabs.map((tab) => {
        const isActive = tab.sessionId === activeTabId;
        const statusIcon = getTabStatusIcon(tab);

        return (
          <div
            key={tab.sessionId}
            className={`tab ${isActive ? "active" : ""}`}
            onClick={() => switchTab(tab.sessionId)}
          >
            <span className={`tab-status ${statusIcon}`} />
            <span className="tab-title">{tab.title || "New session"}</span>
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
