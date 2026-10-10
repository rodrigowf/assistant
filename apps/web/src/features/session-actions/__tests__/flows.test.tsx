/**
 * W-11 session actions as protocol sequences against the mock backend (spec 12 §6): delete,
 * rewind, fork, compact, deny-with-feedback + the queue tray, the three-action Archie conflict
 * dialog (new / resume / cancel), and resuming a past Archie conversation from the history.
 * Every REST call and client frame is recorded in order (`mockEnv.ts`).
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { MockServer } from '../../../../mock-server/server.mjs';
import { getArchieRuntime, getSessionRuntime, openSession, refreshSessionList, startServices, type ArchieRuntime, type SessionRuntime } from '@/services';
import { catalogStore, snackbarStore, tabsStore } from '@/stores';
import { Composer } from '@/features/composer';
import { requestOpenArchie, resumeReadOnly, rewindTo, SessionActionsHost, SessionMenu, resetSessionActions } from '..';
import { endMock, framesOf, recorded, startMock, until, useMock } from './mockEnv';

const CHAT = '/api/sessions/chat';
const ORCH = '/api/orchestrator/chat';
let server: MockServer;

beforeEach(async () => {
  server = await startMock();
  useMock(server);
  resetSessionActions();
  startServices({ skipInitialSync: true, reconnectPolicy: { delayMs: () => 20 } });
});
afterEach(async () => {
  endMock(server);
  await server.close();
});

const tabIds = () => tabsStore.getState().tabs.map((t) => t.id);
const idx = (re: RegExp) => recorded.rest.findIndex((r) => re.test(r));

async function liveAgent(sdkId?: string): Promise<SessionRuntime> {
  const rt = openSession({ kind: 'agent', focus: true, ...(sdkId ? { sdkId } : {}) }) as SessionRuntime;
  await until(() => rt.conv.conn === 'subscribed' && !rt.conv.reloading, 'agent subscribed');
  return rt;
}

async function turn(rt: SessionRuntime, text: string): Promise<void> {
  const before = rt.conv.counters.turns;
  rt.send(text);
  await until(() => rt.conv.counters.turns > before && rt.conv.status === 'idle' && !rt.conv.inTurn, `turn "${text}"`);
}

function host() {
  return render(
    <>
      <SessionActionsHost />
    </>,
  );
}

// ───────────────────────── §6.8 delete ─────────────────────────

describe('delete (spec 12 §6.8)', () => {
  it('closes the pool session BEFORE DELETE, removes the tab by local_id (fixes W-8), and the session leaves the list', async () => {
    const user = userEvent.setup();
    const rt = await liveAgent('mock-sess-weather');
    const id = rt.localId;
    render(
      <>
        <SessionMenu localId={id} />
        <SessionActionsHost />
      </>,
    );
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    await user.click(screen.getByRole('menuitem', { name: /Delete/ }));
    const dlg = await screen.findByRole('alertdialog');
    expect(dlg.textContent).toContain('“Weather script”');
    expect(dlg.textContent).toContain('recoverable from context/trash/');
    await user.click(within(dlg).getByRole('button', { name: 'Delete' }));
    await until(() => !tabIds().includes(id) && snackbarStore.getState().queue.some((q) => q.message === '“Weather script” deleted' || q.message === 'Deleted “Weather script”'), 'deleted');
    const close = idx(new RegExp(`^POST /api/sessions/${id}/close$`));
    const del = idx(/^DELETE \/api\/sessions\/mock-sess-weather$/);
    expect(close).toBeGreaterThanOrEqual(0);
    expect(del).toBeGreaterThan(close);
    expect(server.engine.runs.has(id)).toBe(false);
    await refreshSessionList();
    expect(catalogStore.getState().sessions.items.some((s) => s.session_id === 'mock-sess-weather')).toBe(false);
    expect(framesOf('stop')).toEqual([]); // P-1: close is REST, never a stop frame
  });

  it('before the first reply: Delete, Fork and Rename say why instead of doing nothing (ID-3)', async () => {
    const user = userEvent.setup();
    const rt = await liveAgent();
    render(
      <>
        <SessionMenu localId={rt.localId} onRename={() => undefined} onClose={() => undefined} />
        <SessionActionsHost />
      </>,
    );
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    for (const name of [/^Rename/, /^Fork/, /^Delete/, /^Session settings/]) {
      const item = screen.getByRole('menuitem', { name });
      expect(item.getAttribute('aria-disabled')).toBe('true');
    }
    expect(screen.getByRole('menuitem', { name: /^Delete/ }).textContent).toContain('Available after the first reply');
    expect(screen.getByRole('menuitem', { name: /^Session settings/ }).textContent).toContain('Coming soon');
  });
});

// ───────────────────────── §6.5 rewind / fork ─────────────────────────

describe('rewind and fork (spec 12 §6.5)', () => {
  it('rewind: tail listing → POST close → POST truncate → reopen in place (LOAD-BEARING inv02 F-09 order)', async () => {
    const rt = await liveAgent();
    await turn(rt, 'first question');
    await turn(rt, 'second question');
    const sdk = rt.conv.ref.sdkId as string;
    const target = rt.conv.entries.find((e) => e.kind === 'user' && e.text === 'first question');
    const pos = tabIds().indexOf(rt.localId);
    recorded.rest.length = 0;
    const next = (await rewindTo(rt.localId, target?.id ?? '')) as SessionRuntime;
    if (!next) throw new Error(snackbarStore.getState().queue.map((q) => q.message).join(' | '));
    expect(next).toBeTruthy();
    const list = idx(new RegExp(`^GET /api/sessions/${sdk}/messages$`));
    const close = idx(new RegExp(`^POST /api/sessions/${rt.localId}/close$`));
    const trunc = idx(new RegExp(`^POST /api/sessions/${sdk}/truncate$`));
    expect(list).toBeGreaterThanOrEqual(0);
    expect(close).toBeGreaterThan(list);
    expect(trunc).toBeGreaterThan(close);
    // same tab position, new local id, same sdk id; a user target keeps the prompt and drops its reply (§6.5)
    expect(tabIds()[pos]).toBe(next.localId);
    expect(next.localId).not.toBe(rt.localId);
    await until(() => next.conv.conn === 'subscribed' && !next.conv.reloading, 'reopened');
    expect(framesOf('start', CHAT).pop()).toEqual({ type: 'start', local_id: next.localId, resume_sdk_id: sdk });
    expect(next.conv.entries.map((e) => (e.kind === 'user' ? `u:${e.text}` : e.kind))).toEqual(['u:first question']);
  });

  it('the ⋮ Fork copies the whole conversation (drop_last_n 0) into a new focused tab; the original stays', async () => {
    const user = userEvent.setup();
    const rt = await liveAgent();
    await turn(rt, 'hello there');
    render(
      <>
        <SessionMenu localId={rt.localId} />
        <SessionActionsHost />
      </>,
    );
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    await user.click(screen.getByRole('menuitem', { name: 'Fork' }));
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Fork' }));
    await until(() => tabIds().length === 2, 'fork tab').catch(() => {
      throw new Error(snackbarStore.getState().queue.map((q) => q.message).join(' | '));
    });
    const forkId = tabsStore.getState().activeId as string;
    const fork = getSessionRuntime(forkId) as SessionRuntime;
    expect(fork.conv.ref.sdkId).toMatch(/^mock-fork-/);
    expect(idx(new RegExp(`^POST /api/sessions/${rt.conv.ref.sdkId as string}/fork$`))).toBeGreaterThanOrEqual(0);
    await until(() => fork.conv.entries.length === 2, 'fork history');
    expect(getSessionRuntime(rt.localId)).toBe(rt);
  });
});

// ───────────────────────── §6.4 compact ─────────────────────────

describe('compact (spec 12 §6.4)', () => {
  it('⋮ Compact context sends compact; compact_complete + turn_complete end it', async () => {
    const user = userEvent.setup();
    const rt = await liveAgent();
    await turn(rt, 'some context');
    render(<SessionMenu localId={rt.localId} />);
    await user.click(screen.getByRole('button', { name: 'Session menu' }));
    await user.click(screen.getByRole('menuitem', { name: /^Compact context/ }));
    expect(framesOf('compact', CHAT)).toEqual([{ type: 'compact' }]);
    await until(() => rt.conv.status === 'idle' && !rt.conv.compactPending, 'compacted');
  });
});

// ───────────────────────── §6.9 deny with feedback + tray ─────────────────────────

describe('deny with feedback and the queue tray (spec 12 §6.9, I-12)', () => {
  it('typing while ExitPlanMode waits: send{text} → permission_resolved{deny, message} → the text runs as the next turn', async () => {
    const user = userEvent.setup();
    const rt = await liveAgent();
    rt.send('make a plan');
    await until(() => rt.conv.entries.some((e) => e.kind === 'assistant' && e.blocks.some((b) => b.type === 'permission' && b.state === 'pending')), 'permission');
    render(<Composer localId={rt.localId} />);
    const field = screen.getByRole('textbox', { name: 'Message' });
    expect(field.getAttribute('placeholder')).toBe('Type to give feedback…');
    await user.type(field, 'Use two steps instead{Enter}');
    expect(framesOf('send', CHAT).map((m) => m.text)).toEqual(['make a plan', 'Use two steps instead']);
    expect(framesOf('permission_response')).toEqual([]);
    await until(() => rt.conv.entries.some((e) => e.kind === 'assistant' && e.blocks.some((b) => b.type === 'permission' && b.state !== 'pending')), 'denied');
    const perm = rt.conv.entries.flatMap((e) => (e.kind === 'assistant' ? e.blocks : [])).find((b) => b.type === 'permission');
    expect(perm && perm.type === 'permission' ? perm.state : '').toMatch(/denied|deny/);
    // the feedback was queued behind the turn (tray), then dispatched as a normal prompt
    await until(() => rt.conv.entries.some((e) => e.kind === 'user' && e.text === 'Use two steps instead'), 'dispatched');
    await waitFor(() => {
      expect(screen.queryByRole('list', { name: 'Waiting to send' })).toBeNull();
    });
  });
});

// ───────────────────────── §6.11 Archie: new / conflict ─────────────────────────

async function liveArchie(): Promise<ArchieRuntime> {
  const rt = await requestOpenArchie({ mode: 'new' });
  if (!rt) throw new Error('no archie');
  await until(() => rt.conv.conn === 'subscribed', 'archie subscribed');
  return rt;
}

describe('New Archie and the three-action conflict dialog (spec 12 §6.11, inv02 F-25)', () => {
  it('nothing running: a plain start{local_id} (no resume), no dialog', async () => {
    host();
    const rt = await liveArchie();
    expect(framesOf('start', ORCH)).toEqual([{ type: 'start', local_id: rt.localId }]);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('Archie running → dialog; "Open the running one" focuses it and starts nothing', async () => {
    const user = userEvent.setup();
    host();
    const first = await liveArchie();
    await liveAgent(); // focus moves away
    await act(async () => {
      await requestOpenArchie({ mode: 'new' });
    });
    const dlg = await screen.findByRole('alertdialog', { name: 'Archie is already active' });
    await user.click(within(dlg).getByRole('button', { name: 'Open the running one' }));
    expect(tabsStore.getState().activeId).toBe(first.localId);
    expect(framesOf('start', ORCH)).toHaveLength(1);
    expect(restMatchingClose()).toEqual([]);
  });

  it('"Stop it and start new" closes the running one (P-1) and starts a fresh one', async () => {
    const user = userEvent.setup();
    host();
    const first = await liveArchie();
    await act(async () => {
      await requestOpenArchie({ mode: 'new' });
    });
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Stop it and start new' }));
    await until(() => !!getArchieRuntime() && getArchieRuntime()?.localId !== first.localId, 'new archie');
    const next = getArchieRuntime() as ArchieRuntime;
    await until(() => next.conv.conn === 'subscribed', 'new subscribed');
    expect(restMatchingClose()).toEqual([`POST /api/sessions/${first.localId}/close`]);
    expect(framesOf('start', ORCH).pop()).toEqual({ type: 'start', local_id: next.localId });
    expect(tabIds()).not.toContain(first.localId);
  });

  it('Cancel changes nothing', async () => {
    const user = userEvent.setup();
    host();
    const first = await liveArchie();
    await act(async () => {
      await requestOpenArchie({ mode: 'new' });
    });
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(getArchieRuntime()).toBe(first);
    expect(restMatchingClose()).toEqual([]);
  });

  it('Archie started on another device: the dialog offers it; replacing closes its pool entry first', async () => {
    const user = userEvent.setup();
    host();
    const NodeWebSocket = (await import('ws')).default;
    const other = new NodeWebSocket(`ws://127.0.0.1:${server.port}${ORCH}`);
    await new Promise((r) => (other.onopen = r));
    other.send(JSON.stringify({ type: 'start', local_id: 'other-device-archie' }));
    await until(() => server.engine.runs.has('other-device-archie'), 'other archie');
    await act(async () => {
      await requestOpenArchie({ mode: 'new' });
    });
    const dlg = await screen.findByRole('alertdialog', { name: 'Archie is already active' });
    await user.click(within(dlg).getByRole('button', { name: 'Stop it and start new' }));
    await until(() => !!getArchieRuntime() && getArchieRuntime()?.localId !== 'other-device-archie' && getArchieRuntime()?.conv.conn === 'subscribed', 'replaced');
    expect(server.engine.runs.has('other-device-archie')).toBe(false);
    expect(restMatchingClose()).toContain('POST /api/sessions/other-device-archie/close');
    other.close();
  });
});

function restMatchingClose(): string[] {
  return recorded.rest.filter((r) => /^POST \/api\/sessions\/[^/]+\/close$/.test(r));
}

// ───────────────────────── resume a past Archie conversation (W-11 / inv02 §6.3 #11) ─────────────────────────

const PAST = 'mock-orch-morning';
const texts = (rt: ArchieRuntime) => rt.conv.entries.map((e) => (e.kind === 'user' ? `u:${e.text}` : e.kind));

describe('resume a past Archie conversation from the history', () => {
  it('nothing running: start{local_id: uuid(), resume_sdk_id} and the transcript loads', async () => {
    host();
    const rt = (await requestOpenArchie({ mode: 'resume', sdkId: PAST })) as ArchieRuntime;
    await until(() => rt.conv.conn === 'subscribed' && !rt.conv.reloading && rt.conv.entries.length > 0, 'resumed');
    const start = framesOf('start', ORCH).pop() as { local_id: string; resume_sdk_id: string };
    expect(start.resume_sdk_id).toBe(PAST);
    expect(start.local_id).toBe(rt.localId);
    expect(start.local_id).not.toBe(PAST);
    expect(start.local_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(texts(rt)).toHaveLength(4);
    expect(texts(rt)[0]).toBe("u:What's on today?");
    expect(rt.readOnly).toBe(false);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('another one running: the dialog; "Stop it and resume this one" closes it and resumes ours with its history', async () => {
    const user = userEvent.setup();
    host();
    const first = await liveArchie();
    await act(async () => {
      await requestOpenArchie({ mode: 'resume', sdkId: PAST });
    });
    const dlg = await screen.findByRole('alertdialog', { name: 'Another Archie conversation is running' });
    await user.click(within(dlg).getByRole('button', { name: 'Stop it and resume this one' }));
    await until(() => getArchieRuntime()?.conv.ref.sdkId === PAST && getArchieRuntime()?.conv.conn === 'subscribed' && !getArchieRuntime()?.conv.reloading, 'resumed');
    const rt = getArchieRuntime() as ArchieRuntime;
    expect(restMatchingClose()).toEqual([`POST /api/sessions/${first.localId}/close`]);
    expect(framesOf('start', ORCH).pop()).toEqual({ type: 'start', local_id: rt.localId, resume_sdk_id: PAST });
    expect(texts(rt)).toHaveLength(4);
    expect(tabIds()).toEqual([rt.localId]);
  });

  it('the read-only view (H-3) Resume goes through the same dialog and the read-only tab gives way', async () => {
    const user = userEvent.setup();
    host();
    await liveArchie();
    const ro = openSession({ kind: 'archie', sdkId: PAST, focus: true, readOnly: true }) as ArchieRuntime;
    await until(() => ro.conv.entries.length === 4, 'read-only history');
    expect(framesOf('start', ORCH)).toHaveLength(1); // REST only
    await act(async () => {
      await resumeReadOnly(ro.localId);
    });
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Stop it and resume this one' }));
    await until(() => getArchieRuntime()?.conv.ref.sdkId === PAST && getArchieRuntime()?.conv.conn === 'subscribed', 'resumed');
    expect(tabIds()).not.toContain(ro.localId);
    expect(tabIds()).toEqual([(getArchieRuntime() as ArchieRuntime).localId]);
  });

  it('asking for the conversation that is already running just focuses it', async () => {
    host();
    const first = await requestOpenArchie({ mode: 'resume', sdkId: PAST });
    await until(() => first?.conv.conn === 'subscribed', 'resumed');
    await liveAgent();
    const again = await requestOpenArchie({ mode: 'resume', sdkId: PAST });
    expect(again).toBe(first);
    expect(tabsStore.getState().activeId).toBe(first?.localId);
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(framesOf('start', ORCH)).toHaveLength(1);
  });
});
