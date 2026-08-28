import { useState, useEffect, useCallback } from "react";
import type { VisualizationInfo } from "../types";
import {
  listVisualizations,
  renameVisualization as apiRenameViz,
} from "../api/rest";

interface UseVisualizationsResult {
  visualizations: VisualizationInfo[];
  refresh: () => void;
  renameVisualization: (path: string, title: string) => Promise<void>;
}

/**
 * HTML artifacts under context/public/, listed alongside conversations.
 *
 * Deliberately mirrors `useSessions`: fetch on mount, refetch on demand.
 * There's no file watcher on the backend, so freshness comes from the
 * sidebar refetching when it loads.
 */
export function useVisualizations(): UseVisualizationsResult {
  const [visualizations, setVisualizations] = useState<VisualizationInfo[]>([]);

  // No loading flag: the list renders as a section that's simply absent until
  // the first response arrives, so there's nothing to show a spinner for.
  const refresh = useCallback(() => {
    listVisualizations()
      .then(setVisualizations)
      .catch(() => setVisualizations([]));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleRename = useCallback(async (path: string, title: string) => {
    await apiRenameViz(path, title);
    setVisualizations((prev) =>
      prev.map((v) => (v.path === path ? { ...v, title } : v))
    );
  }, []);

  return {
    visualizations,
    refresh,
    renameVisualization: handleRename,
  };
}
