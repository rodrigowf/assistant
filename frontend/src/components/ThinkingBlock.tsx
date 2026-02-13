import { useState } from "react";

interface Props {
  content: string;
  streaming: boolean;
}

export function ThinkingBlock({ content, streaming }: Props) {
  const [expanded, setExpanded] = useState(streaming);

  return (
    <div className="thinking-block">
      <button
        className="thinking-toggle"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="thinking-icon">
          {streaming ? "..." : ""}
        </span>
        <span className="thinking-label">
          {streaming ? "Thinking" : "Thought"}
        </span>
        <span className="toggle-arrow">{expanded ? "−" : "+"}</span>
      </button>
      {expanded && (
        <div className="thinking-content">{content}</div>
      )}
    </div>
  );
}
