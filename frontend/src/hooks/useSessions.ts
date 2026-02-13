import { useState, useEffect, useCallback } from "react";
import type { SessionInfo } from "../types";
import { listSessions, deleteSession as apiDelete } from "../api/rest";

interface UseSessionsResult {
  sessions: SessionInfo[];
  loading: boolean;
  refresh: () => void;
  deleteSession: (id: string) => Promise<void>;
}

export function useSessions(): UseSessionsResult {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => {
    setLoading(true);
    listSessions()
      .then(setSessions)
      .catch(() => setSessions([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleDelete = useCallback(
    async (id: string) => {
      await apiDelete(id);
      setSessions((prev) => prev.filter((s) => s.session_id !== id));
    },
    []
  );

  return { sessions, loading, refresh, deleteSession: handleDelete };
}
