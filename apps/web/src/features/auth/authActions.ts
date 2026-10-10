/**
 * The sign-in actions of the Claude CLI on the backend (inv02 §1.11, spec 12 §8.1): the link
 * sign-in, the legacy blocking login and pasted credentials. State lives in authStore.ts; these
 * run only from the sign-in screen (a lazy chunk) and Settings → Accounts.
 */
import { api, errorMessage, isApiError, type LoginFlow } from '@/services';
import { authStore, checkAuth } from './authStore';

/**
 * Legacy `POST /api/auth/login` (servers without `/api/accounts`, non-headless only): runs
 * `claude setup-token` on the server and blocks until it exits. Resolves to whether the backend
 * is now signed in.
 */
export async function signIn(): Promise<boolean> {
  authStore.setState({ phase: 'signing-in', actionError: null });
  try {
    const status = await api.auth.login();
    authStore.setState({
      status,
      phase: 'idle',
      checkedAt: Date.now(),
      actionError: status.authenticated ? null : "Sign-in didn't finish on the server. Try again, or paste credentials.",
    });
    return status.authenticated;
  } catch (err) {
    authStore.setState({ phase: 'idle', actionError: errorMessage(err) });
    return false;
  }
}

const LINK_METHOD = 'token';
const ACTIVE: readonly string[] = ['starting', 'waiting', 'verifying'];

export function linkFlowActive(flow: LoginFlow | null): boolean {
  return !!flow && ACTIVE.indexOf(flow.status) >= 0;
}

async function afterLink(flow: LoginFlow): Promise<void> {
  if (flow.status === 'succeeded') await checkAuth();
}

/**
 * The link sign-in (works on a headless server): the server runs `claude setup-token`, this page
 * shows its URL, the user signs in on any device and pastes back the code; the 1-year token is
 * saved as CLAUDE_CODE_OAUTH_TOKEN on the server. A server without `/api/accounts` (older backend)
 * falls back to `POST /api/auth/login` when it has a screen.
 */
export async function startLinkSignIn(): Promise<void> {
  authStore.setState({ phase: 'signing-in', actionError: null });
  try {
    const flow = await api.accounts.startLogin('claude', LINK_METHOD);
    authStore.setState({ flow, phase: 'idle', actionError: flow.status === 'failed' ? flow.message : null });
  } catch (err) {
    if (isApiError(err, 404) && authStore.getState().status?.headless === false) {
      authStore.setState({ phase: 'idle' });
      await signIn();
      return;
    }
    authStore.setState({ phase: 'idle', actionError: errorMessage(err) });
  }
}

export async function submitLinkCode(code: string): Promise<void> {
  authStore.setState({ phase: 'signing-in', actionError: null });
  try {
    const flow = await api.accounts.submitCode('claude', code.trim());
    authStore.setState({ flow, phase: 'idle', actionError: flow.status === 'failed' ? flow.message : null });
    await afterLink(flow);
  } catch (err) {
    authStore.setState({ phase: 'idle', actionError: errorMessage(err) });
  }
}

/** One poll while the flow is active (the panel calls it every 2 s). */
export async function pollLinkSignIn(): Promise<void> {
  const before = authStore.getState().flow;
  if (!before || !linkFlowActive(before)) return;
  try {
    const flow = await api.accounts.login('claude');
    if (authStore.getState().flow?.id !== before.id || flow.id !== before.id) return; // superseded
    authStore.setState({ flow });
    if (!linkFlowActive(flow)) await afterLink(flow);
  } catch {
    // transient: keep polling
  }
}

export async function cancelLinkSignIn(): Promise<void> {
  try {
    await api.accounts.cancelLogin('claude');
  } catch {
    // already gone
  }
  authStore.setState({ flow: null, actionError: null });
}

export type CredentialsCheck = { ok: true; json: string } | { ok: false; error: string };

/** Client-side check before sending (mirrors `manager/auth.py`: `claudeAiOauth.accessToken`). */
export function checkCredentialsText(text: string): CredentialsCheck {
  const t = text.trim();
  if (!t) return { ok: false, error: 'Paste the contents of .credentials.json' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch {
    return { ok: false, error: "That isn't valid JSON. Copy the whole file, including the braces." };
  }
  const oauth = parsed && typeof parsed === 'object' ? (parsed as { claudeAiOauth?: unknown }).claudeAiOauth : undefined;
  const token = oauth && typeof oauth === 'object' ? (oauth as { accessToken?: unknown }).accessToken : undefined;
  if (typeof token !== 'string' || !token) return { ok: false, error: 'Invalid credentials: the file has no claudeAiOauth.accessToken.' };
  return { ok: true, json: t };
}

/** `POST /api/auth/credentials`. Resolves to whether the backend accepted them. */
export async function submitCredentials(text: string): Promise<boolean> {
  const check = checkCredentialsText(text);
  if (!check.ok) {
    authStore.setState({ actionError: check.error });
    return false;
  }
  authStore.setState({ phase: 'saving', actionError: null });
  try {
    const status = await api.auth.credentials(check.json);
    authStore.setState({
      status,
      phase: 'idle',
      checkedAt: Date.now(),
      actionError: status.authenticated ? null : "Invalid credentials: the server didn't accept them.",
    });
    return status.authenticated;
  } catch (err) {
    authStore.setState({ phase: 'idle', actionError: `Failed to set credentials: ${errorMessage(err)}` });
    return false;
  }
}

export function clearAuthError(): void {
  authStore.setState({ actionError: null });
}
