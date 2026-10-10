/**
 * Entry point of `@/platform` (W-01). Browser-environment helpers that every other package may
 * use. Polyfills are not exported here: main.tsx imports `@/platform/polyfills` directly.
 */
import { installDebugHandle } from './debug';
import { initLowEnd } from './lowEnd';

export * from './capabilities';
export * from './clipboard';
export * from './debug';
export * from './emitter';
export * from './lowEnd';
export * from './media';
export * from './notifications';
export * from './remoteLog';
export * from './storage';
export * from './time';
export * from './uuid';

export interface InitPlatformOptions {
  /** The "Reduce motion" device pref, when already known at startup. */
  reduceMotion?: boolean;
}

/** One-time startup work, before the first render. */
export function initPlatform(opts: InitPlatformOptions = {}): void {
  initLowEnd({ forced: opts.reduceMotion ?? false });
  installDebugHandle();
}
