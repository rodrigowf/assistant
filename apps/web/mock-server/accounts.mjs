/**
 * Mock of Settings → Accounts (`/api/accounts`, `/api/env`; spec 12 §8.1): a few services in
 * different states, link flows that need a pasted code ("good" signs in, anything else fails) or
 * finish by themselves after a few seconds (device codes), credentials paste, and an in-memory
 * `.env`. Nothing here talks to a real CLI.
 */

const mask = (v) => (!v ? '' : v.length >= 16 ? `••••${v.slice(-4)}` : '••••');
const NAME_RE = /^[A-Z_][A-Z0-9_]*$/;
const RESTART = new Set(['CLAUDE_CONFIG_DIR', 'HEADLESS', 'PATH', 'HOME', 'PYTHONPATH', 'LD_PRELOAD', 'DISPLAY']);

function method(m) {
  return {
    description: '', recommended: false, active: false, available: true, unavailable_reason: '', needs_code: false, code_label: 'Code',
    code_help: '', input: 'json', path: '', source_hint: '', placeholder: '', warning: '', fields: [], ...m,
  };
}

const field = (name, label, extra = {}) => ({ name, label, secret: true, help: '', placeholder: '', choices: null, ...extra });

export function createAccounts() {
  const env = new Map([
    ['OPENAI_API_KEY', 'sk-mock-openai-0123456789abcdef'],
    ['CLAUDE_CODE_OAUTH_TOKEN', 'sk-ant-oat01-mock-token-0123456789'],
    ['DASHSCOPE_API_KEY', 'sk-mock-dashscope-0123456789'],
    ['ORCHESTRATOR_MODEL', 'gpt-5'],
  ]);
  const signedIn = new Set(['claude', 'qwen']);
  const flows = new Map();

  const keyFields = (fields) =>
    fields.map((f) => {
      const v = env.get(f.name) ?? '';
      return { ...f, set: !!v, preview: f.secret ? mask(v) : '', value: f.secret ? null : v };
    });

  function services() {
    const now = Date.now();
    for (const [id, f] of flows) {
      if (f.status === 'waiting' && !f.needs_code && now - f.startedMs > 6000) {
        Object.assign(f, { status: 'succeeded', message: 'Signed in (mock).', finished_at: new Date().toISOString() });
        signedIn.add(id);
      }
    }
    const s = (id, label, group, description, methods, extra = {}) => ({
      id, label, group, description, state: signedIn.has(id) ? 'signed_in' : 'signed_out', method: null, account: null, plan: null,
      expires_at: null, detail: null, warnings: [], used_by: [], docs: null, flow: flows.get(id) ?? null, verified: null, can_verify: false,
      methods: methods.map((m) => ({ ...m, fields: keyFields(m.fields) })), ...extra,
    });
    return [
      s('claude', 'Claude Code', 'harness', 'Agent sessions (Claude Code harness).', [
        method({ id: 'token', kind: 'link', label: 'Sign in with a link · 1-year token', recommended: true, needs_code: true, code_help: 'Type "good" to succeed in the mock.' }),
        method({ id: 'login', kind: 'link', label: 'Sign in with a link · this server only', needs_code: true }),
        method({ id: 'credentials', kind: 'credentials', label: 'Paste credentials JSON', path: '/home/me/assistant/.claude_config/.credentials.json', source_hint: '~/.claude/.credentials.json', warning: 'Refresh tokens rotate: don\'t share one login between machines.' }),
        method({ id: 'keys', kind: 'env', label: 'Token or API key', fields: [field('CLAUDE_CODE_OAUTH_TOKEN', 'Long-lived token'), field('ANTHROPIC_API_KEY', 'Anthropic API key')] }),
        method({ id: 'signout', kind: 'signout', label: 'Sign out of this server\'s login', description: 'Runs `claude auth logout`.', available: signedIn.has('claude') }),
      ], signedIn.has('claude') ? { method: 'Long-lived token (CLAUDE_CODE_OAUTH_TOKEN in context/.env)', plan: 'Claude Max', expires_at: '2027-10-01T00:00:00Z' } : {}),
      s('codex', 'Codex', 'harness', 'Agent sessions (OpenAI Codex harness), with your ChatGPT plan.', [
        method({ id: 'device', kind: 'link', label: 'Sign in with a device code', recommended: true }),
        method({ id: 'browser', kind: 'link', label: 'Sign in in the browser', needs_code: true, code_label: 'Address the browser landed on' }),
        method({ id: 'apikey', kind: 'credentials', input: 'secret', label: 'OpenAI API key', path: '~/.codex-archie/auth.json' }),
        method({ id: 'auth_json', kind: 'credentials', label: 'Paste auth.json', path: '~/.codex-archie/auth.json', source_hint: '~/.codex/auth.json' }),
      ], signedIn.has('codex') ? { method: 'ChatGPT sign-in', account: 'me@example.com', plan: 'ChatGPT Plus' } : { warnings: [] }),
      s('qwen', 'Qwen Code', 'harness', 'Agent sessions (Qwen Code harness).', [
        method({ id: 'keys', kind: 'env', label: 'API key', recommended: true, fields: [field('DASHSCOPE_API_KEY', 'DASHSCOPE_API_KEY')] }),
        method({ id: 'qwen_oauth', kind: 'link', label: 'Qwen OAuth', available: false, unavailable_reason: 'Discontinued by Alibaba on 2026-04-15.' }),
      ], { method: 'API key (DASHSCOPE_API_KEY)' }),
      s('openai', 'OpenAI', 'api', 'Realtime voice, talk mode, history re-ranking, session summaries.', [
        method({ id: 'keys', kind: 'env', label: 'API key', recommended: true, fields: [field('OPENAI_API_KEY', 'OpenAI API key')] }),
      ], { state: env.get('OPENAI_API_KEY') ? 'signed_in' : 'signed_out', can_verify: true }),
    ];
  }

  function envList() {
    return [...env.entries()].map(([name, v], i) => ({ name, set: !!v, preview: mask(v), length: v.length, line: i + 1, exported: false, duplicates: 0, in_process: true }));
  }

  const change = (name, extra) => ({
    applies: RESTART.has(name) ? 'backend_restart' : 'now',
    note: RESTART.has(name) ? 'Restart the backend for this to take effect.' : 'Applies now.',
    ...extra,
  });

  /** Returns `[status, body]` or null when the path isn't ours. */
  async function handle(p, m, body) {
    if (p === '/api/accounts' && m === 'GET') return [200, { services: services(), env_path: '/home/me/assistant/context/.env' }];
    if (p === '/api/env' && m === 'GET') return [200, { path: '/home/me/assistant/context/.env', exists: true, keys: envList() }];
    if (p === '/api/env' && m === 'POST') {
      if (!body || !NAME_RE.test(body.name ?? '')) return [400, { detail: 'Key names use capital letters, digits and underscores.' }];
      if (env.has(body.name)) return [409, { detail: `${body.name} already exists.` }];
      env.set(body.name, String(body.value ?? ''));
      return [200, change(body.name, { key: envList().find((k) => k.name === body.name) })];
    }
    let mm = /^\/api\/env\/([^/]+)(\/reveal)?$/.exec(p);
    if (mm) {
      const name = mm[1];
      if (mm[2] && m === 'POST') return env.has(name) ? [200, { name, value: env.get(name) }] : [404, { detail: `${name} isn't in the .env file.` }];
      if (m === 'PUT') {
        if (!NAME_RE.test(name)) return [400, { detail: 'Bad key name.' }];
        env.set(name, String(body?.value ?? ''));
        return [200, change(name, { key: envList().find((k) => k.name === name) })];
      }
      if (m === 'DELETE') {
        if (!env.delete(name)) return [404, { detail: `${name} isn't in the .env file.` }];
        return [200, change(name, { name, removed: 1 })];
      }
    }
    mm = /^\/api\/accounts\/([^/]+)(?:\/(login|login\/code|credentials|logout|verify))?$/.exec(p);
    if (!mm) return null;
    const [, id, action] = mm;
    const svc = () => services().find((s) => s.id === id);
    if (!svc()) return [404, { detail: `Unknown service '${id}'.` }];
    if (!action && m === 'GET') return [200, svc()];
    if (action === 'login' && m === 'POST') {
      const meth = svc().methods.find((x) => x.id === body?.method && x.kind === 'link');
      if (!meth) return [404, { detail: 'No such sign-in method.' }];
      const cur = flows.get(id);
      if (cur && ['starting', 'waiting', 'verifying'].includes(cur.status) && cur.method !== meth.id) return [409, { detail: 'Another sign-in for this service is in progress. Cancel it first.' }];
      const f = {
        id: Math.random().toString(16).slice(2, 14), service: id, method: meth.id, status: 'waiting', startedMs: Date.now(),
        url: meth.id === 'device' ? 'https://auth.openai.com/codex/device' : `https://example.com/oauth/authorize?state=${Math.random().toString(36).slice(2)}`,
        user_code: meth.id === 'device' ? 'MOCK-12345' : null, needs_code: meth.needs_code, code_label: meth.code_label, code_help: meth.code_help,
        message: meth.needs_code ? 'Open the link, sign in, then paste the code here.' : 'Open the link and approve the sign-in; this page updates by itself.',
        started_at: new Date().toISOString(), expires_at: new Date(Date.now() + 900_000).toISOString(), finished_at: null,
      };
      flows.set(id, f);
      return [200, f];
    }
    services(); // advances device flows
    const f = flows.get(id);
    if (action === 'login' && m === 'GET') return f ? [200, f] : [404, { detail: 'No sign-in in progress.' }];
    if (action === 'login' && m === 'DELETE') {
      if (!f) return [404, { detail: 'No sign-in in progress.' }];
      Object.assign(f, { status: 'cancelled', message: 'Sign-in cancelled.', needs_code: false });
      return [200, f];
    }
    if (action === 'login/code' && m === 'POST') {
      if (!f || f.status !== 'waiting') return [409, { detail: 'This sign-in has already finished. Start a new one.' }];
      const ok = String(body?.code ?? '').trim() === 'good';
      Object.assign(f, ok ? { status: 'succeeded', message: 'Signed in (mock).' } : { status: 'failed', message: 'Login failed: Request failed with status code 400' }, { needs_code: false, finished_at: new Date().toISOString() });
      if (ok) signedIn.add(id);
      return [200, f];
    }
    if (action === 'credentials' && m === 'POST') {
      try {
        if (body?.method !== 'apikey') JSON.parse(body?.content ?? '');
      } catch {
        return [400, { detail: "That isn't valid JSON." }];
      }
      signedIn.add(id);
      return [200, { message: 'Credentials saved.', service: svc() }];
    }
    if (action === 'logout' && m === 'POST') {
      signedIn.delete(id);
      return [200, { message: 'Signed out.', service: svc() }];
    }
    if (action === 'verify' && m === 'POST') return [200, { ...svc(), verified: { ok: true, message: 'The key works (mock).', checked_at: new Date().toISOString() } }];
    return null;
  }

  return { handle };
}
