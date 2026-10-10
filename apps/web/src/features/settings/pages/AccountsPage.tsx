/**
 * Archie (server) → Accounts (spec 12 §8.1 "Accounts"): the sign-in of every service the server
 * uses — agent harnesses, voice / AI APIs, other credentials — through every method each one
 * offers (sign-in link with a pasted-back code or a device code, credentials file paste, API key,
 * sign out), then the `context/.env` key manager. Status is refetched on open (CFG-3); an active
 * sign-in is polled every 2 s while the page is open.
 *
 * The first-run AuthGate (Claude only, `/api/auth/*`) is separate and unchanged; a Claude change
 * made here re-checks it.
 */
import { useEffect } from 'react';
import { Button } from '@/ui/controls';
import { anyFlowActive, groupServices } from '../accounts/logic';
import { loadAccounts, pollLogin, useAccounts, accountsStore } from '../accounts/accountsStore';
import { EnvKeysSection } from '../accounts/EnvKeys';
import { ServiceCard } from '../accounts/ServiceCard';
import { Loading, Notice } from '../parts';
import accountStyles from '../accounts/accounts.module.css';
import styles from '../settings.module.css';

export const FLOW_POLL_MS = 2000;

function useFlowPolling(active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => {
      for (const s of accountsStore.getState().services ?? []) void pollLogin(s.id);
    }, FLOW_POLL_MS);
    return () => clearInterval(timer);
  }, [active]);
}

export function AccountsPage() {
  const services = useAccounts((s) => s.services);
  const loading = useAccounts((s) => s.loading);
  const loadError = useAccounts((s) => s.loadError);
  useEffect(() => {
    void loadAccounts();
  }, []);
  useFlowPolling(anyFlowActive(services));
  return (
    <>
      {loadError ? (
        <Notice
          tone="error"
          title="Couldn't load the accounts"
          action={
            <Button variant="tonal" icon="refresh" onClick={() => void loadAccounts()}>
              Retry
            </Button>
          }
        >
          {loadError}
        </Notice>
      ) : null}
      {services ? (
        groupServices(services).map((g) => (
          <div key={g.id} className={styles.stackBlock} data-account-group={g.id}>
            <h3 className={styles.stackLabel}>{g.title}</h3>
            {g.help ? <p className={accountStyles.sectionHelp}>{g.help}</p> : null}
            <div className={accountStyles.cards}>
              {g.services.map((s) => (
                <ServiceCard key={s.id} service={s} />
              ))}
            </div>
          </div>
        ))
      ) : !loadError ? (
        <Loading label={loading ? 'Checking every account on the server…' : 'Loading…'} />
      ) : null}
      <div className={styles.stackBlock}>
        <h3 className={styles.stackLabel}>Environment keys</h3>
        <EnvKeysSection />
      </div>
    </>
  );
}
