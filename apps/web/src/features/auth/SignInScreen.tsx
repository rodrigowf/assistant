/**
 * The full-screen sign-in surface the AuthGate shows when the backend's Claude CLI is signed out
 * (spec 13 §3.9, inv02 §1.11). A lazy chunk of the gate; the gallery renders it inline.
 */
import { useRef } from 'react';
import { useOverlayLayer } from '@/ui/a11y';
import { Button } from '@/ui/controls';
import { Icon, ScrollArea } from '@/ui/primitives';
import { AuthPanel } from './AuthPanel';
import { backendHost, checkAuth, dismissGate, useAuth } from './authStore';
import styles from './auth.module.css';

/** The full-screen sign-in surface (also rendered inline by the gallery). */
export function SignInScreen({ inline = false }: { inline?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const phase = useAuth((s) => s.phase);
  useOverlayLayer({ open: !inline, onClose: () => undefined, containerRef: ref, modal: true, escape: false, initialFocus: heading });
  const host = backendHost();
  return (
    <div
      ref={ref}
      className={inline ? `${styles.gate} ${styles.gateInline}` : styles.gate}
      role="dialog"
      aria-modal={inline ? undefined : true}
      aria-labelledby="auth-gate-title"
    >
      <ScrollArea className={styles.gateScroll}>
        <div className={styles.gateCard}>
          <Icon name="account_circle" size={40} className={styles.gateIcon} />
          <h1 id="auth-gate-title" ref={heading} tabIndex={-1} className={styles.gateTitle}>
            Sign in to Claude
          </h1>
          <p className={styles.gateText}>
            Agent sessions run Claude Code on <b>{host}</b>, and it isn&apos;t signed in yet.
          </p>
          <AuthPanel host={host} />
          <div className={styles.gateFooter}>
            <Button variant="text" icon="refresh" disabled={phase !== 'idle'} onClick={() => void checkAuth()}>
              Check again
            </Button>
            <Button variant="text" disabled={phase === 'signing-in' || phase === 'saving'} onClick={dismissGate}>
              Not now
            </Button>
          </div>
        </div>
      </ScrollArea>
    </div>
  );
}
