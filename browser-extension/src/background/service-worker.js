/**
 * MV3 service worker entry point.
 *
 * Owns the backend connection and routes inbound commands to the registry.
 * The worker is ephemeral: Chrome tears it down when idle and restarts it on
 * the next event, so every entry point below re-establishes the connection
 * rather than assuming one already exists.
 */

import { BackendConnection } from "./connection.js";
import { dispatch, COMMANDS } from "./commands.js";

const KEEPALIVE_ALARM = "archie-keepalive";

const connection = new BackendConnection(dispatch);

// Every wake path funnels through connect(); it's a no-op when already open.
connection.connect();

chrome.runtime.onStartup.addListener(() => connection.connect());
chrome.runtime.onInstalled.addListener(() => {
  connection.connect();
  // 30s is the floor Chrome enforces for alarm periods.
  chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === KEEPALIVE_ALARM) {
    connection.ensureConnected();
  }
});

// Popup <-> worker channel. Returning true keeps the response port open
// for the async replies below.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "get_status") {
    sendResponse({ ...connection.getStatus(), commands: COMMANDS });
    return false;
  }
  if (msg?.type === "set_backend_url") {
    connection.setUrl(msg.url).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "set_token") {
    connection.setToken(msg.token).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "reconnect") {
    connection.reconnectNow();
    sendResponse({ ok: true });
    return false;
  }
  return false;
});
