/**
 * W-14 DoD (memory): tree counts + search + keyboard expand, frontmatter chip (verbatim, folded,
 * expands in place), relative links resolved against the current file and opened in the app,
 * encoded fetch (MEM-3), `#anchor` links never touch the URL hash (the app routes on it).
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MemoryNode } from '@/services';
import { bumpContent, contentChangesStore, resetContentChanges, setCatalogItems } from '@/stores';
import { expectNoAxeViolations } from '@/test/axe';
import { setupServices, teardownServices, textResponse, type Harness } from '../../../services/__tests__/fakes';
import { countAll, filterMemory, MemoryDocument, MemoryPane, pinIndexFirst, toTreeNodes } from '..';
import { frontmatterChipLabel, modifiedLine, splitTitle } from '../MemoryDocument';

const file = (path: string): MemoryNode => ({ name: path.split('/').pop() ?? path, path, is_dir: false, children: null });
const dir = (path: string, children: MemoryNode[]): MemoryNode => ({ name: path.split('/').pop() ?? path, path, is_dir: true, children });

const TREE: MemoryNode[] = [
  dir('assistant', [
    dir('assistant/architecture', [file('assistant/architecture/voice_subsystem.md'), file('assistant/architecture/wakeword_subsystem.md')]),
    dir('assistant/devices', [file('assistant/devices/fire_tv.md')]),
  ]),
  dir('projects', [file('projects/garden.md')]),
  file('MEMORY.md'),
];

describe('tree model', () => {
  it('counts files, pins MEMORY.md first and filters by path words', () => {
    expect(countAll(TREE)).toBe(5);
    expect(pinIndexFirst(TREE).map((n) => n.path)).toEqual(['MEMORY.md', 'assistant', 'projects']);
    expect(toTreeNodes(TREE)[0]?.meta).toBe('3');
    const f = filterMemory(TREE, 'arch voice');
    expect(f.map((n) => n.path)).toEqual(['assistant']);
    expect(f[0]?.children?.[0]?.children?.map((n) => n.path)).toEqual(['assistant/architecture/voice_subsystem.md']);
    // a matching folder keeps all of its files
    expect(filterMemory(TREE, 'devices')[0]?.children?.[0]?.children).toHaveLength(1);
    expect(filterMemory(TREE, 'zzz')).toEqual([]);
  });
  it('frontmatter chip label, modified line and title split', () => {
    expect(frontmatterChipLabel('category: architecture\nreferences:\n  - a.md\n  - b.md\n  - c.md\n  - d.md\n')).toBe('Frontmatter · architecture · 4 refs');
    expect(frontmatterChipLabel('title: x')).toBe('Frontmatter');
    const now = new Date(2026, 9, 4, 10, 0).getTime();
    expect(modifiedLine('modified: 2026-10-02', now)).toBe('Modified 2 days ago');
    expect(modifiedLine('modified: 2026-10-04', now)).toBe('Modified today');
    expect(modifiedLine('title: x', now)).toBeNull();
    expect(splitTitle('# Voice\n\nBody')).toEqual({ heading: '# Voice', rest: '\nBody' });
    expect(splitTitle('Body only')).toEqual({ heading: null, rest: 'Body only' });
  });
});

describe('<MemoryPane>', () => {
  let h: Harness;
  beforeEach(() => {
    h = setupServices();
    h.fetch.on('GET', '/api/memory/tree', TREE);
  });
  afterEach(() => {
    teardownServices();
  });

  it('loads on open; top-level folders start expanded; search opens the way to matches; Enter opens a file', async () => {
    const onOpen = vi.fn();
    const { container } = render(<MemoryPane onOpen={onOpen} />);
    await screen.findByRole('searchbox', { name: 'Search 5 memory files' });
    expect(h.fetch.calls('GET', '/api/memory/tree')).toHaveLength(1);
    const tree = screen.getByRole('tree', { name: 'Memory files' });
    const items = (): string[] => within(tree).getAllByRole('treeitem').map((li) => li.getAttribute('aria-level') + ':' + (li.firstElementChild?.textContent ?? ''));
    // MEMORY.md first, top-level folders open (counts as meta), deeper folders closed
    expect(items()).toEqual(['1:MEMORY.md', '1:assistant3', '2:architecture2', '2:devices1', '1:projects1', '2:garden.md']);
    // keyboard: → on a closed folder expands it
    const arch = within(tree).getAllByRole('treeitem')[2] as HTMLElement;
    act(() => {
      arch.focus();
    });
    fireEvent.keyDown(arch, { key: 'ArrowRight' });
    expect(arch.getAttribute('aria-expanded')).toBe('true');
    // search
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'fire' } });
    expect(items()).toEqual(['1:assistant1', '2:devices1', '3:fire_tv.md']);
    const leaf = within(tree).getAllByRole('treeitem')[2] as HTMLElement;
    act(() => {
      leaf.focus();
    });
    fireEvent.keyDown(leaf, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledWith('assistant/devices/fire_tv.md', 'fire_tv.md');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nothing-like-this' } });
    expect(screen.getByText(/No files match/)).toBeTruthy();
    await expectNoAxeViolations(container);
  });

  it('marks the active memory tab and refreshes on demand', async () => {
    setCatalogItems('memory', TREE);
    render(<MemoryPane selectedPath="projects/garden.md" />);
    const sel = screen.getAllByRole('treeitem').find((li) => li.getAttribute('aria-selected') === 'true');
    expect(sel?.textContent).toBe('garden.md');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh memory' }));
    await waitFor(() => {
      expect(h.fetch.calls('GET', '/api/memory/tree').length).toBeGreaterThan(0);
    });
  });
});

describe('<MemoryDocument>', () => {
  let h: Harness;
  const DOC = [
    '---',
    'title: Voice subsystem',
    'category: architecture',
    'modified: 2026-10-02',
    'references:',
    '  - ../plans/plan a.md',
    '  - wakeword_subsystem.md',
    '---',
    '# Voice subsystem',
    '',
    'See [wake word](wakeword_subsystem.md), [the plan](../plans/plan%20a.md#steps), [outside](../../../etc/passwd.md),',
    '[docs](https://example.com/docs) and [lifecycle](#lifecycle).',
    '',
    '## Lifecycle',
    '',
    'Text.',
  ].join('\n');

  beforeEach(() => {
    h = setupServices();
    h.fetch.on('GET', /^\/memory\//, (req) => (req.path === '/memory/assistant/arch%20itecture/voice%23sub.md' ? textResponse(DOC, 200, 'text/markdown') : textResponse('{"detail":"Not Found"}', 404, 'application/json')));
  });
  afterEach(() => {
    teardownServices();
  });

  it('fetches the encoded path, folds the frontmatter into a chip and opens relative links in the app', async () => {
    const onOpenLink = vi.fn();
    const { container } = render(<MemoryDocument path="assistant/arch itecture/voice#sub.md" hidden={false} onOpenLink={onOpenLink} />);
    await screen.findByRole('heading', { name: 'Voice subsystem', level: 1 });
    expect(h.fetch.requests.map((r) => r.path)).toContain('/memory/assistant/arch%20itecture/voice%23sub.md');

    // frontmatter: one collapsed chip, verbatim YAML inside, never rendered as markdown
    const chip = screen.getByRole('button', { name: /Frontmatter · architecture · 2 refs/ });
    expect(chip.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('hr')).toBeNull();
    fireEvent.click(chip);
    expect(chip.getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('pre')?.textContent).toContain('category: architecture\nmodified: 2026-10-02');
    expect(screen.getByText(/^Modified /)).toBeTruthy();

    // relative links resolve against this file's folder (not the app root) and open in-app
    fireEvent.click(screen.getByRole('link', { name: 'wake word' }));
    expect(onOpenLink).toHaveBeenLastCalledWith('assistant/arch itecture/wakeword_subsystem.md');
    const plan = screen.getByRole('link', { name: 'the plan' });
    expect(plan.getAttribute('href')).toBe('/memory/assistant/plans/plan%20a.md');
    fireEvent.click(plan);
    expect(onOpenLink).toHaveBeenLastCalledWith('assistant/plans/plan a.md');
    // escaping the memory root, and absolute URLs, open externally
    expect(screen.getByRole('link', { name: 'outside' }).getAttribute('target')).toBe('_blank');
    expect(screen.getByRole('link', { name: 'docs' }).getAttribute('target')).toBe('_blank');
    expect(onOpenLink).toHaveBeenCalledTimes(2);

    // #anchor: scrolls inside, never changes the app's hash route
    const before = location.hash;
    fireEvent.click(screen.getByRole('link', { name: 'lifecycle' }));
    expect(location.hash).toBe(before);
    await expectNoAxeViolations(container);
  });

  it('refetches on memory_changed for its file and on a resync, keeping the text; the cue only when the text changed', async () => {
    resetContentChanges();
    let body = '# Live\n\nOne.';
    h.fetch.on('GET', '/memory/live.md', () => textResponse(body, 200, 'text/markdown'));
    render(<MemoryDocument path="live.md" hidden={false} />);
    await screen.findByText('One.');
    const fetches = () => h.fetch.calls('GET', '/memory/live.md').length;
    expect(fetches()).toBe(1);

    act(() => {
      bumpContent('memory', [{ path: 'other.md', deleted: false }]);
    });
    await new Promise((r) => setTimeout(r, 400));
    expect(fetches()).toBe(1);

    body = '# Live\n\nTwo.';
    act(() => {
      bumpContent('memory', [{ path: 'live.md', deleted: false }]);
    });
    await screen.findByText('Two.');
    expect(fetches()).toBe(2);
    expect(screen.getByRole('status').textContent).toBe('Updated');

    // the socket came back: quiet refetch, same text → no cue
    await waitFor(() => {
      expect(screen.queryByRole('status')).toBeNull();
    }, { timeout: 4000 });
    act(() => {
      contentChangesStore.setState((st) => ({ resyncEpoch: st.resyncEpoch + 1 }));
    });
    await waitFor(() => {
      expect(fetches()).toBe(3);
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('shows the error with Retry, and Reload re-fetches', async () => {
    render(<MemoryDocument path="missing.md" hidden={false} />);
    expect((await screen.findByRole('alert')).textContent).toMatch(/Could not load missing\.md/);
    const n = h.fetch.calls('GET', '/memory/missing.md').length;
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    await waitFor(() => {
      expect(h.fetch.calls('GET', '/memory/missing.md').length).toBe(n + 1);
    });
  });
});
