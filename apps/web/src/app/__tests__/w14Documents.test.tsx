/**
 * W-14 through the shell (IA §9.2, spec 13 §4.2): memory files and visuals open as **tabs** on
 * expanded (the iframe stays mounted while another tab is active) and as **detail screens** on
 * compact; a relative link inside a memory document opens the other file the same way.
 */
import { act, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { MemoryNode } from '@/services';
import { activateTab, setCatalogItems, tabsStore } from '@/stores';
import { renderUi } from '@/test/render';
import { App } from '../App';
import { routeStore } from '../navigation/route';
import { setupApp, setWidth, SIZES, teardownApp } from './testUtils';
import type { Harness } from '../../services/__tests__/fakes';

const TREE: MemoryNode[] = [
  {
    name: 'assistant',
    path: 'assistant',
    is_dir: true,
    children: [
      { name: 'voice.md', path: 'assistant/voice.md', is_dir: false, children: null },
      { name: 'wake word.md', path: 'assistant/wake word.md', is_dir: false, children: null },
    ],
  },
];
const VOICE = '---\ncategory: architecture\n---\n# Voice\n\nSee [wake word](wake%20word.md).\n';
const VIZ = [{ path: 'charts/weekly.html', url: '/charts/weekly.html', title: 'Weekly energy usage', created: '2026-10-04T10:00:00+00:00', modified: '2026-10-04T11:00:00+00:00', size: 1 }];

let h: Harness;
beforeEach(() => {
  h = setupApp(SIZES.expanded);
  h.fetch.on('GET', '/api/memory/tree', TREE);
  h.fetch.on('GET', '/api/visualizations', VIZ);
  h.fetch.on('GET', /^\/memory\//, (req) => new Response(req.path.indexOf('voice') >= 0 ? VOICE : '# Wake word\n', { status: 200, headers: { 'content-type': 'text/markdown' } }));
  setCatalogItems('memory', TREE);
  setCatalogItems('visuals', VIZ);
});
afterEach(() => {
  teardownApp();
});

describe('expanded: documents open as tabs', () => {
  it('memory file → a tab; a relative link opens the other file as another tab (encoded fetch)', async () => {
    const { getByRole, findByRole } = renderUi(<App services={false} />);
    fireEvent.click(getByRole('button', { name: 'Memory' }));
    const pane = getByRole('complementary', { name: 'Memory' });
    fireEvent.click(within(pane).getByText('voice.md'));
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['memory:assistant/voice.md']);
    fireEvent.click(await findByRole('link', { name: 'wake word' }));
    expect(tabsStore.getState().tabs.map((t) => t.id)).toEqual(['memory:assistant/voice.md', 'memory:assistant/wake word.md']);
    expect(tabsStore.getState().activeId).toBe('memory:assistant/wake word.md');
    await waitFor(() => {
      expect(h.fetch.requests.map((r) => r.path)).toContain('/memory/assistant/wake%20word.md');
    });
  });

  it('visual → a tab whose iframe survives switching to another tab', async () => {
    const { getByRole, container } = renderUi(<App services={false} />);
    fireEvent.click(getByRole('button', { name: 'Visuals' }));
    fireEvent.click(within(getByRole('complementary', { name: 'Visuals' })).getByRole('button', { name: /Weekly energy usage/ }));
    expect(tabsStore.getState().activeId).toBe('viz:charts/weekly.html');
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy()); // lazy viewer chunk
    const frame = container.querySelector('iframe');
    expect(frame?.getAttribute('src')).toBe('http://backend.test/charts/weekly.html');
    fireEvent.click(getByRole('button', { name: 'Memory' }));
    fireEvent.click(within(getByRole('complementary', { name: 'Memory' })).getByText('voice.md'));
    expect(tabsStore.getState().activeId).toBe('memory:assistant/voice.md');
    expect(container.querySelector('iframe')).toBe(frame);
    act(() => {
      activateTab('viz:charts/weekly.html');
    });
    expect(container.querySelector('iframe')).toBe(frame);
  });
});

describe('compact: documents open as detail screens', () => {
  beforeEach(() => {
    setWidth(SIZES.compact);
  });

  it('Memory screen → document screen (no tab); Visuals screen → viewer screen', async () => {
    act(() => {
      routeStore.setState({ route: { name: 'memory', path: null } });
    });
    const { getByRole, findByRole } = renderUi(<App services={false} />);
    const list = getByRole('region', { name: 'Memory' });
    fireEvent.click(within(list).getByText('voice.md'));
    expect(routeStore.getState().route).toEqual({ name: 'memory', path: 'assistant/voice.md' });
    expect(tabsStore.getState().tabs).toHaveLength(0);
    await findByRole('region', { name: 'voice.md' });
    fireEvent.click(await findByRole('link', { name: 'wake word' }));
    expect(routeStore.getState().route).toEqual({ name: 'memory', path: 'assistant/wake word.md' });
    expect(tabsStore.getState().tabs).toHaveLength(0);

    act(() => {
      routeStore.setState({ route: { name: 'visuals', path: null } });
    });
    fireEvent.click(within(getByRole('region', { name: 'Visuals' })).getByRole('button', { name: /Weekly energy usage/ }));
    expect(routeStore.getState().route).toEqual({ name: 'visuals', path: 'charts/weekly.html' });
    // the screen's title is the visual's list title (mockup h), not the file name
    const screen = await waitFor(() => {
      const el = document.querySelector('section[data-screen="visuals:charts/weekly.html"]');
      if (!el) throw new Error('no visual screen');
      return el;
    });
    expect(screen.getAttribute('aria-label')).toBe('Weekly energy usage');
    expect(screen.querySelector('iframe')?.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-popups allow-forms allow-modals');
    expect(tabsStore.getState().tabs).toHaveLength(0);
  });
});
