// Compat shim: remark-gfm uses lookbehind regexes unsupported in Safari 12.
// This shim implements GFM table parsing only, using Safari 12-safe regexes.
// Other GFM features (strikethrough, autolinks) remain disabled.

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Node = any;

// Parse a table row string into cell strings.
// Splits on | but NOT on \| (escaped pipes).
// Uses a simple state machine — no lookbehind needed.
function parseCells(row: string): string[] {
  // Strip leading/trailing pipes and whitespace
  let s = row.trim();
  if (s.charAt(0) === '|') s = s.slice(1);
  if (s.charAt(s.length - 1) === '|') s = s.slice(0, s.length - 1);

  const cells: string[] = [];
  let current = '';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === '\\' && i + 1 < s.length) {
      // Escaped character — keep both chars, advance past next
      current += ch + s[i + 1];
      i++;
    } else if (ch === '|') {
      cells.push(current.trim());
      current = '';
    } else {
      current += ch;
    }
  }
  cells.push(current.trim());
  return cells;
}

// Check if a line is a GFM separator row (e.g. |---|:---:|---:|)
function isSeparator(line: string): boolean {
  const stripped = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells = stripped.split('|');
  return cells.length > 0 && cells.every(function(c) {
    return /^:?-+:?$/.test(c.trim());
  });
}

// Determine alignment from a separator cell string
function getAlign(cell: string): 'left' | 'right' | 'center' | null {
  const c = cell.trim();
  const left  = c.charAt(0) === ':';
  const right = c.charAt(c.length - 1) === ':';
  if (left && right) return 'center';
  if (right) return 'right';
  if (left) return 'left';
  return null;
}

// Build a remark text node
function textNode(value: string): Node {
  return { type: 'text', value };
}

// Build a remark table cell node
function cellNode(text: string, isHeader: boolean): Node {
  return {
    type: isHeader ? 'tableCell' : 'tableCell',
    children: [textNode(text)],
  };
}

// Build a remark table row node
function rowNode(cells: string[], isHeader: boolean): Node {
  return {
    type: 'tableRow',
    children: cells.map(function(c) { return cellNode(c, isHeader); }),
  };
}

// Try to parse lines starting at `start` as a GFM table.
// Returns { node, consumed } or null.
function tryParseTable(lines: string[], start: number): { node: Node; consumed: number } | null {
  if (start + 1 >= lines.length) return null;

  const headerLine = lines[start];
  const sepLine    = lines[start + 1];

  // Header must contain at least one pipe
  if (headerLine.indexOf('|') === -1) return null;
  // Second line must be a separator
  if (!isSeparator(sepLine)) return null;

  const headerCells = parseCells(headerLine);
  const sepCells    = parseCells(sepLine);

  // Column count must match
  if (headerCells.length !== sepCells.length) return null;

  const align = sepCells.map(function(c) { return getAlign(c); });

  const rows: Node[] = [];
  rows.push(rowNode(headerCells, true));

  let i = start + 2;
  while (i < lines.length) {
    const line = lines[i];
    // Empty line ends the table
    if (line.trim() === '') break;
    // Line must look like a table row (has a pipe)
    if (line.indexOf('|') === -1) break;
    const cells = parseCells(line);
    // Pad or trim cells to match column count
    while (cells.length < headerCells.length) cells.push('');
    rows.push(rowNode(cells.slice(0, headerCells.length), false));
    i++;
  }

  const node: Node = {
    type: 'table',
    align,
    children: rows,
  };

  return { node, consumed: i - start };
}

// The remark plugin
function remarkGfmShim() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function transformer(tree: Node) {
    const newChildren: Node[] = [];

    for (let i = 0; i < tree.children.length; i++) {
      const node: Node = tree.children[i];

      // Only process paragraph nodes — tables in GFM appear where paragraphs would be
      if (node.type !== 'paragraph') {
        newChildren.push(node);
        continue;
      }

      // Reconstruct the raw text of this paragraph
      const raw: string = node.children
        .map(function(c: Node) { return c.value || ''; })
        .join('');

      const lines = raw.split('\n');

      // Scan lines for table patterns
      let lineIdx = 0;
      const pendingText: string[] = [];

      while (lineIdx < lines.length) {
        const result = tryParseTable(lines, lineIdx);
        if (result) {
          // Flush any preceding text as a paragraph
          if (pendingText.length > 0) {
            newChildren.push({
              type: 'paragraph',
              children: [textNode(pendingText.join('\n'))],
            });
            pendingText.length = 0;
          }
          newChildren.push(result.node);
          lineIdx += result.consumed;
        } else {
          pendingText.push(lines[lineIdx]);
          lineIdx++;
        }
      }

      // Flush remaining text
      if (pendingText.length > 0) {
        newChildren.push({
          type: 'paragraph',
          children: [textNode(pendingText.join('\n'))],
        });
      }
    }

    tree.children = newChildren;
  };
}

export default remarkGfmShim;
