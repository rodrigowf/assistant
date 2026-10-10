/**
 * Inline cards and message actions against the real SessionRuntime (fake WebSocket and fetch,
 * W-06 test doubles): permission (request_id matching, one-click, typing = deny with feedback,
 * ExitPlanMode plan inline), stall (provider-neutral copy, Interrupt, Keep waiting), connection
 * errors (human copy, Details, Retry, dismiss), "Connection lost at …", turn errors, rewind / fork entry points with the server-derived
 * `drop_last_n` (spec 12 §6.5), and the empty states.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { openSession, type SessionRuntime } from '@/services';
import { FakeWebSocket, flushPromises, jsonResponse, setupServices, teardownServices, type Harness } from '../../../services/__tests__/fakes';
import { ConversationPanel } from '../ConversationPanel';
import { preloadRich } from '../lazyRich';
import { PLAN_HINT, PLAN_TITLE } from '../cards/copy';
import { FOLD_LINES } from '../entries/UserMessage';

// The rich chunk (markdown, tool cards) loads lazily in the app; tests load it up front.
beforeAll(async () => {
  await preloadRich();
});

const CHAT = '/api/sessions/chat';
let h: Harness;

beforeEach(() => {
  h = setupServices();
});
afterEach(() => {
  teardownServices();
});

async function pump(): Promise<void> {
  await act(async () => {
    await flushPromises();
    h.scheduler.flush();
    await Promise.resolve();
  });
}

async function emit(ws: FakeWebSocket, ...frames: Record<string, unknown>[]): Promise<void> {
  for (const f of frames) ws.emit(f);
  await pump();
}

async function openAgent(localId = 'A1', sdkId: string | null = null): Promise<{ rt: SessionRuntime; ws: FakeWebSocket }> {
  const rt = openSession({ kind: 'agent', localId, sdkId, provider: 'claude', focus: true }) as SessionRuntime;
  const ws = FakeWebSocket.last(CHAT);
  ws.open();
  ws.emit({ type: 'session_started', session_id: localId, context_window: 200000 });
  render(<ConversationPanel localId={localId} hidden={false} />);
  await pump();
  return { rt, ws };
}

const card = (tone: string): HTMLElement | null => document.querySelector(`[data-card="${tone}"]`);

describe('permission card (spec 12 §6.9, inv02 F-14)', () => {
  it('Approve sends permission_response once; only the matching resolve closes it', async () => {
    const { rt, ws } = await openAgent();
    rt.send('clean the build');
    await emit(
      ws,
      { type: 'status', status: 'processing' },
      { type: 'tool_use', tool_use_id: 't1', tool_name: 'Bash', tool_input: { command: 'rm -rf build/' } },
      { type: 'permission_request', request_id: 'r1', tool_name: 'Bash', tool_input: { command: 'rm -rf build/' } },
    );
    const c = card('permission') as HTMLElement;
    expect(c.textContent).toContain('Allow Bash?');
    expect(c.textContent).toContain('rm -rf build/');
    expect(c.textContent).toContain('Or type below to give feedback');
    fireEvent.click(within(c).getByRole('button', { name: 'Approve' }));
    expect(ws.messages().filter((m) => m.type === 'permission_response')).toEqual([{ type: 'permission_response', request_id: 'r1', decision: 'allow' }]);
    expect(within(c).getByRole('button', { name: 'Approve' }).getAttribute('aria-disabled')).toBe('true');
    expect(within(c).getByRole('button', { name: 'Reject' }).getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(within(c).getByRole('button', { name: 'Approve' }));
    expect(ws.messages().filter((m) => m.type === 'permission_response')).toHaveLength(1);

    await emit(ws, { type: 'permission_resolved', request_id: 'other', decision: 'allow', responder: 'orchestrator', message: null });
    expect(card('permission')).not.toBeNull(); // a stale / foreign resolve never closes it
    await emit(ws, { type: 'permission_resolved', request_id: 'r1', decision: 'allow', responder: 'user', message: null });
    expect(card('permission')).toBeNull();
    const block = document.querySelector('[data-block="permission"]') as HTMLElement;
    expect(block.textContent).toContain('Approved by you');
  });

  it('ExitPlanMode: the plan renders inline in the timeline; typing a message is the deny-with-feedback path', async () => {
    const { rt, ws } = await openAgent();
    rt.send('plan it');
    await emit(
      ws,
      { type: 'status', status: 'processing' },
      { type: 'permission_request', request_id: 'p1', tool_name: 'ExitPlanMode', tool_input: { plan: '1. Read **the** inputs\n2. Write tests' } },
    );
    const c = card('permission') as HTMLElement;
    expect(c.textContent).toContain(PLAN_TITLE);
    expect(c.textContent).toContain(PLAN_HINT);
    const plan = screen.getByRole('region', { name: 'Plan' });
    expect(within(plan).getByText('the').tagName).toBe('STRONG');
    expect(plan.textContent).toContain('Waiting for your approval');
    rt.send('Also add a rollback step');
    expect(
      ws
        .messages()
        .filter((m) => m.type === 'send')
        .map((m) => m.text),
    ).toEqual(['plan it', 'Also add a rollback step']);
    await emit(ws, { type: 'permission_resolved', request_id: 'p1', decision: 'deny', responder: 'user', message: 'Also add a rollback step' });
    expect(card('permission')).toBeNull();
    expect(plan.textContent).toContain('Rejected by you: Also add a rollback step');
  });
});

describe('stall, errors and connection', () => {
  it('stall card: provider-neutral copy, Interrupt, Keep waiting until the next report', async () => {
    const { rt, ws } = await openAgent();
    rt.send('fetch');
    await emit(
      ws,
      { type: 'status', status: 'processing' },
      { type: 'tool_use', tool_use_id: 't1', tool_name: 'WebFetch', tool_input: { url: 'https://example.com' } },
      { type: 'session_stalled', elapsed_seconds: 134, last_tool_name: 'WebFetch', last_tool_use_id: 't1' },
    );
    const c = card('stall') as HTMLElement;
    expect(c.textContent).toContain('WebFetch silent for 2m14s');
    expect(c.textContent).toContain('WebFetch has been running for 2m14s with no response.');
    expect(c.textContent).not.toContain('Claude');
    fireEvent.click(within(c).getByRole('button', { name: 'Keep waiting' }));
    expect(card('stall')).toBeNull();
    await emit(ws, { type: 'session_stalled', elapsed_seconds: 194, last_tool_name: 'WebFetch', last_tool_use_id: 't1' });
    fireEvent.click(within(card('stall') as HTMLElement).getByRole('button', { name: 'Interrupt' }));
    expect(ws.types()).toContain('interrupt');
  });

  it('start_failed: human copy, Details, Retry restarts the handshake, dismiss clears it', async () => {
    const { ws } = await openAgent();
    await emit(ws, { type: 'error', error: 'start_failed', detail: 'Directory does not exist: /nope' });
    const c = card('error') as HTMLElement;
    expect(c.textContent).toContain("Couldn't start the session");
    expect(c.textContent).toContain('Directory does not exist: /nope');
    expect(c.textContent).not.toContain('start_failed');
    fireEvent.click(within(c).getByRole('button', { name: 'Details' }));
    expect(c.textContent).toContain('Code: start_failed');
    const before = ws.types().filter((t) => t === 'start').length;
    fireEvent.click(within(c).getByRole('button', { name: 'Retry' }));
    expect(ws.types().filter((t) => t === 'start').length).toBe(before + 1);
    fireEvent.click(within(c).getByRole('button', { name: 'Dismiss' }));
    await pump();
    expect(card('error')).toBeNull();
  });

  it('a dropped socket shows "Connection lost at hh:mm:ss" (never an entry); a turn failure is a timeline notice', async () => {
    const { rt, ws } = await openAgent();
    rt.send('go');
    await emit(ws, { type: 'status', status: 'processing' }, { type: 'error', error: 'send_failed', detail: 'The CLI exited' });
    expect(document.querySelector('[data-notice="error"]')?.textContent).toContain('The CLI exited');
    ws.drop();
    await pump();
    expect(screen.getByText(/Connection lost at \d\d:\d\d:\d\d/)).toBeTruthy();
    expect(document.querySelectorAll('[data-entry-id]')).toHaveLength(2);
  });
});

describe('message actions: rewind / fork entry points (spec 12 §6.5)', () => {
  const line = (role: string, text: string) => ({ role, text, blocks: [{ type: 'text', text }] });
  const HISTORY = {
    messages: [line('user', 'A'), line('assistant', 'a'), line('user', 'B'), line('assistant', 'b'), line('user', 'C'), line('assistant', 'c')],
    total_count: 6,
    has_more: false,
    start_index: 0,
  };

  async function openWithHistory(): Promise<{ rt: SessionRuntime; rows: HTMLElement[] }> {
    h.fetch.on('GET', /^\/api\/sessions\/sdk-r\/messages/, HISTORY);
    h.fetch.on('POST', '/api/sessions/R1/close', () => new Response(null, { status: 204 }));
    h.fetch.on('POST', '/api/sessions/sdk-r/truncate', () => jsonResponse({ session_id: 'sdk-r' }));
    h.fetch.on('POST', '/api/sessions/sdk-r/fork', () => jsonResponse({ session_id: 'sdk-copy' }, 201));
    const { rt } = await openAgent('R1', 'sdk-r');
    await pump();
    const rows = Array.from(document.querySelectorAll<HTMLElement>('[data-entry-id]'));
    return { rt, rows };
  }

  function choose(row: HTMLElement, item: string): void {
    fireEvent.click(within(row).getByRole('button', { name: 'Message actions' }));
    fireEvent.click(screen.getByRole('menuitem', { name: new RegExp(item) }));
  }

  it('rewind at a prompt keeps it and drops the 3 visible lines after it (close → truncate)', async () => {
    const { rows } = await openWithHistory();
    expect(rows).toHaveLength(6);
    choose(rows[2] as HTMLElement, 'Rewind to here');
    const dialog = screen.getByRole('alertdialog');
    expect(dialog.textContent).toContain('Messages after this one will be removed. This cannot be undone.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rewind' }));
    for (let i = 0; i < 5; i++) await pump();
    const writes = h.fetch.requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.path} ${JSON.stringify(r.body ?? null)}`);
    expect(writes).toEqual(['POST /api/sessions/R1/close null', 'POST /api/sessions/sdk-r/truncate {"drop_last_n":3}']);
  });

  it('fork from an assistant reply cuts at the next prompt (4 lines); the last entry cannot be rewound', async () => {
    const { rows } = await openWithHistory();
    const last = rows[5] as HTMLElement;
    fireEvent.click(within(last).getByRole('button', { name: 'Message actions' }));
    const rewind = screen.getByRole('menuitem', { name: /Rewind to here/ });
    expect(rewind.getAttribute('aria-disabled')).toBe('true');
    expect(rewind.textContent).toContain('Nothing after this message');
    fireEvent.keyDown(rewind, { key: 'Escape' });
    choose(rows[1] as HTMLElement, 'Fork from here');
    const dialog = screen.getByRole('alertdialog');
    expect(dialog.textContent).toContain('The original is unchanged.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Fork' }));
    for (let i = 0; i < 5; i++) await pump();
    expect(h.fetch.calls('POST', '/api/sessions/sdk-r/fork').map((r) => r.body)).toEqual([{ drop_last_n: 4 }]);
  });

  it('no actions before the session has an sdk id (ID-3)', async () => {
    const { rt } = await openAgent('N1');
    rt.send('hello');
    await pump();
    expect(screen.queryByRole('button', { name: 'Message actions' })).toBeNull();
  });

  it('rewind is disabled while a reply runs', async () => {
    const { rt, rows } = await openWithHistory();
    rt.send('more');
    await pump();
    fireEvent.click(within(rows[0] as HTMLElement).getByRole('button', { name: 'Message actions' }));
    const rewind = screen.getByRole('menuitem', { name: /Rewind to here/ });
    expect(rewind.getAttribute('aria-disabled')).toBe('true');
    expect(rewind.textContent).toContain('Stop the current reply first');
  });
});

describe('empty states and long prompts', () => {
  it('agent: "Start a conversation"', async () => {
    await openAgent('E1');
    expect(screen.getByText('Start a conversation')).toBeTruthy();
  });

  it(`a prompt over ${FOLD_LINES} lines folds with "Show all (N lines)"`, async () => {
    const { rt } = await openAgent('F1');
    const text = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join('\n');
    rt.send(text);
    await pump();
    const toggle = screen.getByRole('button', { name: 'Show all (30 lines)' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Show less' })).toBeTruthy();
    rt.send('one line only');
    await pump();
    expect(screen.queryAllByRole('button', { name: /Show all/ })).toHaveLength(0);
  });
});
