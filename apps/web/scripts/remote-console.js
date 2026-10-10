/* Remote console (spec 13 §1.4, inv02 F-38). LOAD-BEARING for devices without devtools
 * (iPad mini 2, A300M). Ported from frontend/index.html:5-26 and frontend-compat/index.html:11-32.
 *
 * HAND-WRITTEN ES5: this file is inlined into index.html by scripts/vite-plugin-html-target.ts and
 * is NOT transpiled. No let/const, arrow functions, template literals, or other ES2015+ syntax.
 * scripts/__tests__/remote-console.test.ts parses it with acorn at ecmaVersion 5.
 *
 * Changes from the old script:
 *  - console mirroring can be switched off (default: off on main, on for compat), via the
 *    `archie.remoteConsole` localStorage key ('1' / '0') or window.__archieRemoteConsole.setEnabled;
 *  - window `error` and `unhandledrejection` are always sent, even when mirroring is off;
 *  - rate limit: at most 60 messages per 10 s window; extra messages are counted and reported
 *    as one "dropped N" line when the next window opens;
 *  - each message is capped at 4 KB;
 *  - uncaught errors carry the error's stack;
 *  - transport is XMLHttpRequest first, sendBeacon as the fallback (an async XHR is the path
 *    every old WebKit delivers while the page is alive).
 *
 * __REMOTE_CONSOLE_CONFIG__ is replaced by a JSON object literal at build time:
 *   { endpoint: string, prefix: string, defaultOn: boolean }
 */
(function (cfg) {
  'use strict';
  var w = window;
  if (w.__archieRemoteConsole) {
    return;
  }
  var STORAGE_KEY = 'archie.remoteConsole';
  var LIMIT = 60;
  var WINDOW_MS = 10000;
  var MAX_LEN = 4096;
  var endpoint = cfg.endpoint || '/api/debug/log';
  var prefix = cfg.prefix || '';
  var enabled = !!cfg.defaultOn;
  var windowStart = 0;
  var inWindow = 0;
  var dropped = 0;
  var totalDropped = 0;
  var sent = 0;
  var busy = false;

  try {
    var stored = w.localStorage.getItem(STORAGE_KEY);
    if (stored === '1') {
      enabled = true;
    } else if (stored === '0') {
      enabled = false;
    }
  } catch (e) {
    /* storage unavailable: keep the default */
  }

  function now() {
    return new Date().getTime();
  }

  function post(level, msg) {
    var body = JSON.stringify({ level: level, msg: msg, ts: new Date().toISOString() });
    try {
      if (typeof w.XMLHttpRequest === 'function' || typeof w.XMLHttpRequest === 'object') {
        var xhr = new w.XMLHttpRequest();
        xhr.open('POST', endpoint, true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.send(body);
        return;
      }
    } catch (e) {
      /* fall through to sendBeacon */
    }
    try {
      if (w.navigator && typeof w.navigator.sendBeacon === 'function') {
        w.navigator.sendBeacon(endpoint, body);
      }
    } catch (e) {
      /* give up silently */
    }
  }

  function admit() {
    var t = now();
    if (t - windowStart >= WINDOW_MS) {
      windowStart = t;
      inWindow = 0;
      if (dropped > 0) {
        var n = dropped;
        dropped = 0;
        inWindow = 1;
        sent++;
        post('warn', prefix + '[remote-console] dropped ' + n + ' message(s) (rate limit ' + LIMIT + '/' + WINDOW_MS / 1000 + 's)');
      }
    }
    if (inWindow >= LIMIT) {
      dropped++;
      totalDropped++;
      return false;
    }
    inWindow++;
    return true;
  }

  function stringify(args) {
    var parts = [];
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      var s;
      try {
        if (a instanceof Error) {
          s = a.stack ? String(a.stack) : a.name + ': ' + a.message;
        } else if (typeof a === 'object' && a !== null) {
          s = JSON.stringify(a);
        } else {
          s = String(a);
        }
      } catch (e) {
        s = '[unserializable]';
      }
      parts.push(s);
    }
    return parts.join(' ');
  }

  function send(level, msg) {
    if (busy) {
      return false;
    }
    busy = true;
    try {
      if (!admit()) {
        return false;
      }
      var text = prefix + String(msg);
      if (text.length > MAX_LEN) {
        text = text.substring(0, MAX_LEN) + '…[truncated ' + (text.length - MAX_LEN) + ' chars]';
      }
      sent++;
      post(level, text);
      return true;
    } finally {
      busy = false;
    }
  }

  var methods = ['log', 'warn', 'error', 'info'];
  for (var m = 0; m < methods.length; m++) {
    (function (name) {
      var original = w.console && w.console[name];
      if (typeof original !== 'function') {
        return;
      }
      w.console[name] = function () {
        original.apply(w.console, arguments);
        if (enabled) {
          send(name, stringify(arguments));
        }
      };
    })(methods[m]);
  }

  w.addEventListener('error', function (e) {
    var where = e && e.filename ? ' @ ' + e.filename + ':' + e.lineno + ':' + e.colno : '';
    var stack = e && e.error && e.error.stack ? '\n' + String(e.error.stack) : '';
    send('uncaught', (e && e.message ? e.message : 'error') + where + stack);
  });

  w.addEventListener('unhandledrejection', function (e) {
    var r = e ? e.reason : undefined;
    send('unhandledrejection', stringify([r]));
  });

  w.__archieRemoteConsole = {
    key: STORAGE_KEY,
    isEnabled: function () {
      return enabled;
    },
    setEnabled: function (on, persist) {
      enabled = !!on;
      if (persist !== false) {
        try {
          w.localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0');
        } catch (e) {
          /* ignore */
        }
      }
    },
    send: function (level, msg) {
      return send(String(level), String(msg));
    },
    stats: function () {
      return { sent: sent, dropped: totalDropped };
    }
  };
})(__REMOTE_CONSOLE_CONFIG__);
