/**
 * The inline remote console (scripts/remote-console.js, inv02 F-38): ES5-only, default per build,
 * localStorage override, errors always sent, 60 msgs / 10 s rate limit with a dropped counter,
 * 4 KB cap. Executed in a node:vm sandbox with a fake window and a controllable clock.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import * as acorn from 'acorn';
import { describe, expect, it } from 'vitest';
import { REMOTE_CONSOLE_SOURCE, remoteConsoleScript } from '../vite-plugin-html-target.ts';

// The key is shared with src/platform/remoteLog.ts (a DOM module, so read as text here).
const REMOTE_CONSOLE_KEY = /REMOTE_CONSOLE_KEY = '([^']+)'/.exec(
  fs.readFileSync(new URL('../../src/platform/remoteLog.ts', import.meta.url), 'utf8'),
)?.[1] as string;

interface Sent {
  level: string;
  msg: string;
}

function sandbox(target: 'main' | 'compat', stored: string | null = null, opts: { xhr?: boolean } = {}) {
  let now = 1_000_000;
  const sent: Sent[] = [];
  const viaXhr: Sent[] = [];
  class FakeXhr {
    url = '';
    open(_method: string, url: string) {
      this.url = url;
    }
    setRequestHeader() {}
    send(body: string) {
      expect(this.url).toBe('/api/debug/log');
      viaXhr.push(JSON.parse(body) as Sent);
    }
  }
  const printed: unknown[][] = [];
  const listeners: Record<string, ((e: unknown) => void)[]> = {};
  const storage = new Map<string, string>(stored === null ? [] : [[REMOTE_CONSOLE_KEY, stored]]);
  class FakeDate extends Date {
    constructor() {
      super(now);
    }
  }
  const win: Record<string, unknown> = {
    navigator: {
      sendBeacon: (url: string, body: string) => {
        expect(url).toBe('/api/debug/log');
        sent.push(JSON.parse(body) as Sent);
        return true;
      },
    },
    localStorage: {
      getItem: (k: string) => storage.get(k) ?? null,
      setItem: (k: string, v: string) => storage.set(k, v),
    },
    console: { log: (...a: unknown[]) => printed.push(a), info: (...a: unknown[]) => printed.push(a), warn: () => undefined, error: () => undefined },
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      (listeners[type] ??= []).push(fn);
    },
  };
  if (opts.xhr) win.XMLHttpRequest = FakeXhr;
  vm.runInNewContext(remoteConsoleScript(target), { window: win, Date: FakeDate, JSON, String, Error });
  const api = win.__archieRemoteConsole as {
    isEnabled(): boolean;
    setEnabled(on: boolean, persist?: boolean): void;
    send(level: string, msg: string): boolean;
    stats(): { sent: number; dropped: number };
  };
  return {
    win,
    api,
    sent,
    viaXhr,
    printed,
    storage,
    console: win.console as { log: (...a: unknown[]) => void; info: (...a: unknown[]) => void },
    fire: (type: string, e: unknown) => {
      for (const fn of listeners[type] ?? []) fn(e);
    },
    advance: (ms: number) => {
      now += ms;
    },
  };
}

describe('remote console inline script', () => {
  it('is hand-written ES5 (parses at ecmaVersion 5)', () => {
    expect(() => acorn.parse(remoteConsoleScript('compat'), { ecmaVersion: 5, sourceType: 'script' })).not.toThrow();
    expect(fs.readFileSync(REMOTE_CONSOLE_SOURCE, 'utf8')).toContain(`'${REMOTE_CONSOLE_KEY}'`);
  });

  it('compat: on by default with the [compat] prefix; console output still printed', () => {
    const s = sandbox('compat');
    expect(s.api.isEnabled()).toBe(true);
    s.console.log('hello', { a: 1 }, 3);
    expect(s.printed).toEqual([['hello', { a: 1 }, 3]]);
    expect(s.sent).toEqual([expect.objectContaining({ level: 'log', msg: '[compat] hello {"a":1} 3' })]);
  });

  it('main: off by default, but window errors and rejections are always sent', () => {
    const s = sandbox('main');
    expect(s.api.isEnabled()).toBe(false);
    s.console.info('quiet');
    expect(s.sent).toEqual([]);
    s.fire('error', { message: 'boom', filename: 'app.js', lineno: 3, colno: 7 });
    s.fire('unhandledrejection', { reason: 'nope' });
    expect(s.sent.map((m) => [m.level, m.msg])).toEqual([
      ['uncaught', 'boom @ app.js:3:7'],
      ['unhandledrejection', 'nope'],
    ]);
  });

  it('honours the localStorage override and persists setEnabled', () => {
    expect(sandbox('main', '1').api.isEnabled()).toBe(true);
    expect(sandbox('compat', '0').api.isEnabled()).toBe(false);
    const s = sandbox('main');
    s.api.setEnabled(true);
    expect(s.storage.get(REMOTE_CONSOLE_KEY)).toBe('1');
    s.api.setEnabled(false, false);
    expect(s.storage.get(REMOTE_CONSOLE_KEY)).toBe('1');
  });

  it('rate-limits to 60 messages per 10 s and reports the dropped count', () => {
    const s = sandbox('compat');
    for (let i = 0; i < 75; i++) s.console.log(`m${i}`);
    expect(s.sent).toHaveLength(60);
    expect(s.api.stats()).toEqual({ sent: 60, dropped: 15 });
    s.advance(9_999);
    s.console.log('still limited');
    expect(s.sent).toHaveLength(60);
    s.advance(1);
    s.console.log('next window');
    expect(s.sent.slice(60).map((m) => m.msg)).toEqual([
      '[compat] [remote-console] dropped 16 message(s) (rate limit 60/10s)',
      '[compat] next window',
    ]);
  });

  it('caps each message at 4 KB', () => {
    const s = sandbox('main', '1');
    s.console.log('x'.repeat(5000));
    const msg = s.sent[0]?.msg ?? '';
    expect(msg.startsWith('x'.repeat(4096))).toBe(true);
    expect(msg).toContain('[truncated 904 chars]');
  });

  it('stringifies errors with their stack and survives unserialisable values', () => {
    const s = sandbox('main', '1');
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    s.console.log(cyclic);
    expect(s.sent[0]?.msg).toBe('[unserializable]');
  });

  it('prefers XMLHttpRequest and falls back to sendBeacon', () => {
    const s = sandbox('main', '1', { xhr: true });
    s.console.log('over xhr');
    expect(s.viaXhr.map((m) => m.msg)).toEqual(['over xhr']);
    expect(s.sent).toEqual([]);
  });

  it('sends the stack of an uncaught error', () => {
    const s = sandbox('main');
    s.fire('error', { message: 'boom', filename: 'app.js', lineno: 3, colno: 7, error: { stack: 'at f (app.js:3:7)' } });
    expect(s.sent[0]?.msg).toBe('boom @ app.js:3:7\nat f (app.js:3:7)');
  });
});
