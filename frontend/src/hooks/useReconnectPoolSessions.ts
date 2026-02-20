import { useEffect } from "react";
import { listPoolSessions } from "../api/rest";
import { useTabsContext } from "../context/TabsContext";

/**
 * On mount, fetch sessions that are still live in the backend pool and
 * re-open a tab for each one using its original local_id. This allows the
 * frontend to reconnect after a browser close/refresh — the backend keeps
 * sessions running indefinitely until explicitly closed.
 *
 * The existing _handle_start logic in chat.py / orchestrator.py already
 * handles the re-subscription: if the local_id is already in the pool,
 * it just subscribes the new WebSocket without creating a new session.
 */
export function useReconnectPoolSessions() {
  const { openTab, isTabOpen } = useTabsContext();

  useEffect(() => {
    let cancelled = false;

    listPoolSessions()
      .then((sessions) => {
        if (cancelled) return;
        for (const s of sessions) {
          // Don't open a tab that's already open
          if (isTabOpen(s.local_id)) continue;

          const title = s.title || (s.is_orchestrator ? "Orchestrator" : "Session");
          openTab(
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

    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Run once on mount only
}
