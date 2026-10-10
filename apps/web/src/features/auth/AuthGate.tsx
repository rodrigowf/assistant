/**
 * `AuthGate{children}` (spec 13 §3.9, inv02 §1.11). Checks `GET /api/auth/status` once at
 * startup. When the backend's Claude CLI is not signed in, the sign-in screen covers the app
 * (modal: the app stays mounted underneath, aria-hidden, so nothing remounts after sign-in).
 *
 * Changed from the old gate: the app renders while the check runs (the non-headless check can
 * take 10 s); a failed check never shows the sign-in screen (the old one did whenever the backend
 * was unreachable); "Not now" dismisses it for this browser tab, because Archie and the Qwen /
 * Gemini harnesses work without Claude credentials. Settings → Accounts has every service's methods.
 */
import { useEffect, type ReactNode } from 'react';
import { Portal } from '@/ui/overlays';
import { checkAuth, useAuth } from './authStore';
import { SignInScreen } from './lazy';

export interface AuthGateProps {
  children: ReactNode;
  /** Run the status check on mount (default true; tests and the gallery drive the store). */
  check?: boolean;
}

export function AuthGate({ children, check = true }: AuthGateProps) {
  const status = useAuth((s) => s.status);
  const dismissed = useAuth((s) => s.gateDismissed);
  useEffect(() => {
    if (check) void checkAuth();
  }, [check]);
  const blocked = !!status && !status.authenticated && !dismissed;
  return (
    <>
      {children}
      {blocked ? (
        <Portal>
          <SignInScreen />
        </Portal>
      ) : null}
    </>
  );
}
