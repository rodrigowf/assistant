/**
 * Claude CLI sign-in state of the backend (inventory 01 §3.1, inv02 §1.11, spec 12 §8.1). Used by
 * the AuthGate at startup and by Settings → Accounts.
 *
 * Differences from the old gate (fixes): a failed status check is "unknown", not "signed out"
 * (the old app showed the sign-in screen whenever the backend was unreachable); server errors are
 * shown verbatim; status can be re-checked; credentials can be replaced while signed in (the
 * headless check does not look at expiry, so an expired token still reads "signed in").
 *
 * The sign-in actions are in authActions.ts: only the sign-in screen and Settings use them, so they
 * stay out of the initial bundle (spec 13 §5.4).
 */
import { useStore } from 'zustand';
import { createStore } from 'zustand/vanilla';
import { api, errorMessage, getEnv, type AuthStatus, type LoginFlow } from '@/services';
import { sessionStore } from '@/platform';

export type AuthPhase = 'idle' | 'checking' | 'signing-in' | 'saving';

export interface AuthState {
  readonly status: AuthStatus | null;
  readonly phase: AuthPhase;
  /** Verbatim message of the last failed status check (status stays as it was). */
  readonly checkError: string | null;
  /** Message of the last failed sign-in / credentials attempt. */
  readonly actionError: string | null;
  readonly checkedAt: number;
  /** "Not now" on the gate, for this browser tab only. */
  readonly gateDismissed: boolean;
  /** The link sign-in in progress (`/api/accounts/claude/login`, method `token`), or its result. */
  readonly flow: LoginFlow | null;
}

export const GATE_DISMISSED_KEY = 'archie.authGateDismissed';

function initial(): AuthState {
  return {
    status: null,
    phase: 'idle',
    checkError: null,
    actionError: null,
    checkedAt: 0,
    gateDismissed: sessionStore.get(GATE_DISMISSED_KEY) === '1',
    flow: null,
  };
}

export const authStore = createStore<AuthState>(initial);

export function useAuth<T>(selector: (s: AuthState) => T): T {
  return useStore(authStore, selector);
}

export function resetAuth(): void {
  authStore.setState(initial(), true);
}

let inFlight: Promise<AuthStatus | null> | null = null;

/** `GET /api/auth/status`. Never rejects; on failure the previous status is kept. */
export function checkAuth(): Promise<AuthStatus | null> {
  if (inFlight) return inFlight;
  authStore.setState({ phase: 'checking', checkError: null });
  inFlight = api.auth
    .status()
    .then((status) => {
      authStore.setState({ status, phase: 'idle', checkedAt: Date.now() });
      return status;
    })
    .catch((err: unknown) => {
      authStore.setState({ phase: 'idle', checkError: errorMessage(err) });
      return null;
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

export function dismissGate(): void {
  sessionStore.set(GATE_DISMISSED_KEY, '1');
  authStore.setState({ gateDismissed: true });
}

/** One line for the Settings home row (until Accounts has loaded every service). */
export function authSummary(s: Pick<AuthState, 'status' | 'phase' | 'checkError'>): string {
  if (s.phase === 'checking' && !s.status) return 'Claude · checking…';
  if (!s.status) return s.checkError ? "Claude · couldn't check" : 'Claude';
  return s.status.authenticated ? 'Claude · signed in' : 'Claude · not signed in';
}

/** Host of the backend, for copy ("…opens on 192.168.0.200"). */
export function backendHost(): string {
  try {
    const base = getEnv().baseUrl;
    if (base) return new URL(base).hostname;
  } catch {
    // relative or invalid base: fall through
  }
  return typeof location !== 'undefined' && location.hostname ? location.hostname : 'the server';
}
