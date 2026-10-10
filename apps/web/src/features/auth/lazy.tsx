/**
 * The sign-in screen (and its flows) is its own chunk (spec 13 §5.4 initial-JS budget): most
 * starts never show it. Kept out of AuthGate.tsx so the gate and its store stay in the entry chunk.
 */
import { lazy, Suspense } from 'react';

const LazySignInScreen = lazy(() => import('./SignInScreen').then((m) => ({ default: m.SignInScreen })));

/** The full-screen sign-in surface (lazy chunk; the gallery renders it inline). */
export function SignInScreen(props: { inline?: boolean }) {
  return (
    <Suspense fallback={null}>
      <LazySignInScreen {...props} />
    </Suspense>
  );
}

