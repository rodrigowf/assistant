/**
 * Settings → Accounts state (spec 12 §8.1): the services' sign-in status from `/api/accounts`, the
 * actions on them, and the `context/.env` key list. Revealed env values are never stored here
 * (the page keeps them in component state, so they vanish when it closes).
 *
 * Every action resolves (never rejects): failures land in `errors[service]` / `envError` with the
 * server's `detail` verbatim (CFG-2).
 */
import { useStore } from 'zustand';
import { createStore } from 'zustand/vanilla';
import { api, errorMessage, type AccountService, type EnvChangeResponse, type EnvKey, type LoginFlow } from '@/services';
import { showSnackbar } from '@/stores';
import { checkAuth } from '@/features/auth';
import { flowActive } from './logic';

export interface AccountsState {
  readonly services: AccountService[] | null;
  readonly loading: boolean;
  readonly loadError: string | null;
  /** Action in flight per service ("login", "code", "credentials:<method>", "logout", "verify", "refresh", "env:<NAME>"). */
  readonly busy: Readonly<Record<string, string | undefined>>;
  /** Last failed action per service. */
  readonly errors: Readonly<Record<string, string | undefined>>;
  readonly envKeys: EnvKey[] | null;
  readonly envPath: string;
  readonly envLoading: boolean;
  readonly envError: string | null;
  /** Name of the env key whose action is in flight. */
  readonly envBusy: string | null;
}

function initial(): AccountsState {
  return { services: null, loading: false, loadError: null, busy: {}, errors: {}, envKeys: null, envPath: '', envLoading: false, envError: null, envBusy: null };
}

export const accountsStore = createStore<AccountsState>(initial);

export function useAccounts<T>(selector: (s: AccountsState) => T): T {
  return useStore(accountsStore, selector);
}

let loadInFlight: Promise<void> | null = null;
let loadGen = 0;
/** Per-service flow version: a poll answer that started before a newer change is dropped. */
const flowSeq = new Map<string, number>();

export function resetAccounts(): void {
  loadInFlight = null;
  loadGen += 1;
  flowSeq.clear();
  accountsStore.setState(initial(), true);
}

function patch(id: string, update: (s: AccountService) => AccountService): void {
  const list = accountsStore.getState().services;
  if (!list) return;
  accountsStore.setState({ services: list.map((s) => (s.id === id ? update(s) : s)) });
}

function replace(service: AccountService): void {
  patch(service.id, () => service);
}

function setBusy(id: string, action: string | undefined): void {
  const s = accountsStore.getState();
  accountsStore.setState({ busy: { ...s.busy, [id]: action }, errors: action ? { ...s.errors, [id]: undefined } : s.errors });
}

function fail(id: string, err: unknown): void {
  const s = accountsStore.getState();
  accountsStore.setState({ busy: { ...s.busy, [id]: undefined }, errors: { ...s.errors, [id]: errorMessage(err) } });
}

export function clearAccountError(id: string): void {
  const s = accountsStore.getState();
  if (s.errors[id]) accountsStore.setState({ errors: { ...s.errors, [id]: undefined } });
}

/** A Claude change also moves the AuthGate / home row (they read `/api/auth/status`). */
function touched(id: string): void {
  if (id === 'claude') void checkAuth();
}


/**
 * `GET /api/accounts` (CFG-3: refetched every time the page opens). Concurrent calls share one
 * request, except `fresh` (after a change): it always starts a new one, and only the newest
 * answer is applied.
 */
export function loadAccounts(fresh = false): Promise<void> {
  if (loadInFlight && !fresh) return loadInFlight;
  const gen = ++loadGen;
  accountsStore.setState({ loading: true, loadError: null });
  const p: Promise<void> = api.accounts
    .list()
    .then((r) => {
      if (gen === loadGen) accountsStore.setState({ services: r.services, loading: false });
    })
    .catch((err: unknown) => {
      if (gen === loadGen) accountsStore.setState({ loading: false, loadError: errorMessage(err) });
    })
    .finally(() => {
      if (loadInFlight === p) loadInFlight = null;
    });
  loadInFlight = p;
  return p;
}

export async function refreshService(id: string): Promise<void> {
  setBusy(id, 'refresh');
  try {
    replace(await api.accounts.get(id));
    setBusy(id, undefined);
  } catch (err) {
    fail(id, err);
  }
}

function setFlow(id: string, flow: LoginFlow | null): void {
  flowSeq.set(id, (flowSeq.get(id) ?? 0) + 1);
  patch(id, (s) => ({ ...s, flow }));
}

export async function startLogin(id: string, method: string): Promise<void> {
  setBusy(id, 'login');
  try {
    setFlow(id, await api.accounts.startLogin(id, method));
    setBusy(id, undefined);
  } catch (err) {
    fail(id, err);
  }
}

async function afterFlow(id: string, flow: LoginFlow): Promise<void> {
  if (flow.status === 'succeeded') {
    showSnackbar(flow.message || 'Signed in', { durationMs: 4000 });
    touched(id);
    try {
      replace(await api.accounts.get(id));
    } catch {
      // the next poll / Check again shows it
    }
  }
}

export async function submitCode(id: string, code: string): Promise<boolean> {
  setBusy(id, 'code');
  try {
    const flow = await api.accounts.submitCode(id, code);
    setFlow(id, flow);
    setBusy(id, undefined);
    await afterFlow(id, flow);
    return true;
  } catch (err) {
    fail(id, err);
    return false;
  }
}

