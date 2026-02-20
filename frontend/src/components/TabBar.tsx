import { useState } from "react";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { closePoolSession } from "../api/rest";
import { ConfirmCloseModal } from "./ConfirmCloseModal";
import type { TabState } from "../types";

const ACTIVE_STATUSES = new Set(["streaming", "thinking", "tool_use"]);

export function TabBar() {
  const { tabs, activeTabId, switchTab, closeTab } = useTabsContext();
  const [pendingClose, setPendingClose] = useState<TabState | null>(null);

  const doClose = async (sessionId: string) => {
    closeTab(sessionId);
    try {
      await closePoolSession(sessionId);
    } catch {
      // Session may not have been in the pool (e.g., never connected) — ignore
    }
  };

  const handleCloseTab = (tab: TabState) => {
    if (ACTIVE_STATUSES.has(tab.status)) {
      setPendingClose(tab);
    } else {
      doClose(tab.sessionId);
    }
  };

  if (tabs.length === 0) return null;

  // Sort: orchestrator tab always first
  const sorted = [...tabs].sort((a, b) => {
    if (a.isOrchestrator && !b.isOrchestrator) return -1;
    if (!a.isOrchestrator && b.isOrchestrator) return 1;
    return 0;
  });

  return (
    <>
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
                  handleCloseTab(tab);
                }}
                title="Close tab"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>

      {pendingClose && (
        <ConfirmCloseModal
          tab={pendingClose}
          onConfirm={() => {
            doClose(pendingClose.sessionId);
            setPendingClose(null);
          }}
          onCancel={() => setPendingClose(null)}
        />
      )}
    </>
  );
}
