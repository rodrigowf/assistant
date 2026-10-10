/**
 * AuthGate flows (inv02 §1.11, spec 12 §8.1): status check at startup, headless paste flow,
 * sign-in on a server with a browser, failures shown verbatim, "Not now", and a failed check
 * never blocking the app. The link sign-in uses `/api/accounts/claude/login` (spec 12 §8.1).
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { sessionStore } from '@/platform';
import { expectNoAxeViolations } from '@/test/axe';
import { jsonResponse, setupServices, teardownServices, type Harness, type RecordedRequest } from '../../../services/__tests__/fakes';
import { AuthGate, authStore, GATE_DISMISSED_KEY, resetAuth } from '..';
import { checkCredentialsText } from '../authActions';

let h: Harness;

beforeEach(() => {
  sessionStore.remove(GATE_DISMISSED_KEY);
  h = setupServices();
  resetAuth();
});
afterEach(() => {
  teardownServices();
});

const VALID = JSON.stringify({ claudeAiOauth: { accessToken: 'sk-ant-oat01-x', refreshToken: 'r', expiresAt: 1 } });

function app() {
  return render(
    <AuthGate>
      <button type="button">App content</button>
    </AuthGate>,
  );
}

describe('checkCredentialsText', () => {
  it('needs JSON with claudeAiOauth.accessToken', () => {
    expect(checkCredentialsText('')).toMatchObject({ ok: false });
    expect(checkCredentialsText('{oops')).toMatchObject({ ok: false, error: expect.stringMatching(/valid JSON/) });
    expect(checkCredentialsText('{"claudeAiOauth":{}}')).toMatchObject({ ok: false, error: expect.stringMatching(/accessToken/) });
    expect(checkCredentialsText(` ${VALID} `)).toEqual({ ok: true, json: VALID });
  });
});

describe('AuthGate', () => {
  it('signed in: the app renders, no gate', async () => {
    h.fetch.on('GET', '/api/auth/status', { authenticated: true, auth_url: null, headless: true });
    app();
    expect(screen.getByRole('button', { name: 'App content' })).toBeTruthy();
    await waitFor(() => expect(authStore.getState().status?.authenticated).toBe(true));
    expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull();
  });

  it('a failed status check never shows the sign-in screen (old gate did)', async () => {
    h.fetch.on('GET', '/api/auth/status', () => jsonResponse({ detail: 'boom' }, 502));
    app();
    await waitFor(() => expect(authStore.getState().checkError).toBe('boom'));
    expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull();
  });

  it('headless: paste flow; bad JSON is caught locally; valid credentials are POSTed and the gate goes', async () => {
    const user = userEvent.setup();
    h.fetch
      .on('GET', '/api/auth/status', { authenticated: false, auth_url: 'https://console.anthropic.com/settings', headless: true })
      .on('POST', '/api/auth/credentials', (req: RecordedRequest) =>
        jsonResponse({ authenticated: (req.body as { credentials_json: string }).credentials_json.includes('accessToken'), auth_url: null, headless: true }),
      );
    app();
    const gate = await screen.findByRole('dialog', { name: 'Sign in to Claude' });
    expect(within(gate).getByRole('button', { name: 'Sign in with Claude' })).toBeTruthy(); // the link works headless too
    await user.click(within(gate).getByRole('button', { name: 'Paste credentials instead' }));
    expect(within(gate).getByRole('button', { name: 'Claude Console' })).toBeTruthy();
    // the app underneath is hidden from AT while the gate is up
    expect(screen.getByRole('button', { name: 'App content', hidden: true }).closest('[aria-hidden="true"]')).not.toBeNull();
    await expectNoAxeViolations(gate);
    const field = within(gate).getByRole('textbox', { name: 'Credentials JSON' });
    await user.type(field, 'not json');
    await user.click(within(gate).getByRole('button', { name: 'Set credentials' }));
    expect(within(gate).getByText(/isn't valid JSON/)).toBeTruthy();
    expect(h.fetch.calls('POST', '/api/auth/credentials')).toHaveLength(0);
    await user.clear(field);
    await user.click(field);
    await user.paste(VALID);
    await user.click(within(gate).getByRole('button', { name: 'Set credentials' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull());
    expect(h.fetch.calls('POST', '/api/auth/credentials')[0]?.body).toEqual({ credentials_json: VALID });
  });

  it('server rejects the credentials: says so and stays', async () => {
    const user = userEvent.setup();
    h.fetch
      .on('GET', '/api/auth/status', { authenticated: false, auth_url: null, headless: true })
      .on('POST', '/api/auth/credentials', { authenticated: false, auth_url: null, headless: true });
    app();
    const gate = await screen.findByRole('dialog', { name: 'Sign in to Claude' });
    await user.click(within(gate).getByRole('button', { name: 'Paste credentials instead' }));
    await user.click(within(gate).getByRole('textbox', { name: 'Credentials JSON' }));
    await user.paste(VALID);
    await user.click(within(gate).getByRole('button', { name: 'Set credentials' }));
    expect(await within(gate).findByText(/server didn't accept them/)).toBeTruthy();
  });

  it('"Sign in with Claude" runs the link sign-in: URL, pasted code, then the gate goes', async () => {
    const user = userEvent.setup();
    let signedIn = false;
    const flow = {
      id: 'f1', service: 'claude', method: 'token', status: 'waiting', url: 'https://claude.com/cai/oauth/authorize?code=true&state=S',
      user_code: null, needs_code: true, code_label: 'Code', code_help: '', message: 'Open the link…', started_at: '', expires_at: '', finished_at: null,
    };
    h.fetch
      .on('GET', '/api/auth/status', () => jsonResponse({ authenticated: signedIn, auth_url: null, headless: true }))
      .on('POST', '/api/accounts/claude/login', () => jsonResponse(flow))
      .on('POST', '/api/accounts/claude/login/code', () => {
        signedIn = true;
        return jsonResponse({ ...flow, status: 'succeeded', needs_code: false, message: 'Signed in.' });
      });
    app();
    const gate = await screen.findByRole('dialog', { name: 'Sign in to Claude' });
    await user.click(within(gate).getByRole('button', { name: 'Sign in with Claude' }));
    expect(await within(gate).findByText('https://claude.com/cai/oauth/authorize?code=true&state=S')).toBeTruthy();
    expect(h.fetch.calls('POST', '/api/accounts/claude/login')[0]?.body).toEqual({ method: 'token' });
    await user.type(within(gate).getByRole('textbox', { name: 'Code' }), 'abc#def');
    await user.click(within(gate).getByRole('button', { name: /Finish sign-in/ }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull());
    expect(h.fetch.calls('POST', '/api/accounts/claude/login/code')[0]?.body).toEqual({ code: 'abc#def' });
    expect(h.fetch.calls('POST', '/api/auth/login')).toHaveLength(0);
  });

  it('an older backend without /api/accounts falls back to the blocking login when it has a screen', async () => {
    const user = userEvent.setup();
    h.fetch
      .on('GET', '/api/auth/status', { authenticated: false, auth_url: null, headless: false })
      .on('POST', '/api/auth/login', { authenticated: true, auth_url: null, headless: false });
    app();
    const gate = await screen.findByRole('dialog', { name: 'Sign in to Claude' });
    await user.click(within(gate).getByRole('button', { name: 'Sign in with Claude' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull());
    expect(h.fetch.calls('POST', '/api/auth/login')).toHaveLength(1);
  });

  it('"Not now" dismisses it for this browser tab', async () => {
    const user = userEvent.setup();
    h.fetch.on('GET', '/api/auth/status', { authenticated: false, auth_url: null, headless: true });
    const first = app();
    const gate = await screen.findByRole('dialog', { name: 'Sign in to Claude' });
    await user.click(within(gate).getByRole('button', { name: 'Not now' }));
    expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull();
    expect(sessionStore.get(GATE_DISMISSED_KEY)).toBe('1');
    first.unmount();
    resetAuth();
    app();
    await waitFor(() => expect(authStore.getState().status?.authenticated).toBe(false));
    expect(screen.queryByRole('dialog', { name: 'Sign in to Claude' })).toBeNull();
  });
});
