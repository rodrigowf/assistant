// Compat shim: remark-gfm uses lookbehind regexes unsupported in Safari 12.
// This shim implements GFM table parsing only, using Safari 12-safe regexes.
// Other GFM features (strikethrough, autolinks) remain disabled.
//
// Inline formatting inside cells: bold (**x**), italic (*x* / _x_),
// inline code (`x`), links ([text](url)) and <br> are parsed into proper
// mdast nodes so react-markdown renders them — the prior version dumped the
// raw cell text into a single text node and bold/code/etc. appeared as
// literal asterisks/backticks (or got swallowed by react-markdown).

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Node = any;

// Parse a table row string into cell strings.
// Splits on | but NOT on \| (escaped pipes) and NOT on | inside `inline code`.
// Uses a simple state machine — no lookbehind needed.
function parseCells(row: string): string[] {
  // Strip leading/trailing pipes and whitespace
  let s = row.trim();
  if (s.charAt(0) === '|') s = s.slice(1);
  if (s.charAt(s.length - 1) === '|') s = s.slice(0, s.length - 1);

  const cells: string[] = [];
  let current = '';
  let inCode = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === '\\' && i + 1 < s.length) {
      // Escaped character — keep both chars, advance past next
      current += ch + s[i + 1];
      i++;
    } else if (ch === '`') {
      // Toggle inline-code state. Pipes inside backticks are literal.
      inCode = !inCode;
      current += ch;
    } else if (ch === '|' && !inCode) {
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

// Build remark nodes
function textNode(value: string): Node {
  return { type: 'text', value };
}
function strongNode(children: Node[]): Node {
  return { type: 'strong', children };
}
function emphasisNode(children: Node[]): Node {
  return { type: 'emphasis', children };
}
function inlineCodeNode(value: string): Node {
  return { type: 'inlineCode', value };
}
function linkNode(url: string, children: Node[]): Node {
  return { type: 'link', url, title: null, children };
}
function breakNode(): Node {
  return { type: 'break' };
}

// Parse a cell string into mdast inline children.
//
// Supports (kept minimal — this is a compat shim, not a full markdown parser):
//   `code`                    → inlineCode
//   **bold** / __bold__       → strong (children recursively parsed)
//   *italic* / _italic_       → emphasis (children recursively parsed)
//   [text](url)               → link (text recursively parsed)
//   <br> / <br/> / <br />     → break
//   \X                        → literal X (escape)
//
// Anything that doesn't match a delimiter pair is emitted as text so the cell
// is never empty just because we failed to recognise some construct.
function parseInline(text: string): Node[] {
  const out: Node[] = [];
  let buf = '';
  const flush = function () {
    if (buf.length > 0) { out.push(textNode(buf)); buf = ''; }
  };

  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text.charAt(i);

    // Escape: \X — emit literal X, advance past both chars.
    if (ch === '\\' && i + 1 < n) {
      buf += text.charAt(i + 1);
      i += 2;
      continue;
    }

    // <br>, <br/>, <br /> — line break inside a cell.
    if (ch === '<') {
      const rest = text.slice(i);
      const m = /^<br\s*\/?\s*>/i.exec(rest);
      if (m) {
        flush();
        out.push(breakNode());
        i += m[0].length;
        continue;
      }
    }

    // Inline code: `...` — find the next unescaped backtick.
    if (ch === '`') {
      const end = text.indexOf('`', i + 1);
      if (end !== -1) {
        flush();
        out.push(inlineCodeNode(text.slice(i + 1, end)));
        i = end + 1;
        continue;
      }
    }

    // Link: [text](url) — text may contain inline markdown, url is literal.
    if (ch === '[') {
      const close = text.indexOf(']', i + 1);
      if (close !== -1 && text.charAt(close + 1) === '(') {
        const urlEnd = text.indexOf(')', close + 2);
        if (urlEnd !== -1) {
          flush();
          const linkText = text.slice(i + 1, close);
          const url = text.slice(close + 2, urlEnd);
          out.push(linkNode(url, parseInline(linkText)));
          i = urlEnd + 1;
          continue;
        }
      }
    }

    // Bold (** or __) — greedy but bounded: find the matching closing pair.
    if ((ch === '*' || ch === '_') && i + 1 < n && text.charAt(i + 1) === ch) {
      const marker = ch + ch;
      const end = text.indexOf(marker, i + 2);
      // Require non-empty content so "****" isn't treated as a pair.
      if (end !== -1 && end > i + 2) {
        flush();
        out.push(strongNode(parseInline(text.slice(i + 2, end))));
        i = end + 2;
        continue;
      }
    }

    // Italic (* or _) — single delimiter.
    if (ch === '*' || ch === '_') {
      const end = text.indexOf(ch, i + 1);
      if (end !== -1 && end > i + 1) {
        flush();
        out.push(emphasisNode(parseInline(text.slice(i + 1, end))));
        i = end + 1;
        continue;
      }
    }

    buf += ch;
    i++;
  }

  flush();
  return out;
}

// Build a remark table cell node
function cellNode(text: string, _isHeader: boolean): Node {
  const children = parseInline(text);
  return {
    type: 'tableCell',
    children: children.length > 0 ? children : [textNode('')],
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
