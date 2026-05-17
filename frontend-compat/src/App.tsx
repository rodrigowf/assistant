import { useCallback, useEffect, useState } from "react";
import "@/App.css";
import { AuthGate } from "@/components/AuthGate";
import { BusyOverlay } from "@/components/BusyOverlay";
import { Sidebar } from "@/components/Sidebar";
import { TabBar } from "@/components/TabBar";
import { ChatPanelContainer } from "@/components/ChatPanelContainer";
import { ConfigPage } from "@/components/ConfigPage";
import { OrchestratorModal } from "@/components/OrchestratorModal";
import { ConfirmModal } from "@/components/ConfirmModal";
import { TabsProvider, useTabsContext } from "@/context/TabsContext";
import { useSessions } from "@/hooks/useSessions";
import { useReconnectPoolSessions } from "@/hooks/useReconnectPoolSessions";
import { generateUUID } from "@/utils/uuid";

function AppContent() {
  const { sessions, deleting, duplicating, refresh, deleteSession, renameSession, duplicateSession } = useSessions();
  useReconnectPoolSessions();
  const [chatMutationBusy, setChatMutationBusy] = useState<string | null>(null);
  const { tabs, openTab, closeTab, hasActiveOrchestrator } = useTabsContext();
  const [showOrchestratorModal, setShowOrchestratorModal] = useState(false);
  const [pendingOrchestrator, setPendingOrchestrator] = useState<
    { type: "new" } | { type: "resume"; id: string; title: string } | null
  >(null);

  const handleNewSession = useCallback(() => {
    const localId = generateUUID();
    openTab(localId, "New session");
  }, [openTab]);

  const [pendingDelete, setPendingDelete] = useState<{ id: string; title: string } | null>(null);

  const requestDeleteSession = useCallback(
    (id: string) => {
      const session = sessions.find((s) => s.session_id === id);
      setPendingDelete({ id, title: session?.title || "this conversation" });
    },
    [sessions]
  );

  const confirmDeleteSession = useCallback(async () => {
    if (!pendingDelete) return;
    const { id } = pendingDelete;
    setPendingDelete(null);
    await deleteSession(id);
    closeTab(id);
  }, [pendingDelete, deleteSession, closeTab]);

  const closeOrchestratorTabs = useCallback(() => {
    for (const tab of tabs) {
      if (tab.isOrchestrator) closeTab(tab.sessionId);
    }
  }, [tabs, closeTab]);

  const openOrchestrator = useCallback((action: { type: "new" } | { type: "resume"; id: string; title: string }) => {
    if (action.type === "new") {
      openTab(generateUUID(), "Orchestrator", true);
    } else {
      openTab(generateUUID(), action.title, true, action.id);
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

  const [showConfig, setShowConfig] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [sidebarOpen]);

  return (
    <>
      <Sidebar
        sessions={sessions}
        deleting={deleting}
        onDelete={requestDeleteSession}
        onRename={renameSession}
        onDuplicate={(id) => { duplicateSession(id).catch((e) => { console.error("Duplicate failed:", e); }); }}
        onNew={handleNewSession}
        onNewOrchestrator={handleNewOrchestrator}
        onSelectOrchestrator={handleSelectOrchestrator}
        onOpenConfig={() => { setShowConfig(true); setSidebarOpen(false); }}
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
          <TabBar sessions={sessions} onRename={renameSession} />
        </div>
        <ChatPanelContainer sessions={sessions} onSessionChange={refresh} onMutationBusy={setChatMutationBusy} />
        <ConfigPage isOpen={showConfig} onClose={() => setShowConfig(false)} />
      </main>
      {showOrchestratorModal && (
        <OrchestratorModal
          onProceed={handleOrchestratorProceed}
          onCancel={handleOrchestratorCancel}
        />
      )}
      {pendingDelete && (
        <ConfirmModal
          title="Delete conversation?"
          body={
            <>
              <strong>{pendingDelete.title}</strong> will be moved to trash and hidden from
              the assistant. The file is kept on disk and can be recovered manually from
              <code> context/trash/</code>.
            </>
          }
          confirmLabel="Delete"
          destructive
          onConfirm={confirmDeleteSession}
          onCancel={() => setPendingDelete(null)}
        />
      )}
      <BusyOverlay
        show={duplicating || chatMutationBusy !== null}
        label={chatMutationBusy ?? "Duplicating…"}
      />
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
