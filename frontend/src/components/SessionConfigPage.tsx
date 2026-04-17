import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  getSessionConfig,
  updateSessionConfig,
  listMcpServers,
  type AssistantConfig,
  type SessionConfig,
  type McpServerConfig,
} from "../api/rest";
import { WorkingDirectorySection, SessionFlagsSection, McpServersSection } from "./AgentSettings";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  /** SDK session ID (JSONL file name) — used to load/save per-session config. */
  sessionId: string | null;
  /** Whether the session is currently stopped (idle/disconnected, not streaming). */
  canRestart: boolean;
  /** Called when "Save and Restart" is clicked. */
  onSaveAndRestart: () => void;
}

export function SessionConfigPage({ isOpen, onClose, sessionId, canRestart, onSaveAndRestart }: Props) {
  const [globalConfig, setGlobalConfig] = useState<AssistantConfig | null>(null);
  const [sessionConfig, setSessionConfig] = useState<SessionConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    if (!sessionId) { setLoading(false); return; }
    setLoading(true);
    setError(null);
    try {
      const [globalCfg, sessionCfg, mcpRes] = await Promise.all([
        getConfig(),
        getSessionConfig(sessionId),
        listMcpServers(),
      ]);
      setGlobalConfig(globalCfg);
      setSessionConfig(sessionCfg);
      setMcpServers(mcpRes.servers);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load configuration");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (isOpen && !wasOpen.current) load();
    wasOpen.current = isOpen;
  }, [isOpen, load]);

  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, onClose]);

  const showSaved = () => {
    setSavedMsg(true);
    if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current);
    savedMsgTimer.current = setTimeout(() => setSavedMsg(false), 2000);
  };

  const save = useCallback(async (patch: Partial<SessionConfig>) => {
    if (!sessionId) return;
    setSaving(true);
    try {
      const updated = await updateSessionConfig(sessionId, patch);
      setSessionConfig(updated);
      showSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }, [sessionId]);

  // Resolve effective values: session override or fall back to global
  const effectiveWdId = sessionConfig?.working_directory ?? globalConfig?.working_directory ?? null;
  const effectiveMcps = sessionConfig?.enabled_mcps ?? globalConfig?.enabled_mcps ?? [];
  const effectiveChrome = sessionConfig?.chrome_extension ?? globalConfig?.chrome_extension ?? false;

  const isInherited = (field: keyof SessionConfig) =>
    sessionConfig?.[field] === null || sessionConfig?.[field] === undefined;

  const resetToGlobal = useCallback(async (field: keyof SessionConfig) => {
    await save({ [field]: null });
  }, [save]);

  const toggleMcp = useCallback(async (name: string) => {
    const current = new Set(effectiveMcps);
    if (current.has(name)) current.delete(name); else current.add(name);
    await save({ enabled_mcps: Array.from(current) });
  }, [effectiveMcps, save]);

  const wdHistory = globalConfig?.working_directory_history ?? [];

  const handleSaveAndRestart = useCallback(() => {
    onSaveAndRestart();
    onClose();
  }, [onSaveAndRestart, onClose]);

  if (!isOpen) return null;

  return (
    <div className="config-overlay" onClick={onClose}>
      <div className="config-panel" onClick={(e) => e.stopPropagation()}>

        {/* Header */}
        <div className="config-panel-header">
          <div className="config-panel-title-row">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="config-panel-icon">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            <h2 className="config-panel-title">Session Configuration</h2>
            <div className="config-panel-status">
              {saving && <span className="config-saving">Saving…</span>}
              {savedMsg && !saving && <span className="config-saved">✓ Saved</span>}
            </div>
          </div>
          <button className="config-panel-close" onClick={onClose} title="Close (Esc)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="config-panel-body">
          {loading && <div className="config-loading">Loading…</div>}
          {error && <div className="config-error">{error}</div>}

          {!sessionId && !loading && (
            <div className="config-section">
              <p className="config-section-desc">
                Configuration will be available after the first message is sent (session must exist on disk first).
              </p>
            </div>
          )}

          {!loading && sessionConfig && globalConfig && (
            <>
              <WorkingDirectorySection
                history={wdHistory}
                activeId={effectiveWdId ?? ""}
                saving={saving}
                selectedLabel={isInherited("working_directory") ? "global default" : "selected"}
                onSelect={(id) => save({ working_directory: id })}
                onHistoryChange={async (newHistory, newActiveId) => {
                  try {
                    const updated = await updateConfig({ working_directory_history: newHistory, ...(newActiveId ? { working_directory: newActiveId } : {}) });
                    setGlobalConfig(updated);
                    if (newActiveId) await save({ working_directory: newActiveId });
                  } catch (e) { setError(String(e)); }
                }}
                inherited={isInherited("working_directory")}
                onReset={() => resetToGlobal("working_directory")}
              />

              <SessionFlagsSection
                chromeEnabled={effectiveChrome}
                onChange={(v) => save({ chrome_extension: v })}
                saving={saving}
                inherited={isInherited("chrome_extension")}
                onReset={() => resetToGlobal("chrome_extension")}
              />

              <McpServersSection
                mcpServers={mcpServers}
                enabledMcps={effectiveMcps}
                onToggle={toggleMcp}
                saving={saving}
                inherited={isInherited("enabled_mcps")}
                onReset={() => resetToGlobal("enabled_mcps")}
              />
            </>
          )}
        </div>

        {/* Footer */}
        <div className="session-config-footer">
          <p className="session-config-footer-hint">
            {!sessionId
              ? "Send a message first to enable session-specific configuration."
              : canRestart
                ? "Session is stopped. Save and restart to apply changes."
                : "Stop the session to apply configuration changes on next restart."}
          </p>
          <button
            className="session-config-restart-btn"
            onClick={handleSaveAndRestart}
            disabled={!canRestart || saving || !sessionId}
            title={canRestart ? "Restart session with current configuration" : "Session must be stopped first"}
          >
            Save and Restart
          </button>
        </div>

      </div>
    </div>
  );
}
