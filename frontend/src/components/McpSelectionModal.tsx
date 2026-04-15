import { useState, useEffect } from "react";
import { listMcpServers } from "../api/rest";
import type { McpServerConfig } from "../api/rest";

interface Props {
  /** Currently selected MCP server names */
  selectedMcps: string[];
  /** Called when user confirms selection */
  onConfirm: (selectedMcps: string[]) => void;
  /** Called when user cancels */
  onCancel: () => void;
}

export function McpSelectionModal({ selectedMcps, onConfirm, onCancel }: Props) {
  const [servers, setServers] = useState<Record<string, McpServerConfig>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set(selectedMcps));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const response = await listMcpServers();
        if (!cancelled) {
          setServers(response.servers);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load MCP servers");
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const toggleServer = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  const handleConfirm = () => {
    onConfirm(Array.from(selected));
  };

  const serverNames = Object.keys(servers);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card mcp-modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">MCP Server Selection</h3>
        <p className="modal-body mcp-description">
          Select which MCP servers to enable for this session. Changes will restart the session.
        </p>

        {loading && <div className="mcp-loading">Loading servers...</div>}
        {error && <div className="mcp-error">{error}</div>}

        {!loading && !error && serverNames.length === 0 && (
          <div className="mcp-empty">No MCP servers configured in .claude.json</div>
        )}

        {!loading && !error && serverNames.length > 0 && (
          <div className="mcp-server-list">
            {serverNames.map((name) => {
              const config = servers[name];
              const isSelected = selected.has(name);
              return (
                <label key={name} className={`mcp-server-item ${isSelected ? "selected" : ""}`}>
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggleServer(name)}
                  />
                  <div className="mcp-server-info">
                    <span className="mcp-server-name">{name}</span>
                    <span className="mcp-server-command">
                      {config.command} {config.args?.join(" ") || ""}
                    </span>
                  </div>
                </label>
              );
            })}
          </div>
        )}

        <div className="modal-actions">
          <button className="modal-btn modal-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            className="modal-btn modal-btn-primary"
            onClick={handleConfirm}
            disabled={loading}
          >
            Apply &amp; Restart
          </button>
        </div>
      </div>
    </div>
  );
}
