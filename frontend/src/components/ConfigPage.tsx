import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listModels,
  type AssistantConfig,
  type McpServerConfig,
  type ModelInfo,
} from "../api/rest";
import { WorkingDirectoryList } from "./WorkingDirectoryList";

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function ConfigPage({ isOpen, onClose }: Props) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  // Clean up timer on unmount
  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, mcpRes, modelsRes] = await Promise.all([
        getConfig(),
        listMcpServers(),
        listModels(),
      ]);
      setConfig(cfg);
      setMcpServers(mcpRes.servers);
      setModels(modelsRes.models);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load configuration");
    } finally {
      setLoading(false);
    }
  }, []);

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

  const save = useCallback(async (patch: Parameters<typeof updateConfig>[0]) => {
    setSaving(true);
    try {
      const updated = await updateConfig(patch);
      setConfig(updated);
      showSaved();
      return updated;
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to save";
      // Try to surface FastAPI detail string
      try { return Promise.reject(JSON.parse(msg.replace(/^\d+ /, "")).detail ?? msg); }
      catch { return Promise.reject(msg); }
    } finally {
      setSaving(false);
    }
  }, []);

  const toggleMcp = useCallback(async (name: string) => {
    if (!config) return;
    const next = new Set(config.enabled_mcps);
    if (next.has(name)) next.delete(name); else next.add(name);
    try { await save({ enabled_mcps: Array.from(next) }); }
    catch (e) { setError(String(e)); }
  }, [config, save]);

  const mcpNames = Object.keys(mcpServers);

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
            <h2 className="config-panel-title">Configuration</h2>
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

          {!loading && config && (
            <>
              {/* ── Working Directories ───────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Working Directories</h3>
                <p className="config-section-desc">
                  Saved directories for new sessions. Select one to make it active.
                  Local directories run Claude here; SSH directories run it on a remote machine.
                </p>

                <WorkingDirectoryList
                  history={config.working_directory_history}
                  activeId={config.working_directory}
                  saving={saving}
                  onSelect={async (id) => {
                    try { await save({ working_directory: id }); }
                    catch (e) { setError(String(e)); }
                  }}
                  onHistoryChange={async (newHistory, newActiveId) => {
                    try { await save({ working_directory_history: newHistory, ...(newActiveId ? { working_directory: newActiveId } : {}) }); }
                    catch (e) { setError(String(e)); }
                  }}
                />
              </section>

              {/* ── Session Flags ─────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Session Flags</h3>
                <p className="config-section-desc">
                  Extra flags applied when initializing new Claude Code sessions.
                </p>
                <div className="config-item-list">
                  <label className={`config-item${config.chrome_extension ? " enabled" : ""}`}>
                    <input
                      type="checkbox"
                      checked={config.chrome_extension}
                      onChange={() => save({ chrome_extension: !config.chrome_extension })}
                    />
                    <div className="config-item-info">
                      <span className="config-item-name">Chrome Extension</span>
                      <span className="config-item-detail">Launch sessions with --chrome flag to control Google Chrome tabs</span>
                    </div>
                  </label>
                </div>
              </section>

              {/* ── Model Selection ───────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Orchestrator Model</h3>
                <p className="config-section-desc">
                  Default model for new orchestrator sessions. Can be changed mid-conversation.
                </p>
                {models.length === 0 ? (
                  <div className="config-empty">No models available</div>
                ) : (
                  <div className="model-selector">
                    {/* Group by provider */}
                    {["anthropic", "openai"].map((provider) => {
                      const providerModels = models.filter(m => m.provider === provider);
                      if (providerModels.length === 0) return null;
                      return (
                        <div key={provider} className="model-provider-group">
                          <div className="model-provider-label">
                            {provider === "anthropic" ? "Anthropic" : "OpenAI"}
                          </div>
                          <div className="model-list">
                            {providerModels.map((model) => {
                              const isSelected = model.model_id === config.default_model;
                              return (
                                <button
                                  key={model.model_id}
                                  className={`model-option${isSelected ? " selected" : ""}`}
                                  onClick={() => !isSelected && save({ default_model: model.model_id })}
                                  disabled={saving}
                                  title={`${model.display_name}${model.supports_audio ? " (audio)" : ""}${model.supports_vision ? " (vision)" : ""}`}
                                >
                                  <span className={`model-radio${isSelected ? " checked" : ""}`} />
                                  <span className="model-name">{model.display_name}</span>
                                  <span className="model-badges">
                                    {model.supports_audio && (
                                      <span className="model-badge audio" title="Supports audio input">🎤</span>
                                    )}
                                    {model.supports_vision && (
                                      <span className="model-badge vision" title="Supports vision">👁</span>
                                    )}
                                  </span>
                                </button>
                              );
                            })}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* ── MCP Servers ───────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">MCP Servers</h3>
                <p className="config-section-desc">
                  Default MCP servers enabled for new sessions. Override per-session from the chat header.
                </p>
                {mcpNames.length === 0 ? (
                  <div className="config-empty">No MCP servers configured in .claude.json</div>
                ) : (
                  <div className="config-item-list">
                    {mcpNames.map((name) => {
                      const cfg = mcpServers[name];
                      const enabled = config.enabled_mcps.includes(name);
                      return (
                        <label key={name} className={`config-item${enabled ? " enabled" : ""}`}>
                          <input type="checkbox" checked={enabled} onChange={() => toggleMcp(name)} />
                          <div className="config-item-info">
                            <span className="config-item-name">{name}</span>
                            <span className="config-item-detail">{cfg.command} {cfg.args?.join(" ") ?? ""}</span>
                          </div>
                        </label>
                      );
                    })}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
