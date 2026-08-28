/**
 * Command registry — the extension's public surface to the backend.
 *
 * Every command targets the **active tab** (SPEC §0). There is no tabId
 * parameter: to act on another tab, `switch_tab` to it first. This mirrors
 * `chrome.tabs.captureVisibleTab`, which can only ever capture the active tab
 * of the focused window.
 *
 * Handlers run in the service worker. Anything touching page DOM is forwarded
 * to the content script via `sendToTab`, which re-injects the script first if
 * the tab was already open when the extension loaded (manifest content scripts
 * only auto-inject on navigation).
 */

// Both files, in order: the shim depends on the core's global.
const CONTENT_SCRIPT_FILES = [
  "src/content/snapshot-core.js",
  "src/content/content-script.js",
];
const DOM_TIMEOUT_MS = 15_000;
const DEFAULT_JS_TIMEOUT_MS = 15_000;
const DEFAULT_JS_MAX_CHARS = 20_000;
const CAPTURE_RETRIES = 4;

// --- tab plumbing -------------------------------------------------------

async function resolveActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab");
  return tab;
}

/** Content scripts can't run on chrome:// or the Web Store — fail clearly. */
function assertScriptable(tab) {
  const url = tab.url || "";
  if (/^(chrome|edge|about|devtools|chrome-extension|view-source):/.test(url) ||
      url.startsWith("https://chromewebstore.google.com")) {
    throw new Error(`cannot script restricted page: ${url || "unknown"}`);
  }
}

/**
 * Send a message to the tab's content script, unwrapping its {ok, result}
 * envelope into a value or a thrown error.
 */
async function sendToTab(tab, message) {
  assertScriptable(tab);

  let response;
  try {
    response = await chrome.tabs.sendMessage(tab.id, message);
  } catch {
    // No receiver yet — inject and retry once.
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: CONTENT_SCRIPT_FILES,
    });
    response = await chrome.tabs.sendMessage(tab.id, message);
  }

  if (!response) throw new Error("content script returned no response");
  if (!response.ok) throw new Error(response.error || "content script error");
  return response.result;
}

/** Reject rather than hang forever if a page never answers. */
function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)
    ),
  ]);
}

async function domCall(action, params, timeout = DOM_TIMEOUT_MS) {
  const tab = await resolveActiveTab();
  return withTimeout(sendToTab(tab, { action, params }), timeout, action);
}

/** Resolve once the tab finishes loading, so navigate() doesn't return early. */
function waitForTabLoad(tabId, timeoutMs = DOM_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`navigation timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function tabInfo(t) {
  return {
    tabId: t.id, windowId: t.windowId, index: t.index,
    title: t.title, url: t.url, active: t.active, status: t.status,
  };
}

// --- screenshot ---------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * captureVisibleTab is rate-limited (MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND).
 * Back off and retry rather than surfacing a transient quota error.
 */
async function captureWithRetry(windowId, options) {
  let lastError;
  for (let attempt = 0; attempt < CAPTURE_RETRIES; attempt += 1) {
    try {
      return await chrome.tabs.captureVisibleTab(windowId, options);
    } catch (err) {
      lastError = err;
      const message = String(err?.message || err);
      if (!/quota|MAX_CAPTURE/i.test(message)) throw err;
      await sleep(250 * (attempt + 1));
    }
  }
  throw lastError;
}

/** Derive the backend's HTTP origin from the configured WebSocket URL. */
async function backendOrigin() {
  const { backendUrl } = await chrome.storage.local.get("backendUrl");
  const url = new URL(backendUrl || "ws://127.0.0.1:8765/api/browser/ws");
  url.protocol = url.protocol === "wss:" ? "https:" : "http:";
  return url.origin;
}

async function uploadBlob(blob, filename) {
  const origin = await backendOrigin();
  const form = new FormData();
  form.append("file", blob, filename);
  const resp = await fetch(`${origin}/api/uploads`, { method: "POST", body: form });
  if (!resp.ok) throw new Error(`upload failed: HTTP ${resp.status}`);
  return await resp.json();
}

// --- JS injection --------------------------------------------------------

/**
 * Wrap agent code so it runs as an async body and returns a JSON *string*.
 *
 * Returning a string sidesteps structured-clone entirely: DOM nodes, functions
 * and circular references would all either throw or silently vanish crossing
 * the world boundary. Instead they're converted to descriptors here, in the
 * page, where they still mean something.
 *
 * DOM nodes come back as `{__element, tag, selector, text}` — a selector the
 * agent can feed straight back into click/fill. The content script's ref map
 * lives in the isolated world and is unreachable from here, so a selector is
 * the actionable reference available.
 */
export function buildWrapper(code, maxChars) {
  return `(async () => {
  const __seen = new WeakSet();
  const __sel = (el) => {
    try {
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
        return '#' + CSS.escape(el.id);
      }
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1 && node !== document.documentElement) {
        const parent = node.parentElement;
        if (!parent) break;
        const i = Array.prototype.indexOf.call(parent.children, node) + 1;
        parts.unshift(node.tagName.toLowerCase() + ':nth-child(' + i + ')');
        const candidate = parts.join(' > ');
        if (document.querySelectorAll(candidate).length === 1) return candidate;
        node = parent;
      }
      return parts.length ? parts.join(' > ') : null;
    } catch (e) { return null; }
  };
  const __replacer = (key, value) => {
    if (value instanceof Window) return '[Window]';
    if (typeof value === 'function') return '[Function ' + (value.name || 'anonymous') + ']';
    if (value instanceof Element) {
      return { __element: true, tag: value.tagName.toLowerCase(), selector: __sel(value),
               text: (value.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120) };
    }
    if (typeof NodeList !== 'undefined' && (value instanceof NodeList || value instanceof HTMLCollection)) {
      return Array.prototype.slice.call(value);
    }
    if (value instanceof Node) {
      return { __node: true, nodeType: value.nodeType,
               text: (value.textContent || '').slice(0, 120) };
    }
    if (value && typeof value === 'object') {
      if (__seen.has(value)) return '[Circular]';
      __seen.add(value);
    }
    return value;
  };
  try {
    const __value = await (async () => { ${code} })();
    let __json;
    try {
      __json = JSON.stringify(__value === undefined ? null : __value, __replacer);
    } catch (e) {
      __json = JSON.stringify('[Unserializable: ' + String(e && e.message || e) + ']');
    }
    if (__json === undefined) __json = 'null';
    const __over = __json.length > ${maxChars};
    return JSON.stringify({
      ok: true,
      json: __over ? __json.slice(0, ${maxChars}) : __json,
      truncated: __over,
    });
  } catch (e) {
    return JSON.stringify({
      ok: false,
      error: String((e && e.message) || e),
      stack: e && e.stack ? String(e.stack) : null,
    });
  }
})()`;
}

function userScriptsUnavailable() {
  return new Error(
    "userScripts_unavailable: enable \"Allow User Scripts\" for this extension at " +
    `chrome://extensions/?id=${chrome.runtime.id}. Chrome 138+ defaults it to off, ` +
    "and the API reads as undefined (rather than throwing) until it is enabled."
  );
}

