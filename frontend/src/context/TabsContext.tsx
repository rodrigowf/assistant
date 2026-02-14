import {
  createContext,
  useContext,
  useReducer,
  useCallback,
  type ReactNode,
} from "react";
import type { TabState, TabsState, SessionStatus, ConnectionState } from "../types";

// -------------------------------------------------------------------
// Actions
// -------------------------------------------------------------------

type TabsAction =
  | { type: "OPEN_TAB"; sessionId: string; title: string }
  | { type: "CLOSE_TAB"; sessionId: string }
  | { type: "SWITCH_TAB"; sessionId: string }
  | { type: "UPDATE_TAB"; sessionId: string; updates: Partial<Pick<TabState, "status" | "connectionState" | "title">> }
  | { type: "REPLACE_TAB_ID"; oldId: string; newId: string };

// -------------------------------------------------------------------
// Reducer
// -------------------------------------------------------------------

const INITIAL_STATE: TabsState = {
  tabs: [],
  activeTabId: null,
};

function reducer(state: TabsState, action: TabsAction): TabsState {
  switch (action.type) {
    case "OPEN_TAB": {
      // Already open? Just switch to it
      if (state.tabs.some((t) => t.sessionId === action.sessionId)) {
        return { ...state, activeTabId: action.sessionId };
      }
      const tab: TabState = {
        sessionId: action.sessionId,
        title: action.title || "New session",
        status: "connecting",
        connectionState: "disconnected",
      };
      return {
        tabs: [...state.tabs, tab],
        activeTabId: action.sessionId,
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

    case "REPLACE_TAB_ID": {
      const wasActive = state.activeTabId === action.oldId;
      return {
        tabs: state.tabs.map((t) =>
          t.sessionId === action.oldId ? { ...t, sessionId: action.newId } : t
        ),
        activeTabId: wasActive ? action.newId : state.activeTabId,
      };
    }

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
  openTab: (sessionId: string, title?: string) => void;
  closeTab: (sessionId: string) => void;
  switchTab: (sessionId: string) => void;
  updateTab: (sessionId: string, updates: Partial<Pick<TabState, "status" | "connectionState" | "title">>) => void;
  replaceTabId: (oldId: string, newId: string) => void;
  isTabOpen: (sessionId: string) => boolean;
}

const TabsContext = createContext<TabsContextValue | null>(null);

export function TabsProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);

  const openTab = useCallback((sessionId: string, title = "New session") => {
    dispatch({ type: "OPEN_TAB", sessionId, title });
  }, []);

  const closeTab = useCallback((sessionId: string) => {
    dispatch({ type: "CLOSE_TAB", sessionId });
  }, []);

  const switchTab = useCallback((sessionId: string) => {
    dispatch({ type: "SWITCH_TAB", sessionId });
  }, []);

  const updateTab = useCallback(
    (sessionId: string, updates: Partial<Pick<TabState, "status" | "connectionState" | "title">>) => {
      dispatch({ type: "UPDATE_TAB", sessionId, updates });
    },
    []
  );

  const replaceTabId = useCallback((oldId: string, newId: string) => {
    dispatch({ type: "REPLACE_TAB_ID", oldId, newId });
  }, []);

  const isTabOpen = useCallback(
    (sessionId: string) => state.tabs.some((t) => t.sessionId === sessionId),
    [state.tabs]
  );

  return (
    <TabsContext.Provider
      value={{
        tabs: state.tabs,
        activeTabId: state.activeTabId,
        openTab,
        closeTab,
        switchTab,
        updateTab,
        replaceTabId,
        isTabOpen,
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

export function getTabStatusIcon(tab: TabState): string {
  if (tab.connectionState === "connecting") return "loading";
  if (tab.connectionState === "error") return "error";
  if (tab.connectionState === "disconnected") return "waiting";

  switch (tab.status) {
    case "streaming":
    case "thinking":
    case "tool_use":
      return "active";
    case "interrupted":
      return "error";
    default:
      return "idle";
  }
}
