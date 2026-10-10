/**
 * REST half of the mock backend: the endpoints of inventory 01 §3 with synthetic data from
 * mock-server/data/, plus history for live mock runs and scenarios. In-memory: renames, deletes,
 * config writes and uploads last until the process exits.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createAccounts } from './accounts.mjs';
import { DATA_DIR } from './scenarios.mjs';

const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));

function memoryTree(root, rel = '') {
  const dir = path.join(root, rel);
  if (!fs.existsSync(dir)) return [];
  const entries = fs.readdirSync(dir, { withFileTypes: true }).filter((d) => !d.name.startsWith('.'));
  const dirs = [];
  const files = [];
  for (const d of entries) {
    const p = rel ? `${rel}/${d.name}` : d.name;
    if (d.isDirectory()) {
      const children = memoryTree(root, p);
      if (children.length) dirs.push({ name: d.name, path: p, is_dir: true, children });
    } else if (d.name.toLowerCase().endsWith('.md')) {
      files.push({ name: d.name, path: p, is_dir: false, children: null });
    }
  }
  const byName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  return [...dirs.sort(byName), ...files.sort(byName)];
}

function walkHtml(root, rel = '', out = []) {
  const dir = path.join(root, rel);
  if (!fs.existsSync(dir)) return out;
  for (const d of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = rel ? `${rel}/${d.name}` : d.name;
    if (d.isDirectory()) walkHtml(root, p, out);
    else if (d.name.endsWith('.html')) out.push(p);
  }
  return out;
}

function htmlTitle(file, rel) {
  const m = /<title>([^<]*)<\/title>/i.exec(fs.readFileSync(file, 'utf8').slice(0, 8192));
  if (m && m[1].trim()) return m[1].trim();
  const parts = rel.split('/');
  const base = parts[parts.length - 1] === 'index.html' && parts.length > 1 ? parts[parts.length - 2] : parts[parts.length - 1].replace(/\.html$/, '');
  return base.replace(/[-_]+/g, ' ');
}

/** Page a MessagePreview list like manager/store.py:286-296. */
export function pageMessages(all, limit = 50, before) {
  const end = before === undefined ? all.length : Math.max(0, Math.min(before, all.length));
  const start = Math.max(0, end - limit);
  return { messages: all.slice(start, end), total_count: all.length, has_more: start > 0, start_index: start };
}

const CONFIG_RANGES = {
  voice_vad_threshold: [0.15, 0.5],
  voice_vad_min_silence_ms: [800, 5000],
  voice_mic_gain: [0.5, 2.0],
};

const SESSION_CONFIG_DEFAULTS = {
  working_directory: null,
  enabled_mcps: null,
  chrome_extension: null,
  provider: null,
  harness_model: null,
  harness_options: null,
};

/** `manager/harness_catalog.py` validate_options: unknown keys and bad values → a message; null always passes. */
export function validateHarnessOptions(catalog, opts) {
  if (opts === null || opts === undefined) return null;
  if (typeof opts !== 'object' || Array.isArray(opts)) return 'harness options must be an object';
  for (const [key, value] of Object.entries(opts)) {
    const opt = catalog?.options.find((o) => o.key === key);
    if (!opt) return `Unknown option '${key}'; expected one of [${(catalog?.options ?? []).map((o) => `'${o.key}'`).join(', ')}]`;
    if (value === null) continue;
    if (opt.kind === 'toggle' && typeof value !== 'boolean') return `${key} must be true/false (got ${JSON.stringify(value)})`;
    if (opt.kind === 'number') {
      if (typeof value !== 'number') return `${key} must be a number (got ${JSON.stringify(value)})`;
      if (opt.min !== undefined && value < opt.min) return `${key} must be ≥ ${opt.min} (got ${value})`;
      if (opt.max !== undefined && value > opt.max) return `${key} must be ≤ ${opt.max} (got ${value})`;
    }
    if (opt.kind === 'select') {
      if (typeof value !== 'string') return `${key} must be a string (got ${JSON.stringify(value)})`;
      if (opt.choices?.length && !opt.choices.some((c) => c.value === value)) return `${key} must be one of [${opt.choices.map((c) => `'${c.value}'`).join(', ')}] (got '${value}')`;
    }
  }
  return null;
}

