import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listModels,
  listVoiceModels,
  type AssistantConfig,
  type McpServerConfig,
  type ModelInfo,
  type VoiceModelEntry,
} from "../api/rest";
import { WorkingDirectorySection, SessionFlagsSection, McpServersSection } from "./AgentSettings";

const VOICE_PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  qwen: "Qwen (Alibaba)",
  google: "Google Gemini",
};

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function ConfigPage({ isOpen, onClose }: Props) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [voiceProviders, setVoiceProviders] = useState<Record<string, VoiceModelEntry[]>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, mcpRes, modelsRes, voiceRes] = await Promise.all([
        getConfig(),
        listMcpServers(),
        listModels(),
        listVoiceModels(),
      ]);
      setConfig(cfg);
      setMcpServers(mcpRes.servers);
      setModels(modelsRes.models);
      setVoiceProviders(voiceRes.providers);
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

  // Derived model state for dropdowns
  const providers = [...new Set(models.map(m => m.provider))];
  const selectedModel = models.find(m => m.model_id === config?.default_model);
  const selectedProvider = selectedModel?.provider ?? providers[0] ?? "";
  const providerModels = models.filter(m => m.provider === selectedProvider);

  // Derived voice-provider/model/voice/language state for dropdowns
  const voiceProviderIds = Object.keys(voiceProviders);
  const selectedVoiceProvider = config?.default_voice_provider ?? voiceProviderIds[0] ?? "";
  const voiceModels: VoiceModelEntry[] = voiceProviders[selectedVoiceProvider] ?? [];
  const selectedVoiceModel: VoiceModelEntry | undefined =
    voiceModels.find(m => m.id === config?.default_voice_model) ?? voiceModels[0];
  const voiceVoices = selectedVoiceModel?.voices ?? [];
  const selectedVoiceName = config?.default_voice_name ?? selectedVoiceModel?.voice ?? "";
  const voiceLanguages = selectedVoiceModel?.transcription_languages ?? [];
  const selectedVoiceLanguage =
    config?.default_voice_transcription_language ??
    selectedVoiceModel?.default_transcription_language ?? "";

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
              {/* ── Orchestrator Model ─────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Orchestrator Model</h3>
                <p className="config-section-desc">
                  Default model for new orchestrator sessions. Can be changed mid-conversation.
                </p>
                {models.length === 0 ? (
                  <div className="config-empty">No models available</div>
                ) : (
                  <div className="model-dropdowns">
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Provider</label>
                      <select
                        className="model-dropdown-select"
                        value={selectedProvider}
                        disabled={saving}
                        onChange={(e) => {
                          // When provider changes, auto-select first model of that provider
                          const first = models.find(m => m.provider === e.target.value);
                          if (first) save({ default_model: first.model_id }).catch(err => setError(String(err)));
                        }}
                      >
                        {providers.map(p => (
                          <option key={p} value={p}>
                            {p === "anthropic" ? "Anthropic" : p === "openai" ? "OpenAI" : p}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Model</label>
                      <select
                        className="model-dropdown-select"
                        value={config.default_model}
                        disabled={saving}
                        onChange={(e) => save({ default_model: e.target.value }).catch(err => setError(String(err)))}
                      >
                        {providerModels.map(m => (
                          <option key={m.model_id} value={m.model_id}>
                            {m.display_name}
                            {m.supports_audio ? " 🎤" : ""}
                            {m.supports_vision ? " 👁" : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}
              </section>

              {/* ── Voice Model ───────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Voice Model</h3>
                <p className="config-section-desc">
                  Default provider, model, and voice for new realtime voice sessions.
                  Cannot be changed mid-session.
                </p>
                {voiceProviderIds.length === 0 ? (
                  <div className="config-empty">No voice providers available</div>
                ) : (
                  <div className="model-dropdowns">
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Provider</label>
                      <select
                        className="model-dropdown-select"
                        value={selectedVoiceProvider}
                        disabled={saving}
                        onChange={(e) =>
                          save({ default_voice_provider: e.target.value })
                            .catch(err => setError(String(err)))
                        }
                      >
                        {voiceProviderIds.map(p => (
                          <option key={p} value={p}>
                            {VOICE_PROVIDER_LABELS[p] ?? p}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Model</label>
                      <select
                        className="model-dropdown-select"
                        value={selectedVoiceModel?.id ?? ""}
                        disabled={saving || voiceModels.length === 0}
                        onChange={(e) =>
                          save({ default_voice_model: e.target.value })
                            .catch(err => setError(String(err)))
                        }
                      >
                        {voiceModels.map(m => (
                          <option key={m.id} value={m.id}>{m.label}</option>
                        ))}
                      </select>
                    </div>
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Voice</label>
                      <select
                        className="model-dropdown-select"
                        value={selectedVoiceName}
                        disabled={saving || voiceVoices.length === 0}
                        onChange={(e) =>
                          save({ default_voice_name: e.target.value })
                            .catch(err => setError(String(err)))
                        }
                      >
                        {voiceVoices.map(v => (
                          <option key={v.id} value={v.id} title={v.description}>
                            {v.label}
                            {v.description ? ` — ${v.description}` : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                    {voiceLanguages.length > 0 && (
                      <div className="model-dropdown-field">
                        <label className="model-dropdown-label">
                          Transcription language
                        </label>
                        <select
                          className="model-dropdown-select"
                          value={selectedVoiceLanguage}
                          disabled={saving}
                          onChange={(e) =>
                            save({ default_voice_transcription_language: e.target.value })
                              .catch(err => setError(String(err)))
                          }
                          title="Language hint for transcribing your voice into the conversation history. Auto-detect lets the ASR identify per turn — best for multilingual speakers, but more error-prone on short fragments."
                        >
                          {voiceLanguages.map(l => (
                            <option key={l.id || "auto"} value={l.id} title={l.description}>
                              {l.label}
                              {l.description ? ` — ${l.description}` : ""}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                )}
              </section>

              {/* ── Working Directories ───────────────────────── */}
              <WorkingDirectorySection
                history={config.working_directory_history}
                activeId={config.working_directory}
                saving={saving}
                onSelect={(id) => save({ working_directory: id }).catch(e => setError(String(e)))}
                onHistoryChange={async (newHistory, newActiveId) => {
                  try { await save({ working_directory_history: newHistory, ...(newActiveId ? { working_directory: newActiveId } : {}) }); }
                  catch (e) { setError(String(e)); }
                }}
              />

              {/* ── Session Flags ─────────────────────────────── */}
              <SessionFlagsSection
                chromeEnabled={config.chrome_extension}
                onChange={(v) => save({ chrome_extension: v }).catch(e => setError(String(e)))}
                saving={saving}
              />

              {/* ── MCP Servers ───────────────────────────────── */}
              <McpServersSection
                mcpServers={mcpServers}
                enabledMcps={config.enabled_mcps}
                onToggle={toggleMcp}
                saving={saving}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
