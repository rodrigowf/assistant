import { useCallback } from "react";
import "./App.css";
import { AuthGate } from "./components/AuthGate";
import { Sidebar } from "./components/Sidebar";
import { ChatPanel } from "./components/ChatPanel";
import { useChat } from "./hooks/useChat";
import { useSessions } from "./hooks/useSessions";

export default function App() {
  const { sessions, refresh, deleteSession } = useSessions();
  const chat = useChat({ onSessionChange: refresh });

  const handleNewSession = useCallback(() => {
    chat.startSession(null);
  }, [chat]);

  const handleSelectSession = useCallback(
    (id: string | null) => {
      if (id) {
        chat.startSession(id);
      }
    },
    [chat]
  );

  const handleDeleteSession = useCallback(
    async (id: string) => {
      await deleteSession(id);
      if (chat.sessionId === id) {
        chat.stopSession();
      }
    },
    [deleteSession, chat]
  );

  return (
    <AuthGate>
      <Sidebar
        sessions={sessions}
        activeId={chat.sessionId}
        onSelect={handleSelectSession}
        onDelete={handleDeleteSession}
        onNew={handleNewSession}
      />
      <ChatPanel
        messages={chat.messages}
        status={chat.status}
        connectionState={chat.connectionState}
        cost={chat.cost}
        turns={chat.turns}
        error={chat.error}
        onSend={chat.send}
        onInterrupt={chat.interrupt}
      />
    </AuthGate>
  );
}