/** One poll of an active flow (the page calls it every couple of seconds). */
export async function pollLogin(id: string): Promise<void> {
  const before = accountsStore.getState().services?.find((s) => s.id === id)?.flow;
  if (!flowActive(before)) return;
  const seq = flowSeq.get(id) ?? 0;
  try {
    const flow = await api.accounts.login(id);
    if ((flowSeq.get(id) ?? 0) !== seq || flow.id !== before?.id) return; // superseded meanwhile
    setFlow(id, flow);
    if (!flowActive(flow)) await afterFlow(id, flow);
  } catch {
    // transient: keep polling
  }
}

export async function cancelLogin(id: string): Promise<void> {
  setBusy(id, 'cancel');
  try {
    setFlow(id, await api.accounts.cancelLogin(id));
    setBusy(id, undefined);
  } catch (err) {
    fail(id, err);
  }
}

/** Hide a finished flow's panel (the server forgets it after a few minutes anyway). */
export function dismissFlow(id: string): void {
  setFlow(id, null);
}

export async function saveCredentials(id: string, method: string, content: string): Promise<boolean> {
  setBusy(id, `credentials:${method}`);
  try {
    const r = await api.accounts.credentials(id, method, content);
    replace(r.service);
    setBusy(id, undefined);
    showSnackbar(r.message, { durationMs: 4000 });
    touched(id);
    return true;
  } catch (err) {
    fail(id, err);
    return false;
  }
}

export async function signOut(id: string): Promise<boolean> {
  setBusy(id, 'logout');
  try {
    const r = await api.accounts.logout(id);
    replace(r.service);
    setBusy(id, undefined);
    showSnackbar(r.message, { durationMs: 4000 });
    touched(id);
    return true;
  } catch (err) {
    fail(id, err);
    return false;
  }
}

export async function verifyService(id: string): Promise<void> {
  setBusy(id, 'verify');
  try {
    replace(await api.accounts.verify(id));
    setBusy(id, undefined);
  } catch (err) {
    fail(id, err);
  }
}

function changeNotice(r: EnvChangeResponse, what: string): void {
  showSnackbar(r.applies === 'backend_restart' ? `${what}. ${r.note}` : what, { durationMs: r.applies === 'backend_restart' ? 8000 : 3000 });
}

/** Set (or with `""`, clear) a key from a service card; refreshes that service and the key list. */
export async function setServiceKey(id: string, name: string, value: string): Promise<boolean> {
  setBusy(id, `env:${name}`);
  try {
    const r = await api.env.put(name, value);
    changeNotice(r, `${name} saved`);
    setBusy(id, undefined);
    await afterEnvChange(id);
    return true;
  } catch (err) {
    fail(id, err);
    return false;
  }
}

export async function removeServiceKey(id: string, name: string): Promise<boolean> {
  setBusy(id, `env:${name}`);
  try {
    const r = await api.env.remove(name);
    changeNotice(r, `${name} removed`);
    setBusy(id, undefined);
    await afterEnvChange(id);
    return true;
  } catch (err) {
    fail(id, err);
    return false;
  }
}

async function afterEnvChange(id?: string): Promise<void> {
  // Keys are shared between cards (GEMINI_API_KEY, DASHSCOPE_API_KEY…): refresh them all.
  const jobs: Promise<unknown>[] = [loadAccounts(true)];
  if (accountsStore.getState().envKeys) jobs.push(loadEnv());
  await Promise.all(jobs);
  if (id) touched(id);
}

// ───────────────────────── env keys ─────────────────────────

export async function loadEnv(): Promise<void> {
  accountsStore.setState({ envLoading: true, envError: null });
  try {
    const r = await api.env.list();
    accountsStore.setState({ envKeys: r.keys, envPath: r.path, envLoading: false });
  } catch (err) {
    accountsStore.setState({ envLoading: false, envError: errorMessage(err) });
  }
}

/** The full value of one key, or null (the error goes to `envError`). */
export async function revealEnv(name: string): Promise<string | null> {
  try {
    return (await api.env.reveal(name)).value;
  } catch (err) {
    accountsStore.setState({ envError: errorMessage(err) });
    return null;
  }
}

async function envAction(name: string, run: () => Promise<EnvChangeResponse>, what: string): Promise<boolean> {
  accountsStore.setState({ envBusy: name, envError: null });
  try {
    const r = await run();
    changeNotice(r, what);
    accountsStore.setState({ envBusy: null });
    await afterEnvChange(name === 'CLAUDE_CODE_OAUTH_TOKEN' || name === 'ANTHROPIC_API_KEY' ? 'claude' : undefined);
    return true;
  } catch (err) {
    accountsStore.setState({ envBusy: null, envError: errorMessage(err) });
    return false;
  }
}

export const createEnvKey = (name: string, value: string): Promise<boolean> => envAction(name, () => api.env.create(name, value), `${name} added`);
export const updateEnvKey = (name: string, value: string): Promise<boolean> => envAction(name, () => api.env.put(name, value), `${name} saved`);
export const deleteEnvKey = (name: string): Promise<boolean> => envAction(name, () => api.env.remove(name), `${name} deleted`);

export function clearEnvError(): void {
  accountsStore.setState({ envError: null });
}