export function createRest(engine, opts = {}) {
  const dataDir = opts.dataDir ?? DATA_DIR;
  const catalogs = readJson(path.join(dataDir, 'catalogs.json'));
  const harnessFile = path.join(dataDir, 'harnesses.json');
  // GET /api/config/harnesses (models + options per harness); absent = an older server (404).
  const harnesses = fs.existsSync(harnessFile) ? readJson(harnessFile).harnesses : null;
  const catalogOf = (provider) => harnesses?.find((h) => h.id === provider)?.catalog ?? null;
  const providerIds = () => (harnesses ? harnesses.map((h) => h.id) : catalogs.providers.providers.map((x) => x.id));
  const sessions = readJson(path.join(dataDir, 'sessions.json'));
  const messages = new Map();
  const msgDir = path.join(dataDir, 'messages');
  if (fs.existsSync(msgDir))
    for (const f of fs.readdirSync(msgDir)) if (f.endsWith('.json')) messages.set(f.slice(0, -5), readJson(path.join(msgDir, f)));
  let config = readJson(path.join(dataDir, 'config.json'));
  const sessionConfigs = new Map();
  const titles = new Map();
  const uploads = new Map();
  const debugLog = [];
  const accounts = createAccounts();
  const memoryRoot = path.join(dataDir, 'memory');
  const publicRoot = path.join(dataDir, 'public');

  function send(res, status, body, headers = {}) {
    const isText = typeof body === 'string' || Buffer.isBuffer(body);
    res.writeHead(status, {
      'access-control-allow-origin': '*',
      ...(body === undefined ? {} : { 'content-type': isText ? 'text/plain; charset=utf-8' : 'application/json' }),
      ...headers,
    });
    res.end(body === undefined ? undefined : isText ? body : JSON.stringify(body));
  }
  const json = (res, status, body) => send(res, status, body);
  const notFound = (res, detail = 'Not Found') => json(res, 404, { detail });

  function readBody(req) {
    return new Promise((resolve) => {
      const chunks = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', () => resolve(Buffer.alloc(0)));
    });
  }
  async function readJsonBody(req) {
    try {
      return JSON.parse((await readBody(req)).toString('utf8') || '{}');
    } catch {
      return null;
    }
  }

  function sessionList() {
    const rows = [];
    const seen = new Set();
    for (const run of engine.liveRuns()) {
      if (!run.sdkId) continue; // like the backend: a session is listed once its JSONL exists (first turn)
      const sid = run.sdkId;
      seen.add(sid);
      const stored = sessions.find((s) => s.session_id === sid); // a resumed past session keeps its title
      rows.push({
        session_id: sid,
        started_at: run.startedAt,
        last_activity: run.lastActivity,
        title: titles.get(sid) ?? stored?.title ?? run.title(),
        message_count: (pastOf(run)?.length ?? 0) + run.history.length,
        is_orchestrator: run.kind === 'orchestrator',
        provider: run.provider ?? 'claude',
        local_id: run.localId,
      });
    }
    for (const s of sessions) if (!seen.has(s.session_id)) rows.push({ ...s, title: titles.get(s.session_id) ?? s.title });
    for (const [name, fx] of engine.scenarios) {
      const sid = `scn-${name}`;
      if (seen.has(sid)) continue;
      rows.push({
        session_id: sid,
        started_at: '2026-10-03T12:00:00+00:00',
        last_activity: '2026-10-03T12:00:00+00:00',
        title: titles.get(sid) ?? `Scenario: ${name}`,
        message_count: 0,
        is_orchestrator: fx.session.kind === 'orchestrator',
        provider: fx.session.provider ?? 'claude',
        local_id: null,
      });
    }
    return rows;
  }

  /** History for an sdk id: a live run / scenario, else the static data. */
  // A closed run keeps its transcript and its history row, like the backend's JSONL file.
  engine.onRunClosed = (run) => {
    if (run.fixture || !run.sdkId) return;
    messages.set(run.sdkId, (pastOf(run) ?? []).concat(run.history));
    if (!sessions.some((s) => s.session_id === run.sdkId))
      sessions.unshift({
        session_id: run.sdkId,
        started_at: run.startedAt,
        last_activity: run.lastActivity,
        title: run.title(),
        message_count: messages.get(run.sdkId).length,
        is_orchestrator: run.kind === 'orchestrator',
        provider: run.provider ?? 'claude',
        local_id: null,
      });
  };

  /** The stored transcript a live run resumed (`start{resume_sdk_id}` of a past session), like the backend's JSONL. */
  function pastOf(run) {
    return !run.fixture && run.sdkId ? messages.get(run.sdkId) : undefined;
  }

  function historyPage(id, limit, before) {
    const run = engine.runForSdk(id);
    if (run) {
      const past = pastOf(run);
      return past ? pageMessages(past.concat(run.history), limit, before) : run.historyPage(limit, before);
    }
    const scn = id.startsWith('scn-') ? engine.scenarios.get(id.slice(4)) : undefined;
    if (scn) return scn.initial_history ?? null;
    const all = messages.get(id);
    return all ? pageMessages(all, limit, before) : null;
  }

  async function handle(req, res) {
    const url = new URL(req.url, 'http://mock');
    const p = decodeURIComponent(url.pathname);
    const m = req.method;
    if (m === 'OPTIONS') return send(res, 204, undefined, { 'access-control-allow-methods': '*', 'access-control-allow-headers': '*' });

    // auth
    if (p === '/api/auth/status' && m === 'GET') return json(res, 200, catalogs.auth_status);
    if (p === '/api/auth/login' && m === 'POST') return json(res, 200, catalogs.auth_status);
    if (p === '/api/auth/credentials' && m === 'POST') {
      const body = await readJsonBody(req);
      const ok = !!body && typeof body.credentials_json === 'string' && body.credentials_json.includes('accessToken');
      return json(res, 200, { authenticated: ok, auth_url: null, headless: true });
    }

    // Settings → Accounts + env keys (accounts.mjs)
    if (p.startsWith('/api/accounts') || p.startsWith('/api/env')) {
      const r = await accounts.handle(p, m, m === 'GET' || m === 'DELETE' ? null : await readJsonBody(req));
      if (r) return json(res, r[0], r[1]);
    }

    // sessions
    if (p === '/api/sessions' && m === 'GET') return json(res, 200, sessionList());
    if (p === '/api/sessions/pool/live' && m === 'GET') return json(res, 200, engine.poolRows());
    if (p === '/api/sessions/inject' && m === 'POST') {
      const body = await readJsonBody(req);
      if (!body || !body.text) return json(res, 400, { detail: 'text is required' });
      const run = engine.runs.get(body.local_id) ?? engine.runForSdk(body.sdk_session_id);
      if (!run) return notFound(res, 'Session not live');
      run.injectFromRest(body.text);
      return json(res, 200, { ok: true, local_id: run.localId });
    }
    let mm = /^\/api\/sessions\/([^/]+)\/close$/.exec(p);
    if (mm && m === 'POST') {
      engine.closeRun(mm[1]);
      return send(res, 204);
    }
    mm = /^\/api\/sessions\/([^/]+)(?:\/(messages|preview|config|rename|duplicate|truncate|fork))?$/.exec(p);
    if (mm) {
      const [, id, sub] = mm;
      if (sub === 'messages' && m === 'GET') {
        const limit = Math.min(200, Math.max(1, Number(url.searchParams.get('limit') ?? 50) || 50));
        const b = url.searchParams.get('before');
        const pg = historyPage(id, limit, b === null ? undefined : Number(b));
        return pg ? json(res, 200, pg) : notFound(res, `Session '${id}' not found`);
      }
      if (sub === 'preview' && m === 'GET') {
        const pg = historyPage(id, Number(url.searchParams.get('max') ?? 5) || 5);
        return pg ? json(res, 200, pg.messages) : notFound(res);
      }
      if (sub === 'config' && m === 'GET') {
        return json(res, 200, { ...SESSION_CONFIG_DEFAULTS, ...sessionConfigs.get(id) });
      }
      if (sub === 'config' && m === 'PUT') {
        const body = (await readJsonBody(req)) ?? {};
        const keep = {};
        for (const k of Object.keys(SESSION_CONFIG_DEFAULTS)) if (k in body) keep[k] = body[k];
        if (keep.harness_options) {
          // validated against the catalog of the harness the session will run (body, saved, global)
          const provider = keep.provider || sessionConfigs.get(id)?.provider || config.provider;
          const err = validateHarnessOptions(catalogOf(provider), keep.harness_options);
          if (err) return json(res, 400, { detail: err });
        }
        sessionConfigs.set(id, { ...sessionConfigs.get(id), ...keep });
        return json(res, 200, { ...SESSION_CONFIG_DEFAULTS, ...sessionConfigs.get(id) });
      }
      if (sub === 'rename' && m === 'PATCH') {
        const body = await readJsonBody(req);
        if (!body || typeof body.title !== 'string' || !body.title.trim()) return json(res, 400, { detail: 'title is required' });
        if (!historyPage(id, 1)) return notFound(res, `Session '${id}' not found`);
        titles.set(id, body.title.trim());
        return send(res, 204);
      }
      if (sub === 'duplicate' && m === 'POST') {
        const all = messages.get(id);
        if (!all) return notFound(res, `Session '${id}' not found`);
        const nid = `mock-copy-${Date.now().toString(36)}`;
        messages.set(nid, all.slice());
        const src = sessions.find((s) => s.session_id === id);
        sessions.unshift({ ...(src ?? {}), session_id: nid, title: `${titles.get(id) ?? src?.title ?? id} (copy)` });
        return json(res, 201, { session_id: nid });
      }
      if ((sub === 'truncate' || sub === 'fork') && m === 'POST') {
        const body = await readJsonBody(req);
        const n = body?.drop_last_n;
        if (!Number.isInteger(n) || n < 0) return json(res, 400, { detail: 'drop_last_n must be a non-negative integer' });
        if (sub === 'truncate' && engine.runForSdk(id)) return json(res, 409, { detail: 'Session is currently open. Close the tab before rewinding.' });
        const liveRun = engine.runForSdk(id); // forking a live session copies what its JSONL holds now
        const all = liveRun && !liveRun.fixture ? (pastOf(liveRun) ?? []).concat(liveRun.history) : messages.get(id);
        if (!all) return notFound(res, `Session '${id}' not found`);
        const visible = all.map((x, i) => [x, i]).filter(([x]) => x.role === 'assistant' || !(x.blocks?.length && x.blocks.every((b) => b.type === 'tool_result')));
        if (n > visible.length) return notFound(res, 'drop_last_n out of range');
        const cut = n === 0 ? all.length : visible[visible.length - n][1];
        if (sub === 'truncate') {
          messages.set(id, all.slice(0, cut));
          return json(res, 200, { session_id: id });
        }
        const nid = `mock-fork-${Date.now().toString(36)}`;
        messages.set(nid, all.slice(0, cut));
        const src = sessions.find((s) => s.session_id === id);
        sessions.unshift({ ...(src ?? {}), session_id: nid, title: `${titles.get(id) ?? src?.title ?? id} (fork)` });
        return json(res, 201, { session_id: nid });
      }
      if (!sub && m === 'DELETE') {
        const i = sessions.findIndex((s) => s.session_id === id);
        if (i < 0 && !messages.has(id)) return notFound(res, `Session '${id}' not found`);
        if (i >= 0) sessions.splice(i, 1);
        messages.delete(id);
        titles.delete(id);
        return send(res, 204);
      }
      if (!sub && m === 'GET') {
        const all = messages.get(id);
        const s = sessions.find((x) => x.session_id === id);
        return all && s ? json(res, 200, { ...s, is_orchestrator: false, local_id: null, messages: all }) : notFound(res, `Session '${id}' not found`);
      }
    }

    // config & catalogs
    if (p === '/api/config' && m === 'GET') return json(res, 200, config);
    if (p === '/api/config' && m === 'PUT') {
      const body = await readJsonBody(req);
      if (!body || typeof body !== 'object') return json(res, 422, { detail: [{ msg: 'invalid body' }] });
      for (const [k, [lo, hi]] of Object.entries(CONFIG_RANGES))
        if (k in body && !(typeof body[k] === 'number' && body[k] >= lo && body[k] <= hi)) return json(res, 400, { detail: `${k} must be between ${lo} and ${hi}` });
      if ('provider' in body && !providerIds().includes(body.provider)) return json(res, 400, { detail: `Unknown provider '${body.provider}'` });
      if ('harness_model' in body) body.harness_model = { ...config.harness_model, ...body.harness_model };
      if ('harness_options' in body) {
        // per-key merge per provider; null deletes the key (= CLI default)
        const all = { ...(config.harness_options ?? {}) };
        for (const [prov, opts] of Object.entries(body.harness_options ?? {})) {
          if (!providerIds().includes(prov)) return json(res, 400, { detail: `Unknown harness provider '${prov}'` });
          const err = validateHarnessOptions(catalogOf(prov), opts);
          if (err) return json(res, 400, { detail: `harness_options['${prov}']: ${err}` });
          const cur = { ...(all[prov] ?? {}) };
          for (const [k, v] of Object.entries(opts ?? {})) {
            if (v === null) delete cur[k];
            else cur[k] = v;
          }
          all[prov] = cur;
        }
        body.harness_options = all;
      }
      config = { ...config, ...body };
      return json(res, 200, config);
    }
    if (p === '/api/config/harnesses' && harnesses) return json(res, 200, { harnesses });
    mm = /^\/api\/config\/harness\/([^/]+)\/catalog$/.exec(p);
    if (mm && harnesses) {
      const h = harnesses.find((x) => x.id === mm[1]);
      if (!h) return notFound(res, `Unknown harness '${mm[1]}'`);
      return json(res, 200, h.catalog ?? { provider: h.id, models: [], options: [], default_model: null, allow_custom_model: true, warnings: [] });
    }
    if (p === '/api/config/providers') return json(res, 200, harnesses ? { providers: harnesses.map(({ id, label, description }) => ({ id, label, description })) } : catalogs.providers);
    if (p === '/api/config/harness/qwen/models') return json(res, 200, catalogs.qwen_models);
    if (p === '/api/config/voice/google/models') return json(res, 200, catalogs.google_voice_models);
    if (p === '/api/config/openai-key') return notFound(res, 'OpenAI key not configured');
    if (p === '/api/orchestrator/models') return json(res, 200, catalogs.orchestrator_models);
    if (p === '/api/orchestrator/models/audio')
      return json(res, 200, { models: catalogs.orchestrator_models.models.filter((x) => x.supports_audio) });
    if (p === '/api/orchestrator/voice/models') return json(res, 200, catalogs.voice_models);
    if (p === '/api/orchestrator/voice/session' && m === 'POST') return json(res, 503, { detail: 'The mock backend has no voice provider.' });
    if (p === '/api/mcp/servers') return json(res, 200, catalogs.mcp_servers);
    mm = /^\/api\/mcp\/servers\/(.+)$/.exec(p);
    if (mm) {
      const cfg = catalogs.mcp_servers.servers[mm[1]];
      return cfg ? json(res, 200, { name: mm[1], config: cfg }) : notFound(res, `MCP server '${mm[1]}' not found`);
    }
    if (p === '/api/skills') return json(res, 200, catalogs.skills);
    if (p === '/api/agents') return json(res, 200, catalogs.agents);

    // memory
    if (p === '/api/memory/tree') return json(res, 200, memoryTree(memoryRoot));
    if (p === '/memory' || p === '/memory/' || p.startsWith('/memory/')) {
      const rel = p === '/memory' || p === '/memory/' ? 'MEMORY.md' : p.slice('/memory/'.length);
      const file = path.resolve(memoryRoot, rel);
      if (!file.startsWith(memoryRoot + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return notFound(res);
      return send(res, 200, fs.readFileSync(file, 'utf8'), { 'content-type': 'text/markdown; charset=utf-8' });
    }

    // visualizations
    if (p === '/api/visualizations' && m === 'GET') {
      const list = walkHtml(publicRoot).map((rel) => {
        const st = fs.statSync(path.join(publicRoot, rel));
        return {
          path: rel,
          url: `/${rel}`,
          title: titles.get(`viz:${rel}`) ?? htmlTitle(path.join(publicRoot, rel), rel),
          created: st.birthtime.toISOString().replace('Z', '+00:00'),
          modified: st.mtime.toISOString().replace('Z', '+00:00'),
          size: st.size,
        };
      });
      list.sort((a, b) => (a.modified < b.modified ? 1 : -1));
      return json(res, 200, list);
    }
    if (p === '/api/visualizations/rename' && m === 'PATCH') {
      const body = await readJsonBody(req);
      if (!body?.path) return json(res, 400, { detail: 'path is required' });
      if (!body?.title?.trim()) return json(res, 400, { detail: 'title is required' });
      if (!walkHtml(publicRoot).includes(body.path)) return notFound(res);
      titles.set(`viz:${body.path}`, body.title.trim());
      return send(res, 204);
    }
    if (p === '/api/visualizations/cast') {
      if (m === 'GET') return json(res, 200, { available: false, reason: 'The mock backend has no TV.' });
      const body = await readJsonBody(req);
      if (!walkHtml(publicRoot).includes(body?.path)) return notFound(res);
      return json(res, 200, { ok: false, message: 'The mock backend has no TV.' });
    }

    // uploads (G-1: optional nginx 1 MiB HTML 413)
    if (p === '/api/uploads' && m === 'POST') {
      const body = await readBody(req);
      if (opts.nginx413 && body.length > 1024 * 1024)
        return send(res, 413, '<html><head><title>413 Request Entity Too Large</title></head><body><center><h1>413 Request Entity Too Large</h1></center><hr><center>nginx</center></body></html>', { 'content-type': 'text/html' });
      const ct = req.headers['content-type'] ?? '';
      const fn = /filename="([^"]*)"/.exec(body.toString('latin1'))?.[1] ?? 'upload';
      const safe = fn.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._-]+|[._-]+$/g, '') || 'upload';
      const stored = `${new Date().toISOString().replace(/[-:.]/g, '').replace('Z', '')}-${safe}`;
      uploads.set(stored, body);
      return json(res, 200, { filename: safe, path: `/home/mock/project/context/uploads/${stored}`, url: `/uploads/${stored}`, size: body.length, content_type: ct.startsWith('multipart/') ? 'application/octet-stream' : ct });
    }
    if (p.startsWith('/uploads/') && uploads.has(p.slice(9))) return send(res, 200, uploads.get(p.slice(9)), { 'content-type': 'application/octet-stream' });

    // debug log
    if (p === '/api/debug/log' && m === 'POST') {
      const body = await readJsonBody(req);
      if (body) debugLog.push(`[${body.ts ?? new Date().toISOString()}] [${String(body.level ?? 'log').toUpperCase()}] ${body.msg ?? ''}`);
      return send(res, 204);
    }
    if (p === '/api/debug/log' && m === 'GET') return send(res, 200, debugLog.length ? `${debugLog.join('\n')}\n` : 'No logs yet.\n');

    // static visualization files (unknown paths: the SPA shell, like the backend, G-38)
    if (m === 'GET' && !p.startsWith('/api/')) {
      const file = path.resolve(publicRoot, `.${p}`);
      if (file.startsWith(publicRoot + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile())
        return send(res, 200, fs.readFileSync(file), { 'content-type': 'text/html; charset=utf-8' });
      return send(res, 200, '<!doctype html><html><head><title>Archie</title></head><body><div id="root"></div></body></html>', { 'content-type': 'text/html; charset=utf-8' });
    }
    return notFound(res);
  }

  return { handle, sessionList };
}
