import { useRef, useEffect, useCallback } from "react";
import { useTabsContext } from "../context/TabsContext";
import { useChatInstance, type ChatInstance } from "../hooks/useChatInstance";
import { ChatPanel } from "./ChatPanel";
import type { SessionStatus, ConnectionState } from "../types";

/**
 * Headless component that manages one chat instance and syncs its state
 * back to the tabs context. Renders nothing — the ChatPanel is rendered
 * separately for the active tab only.
 */
function TabInstance({
  sessionId,
  resumeId,
  onSessionChange,
  instancesRef,
  wsEndpoint,
  isOrchestrator,
  onAgentSessionOpened,
  onAgentSessionClosed,
}: {
  sessionId: string;
  resumeId: string | null;
  onSessionChange: () => void;
  instancesRef: React.RefObject<Map<string, ChatInstance>>;
  wsEndpoint?: string;
  isOrchestrator?: boolean;
  onAgentSessionOpened?: (sessionId: string) => void;
  onAgentSessionClosed?: (sessionId: string) => void;
}) {
  const { updateTab, replaceTabId } = useTabsContext();
  const tabIdRef = useRef(sessionId);

  const onStatusChange = useCallback(
    (status: SessionStatus, connectionState: ConnectionState) => {
      updateTab(tabIdRef.current, { status, connectionState });
    },
    [updateTab]
  );

  const onSessionStarted = useCallback(
    (newSessionId: string) => {
      const oldId = tabIdRef.current;
      if (oldId !== newSessionId) {
        // Replace temp ID with real session ID from backend
        const instance = instancesRef.current?.get(oldId);
        if (instance) {
          instancesRef.current?.delete(oldId);
          instancesRef.current?.set(newSessionId, instance);
        }
        tabIdRef.current = newSessionId;
        replaceTabId(oldId, newSessionId);
      }
      onSessionChange();
    },
    [replaceTabId, onSessionChange, instancesRef]
  );

  const instance = useChatInstance({
    resumeId,
    onSessionChange,
    onStatusChange,
    onSessionStarted,
    wsEndpoint,
    skipHistory: isOrchestrator,
    onAgentSessionOpened,
    onAgentSessionClosed,
  });

  // Register instance so ChatPanelContainer can access it
  useEffect(() => {
    instancesRef.current?.set(sessionId, instance);
    return () => {
      instancesRef.current?.delete(sessionId);
    };
  });

  return null;
}

/**
 * Container that manages all tab instances and renders the active tab's ChatPanel.
 */
export function ChatPanelContainer({
  onSessionChange,
}: {
  onSessionChange: () => void;
}) {
  const { tabs, activeTabId, openTab, closeTab } = useTabsContext();
  const instancesRef = useRef<Map<string, ChatInstance>>(new Map());

  const handleAgentSessionOpened = useCallback(
    (agentSessionId: string) => {
      openTab(agentSessionId, `Agent ${agentSessionId.slice(0, 8)}`);
    },
    [openTab]
  );

  const handleAgentSessionClosed = useCallback(
    (agentSessionId: string) => {
      closeTab(agentSessionId);
    },
    [closeTab]
  );

  const activeInstance = activeTabId ? instancesRef.current.get(activeTabId) : undefined;

  return (
    <>
      {/* Render a headless TabInstance for each open tab */}
      {tabs.map((tab) => (
        <TabInstance
          key={tab.sessionId}
          sessionId={tab.sessionId}
          resumeId={tab.sessionId.startsWith("new-") ? null : tab.sessionId}
          onSessionChange={onSessionChange}
          instancesRef={instancesRef}
          wsEndpoint={tab.isOrchestrator ? "/api/orchestrator/chat" : undefined}
          isOrchestrator={tab.isOrchestrator}
          onAgentSessionOpened={tab.isOrchestrator ? handleAgentSessionOpened : undefined}
          onAgentSessionClosed={tab.isOrchestrator ? handleAgentSessionClosed : undefined}
        />
      ))}

      {/* Render the ChatPanel for the active tab */}
      {activeInstance ? (
        <ChatPanel
          messages={activeInstance.messages}
          status={activeInstance.status}
          connectionState={activeInstance.connectionState}
          cost={activeInstance.cost}
          turns={activeInstance.turns}
          error={activeInstance.error}
          onSend={activeInstance.send}
          onInterrupt={activeInstance.interrupt}
        />
      ) : (
        <main className="chat-panel">
          <div className="message-list empty">
            <div className="empty-state">
              <div className="empty-title">No session open</div>
              <div className="empty-hint">
                Start a new session or select one from the sidebar.
              </div>
            </div>
          </div>
        </main>
      )}
    </>
  );
}
