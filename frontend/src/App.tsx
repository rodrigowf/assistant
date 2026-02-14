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

  const startOrchestrator = useCallback(() => {
    const tempId = `new-${++tabCounter}`;
    openTab(tempId, "Orchestrator", true);
  }, [openTab]);

  const handleNewOrchestrator = useCallback(() => {
    if (hasActiveOrchestrator()) {
      setShowOrchestratorModal(true);
    } else {
      startOrchestrator();
    }
  }, [hasActiveOrchestrator, startOrchestrator]);

  const handleOrchestratorProceed = useCallback(() => {
    setShowOrchestratorModal(false);
    // Close existing orchestrator tab(s)
    for (const tab of tabs) {
      if (tab.isOrchestrator) {
        closeTab(tab.sessionId);
      }
    }
    startOrchestrator();
  }, [tabs, closeTab, startOrchestrator]);

  return (
    <>
      <Sidebar
        sessions={sessions}
        onDelete={handleDeleteSession}
        onNew={handleNewSession}
        onNewOrchestrator={handleNewOrchestrator}
      />
      <main className="main-content">
        <TabBar />
        <ChatPanelContainer onSessionChange={refresh} />
      </main>
      {showOrchestratorModal && (
        <OrchestratorModal
          onProceed={handleOrchestratorProceed}
          onCancel={() => setShowOrchestratorModal(false)}
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
