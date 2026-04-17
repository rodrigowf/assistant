import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listSkills,
  listAgents,
  listModels,
  type AssistantConfig,
  type WorkingDirectoryEntry,
  type SkillInfo,
  type AgentInfo,
  type McpServerConfig,
  type ModelInfo,
} from "../api/rest";

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export function ConfigPage({ isOpen, onClose }: Props) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  // WD add-new form state
  const [addWdType, setAddWdType] = useState<"local" | "ssh">("local");
  const [addWdPath, setAddWdPath] = useState("");
  const [addWdLabel, setAddWdLabel] = useState("");
  const [addWdHost, setAddWdHost] = useState("");
  const [addWdUser, setAddWdUser] = useState("");
  const [addWdKey, setAddWdKey] = useState("");
  const [addWdConfigDir, setAddWdConfigDir] = useState("");
  const [addWdError, setAddWdError] = useState<string | null>(null);
  const [addWdOpen, setAddWdOpen] = useState(false);
  const [editWdId, setEditWdId] = useState<string | null>(null); // id of entry being edited

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  // Clean up timer on unmount
  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, mcpRes, skillsRes, agentsRes, modelsRes] = await Promise.all([
        getConfig(),
        listMcpServers(),
        listSkills(),
        listAgents(),
        listModels(),
      ]);
      setConfig(cfg);
      setMcpServers(mcpRes.servers);
      setSkills(skillsRes.skills);
      setAgents(agentsRes.agents);
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

  // ── Working directory list ────────────────────────────────────────

  const selectWd = useCallback(async (id: string) => {
    try { await save({ working_directory: id }); }
    catch (e) { setError(String(e)); }
  }, [save]);

  const resetAddWdForm = () => {
    setAddWdPath(""); setAddWdLabel(""); setAddWdHost("");
    setAddWdUser(""); setAddWdKey(""); setAddWdConfigDir(""); setAddWdError(null);
    setAddWdOpen(false); setEditWdId(null);
  };

  const openEditWd = useCallback((entry: WorkingDirectoryEntry) => {
    setEditWdId(entry.id);
    setAddWdType(entry.ssh_host ? "ssh" : "local");
    setAddWdPath(entry.path);
    setAddWdLabel(entry.label ?? "");
    setAddWdHost(entry.ssh_host ?? "");
    setAddWdUser(entry.ssh_user ?? "");
    setAddWdKey(entry.ssh_key ?? "");
    // Only pre-fill if it differs from the auto-derived value
    const derived = entry.ssh_host ? entry.path.replace(/\/$/, "") + "/.claude_config" : "";
    setAddWdConfigDir(entry.claude_config_dir && entry.claude_config_dir !== derived ? entry.claude_config_dir : "");
    setAddWdError(null);
    setAddWdOpen(true);
  }, []);

  const addWd = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    if (!config) return;
    const path = addWdPath.trim();
    if (!path) return;

    setAddWdError(null);

    const isSSH = addWdType === "ssh";
    const host = addWdHost.trim();
    if (isSSH && !host) { setAddWdError("SSH host is required"); return; }

    const id = isSSH ? `${host}:${path}` : path;

    const entry: WorkingDirectoryEntry = {
      id,
      path,
      label: addWdLabel.trim() || null,
      ssh_host: isSSH ? host : null,
      ssh_user: addWdUser.trim() || null,
      ssh_key: addWdKey.trim() || null,
      claude_config_dir: addWdConfigDir.trim() || null,
    };

    try {
      let newHistory: WorkingDirectoryEntry[];
      if (editWdId !== null) {
        // Replace existing entry (id may change if host/path changed)
        newHistory = config.working_directory_history.map(e => e.id === editWdId ? entry : e);
      } else {
        // If already in history, just select it
        if (config.working_directory_history.some(e => e.id === id)) {
          resetAddWdForm();
          await selectWd(id);
          return;
        }
        newHistory = [...config.working_directory_history, entry];
      }
      await save({ working_directory_history: newHistory, working_directory: id });
      resetAddWdForm();
    } catch (e) {
      setAddWdError(String(e));
    }
  }, [addWdType, addWdPath, addWdLabel, addWdHost, addWdUser, addWdKey, addWdConfigDir, editWdId, config, save, selectWd]);

  const deleteWd = useCallback(async (id: string) => {
    if (!config) return;
    const newHistory = config.working_directory_history.filter(e => e.id !== id);
    try { await save({ working_directory_history: newHistory }); }
    catch (e) { setError(String(e)); }
  }, [config, save]);

  // ── Toggle helpers for list fields ────────────────────────────────

  const makeToggle = useCallback(
    (field: "enabled_mcps" | "disabled_skills" | "disabled_agents") =>
      async (name: string) => {
        if (!config) return;
        const next = new Set(config[field]);
        if (next.has(name)) next.delete(name); else next.add(name);
        try { await save({ [field]: Array.from(next) }); }
        catch (e) { setError(String(e)); }
      },
    [config, save],
  );

  const toggleMcp   = useCallback((name: string) => makeToggle("enabled_mcps")(name),   [makeToggle]);
  const toggleSkill = useCallback((name: string) => makeToggle("disabled_skills")(name), [makeToggle]);
  const toggleAgent = useCallback((name: string) => makeToggle("disabled_agents")(name), [makeToggle]);

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

                {/* Saved directory list */}
                {config.working_directory_history.length > 0 && (
                  <div className="wd-list">
                    {config.working_directory_history.map((entry) => {
                      const isActive = entry.id === config.working_directory;
                      const canDelete = config.working_directory_history.length > 1;
                      const isSSH = !!entry.ssh_host;
                      const displayName = entry.label || (isSSH ? `${entry.ssh_host}:${entry.path}` : entry.path);
                      const subtitle = isSSH
                        ? `${entry.ssh_user ? entry.ssh_user + "@" : ""}${entry.ssh_host} · ${entry.path}`
                        : null;
                      return (
                        <div key={entry.id} className={`wd-list-item${isActive ? " active" : ""}${isSSH ? " ssh" : ""}`}>
                          <button
                            className="wd-list-radio"
                            onClick={() => !isActive && selectWd(entry.id)}
                            title={isActive ? "Currently active" : "Set as active"}
                            disabled={saving}
                          >
                            <span className={`wd-radio-dot${isActive ? " checked" : ""}`} />
                          </button>
                          <div className="wd-list-info">
                            <div className="wd-list-path-row">
                              {isSSH && (
                                <span className="wd-ssh-badge" title="Remote SSH session">SSH</span>
                              )}
                              <span className="wd-list-path" title={entry.id}>{displayName}</span>
                            </div>
                            {subtitle && (
                              <span className="wd-list-subtitle">{subtitle}</span>
                            )}
                          </div>
                          {isActive && <span className="wd-active-badge">active</span>}
                          <button
                            className="wd-list-edit"
                            onClick={() => openEditWd(entry)}
                            disabled={saving}
                            title="Edit"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                            </svg>
                          </button>
                          <button
                            className="wd-list-delete"
                            onClick={() => deleteWd(entry.id)}
                            disabled={saving || !canDelete}
                            title={canDelete ? "Remove from list" : "Cannot remove the only directory"}
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                            </svg>
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Add new directory */}
                {!addWdOpen ? (
                  <button className="wd-add-toggle" onClick={() => setAddWdOpen(true)}>
                    + Add directory
                  </button>
                ) : (
                  <form className="wd-add-form" onSubmit={addWd}>
                    {editWdId !== null && <div className="wd-edit-title">Edit directory</div>}
                    {/* Local / SSH tabs */}
                    <div className="wd-type-tabs">
                      <button
                        type="button"
                        className={`wd-type-tab${addWdType === "local" ? " active" : ""}`}
                        onClick={() => setAddWdType("local")}
                      >Local</button>
                      <button
                        type="button"
                        className={`wd-type-tab${addWdType === "ssh" ? " active" : ""}`}
                        onClick={() => setAddWdType("ssh")}
                      >SSH</button>
                    </div>

                    {addWdType === "ssh" && (
                      <div className="wd-field-row">
                        <div className="wd-field">
                          <label className="wd-field-label">Host *</label>
                          <input
                            className="wd-input"
                            type="text"
                            value={addWdHost}
                            onChange={(e) => { setAddWdHost(e.target.value); setAddWdError(null); }}
                            placeholder="192.168.0.200"
                            spellCheck={false}
                          />
                        </div>
                        <div className="wd-field wd-field-sm">
                          <label className="wd-field-label">User</label>
                          <input
                            className="wd-input"
                            type="text"
                            value={addWdUser}
                            onChange={(e) => setAddWdUser(e.target.value)}
                            placeholder="rodrigo"
                            spellCheck={false}
                          />
                        </div>
                      </div>
                    )}

                    <div className="wd-field">
                      <label className="wd-field-label">
                        {addWdType === "ssh" ? "Remote path *" : "Path *"}
                      </label>
                      <input
                        className="wd-input"
                        type="text"
                        value={addWdPath}
                        onChange={(e) => { setAddWdPath(e.target.value); setAddWdError(null); }}
                        placeholder={addWdType === "ssh" ? "/home/rodrigo/projects/myapp" : "/home/user/projects/myapp"}
                        spellCheck={false}
                      />
                    </div>

                    {addWdType === "ssh" && (
                      <>
                        <div className="wd-field">
                          <label className="wd-field-label">SSH key (local path, optional)</label>
                          <input
                            className="wd-input"
                            type="text"
                            value={addWdKey}
                            onChange={(e) => setAddWdKey(e.target.value)}
                            placeholder="~/.ssh/id_rsa"
                            spellCheck={false}
                          />
                        </div>
                        <div className="wd-field">
                          <label className="wd-field-label">CLAUDE_CONFIG_DIR on remote (optional, defaults to &lt;path&gt;/.claude_config)</label>
                          <input
                            className="wd-input"
                            type="text"
                            value={addWdConfigDir}
                            onChange={(e) => setAddWdConfigDir(e.target.value)}
                            placeholder={addWdPath.trim() ? addWdPath.trim().replace(/\/$/, "") + "/.claude_config" : "/home/user/projects/myapp/.claude_config"}
                            spellCheck={false}
                          />
                        </div>
                      </>
                    )}

                    <div className="wd-field">
                      <label className="wd-field-label">Label (optional)</label>
                      <input
                        className="wd-input"
                        type="text"
                        value={addWdLabel}
                        onChange={(e) => setAddWdLabel(e.target.value)}
                        placeholder="Jetson Nano — assistant"
                        spellCheck={false}
                      />
                    </div>

                    {addWdError && <div className="config-field-error">{addWdError}</div>}

                    <div className="wd-add-actions">
                      <button
                        className="wd-add-btn"
                        type="submit"
                        disabled={saving || !addWdPath.trim() || (addWdType === "ssh" && !addWdHost.trim())}
                      >
                        {editWdId !== null ? "Save" : "Add"}
                      </button>
                      <button
                        className="wd-cancel-btn"
                        type="button"
                        onClick={resetAddWdForm}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                )}
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

              {/* ── Skills ───────────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Skills</h3>
                <p className="config-section-desc">
                  Slash commands visible to agents. Disabled skills are hidden from the system prompt.
                </p>
                {skills.length === 0 ? (
                  <div className="config-empty">No skills found</div>
                ) : (
                  <div className="config-item-list">
                    {skills.map((skill) => {
                      const enabled = !config.disabled_skills.includes(skill.name);
                      return (
                        <label key={skill.name} className={`config-item${enabled ? " enabled" : ""}`}>
                          <input type="checkbox" checked={enabled} onChange={() => toggleSkill(skill.name)} />
                          <div className="config-item-info">
                            <span className="config-item-name">/{skill.name}</span>
                            {skill.description && <span className="config-item-detail">{skill.description}</span>}
                          </div>
                        </label>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* ── Agents ───────────────────────────────────── */}
              <section className="config-section">
                <h3 className="config-section-title">Agents</h3>
                <p className="config-section-desc">
                  Specialized subagents available to the orchestrator. Disabled agents are hidden from the system prompt.
                </p>
                {agents.length === 0 ? (
                  <div className="config-empty">No agents found</div>
                ) : (
                  <div className="config-item-list">
                    {agents.map((agent) => {
                      const enabled = !config.disabled_agents.includes(agent.name);
                      return (
                        <label key={agent.name} className={`config-item${enabled ? " enabled" : ""}`}>
                          <input type="checkbox" checked={enabled} onChange={() => toggleAgent(agent.name)} />
                          <div className="config-item-info">
                            <span className="config-item-name">{agent.name}</span>
                            {agent.description && <span className="config-item-detail">{agent.description}</span>}
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
