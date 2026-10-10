/**
 * The Settings screen body (lazy chunk). Expanded (≥ 840 px): two panes, the grouped list beside
 * the selected page. Compact / medium: the list, and a page is pushed over it with its own app bar
 * and a history entry (Back / Escape return to the list; IA §7, spec 13 §4.5).
 *
 * The pushed page renders into the shell's Settings screen element (`[data-screen]`, W-07's
 * ScreenLayer) so it covers that screen's "Settings" app bar; without one (gallery, tests) it
 * renders in place.
 */
import { useEffect, useLayoutEffect, useRef, useState, type ComponentType, type ReactNode } from 'react';
import { mediaMatches, watchMedia } from '@/platform';
import { useOverlayLayer } from '@/ui/a11y';
import { IconButton } from '@/ui/controls';
import { TopAppBar } from '@/ui/navigation';
import { Portal } from '@/ui/overlays';
import { cx, ScrollArea, typeClass } from '@/ui/primitives';
import { backendHost, checkAuth } from '@/features/auth';
import { useSettingsRefresh } from './controller';
import { DEFAULT_PAGE, findSettingsPage, type SettingsPageDef, type SettingsPageId } from './pages';
import { ScopeChip, useFieldId } from './parts';
import { SettingsHome } from './SettingsHome';
import { AccountsPage } from './pages/AccountsPage';
import { AboutPage, AppearancePage, NotificationsPage } from './pages/DevicePages';
import { McpServersPage } from './pages/McpServersPage';
import { AgentSessionsPage, ConversationModelPage } from './pages/ModelPages';
import { VoicePage, VoiceTuningPage } from './pages/VoicePages';
import { WorkingDirectoriesPage } from './pages/WorkingDirectoriesPage';
import styles from './settings.module.css';

export const PAGE_COMPONENTS: Record<SettingsPageId, ComponentType> = {
  appearance: AppearancePage,
  notifications: NotificationsPage,
  'conversation-model': ConversationModelPage,
  voice: VoicePage,
  'voice-tuning': VoiceTuningPage,
  'agent-sessions': AgentSessionsPage,
  'working-directories': WorkingDirectoriesPage,
  'mcp-servers': McpServersPage,
  account: AccountsPage,
  about: AboutPage,
};

export const TWO_PANE_QUERY = '(min-width: 840px)';

function useTwoPane(): boolean {
  const [wide, setWide] = useState(() => mediaMatches(TWO_PANE_QUERY));
  useLayoutEffect(() => {
    const update = (): void => setWide(mediaMatches(TWO_PANE_QUERY));
    update();
    return watchMedia(TWO_PANE_QUERY, update);
  }, []);
  return wide;
}

/** "Chrome on Linux", "Safari on iPad"… for the scope chip. */
export function deviceName(ua: string = typeof navigator !== 'undefined' ? navigator.userAgent : ''): string {
  const os = /iPad/.test(ua)
    ? 'iPad'
    : /iPhone/.test(ua)
      ? 'iPhone'
      : /Android/.test(ua)
        ? 'Android'
        : /Mac OS X/.test(ua)
          ? 'Mac'
          : /Windows/.test(ua)
            ? 'Windows'
            : /Linux/.test(ua)
              ? 'Linux'
              : '';
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : '';
  if (browser && os) return `${browser} on ${os}`;
  return browser || os || 'this browser';
}

function PageScope({ def }: { def: SettingsPageDef }) {
  if (def.group === 'about') return null;
  return def.group === 'device' ? <ScopeChip scope="device" name={deviceName()} /> : <ScopeChip scope="server" name={backendHost()} />;
}

export interface SettingsViewProps {
  page?: string | null;
  /** Navigate to a page (null = the list). Wired to the app's route by the slot registry. */
  onNavigate: (page: SettingsPageId | null) => void;
  /** Refetch config + catalogs on mount (default true; CFG-3). */
  load?: boolean;
  /** Force a layout (gallery, tests); default by window width. */
  layout?: 'two-pane' | 'pushed';
}

export default function SettingsView({ page, onNavigate, load = true, layout }: SettingsViewProps) {
  useSettingsRefresh(load);
  useEffect(() => {
    if (load) void checkAuth(); // the Accounts row's value until the page has loaded every service
  }, [load]);
  const wide = useTwoPane();
  const mode = layout ?? (wide ? 'two-pane' : 'pushed');
  const def = findSettingsPage(page);
  if (mode === 'two-pane') {
    const shown = def ?? (findSettingsPage(DEFAULT_PAGE) as SettingsPageDef);
    return (
      <div className={styles.twoPane} data-settings-layout="two-pane">
        <div className={styles.listPane}>
          <SettingsHome selected={shown.id} onOpen={(id) => onNavigate(id)} />
        </div>
        <DetailPane def={shown} />
      </div>
    );
  }
  return (
    <div data-settings-layout="pushed">
      <HomeUnderneath covered={!!def}>
        <SettingsHome onOpen={(id) => onNavigate(id)} />
      </HomeUnderneath>
      {def ? <PushedPage key={def.id} def={def} onBack={() => onNavigate(null)} /> : null}
    </div>
  );
}

function DetailPane({ def }: { def: SettingsPageDef }) {
  const titleId = useFieldId('st');
  const Page = PAGE_COMPONENTS[def.id];
  return (
    <section className={styles.detailPane} aria-labelledby={titleId} data-settings-page={def.id}>
      <header className={styles.detailHeader}>
        <h2 id={titleId} className={cx(typeClass('headline-small'), styles.detailTitle)}>
          {def.title}
        </h2>
        <PageScope def={def} />
      </header>
      <Page />
    </section>
  );
}

/** The list stays mounted under a pushed page, hidden from AT and focus (inert where supported). */
function HomeUnderneath({ covered, children }: { covered: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (covered) el.setAttribute('inert', '');
    else el.removeAttribute('inert');
  }, [covered]);
  return (
    <div ref={ref} aria-hidden={covered || undefined}>
      {children}
    </div>
  );
}

function PushedPage({ def, onBack }: { def: SettingsPageDef; onBack: () => void }) {
  const anchor = useRef<HTMLSpanElement>(null);
  const [host, setHost] = useState<HTMLElement | null | undefined>(undefined);
  useLayoutEffect(() => {
    const el = anchor.current?.parentElement;
    setHost(el ? (el.closest('[data-screen]') as HTMLElement | null) : null);
  }, []);
  return (
    <>
      <span ref={anchor} hidden />
      {host === undefined ? null : host ? (
        <Portal container={host}>
          <PageSurface def={def} onBack={onBack} />
        </Portal>
      ) : (
        <PageSurface def={def} onBack={onBack} inline />
      )}
    </>
  );
}

function PageSurface({ def, onBack, inline = false }: { def: SettingsPageDef; onBack: () => void; inline?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  useOverlayLayer({ open: true, onClose: onBack, containerRef: ref, history: true });
  useEffect(() => {
    back.current?.focus();
  }, []);
  const Page = PAGE_COMPONENTS[def.id];
  return (
    <section
      ref={ref}
      className={cx(styles.pushed, inline && styles.pushedInline)}
      aria-label={def.title}
      data-settings-page={def.id}
      tabIndex={-1}
    >
      <TopAppBar variant="plain" title={def.title} leading={<IconButton ref={back} icon="arrow_back" aria-label="Back to Settings" onClick={onBack} />} />
      <ScrollArea className={styles.pushedBody}>
        <div className={styles.pushedContent}>
          <div className={styles.pushedScope}>
            <PageScope def={def} />
          </div>
          <Page />
        </div>
      </ScrollArea>
    </section>
  );
}
