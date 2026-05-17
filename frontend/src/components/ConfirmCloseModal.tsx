import type { TabState } from "../types";

export function ConfirmCloseModal({
  tab,
  title,
  onConfirm,
  onCancel,
}: {
  tab: TabState;
  /** Resolved title (derived from sessions[] by the caller). Falls back to
   *  the in-memory tab title for tabs that have no session entry yet. */
  title?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const displayTitle = title || tab.title || "This session";
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Session is running</h3>
        <p className="modal-body">
          <strong>{displayTitle}</strong> is currently active.
          Closing it will interrupt the current response.
        </p>
        <div className="modal-actions">
          <button className="modal-btn modal-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button className="modal-btn modal-btn-primary" onClick={onConfirm}>
            Close anyway
          </button>
        </div>
      </div>
    </div>
  );
}
