import { useRef, useEffect, useCallback, useState, lazy, Suspense } from "react";
import { useTabsContext } from "../context/TabsContext";
import { useChatInstance, type ChatInstance } from "../hooks/useChatInstance";
import { useVoiceOrchestrator } from "../hooks/useVoiceOrchestrator";
import { ChatPanel } from "./ChatPanel";
import { ConfirmModal } from "./ConfirmModal";
import {
  closePoolSession,
  forkSession as apiForkSession,
  listModels,
  truncateSession as apiTruncateSession,
  type ModelsResponse,
} from "../api/rest";
import { generateUUID } from "../utils/uuid";
import type { SessionStatus, ConnectionState } from "../types";

const SessionConfigPage = lazy(() => import("./SessionConfigPage").then(m => ({ default: m.SessionConfigPage })));

/**
 * Headless component that manages one chat instance and syncs its state
 * back to the tabs context. Renders nothing — the ChatPanel is rendered
 * separately for the active tab only.
 */
function TabInstance({
  sessionId,
  resumeSdkId,
  onSessionChange,
  instancesRef,
  notifyUpdate,
  wsEndpoint,
  isOrchestrator,
  onAgentSessionOpened,
  onAgentSessionClosed,
  onSessionClosed,
}: {
  sessionId: string;
  resumeSdkId: string | null;
  onSessionChange: () => void;
  instancesRef: React.RefObject<Map<string, ChatInstance>>;
  notifyUpdate: () => void;
  wsEndpoint?: string;
  isOrchestrator?: boolean;
  onAgentSessionOpened?: (sessionId: string, sdkSessionId?: string) => void;
  onAgentSessionClosed?: (sessionId: string) => void;
  onSessionClosed?: () => void;
}) {
  const { updateTab } = useTabsContext();

  const onStatusChange = useCallback(
    (status: SessionStatus, connectionState: ConnectionState) => {
      updateTab(sessionId, { status, connectionState });
    },
    [updateTab, sessionId]
  );

  const onSdkSessionAssigned = useCallback(
    (sdkSessionId: string) => {
      updateTab(sessionId, { resumeSdkId: sdkSessionId });
    },
    [updateTab, sessionId]
  );

  const instance = useChatInstance({
    localId: sessionId,
    resumeSdkId,
    onSessionChange,
    onStatusChange,
    wsEndpoint,
    skipHistory: !!isOrchestrator && !resumeSdkId,
    onAgentSessionOpened,
    onAgentSessionClosed,
    onSessionClosed,
    onSdkSessionAssigned,
  });

  // Keep instancesRef up to date on every render.
  useEffect(() => {
    instancesRef.current?.set(sessionId, instance);
  });

  // Notify the container when messages or pagination state change so it re-renders with fresh props.
  useEffect(() => {
    notifyUpdate();
  }, [instance.messages, instance.hasMoreMessages, notifyUpdate]);

  // Clean up on unmount only.
  useEffect(() => {
    return () => {
      instancesRef.current?.delete(sessionId);
    };
  }, [sessionId, instancesRef]);

  return null;
}

/**
 * Renders the ChatPanel for orchestrator sessions with voice support.
 * The useVoiceOrchestrator hook lives here so it's always mounted for the
 * active orchestrator tab (hooks can't be conditional).
 */
function OrchestratorChatPanel({
  sessionId,
  resumeSdkId,
  instance,
  onSessionChange,
  isActive,
  supportsAudio,
  onRewindMessage,
  onForkMessage,
}: {
  sessionId: string;
  resumeSdkId?: string | null;
  instance: ChatInstance;
  onSessionChange: () => void;
  isActive?: boolean;
  supportsAudio?: boolean;
  onRewindMessage?: (dropLastN: number) => void;
  onForkMessage?: (dropLastN: number) => void;
}) {
  const { voiceStatus, startVoice, stopVoice, isMuted, toggleMute, isAssistantMuted, toggleAssistantMute, micLevel, speakerLevel, voiceError } = useVoiceOrchestrator({
    localId: sessionId,
    resumeSdkId,
    onUserTranscript: (text) => {
      instance.addDisplayMessage("user", text);
    },
    onAssistantDelta: (delta) => {
      instance.voiceAssistantDelta(delta);
    },
    onAssistantComplete: (text) => {
      instance.voiceAssistantComplete(text);
    },
    onToolUse: (callId, toolName, toolInput) => {
      instance.dispatchToolUse(callId, toolName, toolInput);
    },
    onTurnComplete: () => {
      onSessionChange();
    },
    onBeforeStart: () => {
      instance.stop();
    },
    onAfterStop: () => {
      instance.restart();
    },
  });

  return (
    <ChatPanel
      messages={instance.messages}
      status={instance.status}
      connectionState={instance.connectionState}
      cost={instance.cost}
      turns={instance.turns}
      error={instance.error}
      stall={instance.stall}
      pendingPermission={instance.pendingPermission}
      onRespondToPermission={instance.respondToPermission}
      onSend={instance.send}
      onSendAudio={instance.sendAudio}
      onInterrupt={instance.interrupt}
      onCompact={instance.compact}
      contextUsage={instance.contextUsage}
      isActive={isActive}
      hasMoreMessages={instance.hasMoreMessages}
      onLoadMore={instance.loadMoreMessages}
      onRewindMessage={onRewindMessage}
      onForkMessage={onForkMessage}
      isOrchestrator={true}
      voiceStatus={voiceStatus}
      onVoiceStart={startVoice}
      onVoiceStop={stopVoice}
      isMicMuted={isMuted}
      onMicMuteToggle={toggleMute}
      isAssistantMuted={isAssistantMuted}
      onAssistantMuteToggle={toggleAssistantMute}
      micLevel={micLevel}
      speakerLevel={speakerLevel}
      voiceError={voiceError}
      supportsAudio={supportsAudio}
    />
  );
}

