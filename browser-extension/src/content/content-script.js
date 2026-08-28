/**
 * Content script — messaging shim over `snapshot-core.js`.
 *
 * All DOM logic lives in the core; this file only bridges it to
 * `chrome.runtime` messaging. Keeping the two apart lets the core be loaded
 * into a plain page for testing without stubbing extension APIs.
 *
 * Runs in an isolated world: shares the DOM with the page but not its JS
 * context. Injected both declaratively (manifest, on navigation) and
 * programmatically (from commands.js, for tabs already open) — hence the
 * reentry guard.
 *
 * Not a module: manifest content scripts don't support ES module syntax.
 */

(() => {
  if (window.__archieContentScriptLoaded) return;
  window.__archieContentScriptLoaded = true;

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    const core = globalThis.__archieCore;
    if (!core) {
      sendResponse({ ok: false, error: "snapshot core not loaded" });
      return false;
    }

    const action = core[msg?.action];
    if (typeof action !== "function") {
      sendResponse({ ok: false, error: `unknown action: ${msg?.action}` });
      return false;
    }

    try {
      sendResponse({ ok: true, result: action(msg.params || {}) });
    } catch (err) {
      sendResponse({
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
    return false;
  });
})();
