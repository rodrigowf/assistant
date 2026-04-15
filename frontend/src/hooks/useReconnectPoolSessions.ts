import { useEffect, useRef } from "react";
import { listPoolSessions } from "../api/rest";
import { useTabsContext } from "../context/TabsContext";

/**
 * On mount, fetch sessions that are still live in the backend pool and
 * re-open a tab for each one using its original local_id. This allows the
 * frontend to reconnect after a browser close/refresh — the backend keeps
 * sessions running indefinitely until explicitly closed.
 *
 * Also re-syncs when the tab becomes visible again (after screen lock or
 * switching browser tabs), so sessions created by other clients (e.g. the
 * Android app) while this tab was in the background appear immediately.
 *
 * The existing _handle_start logic in chat.py / orchestrator.py already
 * handles the re-subscription: if the local_id is already in the pool,
 * it just subscribes the new WebSocket without creating a new session.
 */
export function useReconnectPoolSessions() {
  const { openTab, isTabOpen } = useTabsContext();
  // Keep a stable ref to isTabOpen so the visibility handler doesn't go stale
  const isTabOpenRef = useRef(isTabOpen);
  isTabOpenRef.current = isTabOpen;
  const openTabRef = useRef(openTab);
  openTabRef.current = openTab;

  useEffect(() => {
    let cancelled = false;

    function syncPoolSessions() {
      listPoolSessions()
        .then((sessions) => {
          if (cancelled) return;
          for (const s of sessions) {
            // Don't open a tab that's already open
            if (isTabOpenRef.current(s.local_id)) continue;

            const title = s.title || (s.is_orchestrator ? "Orchestrator" : "Session");
            openTabRef.current(
              s.local_id,
              title,
              s.is_orchestrator,
              s.sdk_session_id ?? undefined,
            );
          }
        })
        .catch(() => {
          // Backend not ready or no live sessions — silently ignore
        });
    }

    // Sync on mount
    syncPoolSessions();

    // Re-sync whenever the tab becomes visible (screen unlock, tab switch, etc.)
    const onVisibilityChange = () => {
      if (!document.hidden) {
        syncPoolSessions();
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Refs are stable — no deps needed
}
