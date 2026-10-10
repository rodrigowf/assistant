/**
 * Conversation gallery section (W-09, dev only): the conversation view in every state the mockups
 * show (desktop conversation, Archie with an agent approval, voice transcripts with the live caret,
 * stall + error, connection lost, background / compaction dividers, a visual with Open / Show on
 * TV, empty Archie and agent, a long conversation scrolled up with buffered messages).
 *
 * Screen mode for screenshots: `#/dev/gallery/conversation?screen=<scene>&theme=dark` renders one
 * scene full-viewport. `screen=live&scenario=<fixture>` runs a shared protocol fixture live
 * through the real services against the mock backend (`npm run mock`, dev server with
 * ARCHIE_BACKEND pointed at it).
 */
import { useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  initialConversation,
  type AssistantEntry,
  type Block,
  type Conversation,
  type Entry,
  type NoticeEntry,
  type SessionKind,
  type ToolBlock,
  type UserEntry,
} from '@/protocol';
import { openSession, type SessionRuntime } from '@/services';
import { createSessionStore, getSessionEntry, patchCapabilities, registerSession, setCatalogItems, type SessionStoreHandle } from '@/stores';
import { ConversationPanel } from './ConversationPanel';
import styles from './Conversation.gallery.module.css';

// ───────────────────────── builders ─────────────────────────

let n = 0;
const id = (p: string): string => `${p}${(n += 1)}`;
const user = (text: string, over: Partial<UserEntry> = {}): UserEntry => ({ id: id('u'), kind: 'user', text, origin: 'local', state: 'sent', ...over });
const text = (t: string, streaming = false, scope: 'turn' | 'voice' = 'turn'): Block => ({
  id: id('t'),
  type: 'text',
  text: t,
  streaming,
  scope,
  origin: 'live',
});
const tool = (name: string, input: Record<string, unknown>, output: string | null, status: ToolBlock['status'] = 'done'): ToolBlock => ({
  id: id('k'),
  type: 'tool',
  tool_use_id: id('toolu_'),
  tool_name: name,
  tool_input: input,
  status,
  output,
  scope: 'turn',
  origin: 'live',
});
const run = (...blocks: Block[]): AssistantEntry => ({ id: id('a'), kind: 'assistant', blocks });
const notice = (kind: NoticeEntry['notice'], t = '', data?: Record<string, unknown>): NoticeEntry => ({
  id: id('n'),
  kind: 'notice',
  notice: kind,
  text: t,
  ...(data ? { data } : {}),
});

function conv(localId: string, kind: SessionKind, entries: Entry[], over: Partial<Conversation> = {}): Conversation {
  const c = initialConversation({ localId, kind, sdkId: `sdk-${localId}`, provider: kind === 'agent' ? 'claude' : null, subscribed: true });
  return { ...c, status: 'idle', entries, history: { loaded: true, startIndex: 0, totalCount: entries.length, hasMore: false }, ...over };
}

const inert = (localId: string) => ({
  localId,
  dispose: () => undefined,
  loadOlder: () => Promise.resolve(),
  retry: () => undefined,
  interrupt: () => undefined,
  reload: () => Promise.resolve(),
  dismissBanner: () => undefined,
});

function seed(c: Conversation): SessionStoreHandle {
  const existing = getSessionEntry(c.ref.localId);
  if (existing) return existing.handle;
  const handle = createSessionStore({ localId: c.ref.localId, conv: c });
  registerSession(c.ref.localId, { handle, runtime: inert(c.ref.localId) });
  return handle;
}

// ───────────────────────── scenes (mockups) ─────────────────────────

const EDIT = {
  file_path: '/home/rodrigo/assistant/orchestrator/session.py',
  old_string: '        if self._state == VoiceState.IDLE:\n            return\n        self._voice = False\n',
  new_string: '        self._voice_sm.end(reason)\n',
};

