import { useState, useEffect, useCallback } from "react";
import type { SessionInfo } from "../types";
import {
  listSessions,
  deleteSession as apiDelete,
  renameSession as apiRename,
  duplicateSession as apiDuplicate,
} from "../api/rest";

interface UseSessionsResult {
  sessions: SessionInfo[];
  loading: boolean;
  /** True while a delete is in flight — drives the history-panel spinner. */
  deleting: boolean;
  /** True while a duplicate is in flight — drives the app-wide busy overlay. */
  duplicating: boolean;
  refresh: () => void;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  duplicateSession: (id: string) => Promise<string>;
}

export function useSessions(): UseSessionsResult {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState(false);
  const [duplicating, setDuplicating] = useState(false);

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

  const handleDelete = useCallback(async (id: string) => {
    setDeleting(true);
    try {
      await apiDelete(id);
      setSessions((prev) => prev.filter((s) => s.session_id !== id));
    } finally {
      setDeleting(false);
    }
  }, []);

  const handleRename = useCallback(async (id: string, title: string) => {
    await apiRename(id, title);
    setSessions((prev) =>
      prev.map((s) => s.session_id === id ? { ...s, title } : s)
    );
  }, []);

  const handleDuplicate = useCallback(async (id: string) => {
    setDuplicating(true);
    try {
      const { session_id } = await apiDuplicate(id);
      // Refresh so the new session appears at the top of the sidebar.
      refresh();
      return session_id;
    } finally {
      setDuplicating(false);
    }
  }, [refresh]);

  return {
    sessions,
    loading,
    deleting,
    duplicating,
    refresh,
    deleteSession: handleDelete,
    renameSession: handleRename,
    duplicateSession: handleDuplicate,
  };
}
