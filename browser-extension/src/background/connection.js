/**
 * WebSocket client to the assistant backend.
 *
 * MV3 service workers are killed after ~30s idle.  Two things keep this
 * connection alive: WebSocket activity resets the idle timer (Chrome 116+),
 * and a chrome.alarms heartbeat wakes the worker if the socket goes quiet.
 * If the worker is torn down anyway, `service-worker.js` reconnects on the
 * next wake — so treat disconnects as normal, not exceptional.
 */

export const DEFAULT_BACKEND_URL = "ws://127.0.0.1:8766/api/browser/ws";

const BACKOFF_MIN_MS = 1_000;
// Short ceiling on purpose: the daemon is a loopback process that Claude
// sessions start on demand, so a 30s ceiling would put a cold-start stall in
// front of the first command. Reconnecting fast is cheap over loopback.
const BACKOFF_MAX_MS = 5_000;

/** Backend rejection reasons, phrased for the popup. */
const AUTH_ERRORS = {
  unauthorized: "Token rejected — check the token matches context/.env",
  auth_not_configured: "Backend has no BROWSER_CONTROL_TOKEN configured",
  hello_timeout: "Handshake timed out",
  expected_hello: "Protocol error: backend expected a hello frame",
  invalid_json: "Protocol error: malformed handshake",
};

export class BackendConnection {
  /**
   * @param {(command: string, params: object) => Promise<any>} onCommand
   *   Dispatches an inbound command and resolves with its result.
   */
  constructor(onCommand) {
    this.onCommand = onCommand;
    this.socket = null;
    this.url = DEFAULT_BACKEND_URL;
    this.token = "";
    this.status = "disconnected"; // disconnected | connecting | connected
    this.lastError = null;
    this.attempt = 0;
    this._reconnectTimer = null;
  }

  async loadSettings() {
    const stored = await chrome.storage.local.get(["backendUrl", "token"]);
    this.url = stored.backendUrl || DEFAULT_BACKEND_URL;
    this.token = stored.token || "";
    return this.url;
  }

  async setUrl(url) {
    await chrome.storage.local.set({ backendUrl: url });
    this.url = url;
    this.reconnectNow();
  }

  async setToken(token) {
    await chrome.storage.local.set({ token });
    this.token = token;
    this.reconnectNow();
  }

  /** Open the socket if it isn't already open or opening. */
  async connect() {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    await this.loadSettings();

    this._clearReconnectTimer();
    this.status = "connecting";

    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (err) {
      // Malformed URL — surface it rather than retrying a URL that can never work.
      this.status = "disconnected";
      this.lastError = String(err);
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      // Not "connected" yet — the backend rejects and closes if the token is
      // wrong, so the handshake isn't done until the 'ready' frame arrives.
      // Deliberately NOT resetting `attempt` here: auth failure happens after
      // the socket opens, so resetting on open would defeat the backoff
      // entirely and a bad token would reconnect ~1.3x/second forever.
      this.status = "connecting";
      this._send({
        type: "hello",
        token: this.token,
        client: "chrome-extension",
        version: chrome.runtime.getManifest().version,
      });
    });

    socket.addEventListener("message", (event) => this._handleMessage(event));

    socket.addEventListener("close", () => {
      this.status = "disconnected";
      this.socket = null;
      this._scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      // 'error' is always followed by 'close', which owns the reconnect.
      this.lastError = "websocket error";
    });
  }

  reconnectNow() {
    this.attempt = 0;
    if (this.socket) {
      try {
        this.socket.close();
      } catch {
        // Already closing — the 'close' handler still runs.
      }
      this.socket = null;
    }
    this._clearReconnectTimer();
    this.connect();
  }

  /** Called by the alarm heartbeat; cheap no-op when already connected. */
  ensureConnected() {
    if (this.status !== "connected") {
      this.connect();
      return;
    }
    this._send({ type: "ping" });
  }

  async _handleMessage(event) {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return; // Non-JSON frame — nothing we can route.
    }

    if (msg.type === "ready") {
      // Handshake accepted — only now are we genuinely connected, and only
      // now is it right to clear the backoff.
      this.status = "connected";
      this.lastError = null;
      this.attempt = 0;
      return;
    }
    if (msg.type === "error") {
      // Auth failure and friends. The backend closes right after, so record
      // the reason; without this an unauthorized extension looks identical to
      // an offline backend in the popup.
      this.lastError = AUTH_ERRORS[msg.error] || msg.error || "backend error";
      return;
    }
    if (msg.type === "ping") {
      this._send({ type: "pong" });
      return;
    }
    if (msg.type !== "command") {
      return;
    }

    // Every command gets exactly one result frame keyed by the request id,
    // so the backend can await a specific call rather than the next reply.
    try {
      const result = await this.onCommand(msg.command, msg.params || {});
      this._send({ id: msg.id, type: "result", ok: true, result });
    } catch (err) {
      this._send({
        id: msg.id,
        type: "result",
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  _send(payload) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  _scheduleReconnect() {
    if (this._reconnectTimer !== null) return;
    // Exponential backoff with jitter so a restarting backend doesn't get
    // hammered by every browser reconnecting on the same cadence.
    const base = Math.min(BACKOFF_MIN_MS * 2 ** this.attempt, BACKOFF_MAX_MS);
    const delay = base / 2 + Math.random() * (base / 2);
    this.attempt += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this.connect();
    }, delay);
  }

  _clearReconnectTimer() {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  getStatus() {
    return {
      status: this.status,
      url: this.url,
      hasToken: Boolean(this.token),
      lastError: this.lastError,
    };
  }
}
