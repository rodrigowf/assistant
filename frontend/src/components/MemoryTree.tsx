import { useState } from "react";
import type { MemoryNode } from "../types";

interface Props {
  nodes: MemoryNode[];
  activePath?: string;
  openPaths: Set<string>;
  onSelect: (node: MemoryNode) => void;
  depth?: number;
}

/**
 * Recursive collapsible tree of the context/memory/ markdown wiki.
 *
 * Folder expand/collapse is local state per folder row; the caller only
 * tracks which files are open in tabs (`openPaths`) and which is active.
 */
export function MemoryTree({ nodes, activePath, openPaths, onSelect, depth = 0 }: Props) {
  return (
    <>
      {nodes.map((node) =>
        node.is_dir ? (
          <MemoryFolder
            key={node.path}
            node={node}
            activePath={activePath}
            openPaths={openPaths}
            onSelect={onSelect}
            depth={depth}
          />
        ) : (
          <button
            key={node.path}
            className={[
              "memory-file",
              activePath === node.path ? "active" : "",
              openPaths.has(node.path) ? "tab-open" : "",
            ].filter(Boolean).join(" ")}
            style={{ paddingLeft: 14 + depth * 13 }}
            onClick={() => onSelect(node)}
            title={node.path}
          >
            <svg className="memory-icon" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <path d="M14 2v6h6" />
            </svg>
            <span className="memory-label">{stripExt(node.name)}</span>
          </button>
        )
      )}
    </>
  );
}

function MemoryFolder({
  node,
  activePath,
  openPaths,
  onSelect,
  depth,
}: {
  node: MemoryNode;
  activePath?: string;
  openPaths: Set<string>;
  onSelect: (node: MemoryNode) => void;
  depth: number;
}) {
  // Top-level folders start expanded — the tree is only ~4 deep and this
  // avoids opening on a wall of collapsed rows.
  const [expanded, setExpanded] = useState(depth === 0);

  return (
    <>
      <button
        className="memory-folder"
        style={{ paddingLeft: 8 + depth * 13 }}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <svg
          className={`memory-chevron${expanded ? " expanded" : ""}`}
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M9 18l6-6-6-6" />
        </svg>
        <span className="memory-label">{node.name}</span>
      </button>
      {expanded && node.children && (
        <MemoryTree
          nodes={node.children}
          activePath={activePath}
          openPaths={openPaths}
          onSelect={onSelect}
          depth={depth + 1}
        />
      )}
    </>
  );
}

function stripExt(name: string): string {
  return name.replace(/\.md$/i, "");
}
