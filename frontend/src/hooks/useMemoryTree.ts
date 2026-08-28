import { useState, useEffect, useCallback } from "react";
import type { MemoryNode } from "../types";
import { getMemoryTree } from "../api/rest";

interface UseMemoryTreeResult {
  memoryTree: MemoryNode[];
  refresh: () => void;
}

/**
 * The context/memory/ markdown tree. Mirrors `useVisualizations`: fetch on
 * mount, refetch on demand — no watcher on the backend.
 */
export function useMemoryTree(): UseMemoryTreeResult {
  const [memoryTree, setMemoryTree] = useState<MemoryNode[]>([]);

  const refresh = useCallback(() => {
    getMemoryTree()
      .then(setMemoryTree)
      .catch(() => setMemoryTree([]));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { memoryTree, refresh };
}
