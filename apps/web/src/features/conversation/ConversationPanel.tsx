/**
 * `ConversationPanel{localId, hidden}` (spec 13 §3.6, §3.9): one conversation view. Composes the
 * message list, the inline cards above the composer (IA §6) and the composer / voice-dock slot.
 *
 *   <ConversationPanel localId={tab.id} hidden={hidden} composer={<Composer localId={tab.id} />} />
 *
 * - `composer`: W-11's Composer, or W-12's VoiceDock while voice is active. The panel only places
 *   it (below the cards, inside the 840 column).
 * - `hidden`: the panel stays mounted while its tab is inactive (spec 13 §4.4); the session store
 *   is frozen meanwhile (`setSessionHidden`, W-06) and catches up in one render on show.
 * - `onMessageAction`: W-11 can take over rewind / fork confirmation; by default the panel's own
 *   `MessageActionHost` confirms and runs them.
 * - `onStartVoice`: the empty Archie conversation's big voice button (W-12); hidden when absent.
 */
import { memo, useEffect, useMemo, useState, type ReactNode } from 'react';
import { setSessionHidden } from '@/services';
import { createSessionStore, getSessionEntry, useSession, useSessionRegistryVersion, type SessionStore } from '@/stores';
import { initialConversation } from '@/protocol';
import { ErrorCards, StallCard } from './cards/cards';
import { useRichModule } from './lazyRich';
import { ConversationEmpty } from './ConversationEmpty';
import { MessageActionHost } from './MessageActionHost';
import { MessageActionsContext, type MessageActionRequest, type MessageActionsContextValue } from './MessageActions';
import { MessageList } from './MessageList';
import styles from './Conversation.module.css';

export interface ConversationPanelProps {
  readonly localId: string;
  readonly hidden: boolean;
  /** The composer slot (W-11 Composer, or W-12 VoiceDock). */
  readonly composer?: ReactNode;
  /** Take over rewind / fork (W-11). Default: the built-in confirm + run host. */
  readonly onMessageAction?: (r: MessageActionRequest) => void;
  /** Start realtime voice (W-12); shows the big voice button on an empty Archie conversation. */
  readonly onStartVoice?: () => void;
  /** A suggestion chip was chosen. Default: it becomes the draft (the composer shows it). */
  readonly onSuggestion?: (text: string) => void;
}

let emptyStore: SessionStore | null = null;
/** A store for a `local_id` with no open session (never written). */
function placeholderStore(): SessionStore {
  if (!emptyStore) emptyStore = createSessionStore({ localId: '', conv: initialConversation({ localId: '', kind: 'agent' }), readOnly: true }).store;
  return emptyStore;
}

/** The session's store (re-resolved when sessions are added, removed or re-keyed). */
export function useSessionStore(localId: string): SessionStore {
  useSessionRegistryVersion();
  return getSessionEntry(localId)?.handle.store ?? placeholderStore();
}

/** Cards above the composer, in a fixed order: errors, stall, permission, ended. */
export const InlineCards = memo(function InlineCards({ localId }: { localId: string }) {
  const rich = useRichModule();
  return (
    <div className={styles.cards}>
      <ErrorCards localId={localId} />
      <StallCard localId={localId} />
      {rich ? <rich.RichPermissionCards localId={localId} /> : null}
    </div>
  );
});

function ConversationPanelImpl({ localId, hidden, composer, onMessageAction, onStartVoice, onSuggestion }: ConversationPanelProps) {
  const store = useSessionStore(localId);
  const kind = useSession(localId, (s) => s.conv.ref.kind);
  const [pending, setPending] = useState<MessageActionRequest | null>(null);

  useEffect(() => {
    setSessionHidden(localId, hidden);
  }, [localId, hidden]);

  const actions = useMemo<MessageActionsContextValue>(() => ({ request: onMessageAction ?? setPending }), [onMessageAction]);
  const suggest = onSuggestion ?? ((text: string): void => getSessionEntry(localId)?.handle.setDraft(text));

  return (
    <div className={styles.panel} data-hidden={hidden ? '' : undefined} data-kind={kind}>
      <>
        <MessageActionsContext.Provider value={actions}>
          <MessageList
            localId={localId}
            store={store}
            hidden={hidden}
            empty={<ConversationEmpty kind={kind} onStartVoice={onStartVoice} onSuggestion={suggest} />}
          />
          {/* data-conversation-dock: the floating voice controls stay above it (VoiceOverlayHost) */}
          <div className={styles.dock} data-conversation-dock="">
            <InlineCards localId={localId} />
            {composer ? <div className={styles.composerSlot}>{composer}</div> : null}
          </div>
          {onMessageAction ? null : <MessageActionHost pending={pending} onDone={() => setPending(null)} />}
        </MessageActionsContext.Provider>
      </>
    </div>
  );
}

export const ConversationPanel = memo(ConversationPanelImpl);
