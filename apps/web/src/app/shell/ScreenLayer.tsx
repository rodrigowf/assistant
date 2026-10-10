/**
 * Screen layer (spec 13 §4.2, §4.4): full screens stacked ABOVE the workspace, which stays
 * mounted beneath (aria-hidden + inert while covered). Compact: History, Memory, Visuals,
 * Settings and the document detail screens (back arrow). Medium/expanded: Settings, beside the
 * rail. Each screen is a layer of the shared overlay stack with a history entry, so the browser
 * Back button / Android back gesture closes the topmost screen first (§4.5) and Escape closes it.
 */
import { useRef, type ReactNode } from 'react';
import { catalogStore, findTab, useCatalog } from '@/stores';
import { useOverlayLayer } from '@/ui/a11y';
import { IconButton } from '@/ui/controls';
import { TopAppBar } from '@/ui/navigation';
import { ScrollArea } from '@/ui/primitives';
import { navigate, useRoute, type Route } from '../navigation/route';
import { HistoryPane, MemoryDocument, MemoryPane, SettingsScreen, VisualsPane, VisualViewer, VOICE_OVERLAY_SELECTOR } from '../slots';
import type { WindowClass } from '../useWindowClass';
import styles from './shell.module.css';

interface ScreenSpec {
  key: string;
  title: string;
  /** Where Back goes. */
  parent: Route;
  body: ReactNode;
  /** The body brings its own scroller (documents, iframes). */
  fill?: boolean;
}

function basename(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** The stack of screens for a route at a size class (bottom → top). */
export function screensFor(route: Route, wc: WindowClass): ScreenSpec[] {
  const ws: Route = { name: 'workspace' };
  if (route.name === 'settings') return [{ key: 'settings', title: 'Settings', parent: ws, body: <SettingsScreen page={route.page} /> }];
  if (wc !== 'compact') return [];
  switch (route.name) {
    case 'history':
      return [{ key: 'history', title: 'History', parent: ws, body: <HistoryPane variant="pane" /> }];
    case 'memory': {
      const list: ScreenSpec = { key: 'memory', title: 'Memory', parent: ws, body: <MemoryPane /> };
      if (!route.path) return [list];
      return [
        list,
        {
          key: `memory:${route.path}`,
          title: basename(route.path),
          parent: { name: 'memory', path: null },
          body: <MemoryDocument path={route.path} hidden={false} />,
          fill: true,
        },
      ];
    }
    case 'visuals': {
      const list: ScreenSpec = { key: 'visuals', title: 'Visuals', parent: ws, body: <VisualsPane /> };
      if (!route.path) return [list];
      const tab = findTab(`viz:${route.path}`);
      return [
        list,
        {
          key: `visuals:${route.path}`,
          // W-14: the visual's list title (mockup h), as the tab strip shows it on larger windows.
          title: tab?.titleHint ?? catalogStore.getState().visuals.items.find((v) => v.path === route.path)?.title ?? basename(route.path),
          parent: { name: 'visuals', path: null },
          body: <VisualViewer path={route.path} url={tab?.url} hidden={false} />,
          fill: true,
        },
      ];
    }
    default:
      return [];
  }
}

function Screen({ spec, compact, top }: { spec: ScreenSpec; compact: boolean; top: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  useOverlayLayer({
    open: true,
    onClose: () => {
      navigate(spec.parent);
    },
    containerRef: ref,
    history: true,
    // Compact screens cover everything: modal (background aria-hidden, focus trap, scroll lock).
    // Beside the rail the rail stays usable; the AppShell makes the covered panes `inert`.
    modal: compact && top,
    // The floating voice controls stay usable over a compact screen (not over its dialogs).
    ...(compact && top ? { keepExposed: VOICE_OVERLAY_SELECTOR } : null),
    initialFocus: back,
  });
  return (
    <section
      ref={ref}
      className={compact ? `${styles.screen} ${styles.screenCompact}` : styles.screen}
      aria-label={spec.title}
      data-screen={spec.key}
      tabIndex={-1}
    >
      <TopAppBar
        variant="plain"
        title={spec.title}
        leading={
          <IconButton
            ref={back}
            icon="arrow_back"
            aria-label="Back"
            onClick={() => {
              navigate(spec.parent);
            }}
          />
        }
      />
      {spec.fill ? <div className={styles.screenFill}>{spec.body}</div> : <ScrollArea className={styles.screenBody}>{spec.body}</ScrollArea>}
    </section>
  );
}

export function useScreens(wc: WindowClass): ScreenSpec[] {
  const route = useRoute();
  useCatalog((c) => c.visuals); // W-14: visual screen titles follow the list (loads, renames)
  return screensFor(route, wc);
}

export function ScreenLayer({ wc, screens }: { wc: WindowClass; screens: ScreenSpec[] }) {
  if (!screens.length) return null;
  return (
    <div className={wc === 'compact' ? `${styles.screenLayer} ${styles.screenLayerCompact}` : styles.screenLayer}>
      {screens.map((s, i) => (
        <Screen key={s.key} spec={s} compact={wc === 'compact'} top={i === screens.length - 1} />
      ))}
    </div>
  );
}
