import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listSkills,
  listAgents,
  type AssistantConfig,
  type SkillInfo,
  type AgentInfo,
  type McpServerConfig,
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
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  // WD add-new input
  const [addWdInput, setAddWdInput] = useState("");
  const [addWdError, setAddWdError] = useState<string | null>(null);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  // Clean up timer on unmount
  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, mcpRes, skillsRes, agentsRes] = await Promise.all([
        getConfig(),
        listMcpServers(),
        listSkills(),
        listAgents(),
      ]);
      setConfig(cfg);
      setMcpServers(mcpRes.servers);
      setSkills(skillsRes.skills);
      setAgents(agentsRes.agents);
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

  const selectWd = useCallback(async (dir: string) => {
    try { await save({ working_directory: dir }); }
    catch (e) { setError(String(e)); }
  }, [save]);

  const addWd = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const dir = addWdInput.trim();
    if (!dir || !config) return;
    if (config.working_directory_history.includes(dir)) {
      // Just select it
      setAddWdInput("");
      await selectWd(dir);
      return;
    }
    setAddWdError(null);
    try {
      const newHistory = [...config.working_directory_history, dir];
      // Add to history, then select it
      await save({ working_directory_history: newHistory, working_directory: dir });
      setAddWdInput("");
    } catch (e) {
      setAddWdError(String(e));
    }
  }, [addWdInput, config, save, selectWd]);

  const deleteWd = useCallback(async (dir: string) => {
    if (!config) return;
    const newHistory = config.working_directory_history.filter(d => d !== dir);
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
                  Saved directories for new sessions. Select one to make it active — it will be used for all new Claude Code sessions.
                </p>

                {/* Saved directory list */}
                {config.working_directory_history.length > 0 && (
                  <div className="wd-list">
                    {config.working_directory_history.map((dir) => {
                      const isActive = dir === config.working_directory;
                      const canDelete = config.working_directory_history.length > 1;
                      return (
                        <div key={dir} className={`wd-list-item${isActive ? " active" : ""}`}>
                          <button
                            className="wd-list-radio"
                            onClick={() => !isActive && selectWd(dir)}
                            title={isActive ? "Currently active" : "Set as active"}
                            disabled={saving}
                          >
                            <span className={`wd-radio-dot${isActive ? " checked" : ""}`} />
                          </button>
                          <span className="wd-list-path" title={dir}>{dir}</span>
                          {isActive && <span className="wd-active-badge">active</span>}
                          <button
                            className="wd-list-delete"
                            onClick={() => deleteWd(dir)}
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
                <form className="wd-add-form" onSubmit={addWd}>
                  <input
                    className="wd-input"
                    type="text"
                    value={addWdInput}
                    onChange={(e) => { setAddWdInput(e.target.value); setAddWdError(null); }}
                    placeholder="Add directory path…"
                    spellCheck={false}
                  />
                  <button
                    className="wd-add-btn"
                    type="submit"
                    disabled={saving || !addWdInput.trim()}
                  >
                    Add
                  </button>
                </form>
                {addWdError && <div className="config-field-error">{addWdError}</div>}
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