/**
 * Container that manages all tab instances and renders the active tab's ChatPanel.
 */
export function ChatPanelContainer({
  onSessionChange,
}: {
  onSessionChange: () => void;
}) {
  const { tabs, activeTabId, openTab, closeTab } = useTabsContext();
  const instancesRef = useRef<Map<string, ChatInstance>>(new Map());
  const [, setInstanceVersion] = useState(0);
  const notifyUpdate = useCallback(() => setInstanceVersion(v => v + 1), []);

  // Rewind / fork confirmation state. Both actions act on the conversation
  // file (via sdkSessionId aka resumeSdkId). `dropLastN` is bottom-relative
  // (number of messages to drop from the end), so the request stays correct
  // even when the frontend has only loaded the most recent page.
  const [pendingAction, setPendingAction] = useState<
    | { kind: "rewind"; tabSessionId: string; sdkSessionId: string; dropLastN: number }
    | { kind: "fork"; tabSessionId: string; sdkSessionId: string; dropLastN: number }
    | null
  >(null);

  const requestRewind = useCallback(
    (tabSessionId: string, sdkSessionId: string | null | undefined, dropLastN: number) => {
      if (!sdkSessionId) return;
      setPendingAction({ kind: "rewind", tabSessionId, sdkSessionId, dropLastN });
    },
    []
  );

  const requestFork = useCallback(
    (tabSessionId: string, sdkSessionId: string | null | undefined, dropLastN: number) => {
      if (!sdkSessionId) return;
      setPendingAction({ kind: "fork", tabSessionId, sdkSessionId, dropLastN });
    },
    []
  );

  const confirmAction = useCallback(async () => {
    if (!pendingAction) return;
    const action = pendingAction;
    setPendingAction(null);
    try {
      if (action.kind === "rewind") {
        // Close the pool session first — the backend rejects truncate while
        // the session is open, so the bundled CLI's in-memory state can't
        // diverge from the truncated file. Closing the tab too would just
        // confuse the user; instead we close it and immediately reopen as a
        // fresh tab so the rewound conversation appears in place.
        await closePoolSession(action.tabSessionId).catch(() => {});
        closeTab(action.tabSessionId);
        await apiTruncateSession(action.sdkSessionId, action.dropLastN);
        // Reopen as a brand-new tab — fresh local_id, resume the JSONL.
        openTab(generateUUID(), "Rewound conversation", false, action.sdkSessionId);
      } else {
        const { session_id: newSdkId } = await apiForkSession(
          action.sdkSessionId,
          action.dropLastN,
        );
        // Open the fork in a new tab so the user sees the result immediately.
        openTab(generateUUID(), "Forked conversation", false, newSdkId);
      }
      onSessionChange();
    } catch (err) {
      console.error(`${action.kind} failed:`, err);
      alert(`${action.kind === "rewind" ? "Rewind" : "Fork"} failed: ${(err as Error).message}`);
    }
  }, [pendingAction, closeTab, openTab, onSessionChange]);

  // Track which models support audio
  const [modelsInfo, setModelsInfo] = useState<ModelsResponse | null>(null);

  // Fetch models info on mount
  useEffect(() => {
    listModels().then(setModelsInfo).catch(console.error);
  }, []);

  const handleAgentSessionOpened = useCallback(
    (agentSessionId: string, sdkSessionId?: string) => {
      openTab(agentSessionId, `Agent ${agentSessionId.slice(0, 8)}`, false, sdkSessionId);
    },
    [openTab]
  );

  const handleAgentSessionClosed = useCallback(
    (agentSessionId: string) => {
      closeTab(agentSessionId);
    },
    [closeTab]
  );

  const handleSessionClosed = useCallback(
    (sessionId: string) => {
      closeTab(sessionId);
    },
    [closeTab]
  );

  const activeInstance = activeTabId ? instancesRef.current.get(activeTabId) : undefined;

  // Check if any model supports audio (show button if audio is available)
  const supportsAudio = (modelsInfo?.audio_capable_models?.length ?? 0) > 0;

  // Session config panel state — track which tab has it open
  const [sessionConfigTabId, setSessionConfigTabId] = useState<string | null>(null);

  return (
    <>
      {/* Render a headless TabInstance for each open tab */}
      {tabs.map((tab) => (
        <TabInstance
          key={tab.sessionId}
          sessionId={tab.sessionId}
          resumeSdkId={tab.resumeSdkId || null}
          onSessionChange={onSessionChange}
          instancesRef={instancesRef}
          notifyUpdate={notifyUpdate}
          wsEndpoint={tab.isOrchestrator ? "/api/orchestrator/chat" : undefined}
          isOrchestrator={tab.isOrchestrator}
          onAgentSessionOpened={tab.isOrchestrator ? handleAgentSessionOpened : undefined}
          onAgentSessionClosed={tab.isOrchestrator ? handleAgentSessionClosed : undefined}
          onSessionClosed={!tab.isOrchestrator ? () => handleSessionClosed(tab.sessionId) : undefined}
        />
      ))}

      {/* Render ChatPanels for ALL tabs — inactive ones hidden with display:none
           so hooks (including voice WebRTC) stay alive across tab switches. */}
      {tabs.map((tab) => {
        const inst = instancesRef.current.get(tab.sessionId);
        if (!inst) return null;
        const isActive = tab.sessionId === activeTabId;
        return (
          <div
            key={tab.sessionId}
            style={isActive
              ? { flex: 1, display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }
              : { display: "none" }}
          >
            {tab.isOrchestrator ? (
              <OrchestratorChatPanel
                sessionId={tab.sessionId}
                resumeSdkId={tab.resumeSdkId}
                instance={inst}
                onSessionChange={onSessionChange}
                isActive={isActive}
                supportsAudio={supportsAudio}
                onRewindMessage={(idx) => requestRewind(tab.sessionId, tab.resumeSdkId, idx)}
                onForkMessage={(idx) => requestFork(tab.sessionId, tab.resumeSdkId, idx)}
              />
            ) : (
              <ChatPanel
                messages={inst.messages}
                status={inst.status}
                connectionState={inst.connectionState}
                cost={inst.cost}
                turns={inst.turns}
                error={inst.error}
                stall={inst.stall}
                pendingPermission={inst.pendingPermission}
                onRespondToPermission={inst.respondToPermission}
                onSend={inst.send}
                onInterrupt={inst.interrupt}
                onCompact={inst.compact}
                contextUsage={inst.contextUsage}
                isActive={isActive}
                hasMoreMessages={inst.hasMoreMessages}
                onLoadMore={inst.loadMoreMessages}
                onRewindMessage={(n) => requestRewind(tab.sessionId, tab.resumeSdkId, n)}
                onForkMessage={(n) => requestFork(tab.sessionId, tab.resumeSdkId, n)}
                onOpenSessionConfig={() => setSessionConfigTabId(tab.sessionId)}
              />
            )}
          </div>
        );
      })}

      {/* Empty state when no active instance */}
      {!activeInstance && (
        <main className="chat-panel">
          <div className="message-list empty">
            <div className="empty-state">
              <div className="empty-title">No session open</div>
              <div className="empty-hint">
                Start a new session or select one from the sidebar.
              </div>
            </div>
          </div>
        </main>
      )}

      {/* Rewind / fork confirmation */}
      {pendingAction && (
        <ConfirmModal
          title={
            pendingAction.kind === "rewind"
              ? "Rewind conversation?"
              : "Fork conversation?"
          }
          body={
            pendingAction.kind === "rewind" ? (
              <>
                All messages after the selected one will be removed from this
                conversation. The current tab will be closed; reopen the
                conversation from the sidebar to continue from the rewound point.
              </>
            ) : (
              <>
                A copy of this conversation will be created, truncated to the
                selected message. The original is unchanged.
              </>
            )
          }
          confirmLabel={pendingAction.kind === "rewind" ? "Rewind" : "Fork"}
          destructive={pendingAction.kind === "rewind"}
          onConfirm={confirmAction}
          onCancel={() => setPendingAction(null)}
        />
      )}

      {/* Per-session config panel */}
      <Suspense fallback={null}>
        {sessionConfigTabId && (() => {
          const cfgInst = instancesRef.current.get(sessionConfigTabId);
          const cfgTab = tabs.find(t => t.sessionId === sessionConfigTabId);
          const isStopped = cfgInst
            ? cfgInst.status === "idle" || cfgInst.status === "disconnected"
            : false;
          return (
            <SessionConfigPage
              isOpen={true}
              onClose={() => setSessionConfigTabId(null)}
              sessionId={cfgTab?.resumeSdkId ?? null}
              canRestart={isStopped}
              onSaveAndRestart={() => {
                cfgInst?.restart();
              }}
            />
          );
        })()}
      </Suspense>
    </>
  );
}
