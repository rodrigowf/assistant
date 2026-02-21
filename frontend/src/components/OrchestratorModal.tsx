interface Props {
  onProceed: () => void;
  onCancel: () => void;
}

export function OrchestratorModal({ onProceed, onCancel }: Props) {
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Orchestrator already active</h3>
        <p className="modal-body">
          An orchestrator session is already running. Starting a new one will stop the current session.
        </p>
        <div className="modal-actions">
          <button className="modal-btn modal-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button className="modal-btn modal-btn-primary" onClick={onProceed}>
            Stop &amp; start new
          </button>
        </div>
      </div>
    </div>
  );
}
