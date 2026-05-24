import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listModels,
  listVoiceModels,
  listGoogleVoiceModels,
  listQwenHarnessModels,
  listSessionProviders,
  type AssistantConfig,
  type McpServerConfig,
  type ModelInfo,
  type VoiceModelEntry,
  type QwenModelInfo,
  type SessionProviderSpec,
} from "../api/rest";
import { WorkingDirectorySection, SessionFlagsSection, McpServersSection } from "./AgentSettings";

const VOICE_PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  qwen: "Qwen (Alibaba)",
  google: "Google Gemini",
};

// When Google deprecates a Gemini Live model id (which they do
// periodically — e.g. "gemini-live-2.5-flash-native-audio" was renamed
// to "gemini-2.5-flash-native-audio-latest" on AI Studio in 2026-05),
// a stale ``default_voice_model`` in assistant_config.json breaks
// every voice session with a WS 1008 policy violation before the user
// has any chance to notice. Auto-correct by snapping the saved value
// to the discovered default and surfacing a banner so the change is
// visible. Only fires when the discovered list is non-empty (no list
// = upstream is unhealthy, don't second-guess the user).
async function maybeAutoCorrectVoiceModel(
  cfg: AssistantConfig,
  discovered: VoiceModelEntry[],
  setBanner: (b: { from: string; to: string } | null) => void,
): Promise<AssistantConfig> {
  if (cfg.default_voice_provider !== "google") return cfg;
  if (discovered.length === 0) return cfg;
  if (discovered.some(m => m.id === cfg.default_voice_model)) return cfg;
  const newDefault = discovered.find(m => m.default) ?? discovered[0];
  const from = cfg.default_voice_model;
  try {
    const updated = await updateConfig({
      default_voice_model: newDefault.id,
      // Voice name often pairs with a specific model — snap to the
      // new model's default voice unless the current voice is still
      // listed under it.
      default_voice_name: newDefault.voices.some(v => v.id === cfg.default_voice_name)
        ? cfg.default_voice_name
        : newDefault.voice,
    });
    setBanner({ from, to: newDefault.id });
    return updated;
  } catch (e) {
    console.warn("auto-correct voice model failed:", e);
    return cfg;
  }
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function ConfigPage({ isOpen, onClose }: Props) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [qwenHarnessModels, setQwenHarnessModels] = useState<QwenModelInfo[]>([]);
  const [voiceProviders, setVoiceProviders] = useState<Record<string, VoiceModelEntry[]>>({});
  // Session-harness specs loaded from /api/config/providers — the picker
  // and the "Provider description" text both read from this so adding a
  // new harness server-side surfaces here automatically.
  const [sessionProviders, setSessionProviders] = useState<SessionProviderSpec[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);
  // Surfaces when the previously-saved Google voice model is no longer
  // in the discovered catalog (Google deprecates Live model ids
  // periodically). The Config page silently writes through to the new
  // default; the banner tells the user that happened.
  const [voiceModelAutoCorrected, setVoiceModelAutoCorrected] = useState<
    { from: string; to: string } | null
  >(null);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // First fetch config so we know which Google backend to query.
      const cfg = await getConfig();
      const [mcpRes, modelsRes, voiceRes, googleVoiceRes, qwenHarnessRes, providersRes] = await Promise.all([
        listMcpServers(),
        listModels(),
        listVoiceModels(),
        // Dynamic Gemini Live list — backend queries the catalog for
        // the requested endpoint (vertex / aistudio) and caches for
        // 60s.  Tolerate failure: if ADC isn't set up or the upstream
        // is broken, the route returns {models: []} and we fall back
        // to the static VOICE_MODELS["google"] from listVoiceModels()
        // below.
        listGoogleVoiceModels(cfg.default_voice_endpoint).catch(e => {
          console.warn("listGoogleVoiceModels failed:", e);
          return { models: [] };
        }),
        // Tolerate failure here: if the user hasn't installed the Qwen CLI,
        // the route still works (returns []) but a fetch error would still
        // blank the whole config page.  Swallow + log so the rest renders.
        listQwenHarnessModels().catch(e => {
          console.warn("listQwenHarnessModels failed:", e);
          return { models: [] };
        }),
        listSessionProviders(),
      ]);
      setMcpServers(mcpRes.servers);
      setModels(modelsRes.models);
      // Merge the dynamic Gemini Live list into voiceProviders, replacing
      // the static "google" entries when the dynamic list is non-empty.
      // Empty list → keep the static fallback so the UI still works
      // even when the API key isn't configured.
      const mergedVoiceProviders = { ...voiceRes.providers };
      if (googleVoiceRes.models.length > 0) {
        mergedVoiceProviders.google = googleVoiceRes.models;
      }
      setVoiceProviders(mergedVoiceProviders);
      setQwenHarnessModels(qwenHarnessRes.models);
      setSessionProviders(providersRes.providers);
      // Auto-correct: if the saved Gemini model is no longer in the
      // discovered catalog (Google renames Live ids occasionally),
      // write through to the new default and tell the user.
      const correctedCfg = await maybeAutoCorrectVoiceModel(
        cfg, googleVoiceRes.models, setVoiceModelAutoCorrected,
      );
      setConfig(correctedCfg);
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

  // Refetch the Google voice catalog when the user flips the endpoint
  // selector mid-session — the two backends (AI Studio / Vertex) have
  // different model lists.  Skipped on first render: ``load`` already
  // fetched the right catalog from the initial config.
  const endpointRef = useRef<string | null>(null);
  useEffect(() => {
    const ep = config?.default_voice_endpoint;
    if (!ep) return;
    if (endpointRef.current === null) {
      endpointRef.current = ep;
      return;
    }
    if (endpointRef.current === ep) return;
    endpointRef.current = ep;
    listGoogleVoiceModels(ep)
      .then(async res => {
        if (res.models.length === 0) return;
        setVoiceProviders(prev => ({ ...prev, google: res.models }));
        if (!config) return;
        const corrected = await maybeAutoCorrectVoiceModel(
          config, res.models, setVoiceModelAutoCorrected,
        );
        if (corrected !== config) setConfig(corrected);
      })
      .catch(e => console.warn("listGoogleVoiceModels refetch failed:", e));
  }, [config, config?.default_voice_endpoint]);

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
  // Endpoint sub-selector — applies only when provider === "google".
  const selectedVoiceEndpoint = config?.default_voice_endpoint ?? "vertex";
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
          {voiceModelAutoCorrected && (
            <div className="config-warning" role="status" style={{
              background: "#fef3c7", color: "#78350f", padding: "0.75em 1em",
              borderRadius: 6, marginBottom: "1em", fontSize: "0.9em",
            }}>
              The previously-saved Gemini Live model <code>{voiceModelAutoCorrected.from}</code> is no longer
              available from Google. Switched to <code>{voiceModelAutoCorrected.to}</code>.
              <button
                type="button"
                onClick={() => setVoiceModelAutoCorrected(null)}
                style={{ marginLeft: "1em", background: "transparent", border: "none", color: "inherit", cursor: "pointer", fontWeight: 600 }}
              >
                Dismiss
              </button>
            </div>
          )}

          {!loading && config && (
            <>
              {/* ── Orchestrator ──────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Orchestrator</h3>
                <p className="config-section-desc">
                  Defaults the orchestrator uses for new sessions. Text mode also drives
                  history summarization.
                </p>

                {/* Text mode */}
                <div className="config-subsection">
                  <h4 className="config-subsection-title">Text mode</h4>
                  <p className="config-subsection-desc">
                    Used for typed conversations. Can be changed mid-conversation.
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
                </div>

                {/* Voice mode */}
                <div className="config-subsection">
                  <h4 className="config-subsection-title">Voice mode</h4>
                  <p className="config-subsection-desc">
                    Used for realtime voice sessions. Cannot be changed mid-session.
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
                      {selectedVoiceProvider === "google" && (
                        <div className="model-dropdown-field">
                          <label className="model-dropdown-label">Endpoint</label>
                          <select
                            className="model-dropdown-select"
                            value={selectedVoiceEndpoint}
                            disabled={saving}
                            onChange={(e) =>
                              save({ default_voice_endpoint: e.target.value })
                                .catch(err => setError(String(err)))
                            }
                            title="Which Google backend serves Gemini Live. Vertex AI is the stable default; AI Studio (generativelanguage.googleapis.com) is the legacy path and may return 1008 denials for preview models."
                          >
                            <option value="vertex">Vertex AI (recommended)</option>
                            <option value="aistudio">AI Studio (legacy)</option>
                          </select>
                        </div>
                      )}
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
                </div>

                {/* Voice recording */}
                <div className="config-subsection">
                  <h4 className="config-subsection-title">Voice recording</h4>
                  <p className="config-subsection-desc">
                    Save raw audio from voice sessions for later playback and analysis.
                    Recordings are stored in context/recordings/ and can be used by the
                    orchestrator to analyze emotional states, tone, and pacing.
                  </p>
                  <div className="config-item-list">
                    <label className={`config-item${config.voice_recording_enabled ? " enabled" : ""}`}>
                      <input
                        type="checkbox"
                        checked={config.voice_recording_enabled}
                        onChange={() => save({ voice_recording_enabled: !config.voice_recording_enabled }).catch(e => setError(String(e)))}
                        disabled={saving}
                      />
                      <div className="config-item-info">
                        <span className="config-item-name">Enable voice recording</span>
                        <span className="config-item-detail">
                          Record both user and assistant audio streams during voice sessions
                        </span>
                      </div>
                    </label>
                  </div>
                </div>
              </section>

              {/* ── Session Provider ──────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Session provider</h3>
                <p className="config-section-desc">
                  Which agent backs new chat sessions. Existing sessions keep their original
                  provider; this only affects newly-created tabs.
                </p>
                <div className="model-dropdowns">
                  <div className="model-dropdown-field">
                    <label className="model-dropdown-label">Provider</label>
                    <select
                      className="model-dropdown-select"
                      value={config.provider ?? "claude"}
                      disabled={saving}
                      onChange={(e) =>
                        save({ provider: e.target.value })
                          .catch(err => setError(String(err)))
                      }
                    >
                      {sessionProviders.map(p => (
                        <option key={p.id} value={p.id}>{p.label}</option>
                      ))}
                    </select>
                  </div>

                  {/* Qwen harness model — sourced from ~/.qwen/settings.json so anything
                      the user has wired up there (Qwen, DeepSeek, GLM, a local provider,
                      …) shows up automatically.  An empty value means "let the CLI pick
                      its own default" — we surface that as the first option so users on a
                      fresh install don't have to touch this. */}
                  {(config.provider ?? "claude") === "qwen" && (
                    <div className="model-dropdown-field">
                      <label className="model-dropdown-label">Model</label>
                      <select
                        className="model-dropdown-select"
                        value={config.harness_model?.qwen ?? ""}
                        disabled={saving || qwenHarnessModels.length === 0}
                        onChange={(e) =>
                          save({ harness_model: { qwen: e.target.value } })
                            .catch(err => setError(String(err)))
                        }
                      >
                        <option value="">CLI default</option>
                        {qwenHarnessModels.map(m => {
                          const badges = [
                            m.context_window ? `${Math.round(m.context_window / 1000)}K ctx` : null,
                            m.supports_thinking ? "thinking" : null,
                            m.supports_vision ? "vision" : null,
                            m.supports_video ? "video" : null,
                          ].filter(Boolean).join(" · ");
                          return (
                            <option key={m.id} value={m.id}>
                              {m.display_name}{badges ? ` — ${badges}` : ""}
                            </option>
                          );
                        })}
                      </select>
                    </div>
                  )}
                </div>
                <p className="config-section-desc" style={{ marginTop: 8 }}>
                  {sessionProviders.find(p => p.id === (config.provider ?? "claude"))?.description ?? ""}
                </p>
                {(config.provider ?? "claude") === "qwen" && qwenHarnessModels.length === 0 && (
                  <p className="config-section-desc" style={{ marginTop: 4, opacity: 0.7 }}>
                    No models found in <code>~/.qwen/settings.json</code> — run <code>qwen</code> once to
                    initialize the catalog, or add custom providers there.
                  </p>
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
