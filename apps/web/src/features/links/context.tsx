/**
 * Host wiring for internal links (spec 12 §9.4). The app root provides how to open a
 * visualization / memory file and the backend origin; every `Markdown` below it (chat, plans,
 * tool output, memory documents) then opens internal links in the app and auto-links printed
 * paths (LNK-5). Without a provider, links behave as before.
 *
 * Kept apart from `@/features/markdown` so the app root can provide it without pulling the
 * markdown renderer into the initial bundle (spec 13 §5.4).
 */
import { createContext, useContext, type ReactNode } from 'react';
import type { InternalLinkContext, InternalTarget } from './internalLinks';

export interface InternalLinks {
  readonly context: InternalLinkContext;
  readonly open: (target: InternalTarget) => void;
  /** The absolute URL of a target (the anchor's `href`: middle-click, "copy link", LNK-6). */
  readonly hrefOf: (target: InternalTarget) => string;
}

const InternalLinksContext = createContext<InternalLinks | null>(null);

/** `value` must be stable (memoize it): every Markdown below re-renders when it changes. */
export function InternalLinksProvider({ value, children }: { value: InternalLinks; children: ReactNode }) {
  return <InternalLinksContext.Provider value={value}>{children}</InternalLinksContext.Provider>;
}

export function useInternalLinks(): InternalLinks | null {
  return useContext(InternalLinksContext);
}
