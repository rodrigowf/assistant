import { useCallback, useState } from "react";
import "./App.css";
import { AuthGate } from "./components/AuthGate";
import { Sidebar } from "./components/Sidebar";
import { TabBar } from "./components/TabBar";
import { ChatPanelContainer } from "./components/ChatPanelContainer";
import { OrchestratorModal } from "./components/OrchestratorModal";
import { TabsProvider, useTabsContext } from "./context/TabsContext";
import { useSessions } from "./hooks/useSessions";

let tabCounter = 0;

function AppContent() {
  const { sessions, refresh, deleteSession } = useSessions();
  const { tabs, openTab, closeTab, hasActiveOrchestrator } = useTabsContext();
  const [showOrchestratorModal, setShowOrchestratorModal] = useState(false);
  // Pending orchestrator action: either open new or resume existing
  const [pendingOrchestrator, setPendingOrchestrator] = useState<
    { type: "new" } | { type: "resume"; id: string; title: string } | null
  >(null);

  const handleNewSession = useCallback(() => {
    const tempId = `new-${++tabCounter}`;
    openTab(tempId, "New session");
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
      const tempId = `new-${++tabCounter}`;
      openTab(tempId, "Orchestrator", true);
    } else {
      openTab(action.id, action.title, true);
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

  return (
    <>
      <Sidebar
        sessions={sessions}
        onDelete={handleDeleteSession}
        onNew={handleNewSession}
        onNewOrchestrator={handleNewOrchestrator}
        onSelectOrchestrator={handleSelectOrchestrator}
      />
      <main className="main-content">
        <TabBar />
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
