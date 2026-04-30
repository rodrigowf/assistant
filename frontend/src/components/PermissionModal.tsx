import { useState } from "react";
import type { PendingPermission } from "../hooks/useChatInstance";

interface Props {
  pending: PendingPermission;
  onRespond: (decision: "allow" | "deny", message?: string) => void;
}

export function PermissionModal({ pending, onRespond }: Props) {
  const [reason, setReason] = useState("");
  const isPlan = pending.toolName === "ExitPlanMode";

  const title = isPlan
    ? "Exit plan mode and start implementing?"
    : `Allow ${pending.toolName}?`;
  const body = isPlan
    ? "The plan above is ready. Approve to leave plan mode and execute it; reject to keep planning."
    : "The agent is asking permission to run this tool.";

  return (
    <div className="modal-overlay">
      <div className="modal-card permission-modal" role="dialog" aria-modal="true">
        <div className="modal-title">{title}</div>
        <div className="modal-body">
          <p>{body}</p>
          {!isPlan && (
            <pre className="permission-modal-input">
              {JSON.stringify(pending.toolInput, null, 2)}
            </pre>
          )}
          <label className="permission-modal-reason">
            <span>Optional message (sent back to the agent)</span>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. focus on tests first"
            />
          </label>
        </div>
        <div className="modal-actions">
          <button
            type="button"
            className="modal-btn modal-btn-secondary"
            onClick={() => onRespond("deny", reason || undefined)}
          >
            Reject
          </button>
          <button
            type="button"
            className="modal-btn modal-btn-primary"
            onClick={() => onRespond("allow", reason || undefined)}
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