function desktop(): Conversation {
  return conv('g-desktop', 'agent', [
    user('Why does end_voice sometimes skip its cleanup?'),
    run(
      tool('Grep', { pattern: '_voice', path: 'orchestrator/' }, 'orchestrator/session.py: 14 matches\norchestrator/providers/openai_voice.py: 6 matches'),
      tool('Read', { file_path: '/home/rodrigo/assistant/orchestrator/session.py' }, '642 lines'),
      tool('Read', { file_path: '/home/rodrigo/assistant/orchestrator/providers/openai_voice.py' }, '388 lines'),
      text(
        "Voice state is split between `OrchestratorSession` and `OpenAIVoiceProvider`, and both flip `_voice` directly. That is why `end_voice` can return early. I'll move every transition into one `VoiceStateMachine` and keep the public API as it is.",
      ),
    ),
    user('Go ahead. Keep the parity tests green.'),
    run(
      text('Extracting the state machine now.'),
      tool(
        'Read',
        { file_path: '/home/rodrigo/assistant/orchestrator/voice/state.py' },
        '  1  class VoiceState(Enum):\n  2      IDLE = "idle"; LISTENING = "listening"; SPEAKING = "speaking"',
      ),
      tool('Grep', { pattern: 'self._voice =', path: 'orchestrator/' }, 'session.py:212    self._voice = True\nsession.py:388    self._voice = False'),
      tool('Edit', EDIT, 'The file /home/rodrigo/assistant/orchestrator/session.py has been updated.'),
      tool('Bash', { command: 'pytest tests/test_voice_parity.py -q' }, '..............................................  [100%]\n46 passed in 3.82s'),
      text(
        'Done. `VoiceStateMachine` owns every transition, `end_voice` no longer skips cleanup when idle, and all **46 parity tests** pass. Should I commit this on `voice-refactor`?',
      ),
    ),
  ]);
}

function streaming(): Conversation {
  const base = desktop();
  const last = base.entries[base.entries.length - 1] as AssistantEntry;
  const bash = tool('Bash', { command: 'pytest tests/test_voice_parity.py -q' }, null, 'running');
  return conv('g-stream', 'agent', [...base.entries.slice(0, -1), { ...last, blocks: [...last.blocks.slice(0, 4), bash] }], {
    status: 'tool_use',
    inTurn: true,
  });
}

function archieApproval(): Conversation {
  return conv(
    'g-archie',
    'orchestrator',
    [
      user('Plan the living-room TV setup for movie night on Friday.'),
      run(
        text("On it. I'll check what the Fire TV can play, then ask an agent to draft the setup."),
        tool('run_script', { script: 'connect_tv.py' }, 'Connected to Fire TV · 10.0.0.42'),
        tool('send_to_agent_session', { session_id: 'agent-tv-plan', message: 'Draft the TV setup plan for Friday' }, null, 'running'),
      ),
    ],
    {
      status: 'tool_use',
      inTurn: true,
      agentApprovals: [
        {
          localId: 'agent-tv-plan',
          request_id: 'r-plan',
          tool_name: 'ExitPlanMode',
          tool_input: { plan: '1. Check Kodi and Plex on the Fire TV\n2. Queue three films from the watchlist\n3. Set the soundbar to movie mode' },
        },
      ],
    },
  );
}

function voice(): Conversation {
  return conv(
    'g-voice',
    'orchestrator',
    [
      user('Approve the plan.', { origin: 'voice' }),
      run(
        tool('respond_to_agent_permission', { session_id: 'agent-tv-plan', decision: 'allow' }, 'Approved ExitPlanMode'),
        tool('run_script', { script: 'tv_remote.py', args: ['queue'] }, '3 titles queued'),
        tool('run_script', { script: 'soundbar.py', args: ['mode=movie'] }, 'OK'),
        text('Kodi and Plex are both installed. I queued the three films and set the soundbar to movie mode.', false, 'voice'),
      ),
      user('Dim the living-room lights to thirty percent.', { origin: 'voice' }),
      run(
        tool('run_script', { script: 'home_lights.py', args: ['--room', 'living', '--level', '30'] }, 'living-room: 30% (3 lamps)'),
        text('Done, the lights are at 30 percent.', false, 'voice'),
      ),
      user('And put the jazz playlist on the TV', { origin: 'voice', streaming: true }),
    ],
    { voiceActive: true },
  );
}

function stallError(): Conversation {
  const webfetch = tool('WebFetch', { url: 'https://developer.android.com/media/optimize/audio-focus', prompt: 'How does ducking work?' }, null, 'running');
  return conv(
    'g-stall',
    'agent',
    [
      user('Make echo ducking use audio focus instead of lowering the stream volume.'),
      run(
        tool('Grep', { pattern: 'duck', path: 'android/' }, 'AudioDucker.kt: 9 matches'),
        tool('Read', { file_path: '/home/rodrigo/assistant/android/app/src/main/kotlin/AudioDucker.kt' }, '184 lines'),
        text("Ducking lives in `AudioDucker.kt`. Before I change it I'll check how Android hands out audio focus."),
        webfetch,
      ),
    ],
    {
      status: 'tool_use',
      inTurn: true,
      stall: { elapsed_seconds: 134, last_tool_name: 'WebFetch', last_tool_use_id: webfetch.tool_use_id },
      connectionBanner: { code: 'start_failed', detail: 'Working directory /srv/x does not exist.' },
    },
  );
}

