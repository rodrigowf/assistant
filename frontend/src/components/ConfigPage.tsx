import { useState, useEffect, useCallback, useRef } from "react";
import {
  getConfig,
  updateConfig,
  listMcpServers,
  listSkills,
  type AssistantConfig,
  type SkillInfo,
  type McpServerConfig,
} from "../api/rest";

interface Props {
  isActive: boolean;
}

export function ConfigPage({ isActive }: Props) {
  const [config, setConfig] = useState<AssistantConfig | null>(null);
  const [mcpServers, setMcpServers] = useState<Record<string, McpServerConfig>>({});
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState(false);

  // Working directory input state
  const [wdInput, setWdInput] = useState("");
  const [wdError, setWdError] = useState<string | null>(null);

  const savedMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, mcpRes, skillsRes] = await Promise.all([
        getConfig(),
        listMcpServers(),
        listSkills(),
      ]);
      setConfig(cfg);
      setWdInput(cfg.working_directory);
      setMcpServers(mcpRes.servers);
      setSkills(skillsRes.skills);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load configuration");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const showSaved = () => {
    setSavedMsg(true);
    if (savedMsgTimer.current) clearTimeout(savedMsgTimer.current);
    savedMsgTimer.current = setTimeout(() => setSavedMsg(false), 2000);
  };

  // ── Working directory ──────────────────────────────────────────────

  const applyWorkingDirectory = useCallback(async (dir: string) => {
    if (!config) return;
    setSaving(true);
    setWdError(null);
    try {
      const updated = await updateConfig({ working_directory: dir });
      setConfig(updated);
      setWdInput(updated.working_directory);
      showSaved();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to save";
      // Try to extract FastAPI detail
      try {
        const parsed = JSON.parse(msg.replace(/^\d+ /, ""));
        setWdError(parsed.detail ?? msg);
      } catch {
        setWdError(msg);
      }
    } finally {
      setSaving(false);
    }
  }, [config]);

  const handleWdSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = wdInput.trim();
    if (!trimmed) return;
    applyWorkingDirectory(trimmed);
  };

  // ── MCPs ──────────────────────────────────────────────────────────

  const toggleMcp = useCallback(async (name: string) => {
    if (!config) return;
    const current = new Set(config.enabled_mcps);
    if (current.has(name)) {
      current.delete(name);
    } else {
      current.add(name);
    }
    const enabled_mcps = Array.from(current);
    setSaving(true);
    try {
      const updated = await updateConfig({ enabled_mcps });
      setConfig(updated);
      showSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }, [config]);

  // ── Skills ────────────────────────────────────────────────────────

  const toggleSkill = useCallback(async (skillName: string) => {
    if (!config) return;
    const current = new Set(config.disabled_skills);
    if (current.has(skillName)) {
      current.delete(skillName);
    } else {
      current.add(skillName);
    }
    const disabled_skills = Array.from(current);
    setSaving(true);
    try {
      const updated = await updateConfig({ disabled_skills });
      setConfig(updated);
      showSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }, [config]);

  const mcpNames = Object.keys(mcpServers);

  return (
    <main
      className="config-page"
      style={{ display: isActive ? undefined : "none" }}
    >
      <div className="config-content">
        <div className="config-header">
          <h1 className="config-title">Configuration</h1>
          {saving && <span className="config-saving">Saving…</span>}
          {savedMsg && !saving && <span className="config-saved">Saved</span>}
        </div>

        {loading && <div className="config-loading">Loading…</div>}
        {error && <div className="config-error">{error}</div>}

        {!loading && config && (
          <>
            {/* ── Working Directory ─────────────────────────── */}
            <section className="config-section">
              <h2 className="config-section-title">Working Directory</h2>
              <p className="config-section-desc">
                The working directory used for all new Claude Code sessions.
                Changes apply to new sessions only.
              </p>

              <form className="wd-form" onSubmit={handleWdSubmit}>
                <input
                  className="wd-input"
                  type="text"
                  value={wdInput}
                  onChange={(e) => { setWdInput(e.target.value); setWdError(null); }}
                  placeholder="/path/to/project"
                  spellCheck={false}
                />
                <button
                  className="wd-apply-btn"
                  type="submit"
                  disabled={saving || wdInput.trim() === config.working_directory}
                >
                  Apply
                </button>
              </form>
              {wdError && <div className="config-field-error">{wdError}</div>}

              {config.working_directory_history.length > 1 && (
                <div className="wd-history">
                  <div className="wd-history-label">Recent directories</div>
                  {config.working_directory_history.map((dir) => (
                    <button
                      key={dir}
                      className={`wd-history-item${dir === config.working_directory ? " active" : ""}`}
                      onClick={() => {
                        setWdInput(dir);
                        if (dir !== config.working_directory) {
                          applyWorkingDirectory(dir);
                        }
                      }}
                      title={dir}
                    >
                      <span className="wd-history-path">{dir}</span>
                      {dir === config.working_directory && (
                        <span className="wd-history-current">current</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </section>

            {/* ── MCP Servers ───────────────────────────────── */}
            <section className="config-section">
              <h2 className="config-section-title">MCP Servers</h2>
              <p className="config-section-desc">
                Select which MCP servers are enabled by default for new sessions.
                You can still override per-session from the chat header.
              </p>

              {mcpNames.length === 0 ? (
                <div className="config-empty">
                  No MCP servers configured in .claude.json
                </div>
              ) : (
                <div className="config-item-list">
                  {mcpNames.map((name) => {
                    const cfg = mcpServers[name];
                    const enabled = config.enabled_mcps.includes(name);
                    return (
                      <label
                        key={name}
                        className={`config-item${enabled ? " enabled" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={() => toggleMcp(name)}
                        />
                        <div className="config-item-info">
                          <span className="config-item-name">{name}</span>
                          <span className="config-item-detail">
                            {cfg.command} {cfg.args?.join(" ") ?? ""}
                          </span>
                        </div>
                      </label>
                    );
                  })}
                </div>
              )}
            </section>

            {/* ── Skills ───────────────────────────────────── */}
            <section className="config-section">
              <h2 className="config-section-title">Skills</h2>
              <p className="config-section-desc">
                Choose which skills (slash commands) are visible to agents.
                Disabled skills are hidden from the system prompt.
              </p>

              {skills.length === 0 ? (
                <div className="config-empty">No skills found</div>
              ) : (
                <div className="config-item-list">
                  {skills.map((skill) => {
                    const enabled = !config.disabled_skills.includes(skill.name);
                    return (
                      <label
                        key={skill.name}
                        className={`config-item${enabled ? " enabled" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={() => toggleSkill(skill.name)}
                        />
                        <div className="config-item-info">
                          <span className="config-item-name">/{skill.name}</span>
                          {skill.description && (
                            <span className="config-item-detail">{skill.description}</span>
                          )}
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
    </main>
  );
}
