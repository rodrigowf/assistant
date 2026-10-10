/**
 * Internal links (spec 12 §9.4, LNK-1…LNK-6): which markdown links point at a visualization or a
 * memory file, so the app opens them in its own viewer instead of a browser tab. Pure functions;
 * the Android twin is `:core:markdown` `InternalLinks.kt`, and both pass the shared corpus
 * `apps/protocol-fixtures/links/internal-links.json`.
 *
 * Recognised (first match wins):
 * - LNK-1 filesystem paths as agents print them: `context/public/<x>.html` → visual,
 *   `context/memory/<x>.md` → memory (relative, `./`, or absolute: `/home/u/assistant/context/…`,
 *   `~/assistant/context/…`); `docs/<x>.md` and `…/assistant/docs/<x>.md` → memory `archie/<x>.md`
 *   (context/memory/archie → docs/). A trailing `:line` / `:line-line` is dropped.
 * - LNK-2 root-relative URLs: `/memory/<x>.md` (and `/memory/` → MEMORY.md);
 *   `/markdown_reader.html?file=memory/<x>.md`; `/<x>.html` or `/<dir>/` (→ `<dir>/index.html`)
 *   outside the app's own prefixes (`/api/`, `/assets/`, `/compat/`, `/memory/`, …); no query.
 * - LNK-3 absolute `http(s)` URLs whose host is the backend's (any port) → as LNK-2.
 * - LNK-4 other private-network hosts (the other Archie machine: LAN, Tailscale, localhost) →
 *   LNK-2 only for `/memory/…`, the markdown reader, `/visualizations/…` or a path in the
 *   visualization list (the content is synced, so the current backend serves the same file).
 * - Anything else (public hosts, other schemes, `#anchors`, assets, folders without a trailing
 *   slash) is not internal.
 */

import type { InternalTarget } from './targetUrl';

export type { InternalTarget } from './targetUrl';

export interface InternalLinkContext {
  /** The backend origin (`https://192.168.0.200`), for LNK-3. */
  readonly origin?: string | null;
  /** A visualization-list lookup, for LNK-4. */
  readonly isVisual?: (path: string) => boolean;
}

/** First path segments the app itself serves (never a visualization). */
const RESERVED = ['api', 'assets', 'compat', 'legacy', 'legacy_compat', 'next', 'next-compat', 'memory', 'uploads', 'projects'];

const SCHEME = /^([a-z][a-z0-9+.-]*):/i;
const HTML = /\.html?$/i;
const MD = /\.md$/i;
const LINE_SUFFIX = /:\d+(?:-\d+)?$/;
/** `context/public/` or `context/memory/` at a segment boundary. */
const CONTEXT_ROOT = /(?:^|\/)context\/(public|memory)\/(.+)$/;
/** `docs/` at the start, or after an `assistant` folder of an absolute path. */
const DOCS_ROOT = /^(?:\.\/)?docs\/(.+)$|^(?:~|\/[^\s]*)\/assistant\/docs\/(.+)$/;
/** Absolute filesystem paths (`/tmp/x.html` is a file on some machine, not a URL path). */
const FS_ABSOLUTE = /^(?:~\/|\/(?:home|Users|root|tmp|var|etc|usr|opt|srv|mnt|media|private|Volumes)\/)/;

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Decoded, `.`/`..`-normalised segments; `null` when it climbs above the root, or when a segment
 * decodes to a separator (`..%2F..` must not smuggle a `..` past the check).
 */
function normalize(path: string): string | null {
  const out: string[] = [];
  for (const raw of path.split('/')) {
    const seg = safeDecode(raw);
    if (seg.indexOf('/') >= 0 || seg.indexOf('\\') >= 0) return null;
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (!out.length) return null;
      out.pop();
      continue;
    }
    out.push(seg);
  }
  return out.length ? out.join('/') : null;
}

function splitHash(s: string): [string, string | null] {
  const i = s.indexOf('#');
  return i < 0 ? [s, null] : [s.slice(0, i), s.slice(i + 1) || null];
}

function memoryTarget(path: string | null, fragment: string | null): InternalTarget | null {
  return path && MD.test(path) ? { kind: 'memory', path, fragment } : null;
}

/** LNK-1: a filesystem path (no scheme, no query). */
function fromFilesystem(raw: string): InternalTarget | null {
  const [pathPart, fragment] = splitHash(raw.replace(LINE_SUFFIX, ''));
  const ctx = CONTEXT_ROOT.exec(pathPart);
  if (ctx) {
    const rest = normalize(ctx[2] ?? '');
    if (!rest) return null;
    if (ctx[1] === 'memory') return memoryTarget(rest, fragment);
    return HTML.test(rest) ? { kind: 'visual', path: rest } : null;
  }
  const docs = DOCS_ROOT.exec(pathPart);
  if (docs) {
    const rest = normalize(docs[1] ?? docs[2] ?? '');
    return memoryTarget(rest ? `archie/${rest}` : null, fragment);
  }
  return null;
}

