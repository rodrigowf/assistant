/**
 * Entry point of `@/features/conversation` (W-09, spec 13 §3.6, §3.9).
 *
 *   <ConversationPanel localId hidden composer? onMessageAction? onStartVoice? onSuggestion? />
 *
 * One conversation view: the bounded message list (freeze buffer, prepend anchoring, 80 / 150 px
 * thresholds), message actions (rewind / fork entry points), inline cards above the composer
 * (permission, agent approvals, stall, errors, termination) and the empty states. The composer is
 * a slot (W-11 Composer / W-12 VoiceDock).
 *
 * Markdown and the tool cards load in a separate chunk (rich.tsx, started at module evaluation);
 * nothing exported here pulls them into the initial bundle.
 */
export { ConversationPanel, InlineCards, useSessionStore, type ConversationPanelProps } from './ConversationPanel';
export { MessageList, type MessageListProps } from './MessageList';
export { ConversationEmpty, ARCHIE_SUGGESTIONS, type ConversationEmptyProps } from './ConversationEmpty';
export { MessageActions, MessageActionsContext, type MessageActionKind, type MessageActionRequest, type MessageActionsContextValue } from './MessageActions';
export { MessageActionHost, ACTION_COPY } from './MessageActionHost';
export { ErrorCards, StallCard, bannerText, formatStall, stallText } from './cards/cards';
export { PLAN_HINT, PLAN_TITLE, FEEDBACK_HINT } from './cards/copy';
export { preloadRich } from './lazyRich';
export { InlineCard, type InlineCardProps, type InlineCardTone } from './cards/InlineCard';
export { useMessageWindow, type MessageWindow, type UseMessageWindowOptions } from './useMessageWindow';
export { EXPAND_STEP, LOAD_MORE_PX, NEAR_BOTTOM_PX, WINDOW_CAP_LOW_END, WINDOW_CAP_MAIN, viewOf, type WindowState, type WindowView } from './messageWindow';
export { visualFromTool, type VisualRef } from './entries/VisualCard';
