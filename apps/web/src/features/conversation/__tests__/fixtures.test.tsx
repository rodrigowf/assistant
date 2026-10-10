/**
 * The conversation view against the shared protocol fixtures, replayed state by state through the
 * real reducer into a session store (coalesced publish, W-06):
 *
 * - R4: at every step the DOM shows entries and blocks in exactly the reducer's order.
 * - R7: a tool result shows in its card as soon as it arrives, including after an interleaved user
 *   message (result by id into an earlier run).
 * - Voice transcripts (owner and passive viewer), BG-1 background divider, compaction divider,
 *   termination card (the view stays).
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import type { Conversation } from '@/protocol';
import { clearSessionRegistry, createManualScheduler, type ManualScheduler } from '@/stores';
import { ConversationPanel } from '../ConversationPanel';
import { preloadRich } from '../lazyRich';
import { convSequence, domSequence, fixtureRun, seedSession } from './helpers';

// The rich chunk (markdown, tool cards) loads lazily in the app; tests load it up front.
beforeAll(async () => {
  await preloadRich();
});

let scheduler: ManualScheduler;

beforeEach(() => {
  scheduler = createManualScheduler();
});
afterEach(() => {
  clearSessionRegistry();
});

async function replay(name: string, check: (conv: Conversation, container: HTMLElement, i: number) => void): Promise<Conversation> {
  const { trace, conv: last } = fixtureRun(name);
  const first = trace[0]?.prev ?? last;
  const { handle } = seedSession(first, { scheduler });
  const { container } = render(<ConversationPanel localId={first.ref.localId} hidden={false} />);
  check(first, container, -1);
  for (let i = 0; i < trace.length; i++) {
    const t = trace[i] as (typeof trace)[number];
    await act(async () => {
      handle.setConv(t.next);
      scheduler.flush();
      await Promise.resolve();
    });
    check(t.next, container, i);
  }
  return last;
}

const R4_FIXTURES = [
  'text_tool_interleaving',
  'parallel_tools_reverse_results',
  'thinking_and_text',
  'tool_result_after_interleaved_user_message',
  'tool_result_after_voice_transcript',
  'android_voice_ordering_bug',
  'permission_request_resolve',
  'web_bug5_permission_feedback_then_result',
  'background_notification_wake_turn',
  'compaction',
  'interrupt_mid_tool',
];

describe('R4: render order equals reducer order', () => {
  it.each(R4_FIXTURES)('%s, at every step', async (name) => {
    await replay(name, (conv, container) => {
      expect(domSequence(container)).toEqual(convSequence(conv.entries, conv.ref.kind));
    });
  });

  it('text / tool / text / tool / text keeps the interleaving (no grouping by type)', async () => {
    let seq: string[] = [];
    await replay('text_tool_interleaving', (_c, container) => {
      seq = domSequence(container);
    });
    expect(seq).toEqual(['user:list and read', 'text:Let me look.', 'tool:Bash', 'text:Found two files.', 'tool:Read', 'text:a.txt says hello.']);
  });
});

describe('R7: tool output appears live', () => {
  it('the Bash card shows its output the moment tool_result arrives (live tail, open)', async () => {
    let seenLive = false;
    await replay('text_tool_interleaving', (conv, container) => {
      const run = conv.entries[1];
      if (!run || run.kind !== 'assistant') return;
      const bash = run.blocks.find((b) => b.type === 'tool' && b.tool_name === 'Bash');
      const textsAfter = run.blocks.length;
      if (bash && bash.type === 'tool' && bash.status === 'done' && textsAfter === 2) {
        const card = container.querySelector('[data-tool="Bash"]') as HTMLElement;
        expect(card.getAttribute('data-status')).toBe('done');
        expect(card.textContent).toContain('a.txt');
        expect(card.textContent).toContain('b.txt');
        seenLive = true;
      }
    });
    expect(seenLive).toBe(true);
  });

  it('a result after an interleaved user message lands in the earlier run’s card (R-1)', async () => {
    const last = await replay('tool_result_after_interleaved_user_message', () => undefined);
    const tools = last.entries.flatMap((e) => (e.kind === 'assistant' ? e.blocks.filter((b) => b.type === 'tool') : []));
    expect(tools.every((t) => t.type === 'tool' && t.status === 'done')).toBe(true);
    const cards = document.querySelectorAll('[data-tool]');
    expect(cards.length).toBe(tools.length);
    cards.forEach((c) => expect(c.getAttribute('data-status')).toBe('done'));
    // the first card sits above the user message and still shows its output on expand
    const first = cards[0] as HTMLElement;
    const header = within(first).getAllByRole('button')[0] as HTMLElement;
    if (header.getAttribute('aria-expanded') !== 'true') fireEvent.click(header);
    const t0 = tools[0];
    if (t0 && t0.type === 'tool' && t0.output) expect(first.textContent).toContain(t0.output.split('\n')[0]);
  });
});

describe('I-12: queued prompts', () => {
  it.each(['queued_prompt_sender', 'queued_prompt_echoed_twice', 'queued_prompt_observer_no_reecho', 'history_prepend_queued_prompt_dispatch'])(
    '%s: a queued prompt is never rendered while queued, and exactly once after dispatch',
    async (name) => {
      await replay(name, (conv, container) => {
        const rendered = Array.from(container.querySelectorAll('[data-kind="user"]')).map((r) => r.querySelector('[class*="userText"]')?.textContent ?? '');
        for (const q of conv.queue) {
          const inTimeline = conv.entries.filter((e) => e.kind === 'user' && e.text === q.text).length;
          expect(rendered.filter((t) => t === q.text)).toHaveLength(inTimeline);
        }
        for (const e of conv.entries)
          if (e.kind === 'user')
            expect(rendered.filter((t) => t === e.text).length).toBe(conv.entries.filter((x) => x.kind === 'user' && x.text === e.text).length);
      });
    },
  );
});

describe('voice transcripts', () => {
  it('a Gemini transcript coalesces live with "VOICE · LIVE" and a caret, then settles to "VOICE"', async () => {
    let sawLive = false;
    await replay('voice_transcript_coalescing_gemini', (conv, container) => {
      const live = conv.entries.find((e) => e.kind === 'user' && e.streaming === true);
      if (live && live.kind === 'user') {
        const row = container.querySelector(`[data-entry-id="${live.id}"]`) as HTMLElement;
        expect(row.textContent).toContain('VOICE · LIVE');
        expect(row.querySelector('[class*="caret"]')).not.toBeNull();
        sawLive = true;
      }
    });
    expect(sawLive).toBe(true);
    expect(screen.queryByText('VOICE · LIVE')).toBeNull();
    expect(screen.getAllByText('VOICE').length).toBeGreaterThan(0);
  });

  it('a passive viewer renders the mirrored transcripts like the owner (VT-2)', async () => {
    const last = await replay('voice_passive_viewer_no_wedge', () => undefined);
    const voiceUsers = last.entries.filter((e) => e.kind === 'user' && e.origin === 'voice');
    expect(voiceUsers.length).toBeGreaterThan(0);
    for (const u of voiceUsers) {
      const row = document.querySelector(`[data-entry-id="${u.id}"]`) as HTMLElement;
      expect(row.textContent).toContain('VOICE');
    }
    const voiceText = last.entries.flatMap((e) => (e.kind === 'assistant' ? e.blocks.filter((b) => b.type === 'text' && b.scope === 'voice') : []));
    expect(voiceText.length).toBeGreaterThan(0);
    expect(document.querySelectorAll('[data-block="text"][data-scope="voice"]').length).toBe(voiceText.length);
  });
});

describe('notices and termination', () => {
  it('BG-1: an unprompted orchestrator reply sits under a "Background update" divider', async () => {
    const last = await replay('background_notification_wake_turn', () => undefined);
    const idx = last.entries.findIndex((e) => e.kind === 'notice' && e.notice === 'background');
    expect(idx).toBeGreaterThanOrEqual(0);
    const rows = Array.from(document.querySelectorAll('[data-entry-id]'));
    const divider = rows[idx] as HTMLElement;
    expect(divider.getAttribute('data-notice')).toBe('background');
    expect(divider.textContent).toContain('Background update');
    expect(divider.textContent).toContain('The orchestrator replied to a background task or to another device.');
    expect(rows[idx + 1]?.getAttribute('data-kind')).toBe('assistant');
  });

  it('compaction renders a divider whose summary expands in place', async () => {
    const last = await replay('compaction', () => undefined);
    const notices = last.entries.filter((e) => e.kind === 'notice' && e.notice === 'compaction');
    expect(notices.length).toBeGreaterThan(0);
    const pills = screen.getAllByText(/Context compacted/);
    expect(pills.length).toBe(notices.length);
    const withSummary = notices.find((n) => n.kind === 'notice' && n.text);
    if (withSummary && withSummary.kind === 'notice') {
      const row = document.querySelector(`[data-entry-id="${withSummary.id}"]`) as HTMLElement;
      const toggle = within(row).getByRole('button');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(row.textContent).toContain(withSummary.text);
    }
  });
});
