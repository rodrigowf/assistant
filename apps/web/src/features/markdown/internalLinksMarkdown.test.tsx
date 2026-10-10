/**
 * Internal links in rendered markdown (spec 12 §9.4): under an `InternalLinksProvider`, links to a
 * visualization or memory file open in the app (plain click only, LNK-6), printed paths are
 * auto-linked (LNK-5), an explicit resolver is asked first; without a provider nothing changes.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InternalLinksProvider, internalTargetUrl, type InternalLinks } from '@/features/links';
import { createMemoryLinkResolver, Markdown, StreamingMarkdown } from '.';

function links(open = vi.fn()): InternalLinks {
  return { context: { origin: 'https://192.168.0.200' }, open, hrefOf: (t) => `https://192.168.0.200${internalTargetUrl(t)}` };
}

const SOURCE = [
  'See [the dashboard](https://192.168.0.200/avatar-pipeline/index.html), [notes](/memory/projects/x.md#status)',
  'and [Google](https://google.com).',
  '',
  'Saved to context/public/visualizations/foo.html. Spec: `docs/specs/12-client-protocol.md:40`; code `backend/api/app.py`.',
  '',
  '```',
  'context/public/visualizations/in-a-fence.html',
  '```',
].join('\n');

describe('internal links', () => {
  it('opens visualization and memory links in the app on a plain click, keeps the real href', () => {
    const open = vi.fn();
    render(
      <InternalLinksProvider value={links(open)}>
        <Markdown source={SOURCE} />
      </InternalLinksProvider>,
    );
    const dash = screen.getByRole('link', { name: 'the dashboard' });
    expect(dash.getAttribute('href')).toBe('https://192.168.0.200/avatar-pipeline/index.html');
    expect(dash.getAttribute('target')).toBeNull();
    fireEvent.click(dash);
    expect(open).toHaveBeenLastCalledWith({ kind: 'visual', path: 'avatar-pipeline/index.html' });

    fireEvent.click(screen.getByRole('link', { name: 'notes' }));
    expect(open).toHaveBeenLastCalledWith({ kind: 'memory', path: 'projects/x.md', fragment: 'status' });

    // modified clicks keep the browser default (new tab)
    fireEvent.click(dash, { ctrlKey: true });
    expect(open).toHaveBeenCalledTimes(2);

    expect(screen.getByRole('link', { name: 'Google' }).getAttribute('target')).toBe('_blank');
  });

  it('auto-links printed paths (bare and inline code), never fenced code or plain source paths', () => {
    const open = vi.fn();
    const { container } = render(
      <InternalLinksProvider value={links(open)}>
        <Markdown source={SOURCE} />
      </InternalLinksProvider>,
    );
    const bare = screen.getByRole('link', { name: 'context/public/visualizations/foo.html' });
    fireEvent.click(bare);
    expect(open).toHaveBeenLastCalledWith({ kind: 'visual', path: 'visualizations/foo.html' });
    const code = screen.getByRole('link', { name: 'docs/specs/12-client-protocol.md:40' });
    expect(code.querySelector('code')).not.toBeNull();
    fireEvent.click(code);
    expect(open).toHaveBeenLastCalledWith({ kind: 'memory', path: 'archie/specs/12-client-protocol.md', fragment: null });
    expect(screen.queryByRole('link', { name: 'backend/api/app.py' })).toBeNull();
    expect(container.querySelector('pre a, .md-link pre')).toBeNull();
  });

  it('auto-links carry the canonical URL, so a memory document does not resolve them relative to itself', () => {
    const open = vi.fn();
    const openDoc = vi.fn();
    render(
      <InternalLinksProvider value={links(open)}>
        <Markdown source={'See `docs/specs/x.md` and context/memory/projects/y.md.'} linkResolver={createMemoryLinkResolver('projects/notes/a.md', openDoc)} />
      </InternalLinksProvider>,
    );
    const code = screen.getByRole('link', { name: 'docs/specs/x.md' });
    fireEvent.click(code);
    expect(openDoc).toHaveBeenLastCalledWith('archie/specs/x.md');
    fireEvent.click(screen.getByRole('link', { name: 'context/memory/projects/y.md' }));
    expect(openDoc).toHaveBeenLastCalledWith('projects/y.md');
    expect(open).not.toHaveBeenCalled();
  });

  it('streaming text gets the same handling', () => {
    render(
      <InternalLinksProvider value={links()}>
        <StreamingMarkdown source={'Open /memory/MEMORY.md or `context/memory/projects/x.md` now'} streaming />
      </InternalLinksProvider>,
    );
    expect(screen.getByRole('link', { name: 'context/memory/projects/x.md' })).toBeTruthy();
  });

  it('an explicit resolver wins; without a provider links are external and paths stay text', () => {
    const mine = vi.fn();
    const { unmount } = render(
      <InternalLinksProvider value={links()}>
        <Markdown source="[a](/memory/a.md)" linkResolver={(href) => (href === '/memory/a.md' ? { href: '/x', onActivate: mine } : null)} />
      </InternalLinksProvider>,
    );
    fireEvent.click(screen.getByRole('link', { name: 'a' }));
    expect(mine).toHaveBeenCalled();
    unmount();

    render(<Markdown source={SOURCE} />);
    expect(screen.getByRole('link', { name: 'the dashboard' }).getAttribute('target')).toBe('_blank');
    expect(screen.queryByRole('link', { name: 'context/public/visualizations/foo.html' })).toBeNull();
  });
});
