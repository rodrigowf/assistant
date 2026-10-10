/**
 * Internal links in every rendered markdown (spec 12 §9.4): a link to a visualization or a memory
 * file opens it the way the Visuals / Memory lists do (`openDocument`: a tab on medium/expanded,
 * a document screen on compact) instead of a browser tab. The anchor keeps the real URL, so
 * middle / ctrl-click still open a new tab.
 *
 * A visual opens in the viewer only when it is a real page: in the list, or in the list after a
 * refresh (a page written a moment ago). Otherwise a snackbar offers to
 * open the URL in the browser (a user gesture, so no popup blocker), rather than framing a 404.
 */
import type { ReactNode } from 'react';
import { InternalLinksProvider, internalTargetUrl, type InternalLinks, type InternalTarget } from '@/features/links';
import { vizHref, vizOrigin } from '@/features/visuals';
import { httpUrl, refreshVisuals } from '@/services';
import { catalogStore, showSnackbar } from '@/stores';
import { openDocument } from './shell/actions';
import { currentWindowClass } from './useWindowClass';

function backendOrigin(): string {
  const o = vizOrigin();
  if (o) return o;
  return typeof location !== 'undefined' ? location.origin : '';
}

function listed(path: string) {
  return catalogStore.getState().visuals.items.find((v) => v.path === path);
}

function basename(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] ?? path;
}

export function openInternalTarget(t: InternalTarget): void {
  const compact = currentWindowClass() === 'compact';
  if (t.kind === 'memory') {
    openDocument('memory', t.path, { compact, title: basename(t.path) });
    return;
  }
  const open = (): void => {
    const v = listed(t.path);
    openDocument('visual', t.path, { compact, ...(v ? { url: v.url, title: v.title } : {}) });
  };
  if (listed(t.path)) {
    open();
    return;
  }
  void refreshVisuals().then(() => {
    if (listed(t.path)) {
      open();
      return;
    }
    const href = vizHref(t.path);
    showSnackbar(`Not a visual on this server: /${t.path}`, {
      action: { label: 'Open in browser', run: () => window.open(href, '_blank', 'noopener,noreferrer') },
    });
  });
}

const LINKS: InternalLinks = {
  context: {
    get origin() {
      return backendOrigin();
    },
    isVisual: (path) => !!listed(path),
  },
  open: openInternalTarget,
  hrefOf: (t) => (t.kind === 'memory' ? httpUrl(internalTargetUrl(t)) : vizHref(t.path, listed(t.path)?.url)),
};

export function InternalLinksHost({ children }: { children: ReactNode }) {
  return <InternalLinksProvider value={LINKS}>{children}</InternalLinksProvider>;
}
