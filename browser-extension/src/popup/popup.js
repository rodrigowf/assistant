/** Popup UI — reads status from the service worker and edits the backend URL. */

const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
const urlEl = document.getElementById("url");
const tokenEl = document.getElementById("token");
const errorEl = document.getElementById("error");
const commandsEl = document.getElementById("commands");

const LABELS = {
  connected: "Connected",
  connecting: "Connecting…",
  disconnected: "Disconnected",
};

function render(state) {
  const status = state?.status || "disconnected";
  dot.className = `dot ${status}`;
  statusEl.textContent = LABELS[status] || status;

  // Don't clobber what the user is currently typing.
  if (document.activeElement !== urlEl) {
    urlEl.value = state?.url || "";
  }

  // The token is never read back out of the service worker — only whether one
  // is stored. Leaving the field blank with a "saved" placeholder avoids
  // rendering the secret into the popup DOM on every poll.
  if (document.activeElement !== tokenEl && !tokenEl.value) {
    tokenEl.placeholder = state?.hasToken
      ? "•••••••••• (saved — type to replace)"
      : "BROWSER_CONTROL_TOKEN from context/.env";
  }

  if (state?.lastError && status !== "connected") {
    errorEl.textContent = state.lastError;
    errorEl.hidden = false;
  } else {
    errorEl.hidden = true;
  }

  commandsEl.textContent = state?.commands?.length
    ? `Commands: ${state.commands.join(", ")}`
    : "";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "get_status" }, (state) => {
    if (chrome.runtime.lastError) {
      // Worker asleep or mid-restart; the next poll picks it up.
      render({ status: "disconnected", lastError: chrome.runtime.lastError.message });
      return;
    }
    render(state);
  });
}

document.getElementById("save").addEventListener("click", () => {
  const url = urlEl.value.trim();
  if (!/^wss?:\/\//.test(url)) {
    errorEl.textContent = "URL must start with ws:// or wss://";
    errorEl.hidden = false;
    return;
  }

  const token = tokenEl.value.trim();
  const done = () => {
    tokenEl.value = "";  // don't leave the secret sitting in the field
    refresh();
  };

  // Save the token first so the reconnect triggered by the URL change already
  // carries it — otherwise the first attempt fails auth and waits out a
  // backoff before the token takes effect.
  if (token) {
    chrome.runtime.sendMessage({ type: "set_token", token }, () => {
      chrome.runtime.sendMessage({ type: "set_backend_url", url }, done);
    });
  } else {
    chrome.runtime.sendMessage({ type: "set_backend_url", url }, done);
  }
});

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, refresh);
});

refresh();
const poll = setInterval(refresh, 1000);
window.addEventListener("unload", () => clearInterval(poll));
