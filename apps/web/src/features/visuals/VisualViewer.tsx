/**
 * Visual viewer (spec 13 §3.6 `features/visuals`, mockups phone (h); inv02 F-36 VizPanel).
 *
 * - **[LOAD-BEARING]** inv02 F-36 (frontend/src/components/VizPanel.tsx:60): the iframe sandbox
 *   is exactly `allow-scripts allow-same-origin allow-popups allow-forms allow-modals`.
 *   Same-origin lets a visualization fetch the backend and use storage; leaving out
 *   `allow-top-navigation` and `allow-downloads` stops a framed page from navigating the app away
 *   or starting downloads (VZ-3).
 * - **[LOAD-BEARING]** inv02 F-36 (VizPanel.tsx:15-17): Reload bumps the iframe's React `key`,
 *   i.e. remounts it, which reloads across origins and history states (VZ-4).
 * - **[LOAD-BEARING]** inv02 F-36 / spec 13 §4.4: the iframe stays mounted while the panel is
 *   hidden (`hidden` never unmounts it), so interactive visualizations keep their state.
 * - Toolbar (mockup h): "Updated 2 h ago · folder", **Show on TV** (only when the BX-2 probe says
 *   available — hidden, not disabled, spec 13 §3.7), ⋮ Reload / Open in browser / Copy link.
 * - Live reload (spec 12 §9.3, VZ-6): when the content watcher reports this page or one of its
 *   assets changed, the frame remounts once per burst (debounced) and a short "Updated" cue
 *   shows. A hidden view waits until it is shown again (its state is not thrown away for a page
 *   nobody is looking at, and the scroll position can be read back). A same-origin page gets its scroll position back after the reload
 *   (re-applied briefly while scripts build the page); a deleted page is left as it is.
 */
import { useEffect, useRef, useState } from 'react';
import { parseServerTime } from '@/features/history';
import { copyText, formatRelativeTime } from '@/platform';
import { showSnackbar, useCapabilities, useCatalog, useContentStamp } from '@/stores';
import { Button, IconButton } from '@/ui/controls';
import { Menu, MenuItem } from '@/ui/overlays';
import { findVisual, showOnTv, vizFolder, vizHref } from './viz';
import styles from './visuals.module.css';

/** Changes closer together than this reload once (VZ-6). */
export const LIVE_RELOAD_DEBOUNCE_MS = 300;
/** How long the "Updated" cue stays. */
export const UPDATED_CUE_MS = 2500;
/** After a live reload, the saved scroll position is re-applied at these delays after `load`. */
const SCROLL_RESTORE_DELAYS_MS = [0, 150, 600];

interface ScrollPos {
  readonly x: number;
  readonly y: number;
}

/** The frame's scroll position, or null for a cross-origin page (dev `VITE_VIZ_ORIGIN`). */
function readScroll(frame: HTMLIFrameElement | null): ScrollPos | null {
  try {
    const w = frame?.contentWindow;
    if (!w) return null;
    return { x: w.scrollX || w.pageXOffset || 0, y: w.scrollY || w.pageYOffset || 0 };
  } catch {
    return null;
  }
}

function restoreScroll(frame: HTMLIFrameElement | null, pos: ScrollPos, timers: ReturnType<typeof setTimeout>[]): void {
  for (const ms of SCROLL_RESTORE_DELAYS_MS) {
    timers.push(setTimeout(() => {
      try {
        const w = frame?.contentWindow;
        // The user scrolled meanwhile, or it already holds: leave it.
        if (w && w.scrollY < pos.y && w.scrollX <= pos.x + 1) w.scrollTo(pos.x, pos.y);
      } catch {
        // cross-origin: nothing to restore
      }
    }, ms));
  }
}

/** The exact sandbox tokens (F-36, VZ-3). Exported for tests. */
export const VIZ_SANDBOX = 'allow-scripts allow-same-origin allow-popups allow-forms allow-modals';

export interface VisualViewerProps {
  /** Path relative to context/public/ (the identity). */
  readonly path: string;
  /** The list's `url` (unencoded); falls back to the list entry, then `/<path>`. */
  readonly url: string | undefined;
  /** The panel is hidden (a background tab): the iframe stays mounted. */
  readonly hidden: boolean;
}

