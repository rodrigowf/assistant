import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  getSessionConfig,
  updateSessionConfig,
  listMcpServers,
  listSkills,
  listAgents,
  type AssistantConfig,
  type SessionConfig,
  type SkillInfo,
  type AgentInfo,
  type McpServerConfig,
  type WorkingDirectoryEntry,
} from "../api/rest";

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
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  useEffect(() => () => { if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current); }, []);

  const load = useCallback(async () => {
    if (!sessionId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const [globalCfg, sessionCfg, mcpRes, skillsRes, agentsRes] = await Promise.all([
        getConfig(),
        getSessionConfig(sessionId),
        listMcpServers(),
        listSkills(),
        listAgents(),
      ]);
      setGlobalConfig(globalCfg);
      setSessionConfig(sessionCfg);
      setMcpServers(mcpRes.servers);
      setSkills(skillsRes.skills);
      setAgents(agentsRes.agents);
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
  const effectiveDisabledSkills = sessionConfig?.disabled_skills ?? globalConfig?.disabled_skills ?? [];
  const effectiveDisabledAgents = sessionConfig?.disabled_agents ?? globalConfig?.disabled_agents ?? [];
  const effectiveChrome = sessionConfig?.chrome_extension ?? globalConfig?.chrome_extension ?? false;

  const isInherited = (field: keyof SessionConfig) =>
    sessionConfig?.[field] === null || sessionConfig?.[field] === undefined;

  const resetToGlobal = useCallback(async (field: keyof SessionConfig) => {
    await save({ [field]: null });
  }, [save]);

  const selectWd = useCallback(async (id: string) => {
    await save({ working_directory: id });
  }, [save]);

  const toggleMcp = useCallback(async (name: string) => {
    const current = new Set(effectiveMcps);
    if (current.has(name)) current.delete(name); else current.add(name);
    await save({ enabled_mcps: Array.from(current) });
  }, [effectiveMcps, save]);

  const toggleSkill = useCallback(async (name: string) => {
    const current = new Set(effectiveDisabledSkills);
    if (current.has(name)) current.delete(name); else current.add(name);
    await save({ disabled_skills: Array.from(current) });
  }, [effectiveDisabledSkills, save]);

  const toggleAgent = useCallback(async (name: string) => {
    const current = new Set(effectiveDisabledAgents);
    if (current.has(name)) current.delete(name); else current.add(name);
    await save({ disabled_agents: Array.from(current) });
  }, [effectiveDisabledAgents, save]);

  const mcpNames = Object.keys(mcpServers);
  const wdHistory: WorkingDirectoryEntry[] = globalConfig?.working_directory_history ?? [];

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
              {/* ── Working Directory ─────────────────────────── */}
              <section className="config-section">
                <div className="config-section-header">
                  <h3 className="config-section-title">Working Directory</h3>
                  {!isInherited("working_directory") && (
                    <button className="session-cfg-reset-btn" onClick={() => resetToGlobal("working_directory")} title="Use global active directory">
                      Reset to global
                    </button>
                  )}
                </div>
                <p className="config-section-desc">
                  The directory Claude runs in for this session.
                  {isInherited("working_directory") && <span className="session-cfg-inherited"> (using global active directory)</span>}
                </p>
                {wdHistory.length > 0 && (
                  <div className="wd-list">
                    {wdHistory.map((entry) => {
                      const isActive = entry.id === effectiveWdId;
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
                            title={isActive ? "Currently selected" : "Use this directory"}
                            disabled={saving}
                          >
                            <span className={`wd-radio-dot${isActive ? " checked" : ""}`} />
                          </button>
                          <div className="wd-list-info">
                            <div className="wd-list-path-row">
                              {isSSH && <span className="wd-ssh-badge" title="Remote SSH">SSH</span>}
                              <span className="wd-list-path" title={entry.id}>{displayName}</span>
                            </div>
                            {subtitle && <span className="wd-list-subtitle">{subtitle}</span>}
                          </div>
                          {isActive && <span className="wd-active-badge">
                            {isInherited("working_directory") ? "global default" : "selected"}
                          </span>}
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* ── Session Flags ─────────────────────────────── */}
              <section className="config-section">
                <div className="config-section-header">
                  <h3 className="config-section-title">Session Flags</h3>
                  {!isInherited("chrome_extension") && (
                    <button className="session-cfg-reset-btn" onClick={() => resetToGlobal("chrome_extension")} title="Reset to global default">
                      Reset to global
                    </button>
                  )}
                </div>
                <p className="config-section-desc">
                  Extra flags for this session.
                  {isInherited("chrome_extension") && <span className="session-cfg-inherited"> (using global setting)</span>}
                </p>
                <div className="config-item-list">
                  <label className={`config-item${effectiveChrome ? " enabled" : ""}`}>
                    <input
                      type="checkbox"
                      checked={effectiveChrome}
                      onChange={() => save({ chrome_extension: !effectiveChrome })}
                    />
                    <div className="config-item-info">
                      <span className="config-item-name">Chrome Extension</span>
                      <span className="config-item-detail">Launch with --chrome flag to control Google Chrome tabs</span>
                    </div>
                  </label>
                </div>
              </section>

              {/* ── MCP Servers ───────────────────────────────── */}
              <section className="config-section">
                <div className="config-section-header">
                  <h3 className="config-section-title">MCP Servers</h3>
                  {!isInherited("enabled_mcps") && (
                    <button className="session-cfg-reset-btn" onClick={() => resetToGlobal("enabled_mcps")} title="Reset to global default">
                      Reset to global
                    </button>
                  )}
                </div>
                <p className="config-section-desc">
                  MCP servers enabled for this session.
                  {isInherited("enabled_mcps") && <span className="session-cfg-inherited"> (using global setting)</span>}
                </p>
                {mcpNames.length === 0 ? (
                  <div className="config-empty">No MCP servers configured in .claude.json</div>
                ) : (
                  <div className="config-item-list">
                    {mcpNames.map((name) => {
                      const cfg = mcpServers[name];
                      const enabled = effectiveMcps.includes(name);
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
                <div className="config-section-header">
                  <h3 className="config-section-title">Skills</h3>
                  {!isInherited("disabled_skills") && (
                    <button className="session-cfg-reset-btn" onClick={() => resetToGlobal("disabled_skills")} title="Reset to global default">
                      Reset to global
                    </button>
                  )}
                </div>
                <p className="config-section-desc">
                  Slash commands visible to this session.
                  {isInherited("disabled_skills") && <span className="session-cfg-inherited"> (using global setting)</span>}
                </p>
                {skills.length === 0 ? (
                  <div className="config-empty">No skills found</div>
                ) : (
                  <div className="config-item-list">
                    {skills.map((skill) => {
                      const enabled = !effectiveDisabledSkills.includes(skill.name);
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
                <div className="config-section-header">
                  <h3 className="config-section-title">Agents</h3>
                  {!isInherited("disabled_agents") && (
                    <button className="session-cfg-reset-btn" onClick={() => resetToGlobal("disabled_agents")} title="Reset to global default">
                      Reset to global
                    </button>
                  )}
                </div>
                <p className="config-section-desc">
                  Specialized subagents available to this session.
                  {isInherited("disabled_agents") && <span className="session-cfg-inherited"> (using global setting)</span>}
                </p>
                {agents.length === 0 ? (
                  <div className="config-empty">No agents found</div>
                ) : (
                  <div className="config-item-list">
                    {agents.map((agent) => {
                      const enabled = !effectiveDisabledAgents.includes(agent.name);
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
