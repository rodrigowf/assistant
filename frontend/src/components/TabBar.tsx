import { useEffect, useMemo, useRef, useState } from "react";
import { useTabsContext, getTabStatusIcon } from "../context/TabsContext";
import { closePoolSession } from "../api/rest";
import { ConfirmCloseModal } from "./ConfirmCloseModal";
import type { SessionInfo, TabState } from "../types";

const ACTIVE_STATUSES = new Set(["streaming", "thinking", "tool_use"]);

interface Props {
  /** Authoritative session list (drives tab titles so rename/orchestrator-opened
   *  tabs stay in sync with the sidebar — they share one source of truth). */
  sessions: SessionInfo[];
  /** Persist a rename via the same path the sidebar uses — keeps both UIs and
   *  `.titles.json` aligned. */
  onRename: (sdkSessionId: string, title: string) => Promise<void> | void;
}

export function TabBar({ sessions, onRename }: Props) {
  const { tabs, activeTabId, switchTab, closeTab } = useTabsContext();
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

  // Build lookups so each tab can find its session entry by either join key.
  const { bySdk, byLocal } = useMemo(() => {
    const bySdk = new Map<string, SessionInfo>();
    const byLocal = new Map<string, SessionInfo>();
    for (const s of sessions) {
      bySdk.set(s.session_id, s);
      if (s.local_id) byLocal.set(s.local_id, s);
    }
    return { bySdk, byLocal };
  }, [sessions]);

  // Tab title is derived: prefer the persisted/computed title from the session
  // list (matches the sidebar), fall back to the tab's own placeholder.
  const titleFor = (tab: TabState): string => {
    const info =
      (tab.resumeSdkId && bySdk.get(tab.resumeSdkId)) ||
      byLocal.get(tab.sessionId);
    return info?.title || tab.title || "New session";
  };

  const startEdit = (tab: TabState) => {
    setDraftTitle(titleFor(tab));
    setEditingTabId(tab.sessionId);
  };

  const commitEdit = (tab: TabState) => {
    const trimmed = draftTitle.trim();
    setEditingTabId(null);
    if (!trimmed || trimmed === titleFor(tab)) return;
    // Persist through the shared sidebar path so both UIs refresh from the
    // same authoritative state. Requires the SDK session id — pre-first-turn
    // tabs (no resumeSdkId yet) silently no-op rather than writing a title
    // that won't have anywhere to land in `.titles.json`.
    if (tab.resumeSdkId) {
      Promise.resolve(onRename(tab.resumeSdkId, trimmed)).catch(() => {
        // best-effort — user can retry
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
                  {titleFor(tab)}
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
          title={titleFor(pendingClose)}
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