export function VisualViewer({ path, url, hidden }: VisualViewerProps) {
  const item = useCatalog((s) => findVisual(s.visuals.items, path));
  const castAvailable = useCapabilities((s) => s.castAvailable);
  const [reloadKey, setReloadKey] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [casting, setCasting] = useState(false);
  const [cue, setCue] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const pendingScroll = useRef<ScrollPos | null>(null);
  const scrollTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const stamp = useContentStamp('visuals', path);
  // Changes from before this viewer mounted are already in what it loads.
  const handledVersion = useRef(stamp ? stamp.version : 0);
  const version = stamp ? stamp.version : 0;
  const deleted = !!stamp && stamp.deleted;

  useEffect(() => {
    const timers = scrollTimers.current;
    return () => {
      for (const t of timers.splice(0)) clearTimeout(t);
    };
  }, []);

  useEffect(() => {
    if (version <= handledVersion.current || deleted || hidden) return undefined;
    const t = setTimeout(() => {
      handledVersion.current = version;
      pendingScroll.current = readScroll(frameRef.current);
      setReloadKey((k) => k + 1);
      setCue(true);
    }, LIVE_RELOAD_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [version, deleted, hidden]);

  useEffect(() => {
    if (!cue) return undefined;
    const t = setTimeout(() => {
      setCue(false);
    }, UPDATED_CUE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [cue, reloadKey]);

  const href = vizHref(path, url ?? item?.url);
  const title = item?.title ?? path;
  const modified = item ? parseServerTime(item.modified) : NaN;
  const meta = [deleted ? 'Deleted' : isFinite(modified) ? `Updated ${formatRelativeTime(modified)}` : null, vizFolder(path)].filter(Boolean).join(' · ');

  const reload = (): void => {
    setReloadKey((k) => k + 1);
  };

  return (
    <section className={styles.viewer} aria-label={title} data-hidden={hidden ? '' : undefined}>
      <div className={styles.viewerBar}>
        <span className={styles.viewerMeta} title={href}>
          {meta}
        </span>
        {cue ? (
          <span className={styles.updatedCue} role="status">
            Updated
          </span>
        ) : null}
        {castAvailable ? (
          <Button
            variant="tonal"
            size="small"
            icon="cast"
            loading={casting}
            onClick={() => {
              setCasting(true);
              void showOnTv(path, title).finally(() => {
                setCasting(false);
              });
            }}
          >
            Show on TV
          </Button>
        ) : null}
        <IconButton
          ref={menuRef}
          icon="more_vert"
          size="small"
          aria-label="Visual menu"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => {
            setMenuOpen((o) => !o);
          }}
        />
        <Menu
          open={menuOpen}
          onClose={() => {
            setMenuOpen(false);
          }}
          anchor={menuRef}
          placement="bottom-end"
          minWidth={210}
          aria-label="Visual menu"
        >
          <MenuItem icon="refresh" onSelect={reload}>
            Reload
          </MenuItem>
          <MenuItem
            icon="open_in_new"
            onSelect={() => {
              window.open(href, '_blank', 'noopener,noreferrer');
            }}
          >
            Open in browser
          </MenuItem>
          <MenuItem
            icon="link"
            onSelect={() => {
              void copyText(href).then((ok) => {
                showSnackbar(ok ? 'Link copied' : 'Could not copy the link', ok ? {} : { tone: 'error' });
              });
            }}
          >
            Copy link
          </MenuItem>
        </Menu>
      </div>
      <div className={styles.frameWrap}>
        <iframe
          key={reloadKey}
          ref={frameRef}
          className={styles.frame}
          title={title}
          src={href}
          sandbox={VIZ_SANDBOX}
          data-reload={reloadKey}
          onLoad={() => {
            const pos = pendingScroll.current;
            pendingScroll.current = null;
            if (pos && (pos.x || pos.y)) restoreScroll(frameRef.current, pos, scrollTimers.current);
          }}
        />
      </div>
    </section>
  );
}
