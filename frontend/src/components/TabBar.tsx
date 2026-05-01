import { useEffect, useRef, useState } from "react";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { closePoolSession, renameSession } from "../api/rest";
import { ConfirmCloseModal } from "./ConfirmCloseModal";
import type { TabState } from "../types";

const ACTIVE_STATUSES = new Set(["streaming", "thinking", "tool_use"]);

export function TabBar() {
  const { tabs, activeTabId, switchTab, closeTab, updateTab } = useTabsContext();
  const [pendingClose, setPendingClose] = useState<TabState | null>(null);
  const [editingTabId, setEditingTabId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingTabId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingTabId]);

  const startEdit = (tab: TabState) => {
    setDraftTitle(tab.title || "");
    setEditingTabId(tab.sessionId);
  };

  const commitEdit = (tab: TabState) => {
    const trimmed = draftTitle.trim();
    setEditingTabId(null);
    if (!trimmed || trimmed === tab.title) return;
    updateTab(tab.sessionId, { title: trimmed });
    if (tab.resumeSdkId) {
      renameSession(tab.resumeSdkId, trimmed).catch(() => {
        // Title only lives in memory if persistence fails — user can retry.
      });
    }
  };

  const cancelEdit = () => setEditingTabId(null);

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

          const isEditing = editingTabId === tab.sessionId;

          return (
            <div
              key={tab.sessionId}
              className={`tab ${isActive ? "active" : ""}${tab.isOrchestrator ? " orchestrator" : ""}`}
              onClick={() => {
                if (isEditing) return;
                switchTab(tab.sessionId);
              }}
            >
              {statusIcon && <span className={`tab-status ${statusIcon}`} />}
              {isEditing ? (
                <input
                  ref={inputRef}
                  className="tab-title-input"
                  value={draftTitle}
                  onChange={(e) => setDraftTitle(e.target.value)}
                  onBlur={() => commitEdit(tab)}
                  onClick={(e) => e.stopPropagation()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitEdit(tab);
                    else if (e.key === "Escape") cancelEdit();
                  }}
                />
              ) : (
                <span
                  className="tab-title"
                  title={isActive && !tab.isOrchestrator ? "Double-click to rename" : undefined}
                  onDoubleClick={(e) => {
                    if (tab.isOrchestrator) return;
                    e.stopPropagation();
                    startEdit(tab);
                  }}
                >
                  {tab.title || "New session"}
                </span>
              )}
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
