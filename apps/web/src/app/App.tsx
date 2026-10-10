/**
 * App root (W-07, spec 13 §4.2): AuthGate → AppShell, with internal links (spec 12 §9.4) on. Starts the services once (tabs hydrate,
 * watcher socket, pool sync, probes), applies the theme pref, and mirrors navigation to the hash.
 *
 * P-1: nothing here ever closes a session. There is no unload / pagehide handler and unmounting
 * the app does not stop the services; sessions keep running in the backend with zero frontends.
 */
import { useEffect } from 'react';
import { startServices } from '@/services';
import { usePrefs } from '@/stores';
import { applyTheme } from '@/styles';
import { AppShell } from './AppShell';
import { InternalLinksHost } from './internalLinks';
import { installHashSync } from './navigation/route';
import { startTurnNotifications } from './notifications/start';
import { AuthGate } from './slots';

export interface AppProps {
  /** Start the services on mount (default true; component tests drive the stores directly). */
  services?: boolean;
}

export function App({ services = true }: AppProps) {
  const theme = usePrefs((p) => p.theme);
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);
  useEffect(() => installHashSync(), []);
  useEffect(() => {
    if (services) startServices(); // also loads the session and visuals lists (titles)
  }, [services]);
  // "Agent session finished" notices (Settings → Notifications) and their clicks (a lazy chunk
  // fetched now; frames that arrive before it loads are handed over once it is in)
  useEffect(() => (services ? startTurnNotifications() : undefined), [services]);
  return (
    <AuthGate>
      <InternalLinksHost>
        <AppShell />
      </InternalLinksHost>
    </AuthGate>
  );
}
