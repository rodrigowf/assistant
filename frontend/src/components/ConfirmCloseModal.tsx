import type { TabState } from "../types";

export function ConfirmCloseModal({
  tab,
  onConfirm,
  onCancel,
}: {
  tab: TabState;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Session is running</h3>
        <p className="modal-body">
          <strong>{tab.title || "This session"}</strong> is currently active.
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
