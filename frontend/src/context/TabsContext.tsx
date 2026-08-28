import {
  createContext,
  useContext,
  useReducer,
  useCallback,
  type ReactNode,
} from "react";
import type { TabState, TabsState } from "../types";

// -------------------------------------------------------------------
// Actions
// -------------------------------------------------------------------

type TabsAction =
  | { type: "OPEN_TAB"; sessionId: string; title: string; isOrchestrator?: boolean; resumeSdkId?: string }
  | { type: "OPEN_VIZ_TAB"; vizPath: string; vizUrl: string; title: string }
  | { type: "OPEN_MEMORY_TAB"; memoryPath: string; title: string }
  | { type: "CLOSE_TAB"; sessionId: string }
  | { type: "SWITCH_TAB"; sessionId: string }
  | { type: "UPDATE_TAB"; sessionId: string; updates: Partial<Pick<TabState, "status" | "connectionState" | "title" | "resumeSdkId">> };

// -------------------------------------------------------------------
// Reducer
// -------------------------------------------------------------------

const INITIAL_STATE: TabsState = {
  tabs: [],
  activeTabId: null,
};

/** Stable tab id for a visualization, namespaced so it can't collide with a
 *  session UUID. */
export function vizTabId(vizPath: string): string {
  return `viz:${vizPath}`;
}

/** Stable tab id for a memory file. Same namespacing rationale as vizTabId. */
export function memoryTabId(memoryPath: string): string {
  return `memory:${memoryPath}`;
}

/**
 * True for tabs backed by a static file (visualization or memory doc) rather
 * than a session. These must be skipped anywhere a ChatInstance would be
 * created or a pool session closed — there's no session id behind them.
 */
export function isDocTab(tab: TabState): boolean {
  return !!(tab.vizPath || tab.memoryPath);
}

function reducer(state: TabsState, action: TabsAction): TabsState {
  switch (action.type) {
    case "OPEN_TAB": {
      // Already open? Just switch to it
      if (state.tabs.some((t) => t.sessionId === action.sessionId)) {
        return { ...state, activeTabId: action.sessionId };
      }
      const tab: TabState = {
        sessionId: action.sessionId,
        resumeSdkId: action.resumeSdkId,
        title: action.title || "New session",
        status: "connecting",
        connectionState: "disconnected",
        isOrchestrator: action.isOrchestrator,
      };
      return {
        tabs: [...state.tabs, tab],
        activeTabId: action.sessionId,
      };
    }

    case "OPEN_VIZ_TAB": {
      // Viz tabs are keyed by their file path so reopening the same file
      // switches to the existing tab instead of duplicating it.
      const sessionId = vizTabId(action.vizPath);
      if (state.tabs.some((t) => t.sessionId === sessionId)) {
        return { ...state, activeTabId: sessionId };
      }
      const tab: TabState = {
        sessionId,
        title: action.title || action.vizPath,
        // No backing session — a viz tab is never "connecting". Marking it
        // idle/connected keeps status-derived UI (tab dots, close guards)
        // correct without special-casing them.
        status: "idle",
        connectionState: "connected",
        vizPath: action.vizPath,
        vizUrl: action.vizUrl,
      };
      return {
        tabs: [...state.tabs, tab],
        activeTabId: sessionId,
      };
    }

    case "OPEN_MEMORY_TAB": {
      const sessionId = memoryTabId(action.memoryPath);
      if (state.tabs.some((t) => t.sessionId === sessionId)) {
        return { ...state, activeTabId: sessionId };
      }
      const tab: TabState = {
        sessionId,
        title: action.title || action.memoryPath,
        // No backing session — see the OPEN_VIZ_TAB note.
        status: "idle",
        connectionState: "connected",
        memoryPath: action.memoryPath,
      };
      return {
        tabs: [...state.tabs, tab],
        activeTabId: sessionId,
      };
    }

    case "CLOSE_TAB": {
      const idx = state.tabs.findIndex((t) => t.sessionId === action.sessionId);
      if (idx === -1) return state;
      const tabs = state.tabs.filter((t) => t.sessionId !== action.sessionId);
      let activeTabId = state.activeTabId;
      if (activeTabId === action.sessionId) {
        // Switch to adjacent tab, prefer right then left
        if (tabs.length === 0) {
          activeTabId = null;
        } else if (idx < tabs.length) {
          activeTabId = tabs[idx].sessionId;
        } else {
          activeTabId = tabs[tabs.length - 1].sessionId;
        }
      }
      return { tabs, activeTabId };
    }

    case "SWITCH_TAB":
      if (!state.tabs.some((t) => t.sessionId === action.sessionId)) return state;
      return { ...state, activeTabId: action.sessionId };

    case "UPDATE_TAB":
      return {
        ...state,
        tabs: state.tabs.map((t) =>
          t.sessionId === action.sessionId ? { ...t, ...action.updates } : t
        ),
      };

    default:
      return state;
  }
}