/** LNK-2: a root-relative URL path (with its query and hash). `strong` = LNK-4 restrictions. */
function fromRootPath(pathAndMore: string, lan: boolean, ctx: InternalLinkContext): InternalTarget | null {
  const [beforeHash, fragment] = splitHash(pathAndMore);
  const q = beforeHash.indexOf('?');
  const pathname = q < 0 ? beforeHash : beforeHash.slice(0, q);
  const query = q < 0 ? '' : beforeHash.slice(q + 1);
  if (pathname.charAt(0) !== '/') return null;
  const first = safeDecode(pathname.split('/')[1] ?? '');

  if (first === 'memory') {
    if (query) return null;
    if (pathname === '/memory' || pathname === '/memory/') return { kind: 'memory', path: 'MEMORY.md', fragment };
    return memoryTarget(normalize(pathname.slice('/memory/'.length)), fragment);
  }
  if (/^\/markdown_reader\.html$/i.test(pathname)) {
    const file = /(?:^|&)file=([^&]*)/.exec(query);
    const value = file ? safeDecode((file[1] ?? '').replace(/\+/g, ' ')) : '';
    if (value.indexOf('memory/') !== 0) return null;
    return memoryTarget(normalize(value.slice('memory/'.length)), fragment);
  }
  if (query || RESERVED.indexOf(first) >= 0) return null;
  let path = normalize(pathname);
  if (path && pathname.charAt(pathname.length - 1) === '/') path = `${path}/index.html`;
  if (!path || !HTML.test(path)) return null;
  if (lan && path.indexOf('visualizations/') !== 0 && !(ctx.isVisual && ctx.isVisual(path))) return null;
  return { kind: 'visual', path };
}

function hostOf(origin: string | null | undefined): string | null {
  if (!origin) return null;
  const m = /^[a-z][a-z0-9+.-]*:\/\/(\[[^\]]+\]|[^/:?#]+)/i.exec(origin.trim());
  return m && m[1] ? m[1].toLowerCase() : null;
}

/** LAN, Tailscale (CGNAT 100.64/10, *.ts.net), loopback and mDNS hosts. */
export function isPrivateHost(host: string): boolean {
  const h = host.toLowerCase();
  if (h === 'localhost' || h === '[::1]' || /\.local$/.test(h) || /\.ts\.net$/.test(h)) return true;
  const m = /^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/.exec(h);
  if (!m) return false;
  const a = Number(m[1]);
  const b = Number(m[2]);
  return a === 10 || a === 127 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 100 && b >= 64 && b <= 127);
}

/** Where `href` goes inside the app, or `null` when it is not an internal link (LNK-1…LNK-4). */
export function resolveInternalLink(href: string, ctx: InternalLinkContext = {}): InternalTarget | null {
  const h = href.trim();
  if (!h || h.charAt(0) === '#' || h.indexOf('//') === 0) return null;
  const scheme = SCHEME.exec(h);
  if (scheme) {
    const proto = (scheme[1] ?? '').toLowerCase();
    if (proto !== 'http' && proto !== 'https') return null;
    const m = /^[a-z]+:\/\/(\[[^\]]+\]|[^/:?#]+)(?::\d+)?([/?#].*)?$/i.exec(h);
    if (!m || !m[1]) return null;
    const host = m[1].toLowerCase();
    const rest = m[2] ?? '/';
    const own = hostOf(ctx.origin ?? null);
    if (own !== null && host === own) return fromRootPath(rest.charAt(0) === '/' ? rest : `/${rest}`, false, ctx);
    if (isPrivateHost(host)) return fromRootPath(rest.charAt(0) === '/' ? rest : `/${rest}`, true, ctx);
    return null;
  }
  const fs = fromFilesystem(h);
  if (fs) return fs;
  if (h.charAt(0) === '/' && !FS_ABSOLUTE.test(h)) return fromRootPath(h, false, ctx);
  return null;
}

/** The app URL of a target (kept in its own module: the app root needs only this, spec 13 §5.4). */
export { internalTargetUrl } from './targetUrl';

// ───────────────────────── LNK-5: auto-linking ─────────────────────────

/**
 * Inline code that is exactly an internal path or URL (`context/public/x/index.html`,
 * `/memory/projects/x.md`, `docs/specs/12-client-protocol.md:40`) becomes a link. Never code
 * with whitespace or glob characters.
 */
export function linkableCode(code: string, ctx: InternalLinkContext = {}): InternalTarget | null {
  const c = code.trim();
  if (!c || /[\s*?{}<>|]/.test(c.replace(/\?file=/, ''))) return null;
  return resolveInternalLink(c, ctx);
}

/** One bare path found in plain text (`start`/`end` are offsets into the text). */
export interface BarePath {
  readonly start: number;
  readonly end: number;
  readonly path: string;
}

/**
 * Bare filesystem paths under `context/public/` (`.html`) or `context/memory/` (`.md`) in plain
 * text. Conservative: `docs/` and URLs are not auto-linked here (GFM already links URLs).
 */
const BARE_PATH = /(^|[\s(["'`,;])((?:~\/|\/)?(?:[\w.@+-]+\/)*context\/(?:public|memory)\/[\w.@+%/-]+?\.(?:html?|md))(?=$|[\s)\]"'`,;:!?]|\.(?:\s|$))/g;

export function findBarePaths(text: string): BarePath[] {
  if (text.indexOf('context/') < 0) return [];
  const out: BarePath[] = [];
  BARE_PATH.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = BARE_PATH.exec(text))) {
    const lead = m[1] ?? '';
    const path = m[2] ?? '';
    const start = m.index + lead.length;
    if (fromFilesystem(path)) out.push({ start, end: start + path.length, path });
  }
  return out;
}
