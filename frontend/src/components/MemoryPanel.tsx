import { useEffect, useState, useCallback } from "react";
import { Markdown } from "./Markdown";
import { getMemoryFile } from "../api/rest";

interface Props {
  title: string;
  /** Path relative to context/memory/. */
  path: string;
}

/**
 * Viewer for a context/memory/ markdown file. Renders natively through the
 * shared Markdown component (same styling as chat messages) rather than
 * framing it, so theming and code highlighting come for free.
 */
export function MemoryPanel({ title, path }: Props) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // State is set only in the promise callbacks — never synchronously — so the
  // mount effect below doesn't trigger a cascading render.
  const load = useCallback(() => {
    getMemoryFile(path)
      .then((text) => {
        setContent(text);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [path]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="chat-panel memory-panel">
      <div className="viz-toolbar">
        <span className="viz-toolbar-title" title={path}>{title}</span>
        <div className="viz-toolbar-actions">
          <button className="viz-toolbar-btn" onClick={load} title="Reload">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M23 4v6h-6M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          </button>
          <a
            className="viz-toolbar-btn"
            href={`/memory/${path}`}
            target="_blank"
            rel="noreferrer"
            title="Open raw file in new browser tab"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <path d="M15 3h6v6M10 14L21 3" />
            </svg>
          </a>
        </div>
      </div>
      <div className="memory-content">
        {error !== null ? (
          <div className="memory-error">Could not load {path} — {error}</div>
        ) : content === null ? (
          <div className="memory-loading">Loading…</div>
        ) : (
          <div className="message-assistant">
            {(() => {
              const { frontmatter, body } = splitFrontmatter(content);
              return (
                <>
                  {frontmatter && (
                    <details className="memory-frontmatter">
                      <summary>Frontmatter</summary>
                      <pre>{frontmatter}</pre>
                    </details>
                  )}
                  <Markdown content={body} />
                </>
              );
            })()}
          </div>
        )}
      </div>
    </main>
  );
}

/**
 * Split a leading YAML frontmatter block off the markdown body.
 *
 * Every file under context/memory/ starts with one, and react-markdown has no
 * concept of it — rendered inline it becomes a run-on paragraph with the `---`
 * fences showing up as horizontal rules. Keeping it verbatim in a collapsed
 * block loses no information and needs no YAML parsing.
 */
function splitFrontmatter(raw: string): { frontmatter: string | null; body: string } {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(raw);
  if (!match) return { frontmatter: null, body: raw };
  return { frontmatter: match[1], body: raw.slice(match[0].length) };
}
