/**
 * AgentSettings — shared configuration sections used in both the global
 * ConfigPage and per-session SessionConfigPage.
 *
 * Renders:
 *   - Working Directory (via WorkingDirectoryList)
 *   - Session Flags (chrome extension)
 *   - MCP Servers
 *
 * Each section accepts optional `inherited` / `onReset` props for the
 * per-session panel's "Reset to global" + inherited label behaviour.
 */
import type { McpServerConfig, WorkingDirectoryEntry } from "../api/rest";
import { WorkingDirectoryList } from "./WorkingDirectoryList";

// ── Working Directory ─────────────────────────────────────────────────────────

interface WorkingDirectorySectionProps {
  history: WorkingDirectoryEntry[];
  activeId: string;
  saving?: boolean;
  selectedLabel?: string;
  onSelect: (id: string) => void;
  onHistoryChange: (newHistory: WorkingDirectoryEntry[], newActiveId?: string) => void;
  inherited?: boolean;
  onReset?: () => void;
}

export function WorkingDirectorySection({
  history,
  activeId,
  saving,
  selectedLabel,
  onSelect,
  onHistoryChange,
  inherited,
  onReset,
}: WorkingDirectorySectionProps) {
  return (
    <section className="config-section">
      <div className="config-section-header">
        <h3 className="config-section-title">Working Directories</h3>
        {!inherited && onReset && (
          <button className="session-cfg-reset-btn" onClick={onReset} title="Use global active directory">
            Reset to global
          </button>
        )}
      </div>
      <p className="config-section-desc">
        Saved directories for new sessions. Select one to make it active.
        Local directories run Claude here; SSH directories run it on a remote machine.
        {inherited && <span className="session-cfg-inherited"> (using global active directory)</span>}
      </p>
      <WorkingDirectoryList
        history={history}
        activeId={activeId}
        saving={saving}
        selectedLabel={selectedLabel}
        onSelect={onSelect}
        onHistoryChange={onHistoryChange}
      />
    </section>
  );
}

// ── Session Flags ─────────────────────────────────────────────────────────────

interface SessionFlagsSectionProps {
  chromeEnabled: boolean;
  onChange: (value: boolean) => void;
  saving?: boolean;
  inherited?: boolean;
  onReset?: () => void;
}

export function SessionFlagsSection({
  chromeEnabled,
  onChange,
  saving,
  inherited,
  onReset,
}: SessionFlagsSectionProps) {
  return (
    <section className="config-section">
      <div className="config-section-header">
        <h3 className="config-section-title">Session Flags</h3>
        {!inherited && onReset && (
          <button className="session-cfg-reset-btn" onClick={onReset} title="Reset to global default">
            Reset to global
          </button>
        )}
      </div>
      <p className="config-section-desc">
        Extra flags applied when initializing new Claude Code sessions.
        {inherited && <span className="session-cfg-inherited"> (using global setting)</span>}
      </p>
      <div className="config-item-list">
        <label className={`config-item${chromeEnabled ? " enabled" : ""}`}>
          <input
            type="checkbox"
            checked={chromeEnabled}
            onChange={() => onChange(!chromeEnabled)}
            disabled={saving}
          />
          <div className="config-item-info">
            <span className="config-item-name">Chrome Extension</span>
            <span className="config-item-detail">Launch sessions with --chrome flag to control Google Chrome tabs</span>
          </div>
        </label>
      </div>
    </section>
  );
}

// ── MCP Servers ───────────────────────────────────────────────────────────────

interface McpServersSectionProps {
  mcpServers: Record<string, McpServerConfig>;
  enabledMcps: string[];
  onToggle: (name: string) => void;
  saving?: boolean;
  inherited?: boolean;
  onReset?: () => void;
}

export function McpServersSection({
  mcpServers,
  enabledMcps,
  onToggle,
  saving,
  inherited,
  onReset,
}: McpServersSectionProps) {
  const mcpNames = Object.keys(mcpServers);

  return (
    <section className="config-section">
      <div className="config-section-header">
        <h3 className="config-section-title">MCP Servers</h3>
        {!inherited && onReset && (
          <button className="session-cfg-reset-btn" onClick={onReset} title="Reset to global default">
            Reset to global
          </button>
        )}
      </div>
      <p className="config-section-desc">
        {inherited
          ? "MCP servers enabled for this session."
          : "Default MCP servers enabled for new sessions. Override per-session from the chat header."}
        {inherited && <span className="session-cfg-inherited"> (using global setting)</span>}
      </p>
      {mcpNames.length === 0 ? (
        <div className="config-empty">No MCP servers configured in .claude.json</div>
      ) : (
        <div className="config-item-list">
          {mcpNames.map((name) => {
            const cfg = mcpServers[name];
            const enabled = enabledMcps.includes(name);
            return (
              <label key={name} className={`config-item${enabled ? " enabled" : ""}`}>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={() => onToggle(name)}
                  disabled={saving}
                />
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
  );
}
