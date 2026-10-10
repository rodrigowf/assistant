/**
 * Markdown side of internal links (spec 12 §9.4): the `LinkResolver` built from the app's
 * `InternalLinks` (`@/features/links`), and resolver chaining (an explicit resolver first).
 */
import { resolveInternalLink, type InternalLinks } from '@/features/links';
import type { LinkResolver } from './links';

export function createInternalLinkResolver(links: InternalLinks): LinkResolver {
  return (href) => {
    const target = resolveInternalLink(href, links.context);
    if (!target) return null;
    return { href: links.hrefOf(target), onActivate: () => links.open(target), title: target.path };
  };
}

/** `first`, then `second` for the links `first` does not handle. */
export function chainResolvers(first: LinkResolver | undefined, second: LinkResolver | undefined): LinkResolver | undefined {
  if (!first || !second) return first ?? second;
  return (href) => first(href) ?? second(href);
}