function connectionLost(): Conversation {
  return conv(
    'g-lost',
    'orchestrator',
    [
      user('Dim the living-room lights to thirty percent.', { origin: 'voice' }),
      run(
        tool('run_script', { script: 'home_lights.py', args: ['--room', 'living', '--level', '30'] }, 'living-room: 30% (3 lamps)'),
        text('Done, the lights are at 30 percent.', false, 'voice'),
      ),
      user('And put the jazz playlist on the TV.', { origin: 'voice' }),
      run(text('Starting the jazz playlist on the', true, 'voice')),
    ],
    { conn: 'offline', connectionBanner: { code: 'disconnected', detail: null }, voiceActive: true },
  );
}

function dividers(): Conversation {
  return conv('g-div', 'orchestrator', [
    user('Good morning. Anything I should know today?'),
    run(
      tool('search_memory', { query: 'today' }, '3 results'),
      tool('run_script', { script: 'weather.py', args: ['--tomorrow'] }, 'Sunny, 29 °C'),
      text(
        'Three things:\n\n- The voice refactor finished overnight and all 46 parity tests pass.\n- Tomorrow is sunny, 29 °C.\n- The Jetson ran warm at 3 am (71 °C) but has been fine since.',
      ),
    ),
    notice('compaction', '', { trigger: 'manual', tokens_before: 182000, tokens_after: 21000 }),
    notice('background'),
    run(text('The **Energy dashboard** agent finished: the weekly chart is ready.')),
    user('Show it on the TV.'),
    run(
      tool(
        'Write',
        { file_path: '/home/rodrigo/assistant/context/public/visualizations/weekly-energy/index.html', content: '<!doctype html>' },
        'File created successfully',
      ),
      text("It's on the living-room TV. Want a monthly comparison next to it?"),
    ),
    notice('interrupted'),
    notice('error', 'The model provider returned 529 (overloaded). Try again in a moment.'),
  ]);
}

function long(): Conversation {
  const entries: Entry[] = [];
  for (let i = 1; i <= 40; i++) {
    entries.push(user(`Question ${i}: what changed in step ${i}?`));
    entries.push(run(text(`Step ${i} renamed \`helper_${i}\` and added a test. Nothing else moved.`)));
  }
  return conv('g-long', 'agent', entries, { history: { loaded: true, startIndex: 120, totalCount: 200, hasMore: true } });
}

const SCENES: Record<string, { title: string; note: string; make: () => Conversation; voice?: boolean }> = {
  desktop: { title: 'Agent conversation', note: 'Mockups desktop: groups collapse once text follows; prose full width; tonal user bubbles', make: desktop },
  streaming: { title: 'Streaming', note: 'Live tail: the group stays open and the running Bash card shows "Running…"', make: streaming },
  archie: { title: 'Archie with an agent approval', note: 'Mockups phone (a): permission card above the composer', make: archieApproval },
  voice: { title: 'Voice transcripts', note: 'Mockups phone (d): VOICE tags, the live one with a caret', make: voice },
  stall: { title: 'Stall and error', note: 'Mockups phone (j): stall card with Interrupt, error card with Details / Retry', make: stallError },
  lost: { title: 'Connection lost', note: 'Mockups phone (k): a system line, never a timeline entry', make: connectionLost },
  dividers: {
    title: 'Dividers, visual card, notices',
    note: 'Compaction, Background update (BG-1), inline visual (tablet frame), interrupted, turn error',
    make: dividers,
  },
  long: { title: 'Long conversation', note: 'Scroll up: "Scroll up for older messages", freeze buffer, Jump to latest', make: long },
  'empty-archie': { title: 'Empty Archie', note: 'Mockups phone (i)', make: () => conv('g-empty-a', 'orchestrator', []), voice: true },
  'empty-agent': { title: 'Empty agent session', note: 'Start a conversation', make: () => conv('g-empty-g', 'agent', []) },
};

