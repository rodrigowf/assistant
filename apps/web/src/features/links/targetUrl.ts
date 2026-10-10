/**
 * Where an internal link goes (spec 12 §9.4) and its app URL. Apart from `internalLinks.ts` (the
 * resolver) so the app root, which only builds hrefs, does not pull the resolver into the initial
 * bundle (spec 13 §5.4): the resolver stays in the markdown chunk.
 */

export type InternalTarget =
  | { readonly kind: 'visual'; readonly path: string }
  | { readonly kind: 'memory'; readonly path: string; readonly fragment: string | null };

/** The app URL of a target, root-relative (the `href` for middle-click and "copy link"). */
export function internalTargetUrl(t: InternalTarget): string {
  const enc = (p: string): string => p.split('/').map(encodeURIComponent).join('/');
  return t.kind === 'memory' ? `/memory/${enc(t.path)}${t.fragment ? `#${t.fragment}` : ''}` : `/${enc(t.path)}`;
}
