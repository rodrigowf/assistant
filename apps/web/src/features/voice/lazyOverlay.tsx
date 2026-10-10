/**
 * The floating voice controls are their own chunk (spec 13 §5.4 initial-JS budget): nothing on the
 * first paint needs them. The shell preloads the chunk as soon as this device has a call, so it is
 * there by the time another view opens.
 */
import { lazy, Suspense } from 'react';
import type { VoiceOverlayProps } from './VoiceOverlay';

const loadOverlay = () => import('./VoiceOverlay');
const LazyOverlay = lazy(() => loadOverlay().then((m) => ({ default: m.VoiceOverlay })));

/** Start fetching the overlay chunk (a call started on this device). */
export function preloadVoiceOverlay(): void {
  void loadOverlay();
}

export function VoiceOverlay(props: VoiceOverlayProps) {
  return (
    <Suspense fallback={null}>
      <LazyOverlay {...props} />
    </Suspense>
  );
}
