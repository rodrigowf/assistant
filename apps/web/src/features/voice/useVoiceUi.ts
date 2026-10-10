/**
 * `useVoiceUi(localId)` (spec 13 §3.9): the voice snapshot of one Archie conversation and its
 * controller. Agent sessions, read-only Archie views and closed tabs get the `off` snapshot.
 */
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useStore } from 'zustand';
import { createStore } from 'zustand/vanilla';
import { useSessionRegistryVersion } from '@/stores';
import { dockVisible, getVoiceController, OFF_SNAPSHOT, type VoiceController, type VoiceSnapshot } from '@/voice';

const offStore = createStore<VoiceSnapshot>(() => OFF_SNAPSHOT);

export interface VoiceUi {
  readonly snapshot: VoiceSnapshot;
  readonly controller: VoiceController | undefined;
}

function controllerAt(localId: string, _registryVersion: number): VoiceController | undefined {
  return getVoiceController(localId);
}

export function useVoiceUi(localId: string): VoiceUi {
  const version = useSessionRegistryVersion();
  // getVoiceController is idempotent (one controller per runtime); re-read when tabs open/close
  const controller = useMemo(() => controllerAt(localId, version), [localId, version]);
  const snapshot = useStore(controller?.store ?? offStore);
  return { snapshot, controller };
}

/**
 * Which of these Archie conversations has voice on this device (its dock would show: any state
 * but `off`; "Active elsewhere" does not count), or null. Drives the floating controls.
 */
export function useLiveVoiceId(ids: readonly string[]): string | null {
  const version = useSessionRegistryVersion();
  const key = ids.join('\n');
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` is `ids` by value
  const controllers = useMemo(() => ids.map((id) => [id, controllerAt(id, version)] as const), [key, version]);
  const subscribe = useCallback(
    (fn: () => void) => {
      const offs = controllers.map(([, c]) => c?.store.subscribe(fn));
      return () => offs.forEach((off) => off?.());
    },
    [controllers],
  );
  const get = useCallback((): string | null => {
    for (const [id, c] of controllers) if (c && dockVisible(c.store.getState())) return id;
    return null;
  }, [controllers]);
  return useSyncExternalStore(subscribe, get, get);
}

/** Re-render every `ms` while `on` (elapsed timers, the VAD counter). */
export function useTicker(on: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return undefined;
    const first = setTimeout(() => setNow(Date.now()), 0);
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [on, ms]);
  return now;
}
