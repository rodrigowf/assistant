import type { PendingPermission } from "../hooks/useChatInstance";

interface Props {
  pending: PendingPermission;
  onRespond: (decision: "allow" | "deny", message?: string) => void;
}

export function PermissionBar({ pending, onRespond }: Props) {
  const isPlan = pending.toolName === "ExitPlanMode";
  const label = isPlan
    ? "Exit plan mode and start implementing?"
    : `Allow ${pending.toolName}?`;
  const hint = isPlan
    ? "Approve to execute the plan above, reject or type a message to keep planning."
    : "Approve, reject, or type a message to send feedback.";

  return (
    <div className="permission-bar" role="region" aria-label="Permission request">
      <div className="permission-bar-inner">
        <div className="permission-bar-text">
          <span className="permission-bar-label">{label}</span>
          <span className="permission-bar-hint">{hint}</span>
        </div>
        <div className="permission-bar-actions">
          <button
            type="button"
            className="permission-bar-btn permission-bar-btn-secondary"
            onClick={() => onRespond("deny")}
          >
            Reject
          </button>
          <button
            type="button"
            className="permission-bar-btn permission-bar-btn-primary"
            onClick={() => onRespond("allow")}
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
