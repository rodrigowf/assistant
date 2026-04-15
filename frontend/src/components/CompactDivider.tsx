import { useState } from "react";

export function CompactDivider({ summary }: { summary: string }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="compact-divider">
      <div className="compact-divider-line" />
      <button
        className="compact-divider-label"
        onClick={() => setExpanded(!expanded)}
        title={expanded ? "Hide summary" : "Show compact summary"}
      >
        <span className="compact-divider-icon">⟳</span>
        Context compacted
        {summary && <span className="compact-divider-toggle">{expanded ? " ▲" : " ▼"}</span>}
      </button>
      <div className="compact-divider-line" />
      {expanded && summary && (
        <div className="compact-divider-summary">{summary}</div>
      )}
    </div>
  );
}
