import { useCallback } from "react";
import "./App.css";
import { AuthGate } from "./components/AuthGate";
import { Sidebar } from "./components/Sidebar";
import { TabBar } from "./components/TabBar";
import { ChatPanelContainer } from "./components/ChatPanelContainer";
import { TabsProvider, useTabsContext } from "./context/TabsContext";
import { useSessions } from "./hooks/useSessions";

let tabCounter = 0;

function AppContent() {
  const { sessions, refresh, deleteSession } = useSessions();
  const { openTab, closeTab } = useTabsContext();

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

  return (
    <>
      <Sidebar
        sessions={sessions}
        onDelete={handleDeleteSession}
        onNew={handleNewSession}
      />
      <main className="main-content">
        <TabBar />
        <ChatPanelContainer onSessionChange={refresh} />
      </main>
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
