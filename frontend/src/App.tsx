import { useCallback, useEffect, useState } from "react";
import "./App.css";
import { AuthGate } from "./components/AuthGate";
import { Sidebar } from "./components/Sidebar";
import { TabBar } from "./components/TabBar";
import { ChatPanelContainer } from "./components/ChatPanelContainer";
import { OrchestratorModal } from "./components/OrchestratorModal";
import { TabsProvider, useTabsContext } from "./context/TabsContext";
import { useSessions } from "./hooks/useSessions";
import { useReconnectPoolSessions } from "./hooks/useReconnectPoolSessions";
import { generateUUID } from "./utils/uuid";

function AppContent() {
  const { sessions, refresh, deleteSession, renameSession } = useSessions();
  useReconnectPoolSessions();
  const { tabs, openTab, closeTab, hasActiveOrchestrator } = useTabsContext();
  const [showOrchestratorModal, setShowOrchestratorModal] = useState(false);
  // Pending orchestrator action: either open new or resume existing
  const [pendingOrchestrator, setPendingOrchestrator] = useState<
    { type: "new" } | { type: "resume"; id: string; title: string } | null
  >(null);

  const handleNewSession = useCallback(() => {
    const localId = generateUUID();
    openTab(localId, "New session");
  }, [openTab]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      await deleteSession(id);
      closeTab(id);
    },
    [deleteSession, closeTab]
  );

  const closeOrchestratorTabs = useCallback(() => {
    for (const tab of tabs) {
      if (tab.isOrchestrator) {
        closeTab(tab.sessionId);
      }
    }
  }, [tabs, closeTab]);

  const openOrchestrator = useCallback((action: { type: "new" } | { type: "resume"; id: string; title: string }) => {
    if (action.type === "new") {
      const localId = generateUUID();
      openTab(localId, "Orchestrator", true);
    } else {
      // Resuming from history: generate a stable local_id, pass SDK ID as resumeSdkId
      const localId = generateUUID();
      openTab(localId, action.title, true, action.id);
    }
  }, [openTab]);

  const handleNewOrchestrator = useCallback(() => {
    const action = { type: "new" as const };
    if (hasActiveOrchestrator()) {
      setPendingOrchestrator(action);
      setShowOrchestratorModal(true);
    } else {
      openOrchestrator(action);
    }
  }, [hasActiveOrchestrator, openOrchestrator]);

  const handleSelectOrchestrator = useCallback((id: string, title: string) => {
    const action = { type: "resume" as const, id, title };
    if (hasActiveOrchestrator()) {
      setPendingOrchestrator(action);
      setShowOrchestratorModal(true);
    } else {
      openOrchestrator(action);
    }
  }, [hasActiveOrchestrator, openOrchestrator]);

  const handleOrchestratorProceed = useCallback(() => {
    setShowOrchestratorModal(false);
    closeOrchestratorTabs();
    if (pendingOrchestrator) {
      openOrchestrator(pendingOrchestrator);
      setPendingOrchestrator(null);
    }
  }, [closeOrchestratorTabs, openOrchestrator, pendingOrchestrator]);

  const handleOrchestratorCancel = useCallback(() => {
    setShowOrchestratorModal(false);
    setPendingOrchestrator(null);
  }, []);

  // Mobile sidebar
  const [sidebarOpen, setSidebarOpen] = useState(false);
  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [sidebarOpen]);

  return (
    <>
      <Sidebar
        sessions={sessions}
        onDelete={handleDeleteSession}
        onRename={renameSession}
        onNew={handleNewSession}
        onNewOrchestrator={handleNewOrchestrator}
        onSelectOrchestrator={handleSelectOrchestrator}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />
      <main className="main-content">
        <div className="topbar-row">
          <button className="sidebar-toggle-btn" onClick={() => setSidebarOpen(true)} aria-label="Open sidebar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
          <TabBar />
        </div>
        <ChatPanelContainer onSessionChange={refresh} />
      </main>
      {showOrchestratorModal && (
        <OrchestratorModal
          onProceed={handleOrchestratorProceed}
          onCancel={handleOrchestratorCancel}
        />
      )}
    </>
  );
}

export default function App() {
  return (
    <AuthGate>
      <TabsProvider>
        <AppContent />
      </TabsProvider>
    </AuthGate>
  );
}
