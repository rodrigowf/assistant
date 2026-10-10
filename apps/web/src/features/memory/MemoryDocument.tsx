/**
 * Memory document (spec 13 §3.6, mockups phone (g) document; inv02 F-37 MemoryPanel).
 *
 * - **[LOAD-BEARING]** inv02 F-37 (frontend/src/components/MemoryPanel.tsx:88-100): the leading
 *   YAML frontmatter is split off and shown verbatim, folded into one chip
 *   ("Frontmatter · architecture · 4 refs") that expands in place; rendered as markdown it would
 *   be a run-on paragraph between two rules. The body renders with the chat's Markdown.
 * - Relative links resolve against **this file's folder** (MEM-2, fixes inv02 §6.2: they resolved
 *   against the app root) and open the other file in the app; links escaping the memory root and
 *   absolute URLs open externally. In-page `#anchor` links scroll inside the document (the app
 *   routes on the URL hash, so they must not change it).
 * - The file is fetched with every path segment URL-encoded (MEM-3, fixes `fetch("/memory/" +
 *   path)` in frontend/src/api/rest.ts:58). Reload keeps the old text visible until the new one
 *   arrives (inv02 F-37); "Open raw" opens `/memory/<path>` in a new tab.
 * - Live (spec 12 §9.3): a `memory_changed` for this file, or the socket coming back after a drop
 *   (VZ-6), refetches it the same way, so the scroll position stays; the "Updated" cue shows only
 *   when the text actually changed.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { api, errorMessage, httpUrl } from '@/services';
import { createMemoryLinkResolver, Markdown, MarkdownPrefsProvider, memoryFileUrl, splitFrontmatter, summarizeFrontmatter } from '@/features/markdown';
import { Button, Disclosure, IconButton } from '@/ui/controls';
import { ScrollArea, Spinner, type ScrollAreaHandle } from '@/ui/primitives';
import { parseServerTime } from '@/features/history';
import { formatRelativeTime } from '@/platform';
import { useContentResyncEpoch, useContentStamp, usePrefs } from '@/stores';
import { fileName, folderCrumb } from './tree';
import styles from './memory.module.css';

export interface MemoryDocumentProps {
  /** Path relative to context/memory/ (POSIX). */
  readonly path: string;
  /** The panel is hidden (a background tab). The document keeps its state. */
  readonly hidden: boolean;
  /** Open another memory file (a relative link). Without it, links open the raw file externally. */
  readonly onOpenLink?: (path: string) => void;
}

interface Loaded {
  readonly path: string;
  readonly tick: number;
  readonly text: string | null;
  readonly error: string | null;
}

interface DocState {
  readonly text: string | null;
  readonly error: string | null;
  readonly loading: boolean;
}

/** "Frontmatter · architecture · 4 refs" (mockup g2). */
export function frontmatterChipLabel(frontmatter: string): string {
  const { fields, listLengths } = summarizeFrontmatter(frontmatter);
  const parts = ['Frontmatter'];
  const category = fields.category ?? fields.type;
  // `assistant/architecture` → `architecture` (the chip is one short line; the full text is inside).
  if (category) parts.push(category.split('/').filter(Boolean).pop() ?? category);
  const refs = listLengths.references;
  if (refs !== undefined && refs > 0) parts.push(`${refs} ${refs === 1 ? 'ref' : 'refs'}`);
  return parts.join(' · ');
}

/** "Modified 2 days ago" from the frontmatter's `modified` (a local date or a timestamp). */
export function modifiedLine(frontmatter: string | null, now: number = Date.now()): string | null {
  if (!frontmatter) return null;
  const raw = summarizeFrontmatter(frontmatter).fields.modified;
  if (!raw) return null;
  const t = parseServerTime(raw);
  if (!isFinite(t)) return null;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw.trim());
  if (dateOnly) {
    const today = new Date(now);
    const d = new Date(t);
    const days = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime() - t) / 86_400_000);
    if (days <= 0) return 'Modified today';
    if (days === 1) return 'Modified yesterday';
    if (days < 7) return `Modified ${days} days ago`;
    return `Modified ${formatRelativeTime(d, now)}`;
  }
  return `Modified ${formatRelativeTime(t, now)}`;
}

