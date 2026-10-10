/**
 * Pure helpers of Settings → Accounts (spec 12 §8.1 "Accounts"): status words, grouping, expiry,
 * flow states, env-key names.
 */
import type { AccountGroup, AccountService, AccountState, LoginFlow, LoginFlowStatus } from '@/services';

export const GROUPS: readonly { id: AccountGroup; title: string; help: string }[] = [
  { id: 'harness', title: 'Agent harnesses', help: 'The CLIs that run agent sessions on the server.' },
  { id: 'api', title: 'Voice & AI APIs', help: 'Keys the server itself uses: voice, orchestrator, search.' },
  { id: 'other', title: 'Other', help: '' },
];

export function groupServices(services: readonly AccountService[]): { id: AccountGroup; title: string; help: string; services: AccountService[] }[] {
  return GROUPS.map((g) => ({ ...g, services: services.filter((s) => s.group === g.id) })).filter((g) => g.services.length);
}

export const STATE_LABEL: Record<AccountState, string> = {
  signed_in: 'Signed in',
  signed_out: 'Not signed in',
  expired: 'Expired',
  unavailable: 'Unavailable',
  unknown: 'Unknown',
};

export type StateTone = 'ok' | 'off' | 'bad' | 'unknown';

export function stateTone(state: AccountState): StateTone {
  if (state === 'signed_in') return 'ok';
  if (state === 'signed_out') return 'off';
  if (state === 'expired' || state === 'unavailable') return 'bad';
  return 'unknown';
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "until 9 Oct 2027", "expired 2 Oct 2026"; null for a missing or bad date. */
export function formatExpiry(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const d = new Date(t);
  const day = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  return t < now ? `expired ${day}` : `until ${day}`;
}

const ACTIVE_FLOW: ReadonlySet<LoginFlowStatus> = new Set(['starting', 'waiting', 'verifying']);

export function flowActive(flow: LoginFlow | null | undefined): boolean {
  return !!flow && ACTIVE_FLOW.has(flow.status);
}

export function anyFlowActive(services: readonly AccountService[] | null): boolean {
  return !!services && services.some((s) => flowActive(s.flow));
}

export const FLOW_LABEL: Record<LoginFlowStatus, string> = {
  starting: 'Starting…',
  waiting: 'Waiting for you',
  verifying: 'Checking…',
  succeeded: 'Signed in',
  failed: 'Failed',
  cancelled: 'Cancelled',
  expired: 'Timed out',
};

/** One line for the Settings home row. */
export function accountsSummary(services: readonly AccountService[]): string {
  const harnesses = services.filter((s) => s.group === 'harness');
  const signedIn = harnesses.filter((s) => s.state === 'signed_in');
  if (!harnesses.length) return `${services.filter((s) => s.state === 'signed_in').length} of ${services.length} signed in`;
  if (signedIn.length === harnesses.length) return 'All agent harnesses signed in';
  return `${signedIn.length} of ${harnesses.length} agent harnesses signed in`;
}

export const ENV_NAME_RE = /^[A-Z_][A-Z0-9_]*$/;

/** null when valid, else the message. */
export function envNameError(name: string, existing: readonly string[] = []): string | null {
  const n = name.trim();
  if (!n) return 'Enter a name';
  if (!ENV_NAME_RE.test(n)) return 'Capital letters, digits and underscores; not starting with a digit (MY_API_KEY)';
  if (existing.indexOf(n) >= 0) return `${n} already exists`;
  return null;
}

/** Client-side JSON check before sending a credentials file. */
export function jsonError(text: string): string | null {
  const t = text.trim();
  if (!t) return 'Paste the file contents';
  try {
    const v: unknown = JSON.parse(t);
    if (!v || typeof v !== 'object' || Array.isArray(v)) return 'The file is a JSON object ({ … })';
  } catch {
    return "That isn't valid JSON. Copy the whole file, including the braces.";
  }
  return null;
}
