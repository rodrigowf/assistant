/**
 * Settings → Accounts against a fake backend (spec 12 §8.1 Accounts / Env keys, ACC-1..3): every
 * card state, the link flow (URL, device code, pasted code, polling, cancel), credentials paste,
 * API-key fields, sign out, Test, and the env-key manager (masked list, reveal on demand, edit,
 * add with validation, delete with confirm).
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AccountMethod, AccountService, LoginFlow } from '@/services';
import { expectNoAxeViolations } from '@/test/axe';
import { resetAuth } from '@/features/auth';
import { jsonResponse, setupServices, teardownServices, type Harness } from '../../../services/__tests__/fakes';
import { accountsStore, loadAccounts, pollLogin, resetAccounts, startLogin } from '../accounts/accountsStore';
import { accountsSummary, envNameError, formatExpiry, groupServices, jsonError, stateTone } from '../accounts/logic';
import { resetSettingsUi } from '../controller';
import SettingsView from '../SettingsView';
import { serveConfig } from './fixtures';

let h: Harness;

beforeEach(() => {
  h = setupServices();
  resetSettingsUi();
  resetAuth();
  resetAccounts();
});
afterEach(() => {
  teardownServices();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function method(p: Partial<AccountMethod> & Pick<AccountMethod, 'id' | 'kind' | 'label'>): AccountMethod {
  return {
    description: '',
    recommended: false,
    active: false,
    available: true,
    unavailable_reason: '',
    needs_code: false,
    code_label: '',
    code_help: '',
    input: 'json',
    path: '',
    source_hint: '',
    placeholder: '',
    warning: '',
    fields: [],
    ...p,
  };
}

function service(p: Partial<AccountService> & Pick<AccountService, 'id' | 'label'>): AccountService {
  return {
    group: 'harness',
    description: `${p.label} sessions`,
    state: 'signed_out',
    method: null,
    account: null,
    plan: null,
    expires_at: null,
    detail: null,
    warnings: [],
    methods: [],
    used_by: [],
    docs: null,
    flow: null,
    verified: null,
    can_verify: false,
    ...p,
  };
}

const field = (name: string, set: boolean, extra: object = {}) => ({
  name,
  label: name,
  secret: true,
  help: '',
  placeholder: '',
  choices: null,
  set,
  preview: set ? '••••1234' : '',
  value: null,
  ...extra,
});

const CLAUDE = service({
  id: 'claude',
  label: 'Claude Code',
  state: 'signed_in',
  method: 'Long-lived token (CLAUDE_CODE_OAUTH_TOKEN in context/.env)',
  account: 'me@example.test',
  plan: 'Claude Max',
  expires_at: '2099-01-01T00:00:00Z',
  warnings: ['ANTHROPIC_API_KEY is set too.'],
  methods: [
    method({ id: 'token', kind: 'link', label: 'Sign in with a link · 1-year token', recommended: true, needs_code: true, code_label: 'Code', warning: 'A token is already set: the new one replaces it only once the sign-in succeeds.' }),
    method({ id: 'credentials', kind: 'credentials', label: 'Paste credentials JSON', path: '/srv/.claude_config/.credentials.json', source_hint: '~/.claude/.credentials.json', warning: 'Refresh tokens rotate.' }),
    method({ id: 'keys', kind: 'env', label: 'Token or API key', fields: [field('CLAUDE_CODE_OAUTH_TOKEN', true), field('ANTHROPIC_API_KEY', false)] }),
    method({ id: 'signout', kind: 'signout', label: 'Sign out of this server\'s login', description: 'Runs claude auth logout.' }),
  ],
});
const CODEX = service({
  id: 'codex',
  label: 'Codex',
  methods: [method({ id: 'device', kind: 'link', label: 'Sign in with a device code', recommended: true })],
});
const QWEN = service({
  id: 'qwen',
  label: 'Qwen Code',
  state: 'signed_in',
  methods: [
    method({ id: 'keys', kind: 'env', label: 'API key', fields: [field('DASHSCOPE_API_KEY', true)] }),
    method({ id: 'qwen_oauth', kind: 'link', label: 'Qwen OAuth', available: false, unavailable_reason: 'Discontinued on 2026-04-15.' }),
  ],
});
const OPENAI = service({ id: 'openai', label: 'OpenAI', group: 'api', state: 'signed_in', can_verify: true, methods: [method({ id: 'keys', kind: 'env', label: 'API key', fields: [field('OPENAI_API_KEY', true)] })] });
const GEMINI = service({
  id: 'gemini',
  label: 'Gemini CLI',
  methods: [
    method({
      id: 'mode',
      kind: 'env',
      label: 'Sign-in type',
      fields: [field('ARCHIE_GEMINI_AUTH_TYPE', false, { secret: false, choices: [{ value: '', label: 'API key (default)' }, { value: 'vertex-ai', label: 'Vertex AI' }], value: '' })],
    }),
  ],
});
const UNKNOWN = service({ id: 'google_oauth', label: 'Google OAuth tokens', group: 'other', state: 'unknown', warnings: ["Couldn't check: boom"] });

const flow = (p: Partial<LoginFlow>): LoginFlow => ({
  id: 'f1',
  service: 'claude',
  method: 'token',
  status: 'waiting',
  url: 'https://claude.com/cai/oauth/authorize?code=true&state=S',
  user_code: null,
  needs_code: true,
  code_label: 'Code',
  code_help: 'The page shows a code after you sign in.',
  message: 'Open the link, sign in, then paste the code here.',
  started_at: '2026-10-09T00:00:00Z',
  expires_at: '2026-10-09T00:15:00Z',
  finished_at: null,
  ...p,
});

function serve(services: AccountService[] = [CLAUDE, CODEX, QWEN, OPENAI, GEMINI, UNKNOWN]) {
  serveConfig(h.fetch);
  h.fetch
    .on('GET', '/api/accounts', { services, env_path: '/srv/context/.env' })
    .on('GET', '/api/env', {
      path: '/srv/context/.env',
      exists: true,
      keys: [
        { name: 'OPENAI_API_KEY', set: true, preview: '••••abcd', length: 51, line: 1, exported: false, duplicates: 0, in_process: true },
        { name: 'VOICE_DEBUG_VAD', set: true, preview: '••••', length: 1, line: 4, exported: true, duplicates: 0, in_process: false },
      ],
    });
}

function view() {
  const user = userEvent.setup();
  const r = render(<SettingsView page="account" layout="two-pane" onNavigate={vi.fn()} />);
  return { user, ...r };
}

const card = (id: string): HTMLElement => document.querySelector(`[data-account="${id}"]`) as HTMLElement;

describe('Accounts logic', () => {
  it('groups, tones, expiry, summary and validation', () => {
    expect(groupServices([OPENAI, CLAUDE, UNKNOWN]).map((g) => [g.id, g.services.map((s) => s.id)])).toEqual([
      ['harness', ['claude']],
      ['api', ['openai']],
      ['other', ['google_oauth']],
    ]);
    expect(stateTone('signed_in')).toBe('ok');
    expect(stateTone('unavailable')).toBe('bad');
    expect(formatExpiry('2027-10-09T00:00:00Z', Date.parse('2026-01-01'))).toBe('until 9 Oct 2027');
    expect(formatExpiry('2020-01-02T00:00:00Z')).toBe('expired 2 Jan 2020');
    expect(formatExpiry('nope')).toBeNull();
    expect(accountsSummary([CLAUDE, CODEX, QWEN, OPENAI])).toBe('2 of 3 agent harnesses signed in');
    expect(accountsSummary([CLAUDE])).toBe('All agent harnesses signed in');
    expect(envNameError('my_key')).toMatch(/Capital letters/);
    expect(envNameError('1KEY')).toMatch(/Capital letters/);
    expect(envNameError('KEY', ['KEY'])).toBe('KEY already exists');
    expect(envNameError('MY_KEY_2')).toBeNull();
    expect(jsonError('{')).toMatch(/valid JSON/);
    expect(jsonError('[]')).toMatch(/object/);
    expect(jsonError('{"a":1}')).toBeNull();
  });
});

describe('Accounts store', () => {
  it('drops a poll answer about another (older) flow', async () => {
    serve([CODEX]);
    await loadAccounts();
    h.fetch
      .on('POST', '/api/accounts/codex/login', () => jsonResponse(flow({ id: 'new', service: 'codex', method: 'device', needs_code: false })))
      .on('GET', '/api/accounts/codex/login', () => jsonResponse(flow({ id: 'old', service: 'codex', status: 'succeeded' })));
    await startLogin('codex', 'device');
    await pollLogin('codex');
    const f = accountsStore.getState().services?.[0]?.flow;
    expect([f?.id, f?.status]).toEqual(['new', 'waiting']);
  });
});

describe('Accounts page', () => {
  it('renders every group and state, with facts, warnings and unavailable methods', async () => {
    serve();
    const { container } = view();
    expect(await screen.findByRole('heading', { level: 2, name: 'Accounts' })).toBeTruthy();
    await waitFor(() => expect(card('claude')).toBeTruthy());
    expect(screen.getByRole('heading', { name: 'Agent harnesses' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Voice & AI APIs' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Other' })).toBeTruthy();
    const claude = within(card('claude'));
    expect(claude.getByText('Signed in')).toBeTruthy();
    expect(claude.getByText('me@example.test')).toBeTruthy();
    expect(claude.getByText('Claude Max')).toBeTruthy();
    expect(claude.getByText('until 1 Jan 2099')).toBeTruthy();
    expect(claude.getByText('ANTHROPIC_API_KEY is set too.')).toBeTruthy();
    expect(within(card('codex')).getByText('Not signed in')).toBeTruthy();
    expect(within(card('google_oauth')).getByText('Unknown')).toBeTruthy();
    expect(within(card('qwen')).getByText(/Qwen OAuth: Discontinued/)).toBeTruthy();
    expect(within(card('openai')).getByRole('button', { name: 'Test' })).toBeTruthy();
    expect(within(card('codex')).queryByRole('button', { name: 'Test' })).toBeNull();
    await waitFor(() => expect(container.querySelector('[data-env-key="OPENAI_API_KEY"]')).toBeTruthy());
    await expectNoAxeViolations(container);
  });

  it('link flow: start → URL + Copy/Open → paste the code → signed in, service refetched', async () => {
    serve();
    let state: AccountService = CLAUDE;
    h.fetch
      .on('POST', '/api/accounts/claude/login', () => jsonResponse(flow({})))
      .on('POST', '/api/accounts/claude/login/code', () => {
        state = { ...CLAUDE, flow: null };
        return jsonResponse(flow({ status: 'succeeded', needs_code: false, message: 'Signed in. The token is saved.' }));
      })
      .on('GET', '/api/accounts/claude', () => jsonResponse(state))
      .on('GET', '/api/auth/status', { authenticated: true, auth_url: null, headless: true });
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const { user } = view();
    await waitFor(() => expect(card('claude')).toBeTruthy());
    await user.click(within(card('claude')).getByRole('button', { name: 'Sign in with a link · 1-year token' }));
    const c = within(card('claude'));
    expect(await c.findByText('https://claude.com/cai/oauth/authorize?code=true&state=S')).toBeTruthy();
    expect(c.getByText(/replaces it only once the sign-in succeeds/)).toBeTruthy();
    expect(h.fetch.calls('POST', '/api/accounts/claude/login')[0]?.body).toEqual({ method: 'token' });
    await user.click(c.getByRole('button', { name: /Open link/ }));
    expect(open).toHaveBeenCalledWith('https://claude.com/cai/oauth/authorize?code=true&state=S', '_blank', 'noopener,noreferrer');
    // while the flow runs, link methods are disabled (one flow per service)
    expect(c.getByRole('button', { name: 'Sign in with a link · 1-year token' }).getAttribute('aria-disabled')).toBe('true');
    await user.type(c.getByRole('textbox', { name: 'Code' }), 'abc#def');
    await user.click(c.getByRole('button', { name: /Finish sign-in/ }));
    await waitFor(() => expect(h.fetch.calls('POST', '/api/accounts/claude/login/code')[0]?.body).toEqual({ code: 'abc#def' }));
    await waitFor(() => expect(h.fetch.calls('GET', '/api/accounts/claude').length).toBeGreaterThan(0));
    await waitFor(() => expect(h.fetch.calls('GET', '/api/auth/status').length).toBeGreaterThan(0));
  });

  it('device flow shows the code and is polled until it ends; cancel kills it', async () => {
    serve([CODEX]);
    let polls = 0;
    h.fetch
      .on('POST', '/api/accounts/codex/login', () => jsonResponse(flow({ service: 'codex', method: 'device', needs_code: false, url: 'https://auth.openai.com/codex/device', user_code: 'ABCD-EFG12' })))
      .on('GET', '/api/accounts/codex/login', () => {
        polls += 1;
        return jsonResponse(flow({ service: 'codex', method: 'device', needs_code: false, url: 'https://auth.openai.com/codex/device', user_code: 'ABCD-EFG12' }));
      })
      .on('DELETE', '/api/accounts/codex/login', () => jsonResponse(flow({ service: 'codex', method: 'device', status: 'cancelled', needs_code: false, message: 'Sign-in cancelled.' })));
    const { user } = view();
    await waitFor(() => expect(card('codex')).toBeTruthy());
    await user.click(within(card('codex')).getByRole('button', { name: 'Sign in with a device code' }));
    const c = within(card('codex'));
    expect(await c.findByText('ABCD-EFG12')).toBeTruthy();
    expect(c.queryByRole('textbox', { name: 'Code' })).toBeNull();
    await waitFor(() => expect(polls).toBeGreaterThan(0), { timeout: 4000 });
    expect(c.getByText('ABCD-EFG12')).toBeTruthy();
    await user.click(c.getByRole('button', { name: /Cancel sign-in/ }));
    expect(await c.findByText('Sign-in cancelled.')).toBeTruthy();
    await user.click(c.getByRole('button', { name: 'Dismiss' }));
    expect(c.queryByText('Sign-in cancelled.')).toBeNull();
  });

  it('a failed start shows the server detail verbatim', async () => {
    serve([CODEX]);
    h.fetch.on('POST', '/api/accounts/codex/login', () => jsonResponse({ detail: "The codex CLI isn't installed on the server." }, 409));
    const { user } = view();
    await waitFor(() => expect(card('codex')).toBeTruthy());
    await user.click(within(card('codex')).getByRole('button', { name: 'Sign in with a device code' }));
    expect(await within(card('codex')).findByRole('alert')).toHaveProperty('textContent', "The codex CLI isn't installed on the server.");
  });

  it('credentials paste: shows where to copy from and where it lands; validates JSON first', async () => {
    serve();
    h.fetch.on('POST', '/api/accounts/claude/credentials', () => jsonResponse({ message: 'Credentials saved.', service: CLAUDE }));
    const { user } = view();
    await waitFor(() => expect(card('claude')).toBeTruthy());
    const c = within(card('claude'));
    await user.click(c.getByRole('button', { name: 'Paste credentials JSON' }));
    expect(c.getByText('~/.claude/.credentials.json')).toBeTruthy();
    expect(c.getByText('/srv/.claude_config/.credentials.json')).toBeTruthy();
    expect(c.getByText('Refresh tokens rotate.')).toBeTruthy();
    const box = c.getByRole('textbox', { name: 'File contents (JSON)' });
    await user.type(box, 'nope');
    await user.click(c.getByRole('button', { name: 'Save' }));
    expect(c.getByText(/isn't valid JSON/)).toBeTruthy();
    expect(h.fetch.calls('POST', '/api/accounts/claude/credentials')).toHaveLength(0);
    await user.clear(box);
    await user.click(box);
    await user.paste('{"claudeAiOauth":{"accessToken":"x"}}');
    await user.click(c.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(h.fetch.calls('POST', '/api/accounts/claude/credentials')[0]?.body).toEqual({ method: 'credentials', content: '{"claudeAiOauth":{"accessToken":"x"}}' }));
  });

  it('API-key fields: set a key (masked input), remove one with confirm, mode switch via select', async () => {
    serve();
    h.fetch
      .on('PUT', /^\/api\/env\/[A-Z_]+$/, () => jsonResponse({ applies: 'now', note: '', key: {} }))
      .on('DELETE', /^\/api\/env\/[A-Z_]+$/, () => jsonResponse({ applies: 'now', note: '', name: 'CLAUDE_CODE_OAUTH_TOKEN', removed: 1 }));
    const { user } = view();
    await waitFor(() => expect(card('claude')).toBeTruthy());
    const c = within(card('claude'));
    await user.click(c.getByRole('button', { name: 'Token or API key' }));
    expect(c.getByText('••••1234')).toBeTruthy();
    expect(c.getByText('Not set')).toBeTruthy();
    await user.click(c.getByRole('button', { name: 'Set ANTHROPIC_API_KEY' }));
    const input = c.getByLabelText('ANTHROPIC_API_KEY') as HTMLInputElement;
    expect(input.type).toBe('password');
    await user.type(input, 'sk-ant-api03-new');
    await user.click(c.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(h.fetch.calls('PUT', '/api/env/ANTHROPIC_API_KEY')[0]?.body).toEqual({ value: 'sk-ant-api03-new' }));
    await waitFor(() => expect(h.fetch.calls('GET', '/api/accounts').length).toBeGreaterThanOrEqual(2)); // ACC-3
    await user.click(c.getByRole('button', { name: 'Remove CLAUDE_CODE_OAUTH_TOKEN' }));
    await user.click(await screen.findByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(h.fetch.calls('DELETE', '/api/env/CLAUDE_CODE_OAUTH_TOKEN')).toHaveLength(1));
  });

  it('sign out asks first; Test shows the verdict', async () => {
    serve();
    h.fetch
      .on('POST', '/api/accounts/claude/logout', () => jsonResponse({ message: 'Signed out.', service: { ...CLAUDE, state: 'signed_out' } }))
      .on('POST', '/api/accounts/openai/verify', () => jsonResponse({ ...OPENAI, verified: { ok: false, message: 'The provider rejected the key (401).', checked_at: 'x' } }))
      .on('GET', '/api/auth/status', { authenticated: false, auth_url: null, headless: true });
    const { user } = view();
    await waitFor(() => expect(card('claude')).toBeTruthy());
    await user.click(within(card('claude')).getByRole('button', { name: /Sign out of this server/ }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText('Runs claude auth logout.')).toBeTruthy();
    await user.click(within(dialog).getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(within(card('claude')).getByText('Not signed in')).toBeTruthy());
    await user.click(within(card('openai')).getByRole('button', { name: 'Test' }));
    expect(await within(card('openai')).findByText('The provider rejected the key (401).')).toBeTruthy();
  });

  it('a load error offers Retry', async () => {
    serveConfig(h.fetch);
    h.fetch.on('GET', '/api/accounts', () => jsonResponse({ detail: 'boom' }, 500)).on('GET', '/api/env', { path: '', exists: false, keys: [] });
    const { user } = view();
    expect(await screen.findByText("Couldn't load the accounts")).toBeTruthy();
    h.fetch.on('GET', '/api/accounts', { services: [CODEX], env_path: '' });
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(card('codex')).toBeTruthy());
  });
});

describe('Environment keys', () => {
  it('lists masked values; reveal fetches one value on demand and hides it again (ACC-1)', async () => {
    serve();
    h.fetch.on('POST', '/api/env/OPENAI_API_KEY/reveal', { name: 'OPENAI_API_KEY', value: 'sk-full-secret-value' });
    const { user, container } = view();
    const row = await waitFor(() => {
      const el = container.querySelector('[data-env-key="OPENAI_API_KEY"]') as HTMLElement | null;
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(within(row).getByText('••••abcd · 51 chars')).toBeTruthy();
    expect(container.textContent).not.toContain('sk-full-secret-value');
    const vad = container.querySelector('[data-env-key="VOICE_DEBUG_VAD"]') as HTMLElement;
    expect(within(vad).getByText('export')).toBeTruthy();
    expect(within(vad).getByText('not loaded')).toBeTruthy();
    await user.click(within(row).getByRole('button', { name: 'Reveal OPENAI_API_KEY' }));
    expect(await within(row).findByText('sk-full-secret-value')).toBeTruthy();
    await user.click(within(row).getByRole('button', { name: 'Hide OPENAI_API_KEY' }));
    expect(within(row).queryByText('sk-full-secret-value')).toBeNull();
    expect(h.fetch.calls('POST', '/api/env/OPENAI_API_KEY/reveal')).toHaveLength(1);
  });

  it('edit prefills from reveal and PUTs; add validates the name; delete confirms', async () => {
    serve();
    h.fetch
      .on('POST', '/api/env/VOICE_DEBUG_VAD/reveal', { name: 'VOICE_DEBUG_VAD', value: '1' })
      .on('PUT', '/api/env/VOICE_DEBUG_VAD', () => jsonResponse({ applies: 'now', note: '', key: {} }))
      .on('POST', '/api/env', () => jsonResponse({ applies: 'backend_restart', note: 'Restart the backend for this to take effect.', key: {} }))
      .on('DELETE', '/api/env/OPENAI_API_KEY', () => jsonResponse({ applies: 'now', note: '', name: 'OPENAI_API_KEY', removed: 1 }));
    const { user, container } = view();
    const vad = await waitFor(() => {
      const el = container.querySelector('[data-env-key="VOICE_DEBUG_VAD"]') as HTMLElement | null;
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    await user.click(within(vad).getByRole('button', { name: 'Edit VOICE_DEBUG_VAD' }));
    expect(((await within(vad).findByLabelText('Value')) as HTMLInputElement).value).toBe('1');
    // Cancel hides the value that was revealed only to prefill the editor
    await user.click(within(vad).getByRole('button', { name: 'Cancel' }));
    expect(within(vad).queryByText('1', { exact: true })).toBeNull();
    await user.click(within(vad).getByRole('button', { name: 'Edit VOICE_DEBUG_VAD' }));
    const input = (await within(vad).findByLabelText('Value')) as HTMLInputElement;
    expect(input.value).toBe('1');
    await user.clear(input);
    await user.type(input, '0');
    await user.click(within(vad).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(h.fetch.calls('PUT', '/api/env/VOICE_DEBUG_VAD')[0]?.body).toEqual({ value: '0' }));

    await user.click(screen.getByRole('button', { name: 'Add key' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a key' });
    const name = within(dialog).getByRole('textbox', { name: /Name/ });
    await user.type(name, 'openai_api_key');
    await user.tab();
    expect(within(dialog).getByText('OPENAI_API_KEY already exists')).toBeTruthy();
    await user.clear(name);
    await user.type(name, 'new_key');
    await user.type(within(dialog).getByLabelText('Value'), 'v a l');
    await user.click(within(dialog).getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(h.fetch.requests.find((r) => r.method === 'POST' && r.path === '/api/env')?.body).toEqual({ name: 'NEW_KEY', value: 'v a l' }));

    const row = container.querySelector('[data-env-key="OPENAI_API_KEY"]') as HTMLElement;
    await user.click(within(row).getByRole('button', { name: 'Delete OPENAI_API_KEY' }));
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(h.fetch.calls('DELETE', '/api/env/OPENAI_API_KEY')).toHaveLength(1));
  });
});