// -------------------------------------------------------------------
// Context
// -------------------------------------------------------------------

interface TabsContextValue {
  tabs: TabState[];
  activeTabId: string | null;
  openTab: (sessionId: string, title?: string, isOrchestrator?: boolean, resumeSdkId?: string) => void;
  /** Open (or switch to) a visualization tab for a context/public/ HTML file. */
  openVizTab: (vizPath: string, vizUrl: string, title: string) => void;
  /** Open (or switch to) a tab rendering a context/memory/ markdown file. */
  openMemoryTab: (memoryPath: string, title: string) => void;
  closeTab: (sessionId: string) => void;
  switchTab: (sessionId: string) => void;
  updateTab: (sessionId: string, updates: Partial<Pick<TabState, "status" | "connectionState" | "title" | "resumeSdkId">>) => void;
  isTabOpen: (sessionId: string) => boolean;
  hasActiveOrchestrator: () => boolean;
  /** Find a tab that was opened to resume a given SDK session ID. */
  findTabByResumeId: (sdkId: string) => TabState | undefined;
}

const TabsContext = createContext<TabsContextValue | null>(null);

export function TabsProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);

  const openTab = useCallback((sessionId: string, title = "New session", isOrchestrator?: boolean, resumeSdkId?: string) => {
    dispatch({ type: "OPEN_TAB", sessionId, title, isOrchestrator, resumeSdkId });
  }, []);

  const openVizTab = useCallback((vizPath: string, vizUrl: string, title: string) => {
    dispatch({ type: "OPEN_VIZ_TAB", vizPath, vizUrl, title });
  }, []);

  const openMemoryTab = useCallback((memoryPath: string, title: string) => {
    dispatch({ type: "OPEN_MEMORY_TAB", memoryPath, title });
  }, []);

  const closeTab = useCallback((sessionId: string) => {
    dispatch({ type: "CLOSE_TAB", sessionId });
  }, []);

  const switchTab = useCallback((sessionId: string) => {
    dispatch({ type: "SWITCH_TAB", sessionId });
  }, []);

  const updateTab = useCallback(
    (sessionId: string, updates: Partial<Pick<TabState, "status" | "connectionState" | "title" | "resumeSdkId">>) => {
      dispatch({ type: "UPDATE_TAB", sessionId, updates });
    },
    []
  );

  const isTabOpen = useCallback(
    (sessionId: string) => state.tabs.some((t) => t.sessionId === sessionId),
    [state.tabs]
  );

  const hasActiveOrchestrator = useCallback(
    () => state.tabs.some((t) => t.isOrchestrator),
    [state.tabs]
  );

  const findTabByResumeId = useCallback(
    (sdkId: string) => state.tabs.find((t) => t.resumeSdkId === sdkId),
    [state.tabs]
  );

  return (
    <TabsContext.Provider
      value={{
        tabs: state.tabs,
        activeTabId: state.activeTabId,
        openTab,
        openVizTab,
        openMemoryTab,
        closeTab,
        switchTab,
        updateTab,
        isTabOpen,
        hasActiveOrchestrator,
        findTabByResumeId,
      }}
    >
      {children}
    </TabsContext.Provider>
  );
}

export function useTabsContext(): TabsContextValue {
  const ctx = useContext(TabsContext);
  if (!ctx) throw new Error("useTabsContext must be used inside TabsProvider");
  return ctx;
}

// -------------------------------------------------------------------
// Utility
// -------------------------------------------------------------------

export function getTabStatusIcon(tab: TabState): string | null {
  // Doc tabs have no backing session — no liveness to report.
  if (isDocTab(tab)) return null;
  // No dot for disconnected/error/connecting — shown via tab opacity
  if (tab.connectionState !== "connected") return null;

  switch (tab.status) {
    case "streaming":
    case "thinking":
    case "tool_use":
      return "active";
    default:
      return "idle";
  }
}
