import { useRef, useState } from "react";

interface Props {
  title: string;
  url: string;
}

/**
 * Viewer for a visualization tab. The file is already served at its own URL
 * by the backend's static route, so this is just a framed view of it with a
 * reload and an escape hatch to a real browser tab.
 */
export function VizPanel({ title, url }: Props) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // Bumping the key remounts the iframe — a reload that works regardless of
  // the framed document's origin/history state.
  const [reloadKey, setReloadKey] = useState(0);

  return (
    <main className="chat-panel viz-panel">
      <div className="viz-toolbar">
        <span className="viz-toolbar-title" title={url}>{title}</span>
        <div className="viz-toolbar-actions">
          <button
            className="viz-toolbar-btn"
            onClick={() => setReloadKey((k) => k + 1)}
            title="Reload"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M23 4v6h-6M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          </button>
          <a
            className="viz-toolbar-btn"
            href={url}
            target="_blank"
            rel="noreferrer"
            title="Open in new browser tab"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <path d="M15 3h6v6M10 14L21 3" />
            </svg>
          </a>
        </div>
      </div>
      <iframe
        key={reloadKey}
        ref={iframeRef}
        className="viz-frame"
        src={url}
        title={title}
        // These are first-party files from context/public/, so the goal isn't
        // isolation from their scripts — allow-scripts + allow-same-origin
        // together are, by design, escapable (Chrome logs a warning saying so).
        // What the attribute still buys is the tokens NOT listed: a framed page
        // can't navigate the whole app away or trigger downloads. Same-origin is
        // required for visualizations that fetch from the backend or use storage.
        sandbox="allow-scripts allow-same-origin allow-popups allow-forms allow-modals"
      />
    </main>
  );
}
