/**
 * Starts the "agent session finished" notifier (turnNotifier.ts) without putting it in the initial
 * bundle (spec 13 §5.4): the notifier and the notification platform code are a lazy chunk fetched
 * right at startup. Until the chunk is in, `agent_turn_finished` frames are kept here and handed to
 * the notifier once it is installed, so a turn that ends during startup still notifies.
 */
import type { AgentTurnFinishedFrame } from '@/protocol';
import { onAgentTurn } from '@/services';

type Notifier = Pick<typeof import('./turnNotifier'), 'installTurnNotifications' | 'handleAgentTurn'>;

const loadNotifier = (): Promise<Notifier> => import('./turnNotifier');

/** Call once, after the services started. Returns an uninstall (also before the chunk loaded). */
export function startTurnNotifications(load: () => Promise<Notifier> = loadNotifier): () => void {
  let stopped = false;
  let uninstall: (() => void) | null = null;
  const early: AgentTurnFinishedFrame[] = [];
  const offEarly = onAgentTurn((f) => {
    if (f.type === 'agent_turn_finished') early.push(f);
  });
  load().then(
    (m) => {
      offEarly();
      if (stopped) return;
      uninstall = m.installTurnNotifications();
      for (const f of early.splice(0)) m.handleAgentTurn(f);
    },
    (err: unknown) => {
      offEarly();
      early.length = 0;
      console.warn('[notify] notifier failed to load', err);
    },
  );
  return () => {
    stopped = true;
    offEarly();
    early.length = 0;
    uninstall?.();
    uninstall = null;
  };
}
