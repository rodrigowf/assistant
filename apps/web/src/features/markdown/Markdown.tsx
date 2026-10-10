/**
 * Static markdown (spec 13 §2.4, §3.6; inv02 F-03): react-markdown 10 + remark-gfm 4 on both
 * builds. `mdast-util-gfm-autolink-literal` is aliased to the lookbehind-free fork in
 * `gfm/autolinkLiteralSafe.ts`, so compat gets full GFM (tables, task lists, strikethrough,
 * autolinks, footnotes) with inline formatting intact everywhere (fixes inv02 §5.3 / §6.3 #12).
 *
 * - No raw HTML (no rehype-raw). react-markdown's default `urlTransform` drops `javascript:` URLs.
 * - Links: external in a new tab with the `md-link` class; a `linkResolver` may take some
 *   links in-app (memory documents, `links.ts`). Under an `InternalLinksProvider`, links to a
 *   visualization or memory file open in the app and printed paths are auto-linked (spec 12 §9.4,
 *   `@/features/links`); the explicit `linkResolver` is asked first.
 * - Every fence becomes a <CodeBlock> (with or without a language). Tables scroll sideways.
 * - Memoized by props: a host must keep `linkResolver` stable (useMemo/useCallback).
 */
import type { Element, ElementContent } from 'hast';
import { memo, useMemo, type MouseEvent } from 'react';
import ReactMarkdown, { type Components, type Options } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { useInternalLinks } from '@/features/links';
import { chainResolvers, createInternalLinkResolver } from './internalLinksContext';
import { EXTERNAL_LINK_PROPS, MD_LINK_CLASS, type LinkResolver } from './links';
import { remarkInternalPaths } from './remarkInternalPaths';
import styles from './Markdown.module.css';

export interface MarkdownProps {
  source: string;
  /** In-app link handling (e.g. relative memory links); other links open in a new tab. */
  linkResolver?: LinkResolver;
  /** Allow syntax highlighting of code fences (default true). */
  highlight?: boolean;
  /** Extra class on the root element. */
  className?: string;
}

type PluggableList = NonNullable<Options['remarkPlugins']>;

const REMARK_PLUGINS: PluggableList = [remarkGfm];

function textOf(node: ElementContent): string {
  if (node.type === 'text') return node.value;
  if (node.type === 'element') return node.children.map(textOf).join('');
  return '';
}

/** Code and language of a `<pre><code class="language-x">` block (hast from mdast-util-to-hast). */
export function codeOfPre(node: Element | undefined): { code: string; lang: string | null } {
  const codeEl = node?.children.find((c): c is Element => c.type === 'element' && c.tagName === 'code');
  if (!codeEl) return { code: node ? node.children.map(textOf).join('') : '', lang: null };
  const cls = codeEl.properties.className;
  const classes = Array.isArray(cls) ? cls.map(String) : cls == null ? [] : String(cls).split(' ');
  const langClass = classes.find((c) => c.startsWith('language-'));
  // mdast-util-to-hast appends one "\n" to every non-empty fence.
  const code = codeEl.children.map(textOf).join('').replace(/\n$/, '');
  return { code, lang: langClass ? langClass.slice('language-'.length) : null };
}

function isModifiedClick(e: MouseEvent): boolean {
  return e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
}

function buildComponents(linkResolver: LinkResolver | undefined, highlight: boolean): Components {
  return {
    a({ node: _node, href, children, className, ...rest }) {
      const cls = className ? `${MD_LINK_CLASS} ${className}` : MD_LINK_CLASS;
      if (href && href.startsWith('#')) {
        // In-page anchors (GFM footnotes): same tab.
        return (
          <a href={href} className={cls} {...rest}>
            {children}
          </a>
        );
      }
      const resolved = href && linkResolver ? linkResolver(href) : null;
      if (resolved) {
        const onClick = (e: MouseEvent<HTMLAnchorElement>): void => {
          if (isModifiedClick(e)) return;
          e.preventDefault();
          resolved.onActivate();
        };
        return (
          <a href={resolved.href} className={cls} title={resolved.title} onClick={onClick} {...rest}>
            {children}
          </a>
        );
      }
      return (
        <a href={href} className={cls} {...EXTERNAL_LINK_PROPS} {...rest}>
          {children}
        </a>
      );
    },
    pre({ node }) {
      const { code, lang } = codeOfPre(node);
      return <CodeBlock code={code} lang={lang} highlight={highlight} />;
    },
    code({ node: _node, className, children, ...rest }) {
      // Only inline code reaches here: fences are rendered whole by `pre` above.
      return (
        <code className={className ? `${styles.inlineCode} ${className}` : styles.inlineCode} {...rest}>
          {children}
        </code>
      );
    },
    table({ node: _node, children, ...rest }) {
      return (
        // Focusable so keyboard users can scroll wide tables (axe: scrollable-region-focusable).
        // No role="region": a page with several tables would repeat one landmark name.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        <div className={styles.tableScroll} tabIndex={0}>
          <table {...rest}>{children}</table>
        </div>
      );
    },
    input({ node: _node, type, checked }) {
      // GFM task-list checkboxes: a read-only visual box plus a text alternative, instead of a
      // disabled <input> with no label (axe "label") and no 16px rule (spec 13 §2.6).
      if (type !== 'checkbox') return null;
      return (
        <span className={styles.taskBox} data-checked={checked ? 'true' : 'false'}>
          <span className={styles.visuallyHidden}>{checked ? 'Done:' : 'To do:'}</span>
        </span>
      );
    },
  };
}

interface MarkdownBodyProps {
  source: string;
  linkResolver?: LinkResolver;
  highlight: boolean;
}

/** The parsed content with no wrapper; StreamingMarkdown renders several inside one root. */
function MarkdownBodyImpl({ source, linkResolver, highlight }: MarkdownBodyProps) {
  const internal = useInternalLinks();
  const resolver = useMemo(() => chainResolvers(linkResolver, internal ? createInternalLinkResolver(internal) : undefined), [linkResolver, internal]);
  const plugins = useMemo<PluggableList>(() => (internal ? [remarkGfm, [remarkInternalPaths, internal.context]] : REMARK_PLUGINS), [internal]);
  const components = useMemo(() => buildComponents(resolver, highlight), [resolver, highlight]);
  return (
    <ReactMarkdown remarkPlugins={plugins} components={components}>
      {source}
    </ReactMarkdown>
  );
}

export const MarkdownBody = memo(MarkdownBodyImpl);

function MarkdownImpl({ source, linkResolver, highlight = true, className }: MarkdownProps) {
  return (
    <div className={className ? `${styles.root} ${className}` : styles.root}>
      <MarkdownBody source={source} linkResolver={linkResolver} highlight={highlight} />
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);

/** Root class, for hosts that compose their own container (StreamingMarkdown). */
export const markdownRootClass = styles.root;
