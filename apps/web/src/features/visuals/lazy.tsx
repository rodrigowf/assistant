/**
 * The visual viewer is its own chunk (spec 13 §5.4 initial-JS budget): the first paint does not
 * need it. Fetched the first time a visualization opens (or a restored visual tab mounts).
 */
import { lazy, Suspense } from 'react';
import type { VisualViewerProps } from './VisualViewer';

const loadViewer = () => import('./VisualViewer');
const LazyViewer = lazy(() => loadViewer().then((m) => ({ default: m.VisualViewer })));

/** Start fetching the viewer chunk (e.g. when the Visuals pane opens). */
export function preloadVisualViewer(): void {
  void loadViewer();
}

export function VisualViewer(props: VisualViewerProps) {
  return (
    <Suspense fallback={null}>
      <LazyViewer {...props} />
    </Suspense>
  );
}