/** A leading `# Title` line, split off so the modified line can sit under it. */
export function splitTitle(body: string): { heading: string | null; rest: string } {
  const m = /^\s*(#[ \t][^\n]*)(\r?\n|$)/.exec(body);
  return m ? { heading: m[1] ?? null, rest: body.slice(m[0].length) } : { heading: null, rest: body };
}

/** GitHub-style heading slug (for `#anchor` links). */
export function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .replace(/\s+/g, '-');
}

/** Changes closer together than this refetch once. */
export const LIVE_REFETCH_DEBOUNCE_MS = 300;
const UPDATED_CUE_MS = 2500;

export function MemoryDocument({ path, hidden, onOpenLink }: MemoryDocumentProps) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [cue, setCue] = useState(false);
  const stamp = useContentStamp('memory', path);
  const epoch = useContentResyncEpoch();
  const liveKey = `${stamp ? stamp.version : 0}:${epoch}`;
  const deleted = !!stamp && stamp.deleted;
  // What this view already reflects (a change before it opened is in the first fetch).
  const handled = useRef(liveKey);
  /** The reload tick a live change started, and the text it replaces (for the cue). */
  const liveTick = useRef<{ tick: number; text: string | null } | null>(null);
  const lastText = useRef<string | null>(null);
  const tickNow = useRef(reloadTick);

  useEffect(() => {
    tickNow.current = reloadTick;
  }, [reloadTick]);

  useEffect(() => {
    handled.current = liveKey; // another file: start from what it has now
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on a path change
  }, [path]);

  useEffect(() => {
    if (liveKey === handled.current || deleted) return undefined;
    const t = setTimeout(() => {
      handled.current = liveKey;
      liveTick.current = { tick: tickNow.current + 1, text: lastText.current };
      setReloadTick(tickNow.current + 1);
    }, LIVE_REFETCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [liveKey, deleted]);

  useEffect(() => {
    if (!cue) return undefined;
    const t = setTimeout(() => {
      setCue(false);
    }, UPDATED_CUE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [cue]);
  const [fmOpen, setFmOpen] = useState(false);
  const scroller = useRef<ScrollAreaHandle>(null);
  const body = useRef<HTMLDivElement>(null);
  const syntaxHighlight = usePrefs((p) => p.syntaxHighlighting);
  const mdPrefs = useMemo(() => ({ syntaxHighlight }), [syntaxHighlight]);

  // Fetch on open, on a path change and on Reload. The previous text of the same file stays
  // visible until the new one resolves (inv02 F-37); a late answer for an old path is dropped.
  useEffect(() => {
    let live = true;
    api.memory.file(path).then(
      (text) => {
        if (!live) return;
        const body = typeof text === 'string' ? text : '';
        const started = liveTick.current;
        if (started && started.tick === reloadTick) {
          liveTick.current = null;
          if (started.text !== null && started.text !== body) setCue(true);
        }
        lastText.current = body;
        setLoaded({ path, tick: reloadTick, text: body, error: null });
      },
      (err: unknown) => {
        if (live) setLoaded((prev) => ({ path, tick: reloadTick, text: prev && prev.path === path ? prev.text : null, error: errorMessage(err) }));
      },
    );
    return () => {
      live = false;
    };
  }, [path, reloadTick]);

  const load = useCallback(() => {
    setReloadTick((t) => t + 1);
  }, []);
  const mine = loaded && loaded.path === path ? loaded : null;
  const state: DocState = {
    text: mine ? mine.text : null,
    error: mine && mine.tick === reloadTick ? mine.error : null,
    loading: !mine || mine.tick !== reloadTick,
  };

  const split = useMemo(() => (state.text === null ? null : splitFrontmatter(state.text)), [state.text]);
  const resolver = useMemo(() => (onOpenLink ? createMemoryLinkResolver(path, onOpenLink) : undefined), [path, onOpenLink]);
  const crumb = folderCrumb(path);
  const modified = split ? modifiedLine(split.frontmatter) : null;
  // Mockup g2: the "Modified …" line sits under the document's title.
  const titled = useMemo(() => splitTitle(split ? split.body : ''), [split]);

  /** `#anchor` links: scroll to the heading / footnote inside this document. */
  const onClickCapture = (e: MouseEvent<HTMLDivElement>): void => {
    const target = e.target as HTMLElement | null;
    const a = target && typeof target.closest === 'function' ? target.closest('a') : null;
    const href = a ? a.getAttribute('href') : null;
    if (!href || href.charAt(0) !== '#' || e.button !== 0) return;
    e.preventDefault();
    const id = decodeURIComponent(href.slice(1));
    const root = body.current;
    if (!root) return;
    let el: HTMLElement | null = null;
    const byId = root.querySelectorAll<HTMLElement>('[id]');
    for (let i = 0; i < byId.length && !el; i++) {
      const cand = byId[i];
      if (cand && (cand.id === id || cand.id === `user-content-${id}`)) el = cand;
    }
    if (!el) {
      const headings = root.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6');
      for (let i = 0; i < headings.length && !el; i++) {
        const h = headings[i];
        if (h && slugify(h.textContent ?? '') === slugify(id)) el = h;
      }
    }
    const area = scroller.current?.element ?? null;
    if (el && area) area.scrollTop += el.getBoundingClientRect().top - area.getBoundingClientRect().top - 8;
  };

  return (
    <article className={styles.doc} aria-label={fileName(path)} data-hidden={hidden ? '' : undefined}>
      <div className={styles.docBar}>
        <span className={styles.crumb} title={path}>
          {crumb || 'memory'}
        </span>
        {cue ? (
          <span className={styles.updatedCue} role="status">
            Updated
          </span>
        ) : null}
        {state.loading && state.text !== null ? <Spinner size={18} label="Reloading" /> : null}
        <IconButton icon="refresh" size="small" iconSize={20} aria-label="Reload" title="Reload" onClick={load} />
        <IconButton
          icon="open_in_new"
          size="small"
          iconSize={20}
          aria-label="Open raw file in a new tab"
          title="Open raw file"
          onClick={() => {
            window.open(httpUrl(memoryFileUrl(path)), '_blank', 'noopener,noreferrer');
          }}
        />
      </div>
      <ScrollArea ref={scroller} className={styles.docScroll}>
        {/* Capture: `#anchor` links inside the rendered markdown (keyboard activation dispatches click too). */}
        <div ref={body} className={styles.docBody} onClickCapture={onClickCapture}>
          {state.text === null && state.loading ? (
            <p className={styles.note}>
              <Spinner size={18} /> <span className={styles.noteText}>Loading…</span>
            </p>
          ) : null}
          {state.error ? (
            <div className={styles.error} role="alert">
              <span>
                Could not load {path} — {state.error}
              </span>
              <Button variant="text" size="small" onClick={load}>
                Retry
              </Button>
            </div>
          ) : null}
          {split ? (
            <>
              {split.frontmatter !== null ? (
                <Disclosure
                  variant="chip"
                  icon="data_object"
                  summary={frontmatterChipLabel(split.frontmatter)}
                  open={fmOpen}
                  onOpenChange={setFmOpen}
                  className={styles.frontmatter}
                  bodyClassName={styles.fmBody}
                >
                  <pre className={styles.fmPre}>{split.frontmatter}</pre>
                </Disclosure>
              ) : null}
              <MarkdownPrefsProvider value={mdPrefs}>
                {titled.heading !== null ? <Markdown source={titled.heading} linkResolver={resolver} className={styles.prose} /> : null}
                {modified ? <p className={styles.modified}>{modified}</p> : null}
                <Markdown source={titled.rest} linkResolver={resolver} className={styles.prose} />
              </MarkdownPrefsProvider>
            </>
          ) : null}
        </div>
      </ScrollArea>
    </article>
  );
}