let prepared = false;
function prepare(): void {
  if (prepared) return;
  prepared = true;
  // Screenshot hook: append entries to a scene (e.g. while scrolled up, to show the freeze buffer).
  (window as unknown as { __w09: unknown }).__w09 = {
    push(localId: string, count = 1): void {
      const e = getSessionEntry(localId);
      if (!e) return;
      const c = e.handle.store.getState().conv;
      const more: Entry[] = [];
      for (let i = 0; i < count; i++)
        more.push(i % 2 ? run(text('A new reply arrived while you were reading.')) : user('A new question from another device.', { origin: 'echo' }));
      e.handle.setConv({ ...c, entries: [...c.entries, ...more] });
    },
  };
  patchCapabilities({ castAvailable: true });
  setCatalogItems('sessions', [
    {
      session_id: 'sdk-agent-tv-plan',
      local_id: 'agent-tv-plan',
      title: 'TV setup plan',
      started_at: '',
      last_activity: '',
      message_count: 2,
      is_orchestrator: false,
      provider: 'claude',
    },
  ] as never);
}

/** Gallery-only stand-in for W-11's composer, so scenes read like the mockups. */
function ComposerStub({ kind }: { kind: SessionKind }) {
  return (
    <div className={styles.composer} aria-hidden="true">
      <span className={styles.composerText}>{kind === 'orchestrator' ? 'Message Archie…' : 'Message the agent…'}</span>
      <span className={styles.composerNote}>composer · W-11</span>
    </div>
  );
}

function Scene({ name }: { name: string }) {
  prepare();
  const s = SCENES[name] ?? (SCENES.desktop as (typeof SCENES)[string]);
  const [c] = useState(() => {
    const made = s.make();
    seed(made);
    return made;
  });
  return (
    <ConversationPanel
      localId={c.ref.localId}
      hidden={false}
      composer={<ComposerStub kind={c.ref.kind} />}
      {...(s.voice ? { onStartVoice: () => undefined } : {})}
    />
  );
}

/** A shared protocol fixture run live through the real services (mock backend). */
function LiveScene({ scenario, prompt }: { scenario: string; prompt: string }) {
  const [localId] = useState(() => `${scenario}:${Math.random().toString(36).slice(2, 8)}`);
  const [started, setStarted] = useState(false);
  useEffect(() => {
    const rt = openSession({ kind: 'agent', localId, provider: 'claude', focus: false }) as SessionRuntime;
    const t = setTimeout(() => {
      rt.send(prompt);
      setStarted(true);
    }, 600);
    return () => {
      clearTimeout(t);
    };
  }, [localId, prompt]);
  return (
    <div className={styles.live} data-started={started ? '' : undefined}>
      <ConversationPanel localId={localId} hidden={false} composer={<ComposerStub kind="agent" />} />
    </div>
  );
}

function hashParams(): Record<string, string> {
  const h = window.location.hash;
  const q = h.indexOf('?');
  const out: Record<string, string> = {};
  if (q < 0) return out;
  for (const pair of h.slice(q + 1).split('&')) {
    const [k, v = ''] = pair.split('=');
    if (k) out[decodeURIComponent(k)] = decodeURIComponent(v);
  }
  return out;
}

function Frame({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <section className={styles.section}>
      <h3 className={styles.title}>
        {title}
        <small>{note}</small>
      </h3>
      <div className={styles.frame}>{children}</div>
    </section>
  );
}

export function ConversationGallery() {
  const params = hashParams();
  // Portaled to <body>: the gallery page's containers would otherwise offset a fixed element.
  if (params.screen === 'live') {
    return createPortal(
      <div className={styles.screen}>
        <LiveScene scenario={params.scenario ?? 'text_tool_interleaving'} prompt={params.prompt ?? 'list and read'} />
      </div>,
      document.body,
    );
  }
  if (params.screen) {
    return createPortal(
      <div className={styles.screen}>
        <Scene key={params.screen} name={params.screen} />
      </div>,
      document.body,
    );
  }
  return (
    <div className={styles.page}>
      <p className={styles.lead}>
        Open one scene full-viewport with <code>?screen=&lt;name&gt;</code> (names: {Object.keys(SCENES).join(', ')}, or{' '}
        <code>live&amp;scenario=&lt;fixture&gt;</code>).
      </p>
      {Object.keys(SCENES).map((k) => {
        const s = SCENES[k] as (typeof SCENES)[string];
        return (
          <Frame key={k} title={s.title} note={`${k} · ${s.note}`}>
            <Scene name={k} />
          </Frame>
        );
      })}
    </div>
  );
}