async function runInWorld(tabId, wrapped, world, timeout) {
  const results = await withTimeout(
    chrome.userScripts.execute({
      target: { tabId },
      js: [{ code: wrapped }],
      world,
      injectImmediately: true,
    }),
    timeout,
    "execute_js",
  );

  const first = Array.isArray(results) ? results[0] : results;
  if (first?.error) throw new Error(`injection_failed: ${first.error}`);
  return first?.result;
}

function looksLikeCspFailure(message) {
  return /content security policy|unsafe-eval|refused to|EvalError/i.test(message);
}

// --- command handlers ----------------------------------------------------

const handlers = {
  /** Liveness probe — answers without touching any tab. */
  async ping() {
    return { pong: true, at: new Date().toISOString() };
  },

  /** Every open tab, plus the windows containing them. */
  async list_tabs() {
    const [tabs, windows] = await Promise.all([
      chrome.tabs.query({}),
      chrome.windows.getAll({}),
    ]);
    return {
      tabs: tabs.map(tabInfo),
      windows: windows.map((w) => ({
        windowId: w.id, focused: w.focused, state: w.state, type: w.type,
      })),
    };
  },

  async get_active_tab() {
    return tabInfo(await resolveActiveTab());
  },

  /**
   * Make a tab active. Since all other commands implicitly target the active
   * tab, this is how the agent reaches anything else.
   */
  async switch_tab(params) {
    if (params.tabId === undefined || params.tabId === null) {
      throw new Error("switch_tab requires 'tabId'");
    }
    const tabId = Number(params.tabId);
    const target = await chrome.tabs.get(tabId);

    await chrome.tabs.update(tabId, { active: true });
    // Crossing windows also needs the window focused, or captureVisibleTab
    // would still target the previously focused window.
    await chrome.windows.update(target.windowId, { focused: true });

    const settled = await chrome.tabs.get(tabId);
    return tabInfo(settled);
  },

  /**
   * Open a URL in a NEW tab, rather than replacing the active one.
   *
   * Active by default, which keeps the active-tab constraint coherent: the new
   * tab becomes the target of every subsequent command with no `switch` step.
   * `background: true` opens it without focus — useful for staging tabs, but
   * then you must `switch_tab` to it before acting, since commands still go to
   * whatever is active.
   */
  async open_tab(params) {
    if (!params.url) throw new Error("open_tab requires 'url'");
    const active = params.background !== true;

    const tab = await chrome.tabs.create({ url: params.url, active });
    // A backgrounded tab still loads, so waiting is meaningful either way.
    await waitForTabLoad(tab.id);

    if (active) {
      // chrome.tabs.create puts the tab in the current window, but that window
      // may not be the focused one; without this, captureVisibleTab and the
      // active-tab lookups could still resolve elsewhere.
      await chrome.windows.update(tab.windowId, { focused: true });
    }

    const settled = await chrome.tabs.get(tab.id);
    return { ...tabInfo(settled), opened: true, background: !active };
  },

  /** Navigate the active tab and wait for the load to complete. */
  async navigate(params) {
    if (!params.url) throw new Error("navigate requires 'url'");
    const tab = await resolveActiveTab();
    const updated = await chrome.tabs.update(tab.id, { url: params.url });
    await waitForTabLoad(updated.id);
    return tabInfo(await chrome.tabs.get(updated.id));
  },

  /**
   * Capture the visible viewport of the active tab.
   *
   * Returns the full coordinate envelope (PLAN §0.1) so proportional
   * coordinates from a snapshot can be reconciled with screenshot pixels.
   */
  async capture_screenshot(params) {
    const tab = await resolveActiveTab();
    const viewport = await sendToTab(tab, { action: "get_viewport", params: {} });

    const format = params.format === "png" ? "png" : "jpeg";
    const options = { format };
    if (format === "jpeg") options.quality = Number(params.quality) || 80;

    const dataUrl = await captureWithRetry(tab.windowId, options);
    const blob = await (await fetch(dataUrl)).blob();

    const bitmap = await createImageBitmap(blob);
    const width = bitmap.width;
    const height = bitmap.height;
    bitmap.close();

    const result = {
      width, height,
      viewportCssWidth: viewport.cssWidth,
      viewportCssHeight: viewport.cssHeight,
      devicePixelRatio: viewport.devicePixelRatio,
      scrollX: viewport.scrollX,
      scrollY: viewport.scrollY,
      format,
      url: tab.url,
      capturedAt: new Date().toISOString(),
    };

    if (params.inline) {
      result.dataUrl = dataUrl;
      return result;
    }

    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const upload = await uploadBlob(blob, `screenshot-${stamp}.${format === "png" ? "png" : "jpg"}`);
    result.uploadUrl = upload.url;
    result.path = upload.path;
    result.size = upload.size;
    return result;
  },

  /** Structured markdown + actionable element references for the page. */
  async snapshot(params) {
    return domCall("snapshot", params, Number(params.timeout) || 30_000);
  },

  async click(params) {
    return domCall("click", params);
  },

  async fill(params) {
    return domCall("fill", params);
  },

  async scroll(params) {
    return domCall("scroll", params);
  },

  /**
   * Run arbitrary JavaScript in the page.
   *
   * MAIN world by default: it shares the page's JS context, which is what
   * "like the DevTools console" means — page globals, framework internals,
   * app state. USER_SCRIPT is CSP-exempt but isolated, so it sees the DOM
   * without the page's own JS.
   */
  async execute_js(params) {
    if (typeof params.code !== "string" || !params.code.trim()) {
      throw new Error("execute_js requires 'code'");
    }
    if (!chrome.userScripts || typeof chrome.userScripts.execute !== "function") {
      throw userScriptsUnavailable();
    }

    const tab = await resolveActiveTab();
    assertScriptable(tab);

    const timeout = Number(params.timeout) || DEFAULT_JS_TIMEOUT_MS;
    const maxChars = Number(params.maxChars) || DEFAULT_JS_MAX_CHARS;
    const wrapped = buildWrapper(params.code, maxChars);
    const requested = params.world === "USER_SCRIPT" ? "USER_SCRIPT" : "MAIN";

    let raw;
    let worldUsed = requested;
    try {
      raw = await runInWorld(tab.id, wrapped, requested, timeout);
    } catch (err) {
      const message = String(err?.message || err);
      const canFallback = requested === "MAIN" &&
                          params.fallback !== false &&
                          looksLikeCspFailure(message);
      if (!canFallback) throw err;
      // CSP blocked the main world. USER_SCRIPT is exempt, but loses access to
      // the page's own globals — so the caller is told which world ran.
      worldUsed = "USER_SCRIPT";
      raw = await runInWorld(tab.id, wrapped, "USER_SCRIPT", timeout);
    }

    let payload;
    try {
      payload = typeof raw === "string" ? JSON.parse(raw) : raw;
    } catch {
      throw new Error(`unparseable injection result: ${String(raw).slice(0, 200)}`);
    }
    if (!payload) throw new Error("injection returned no result");

    if (!payload.ok) {
      const err = new Error(payload.error || "script threw");
      err.stack = payload.stack || err.stack;
      throw err;
    }

    // A truncated payload is no longer valid JSON, so hand it back as text
    // rather than failing to parse something the agent can still read.
    let value;
    try {
      value = JSON.parse(payload.json);
    } catch {
      return { world: worldUsed, truncated: true, text: payload.json };
    }
    return { world: worldUsed, truncated: Boolean(payload.truncated), result: value };
  },
};

export async function dispatch(command, params) {
  const handler = handlers[command];
  if (!handler) {
    throw new Error(`unknown command: ${command}`);
  }
  return await handler(params || {});
}

export const COMMANDS = Object.keys(handlers);
