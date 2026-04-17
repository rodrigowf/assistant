import { useState, useCallback } from "react";
import type { WorkingDirectoryEntry } from "../api/rest";

interface Props {
  history: WorkingDirectoryEntry[];
  activeId: string;           // currently selected entry id
  onSelect: (id: string) => void;
  onHistoryChange: (newHistory: WorkingDirectoryEntry[], newActiveId?: string) => void;
  saving?: boolean;
  /** If provided, show this label instead of "active" for the active badge */
  selectedLabel?: string;
}

export function WorkingDirectoryList({
  history,
  activeId,
  onSelect,
  onHistoryChange,
  saving,
  selectedLabel,
}: Props) {
  const [addWdType, setAddWdType] = useState<"local" | "ssh">("local");
  const [addWdPath, setAddWdPath] = useState("");
  const [addWdLabel, setAddWdLabel] = useState("");
  const [addWdHost, setAddWdHost] = useState("");
  const [addWdUser, setAddWdUser] = useState("");
  const [addWdKey, setAddWdKey] = useState("");
  const [addWdConfigDir, setAddWdConfigDir] = useState("");
  const [addWdError, setAddWdError] = useState<string | null>(null);
  const [addWdOpen, setAddWdOpen] = useState(false);
  const [editWdId, setEditWdId] = useState<string | null>(null);

  const badgeLabel = selectedLabel ?? "active";

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
        newHistory = history.map(e => e.id === editWdId ? entry : e);
      } else {
        // If already in history, just select it
        if (history.some(e => e.id === id)) {
          resetAddWdForm();
          onSelect(id);
          return;
        }
        newHistory = [...history, entry];
      }
      onHistoryChange(newHistory, id);
      resetAddWdForm();
    } catch (e) {
      setAddWdError(String(e));
    }
  }, [addWdType, addWdPath, addWdLabel, addWdHost, addWdUser, addWdKey, addWdConfigDir, editWdId, history, onSelect, onHistoryChange]);

  const deleteWd = useCallback((id: string) => {
    const newHistory = history.filter(e => e.id !== id);
    onHistoryChange(newHistory);
  }, [history, onHistoryChange]);

  return (
    <>
      {/* Saved directory list */}
      {history.length > 0 && (
        <div className="wd-list">
          {history.map((entry) => {
            const isActive = entry.id === activeId;
            const canDelete = history.length > 1;
            const isSSH = !!entry.ssh_host;
            const displayName = entry.label || (isSSH ? `${entry.ssh_host}:${entry.path}` : entry.path);
            const subtitle = isSSH
              ? `${entry.ssh_user ? entry.ssh_user + "@" : ""}${entry.ssh_host} · ${entry.path}`
              : null;
            return (
              <div key={entry.id} className={`wd-list-item${isActive ? " active" : ""}${isSSH ? " ssh" : ""}`}>
                <button
                  className="wd-list-radio"
                  onClick={() => !isActive && onSelect(entry.id)}
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
                {isActive && <span className="wd-active-badge">{badgeLabel}</span>}
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
    </>
  );
}
