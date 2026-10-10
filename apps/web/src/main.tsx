/**
 * Entry (spec 13 §3.1): compat polyfills → platform init → createRoot(<App/>).
 * `@/platform/polyfills` must stay the first import (ES modules evaluate in import order).
 */
import '@/platform/polyfills';
import '@/styles';
import { StrictMode, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@/app/App';
import { RootErrorBoundary } from '@/app/RootErrorBoundary';
import { initPlatform } from '@/platform';

initPlatform();

// Dev gallery of UI primitives and components (W-02), loaded lazily on #/dev/gallery. Only in the
// dev server or a `VITE_GALLERY=1` build (`npm run build:gallery`, for device QA): both flags are
// replaced at build time, so a normal production build drops the import and ships no gallery chunk.
const GALLERY_ENABLED = import.meta.env.DEV || import.meta.env.VITE_GALLERY === '1';
const Gallery = GALLERY_ENABLED ? lazy(() => import('@/dev/gallery').then((m) => ({ default: m.Gallery }))) : null;
const isGallery = Gallery !== null && window.location.hash.indexOf('#/dev/gallery') === 0;

const container = document.getElementById('root');
if (!container) throw new Error('#root element missing from index.html');

createRoot(container).render(
  <StrictMode>
    <RootErrorBoundary>
      {isGallery && Gallery ? (
        <Suspense fallback={null}>
          <Gallery />
        </Suspense>
      ) : (
        <App />
      )}
    </RootErrorBoundary>
  </StrictMode>,
);
