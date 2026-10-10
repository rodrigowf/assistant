/**
 * The app shell (spec 13 §4.2). One structure, adapted by window size class:
 *
 *   <AppShell data-wc>
 *     {wc !== 'compact' && <AppRail/>}               ＋ New · Chats · Memory · Visuals · Settings
 *     {wc === 'expanded' && listPaneOpen && <ListPane/>}
 *     <main class="workspace">                        ← ALWAYS the same element, same tree position
 *       <WorkspaceTopBar/> <PanelHost/>
 *     </main>
 *     {wc === 'medium' && <ListPaneOverlay/>}
 *     {wc === 'compact' && <AppDrawer/>}
 *     <ScreenLayer/>                                  screens above the workspace
 *     <VoiceOverlayHost/>                             floating voice controls during a call (not on its Archie view)
 *     <SessionSwitcherSheet/> <ShellDialogs/> <AppSnackbars/> <div id="overlay-root"/>
 *
 * **Stable tree position (§4.4, must-preserve inv02 §7 #1):** the conditional siblings before
 * `<main>` render `false` placeholders, so `<main>` and `PanelHost` keep their parent and index in
 * every window class. Rotating the iPad or resizing the window never remounts a panel.
 */
import { useEffect, useRef } from 'react';
import { ensureMemoryTree } from '@/services';
import { dismissSnackbar, usePrefs, useSnackbar } from '@/stores';
import { OVERLAY_ROOT_ID, SnackbarHost } from '@/ui/overlays';
import { useAppKeyboard } from './keyboard/useAppKeyboard';
import { navigate, useRoute } from './navigation/route';
import { openDocument } from './shell/actions';
import { AppDrawer } from './shell/AppDrawer';
import { AppRail, useRailValue } from './shell/AppRail';
import { ListPane, ListPaneOverlay } from './shell/ListPane';
import { ScreenLayer, useScreens } from './shell/ScreenLayer';
import { closeShellOverlays } from './shell/shellState';
import { VoiceOverlayHost } from './shell/VoiceOverlayHost';
import { useWindowClass, type WindowClass } from './useWindowClass';
import { PanelHost } from './workspace/PanelHost';
import { SessionSwitcherSheet } from './workspace/SessionSwitcherSheet';
import { ShellDialogs } from './workspace/ShellDialogs';
import { WorkspaceTopBar } from './workspace/WorkspaceTopBar';
import styles from './shell/shell.module.css';

function AppSnackbars() {
  const head = useSnackbar((s) => s.queue[0] ?? null);
  return (
    <SnackbarHost
      snackbar={
        head
          ? {
              id: head.id,
              message: head.message,
              duration: head.durationMs > 0 ? head.durationMs : null,
              ...(head.action ? { action: { label: head.action.label, onAction: head.action.run } } : {}),
            }
          : null
      }
      onDismiss={(id) => {
        dismissSnackbar(Number(id));
      }}
    />
  );
}

/** Size-class transitions: close the overlays that do not exist in the new class. */
function useWindowClassTransitions(wc: WindowClass): void {
  useEffect(() => {
    closeShellOverlays();
  }, [wc]);
}

/**
 * Medium/expanded have no document screens: a `#/memory/<path>` or `#/visuals/<path>` link (or a
 * rotation from compact with a document screen open) opens the document as a tab instead.
 */
function useDocumentRoutesAsTabs(wc: WindowClass): void {
  const route = useRoute();
  useEffect(() => {
    if (wc === 'compact') return;
    if (route.name === 'memory' && route.path) {
      openDocument('memory', route.path, { compact: false });
      navigate({ name: 'memory', path: null });
    } else if (route.name === 'visuals' && route.path) {
      openDocument('visual', route.path, { compact: false });
      navigate({ name: 'visuals', path: null });
    }
  }, [wc, route]);
}

/** Memory is pull-only (spec 12 §9.2): load the tree the first time the Memory section opens. */
function useMemoryOnOpen(): void {
  const dest = useRailValue();
  useEffect(() => {
    if (dest === 'memory') void ensureMemoryTree();
  }, [dest]);
}

export function AppShell() {
  const wc = useWindowClass();
  const collapsed = usePrefs((p) => p.listPaneCollapsed);
  const screens = useScreens(wc);
  const covered = screens.length > 0;
  const workspaceRef = useRef<HTMLElement>(null);
  useAppKeyboard();
  useWindowClassTransitions(wc);
  useDocumentRoutesAsTabs(wc);
  useMemoryOnOpen();

  // Beside the rail (Settings on medium/expanded) `inert` keeps the covered panes out of the tab
  // order and the accessibility tree. Compact screens are modal layers (hideOthers + focus trap),
  // which also covers Safari 12, where `inert` is ignored. No React-managed aria-hidden here: it
  // would fight hideOthers' reference counting.
  const coveredProps = covered && wc !== 'compact' ? ({ inert: '' } as Record<string, unknown>) : {};

  return (
    <div className={styles.shell} data-wc={wc}>
      {wc !== 'compact' ? <AppRail wc={wc} /> : false}
      {wc === 'expanded' && !collapsed ? (
        <div className={styles.listSlot} {...coveredProps}>
          <ListPane />
        </div>
      ) : (
        false
      )}
      <main ref={workspaceRef} className={styles.workspace} aria-label="Workspace" {...coveredProps}>
        <WorkspaceTopBar wc={wc} />
        <PanelHost />
      </main>
      {wc === 'medium' ? <ListPaneOverlay /> : false}
      {wc === 'compact' ? <AppDrawer /> : false}
      <ScreenLayer wc={wc} screens={screens} />
      <VoiceOverlayHost wc={wc} covered={covered} screenKey={screens.map((s) => s.key).join(',')} workspaceRef={workspaceRef} />
      {wc === 'compact' ? <SessionSwitcherSheet /> : false}
      <ShellDialogs />
      <AppSnackbars />
      <div id={OVERLAY_ROOT_ID} />
    </div>
  );
}
