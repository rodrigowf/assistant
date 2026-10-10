/**
 * Device-local preferences (spec 12 §8.2, spec 13 §3.3). Never sent to the backend. Persisted in
 * `localStorage` (`prefs:v1`) through the try/catch wrapper of `@/platform` (Safari 12 private
 * mode). Remote logging is the platform's own flag (`archie.remoteConsole`), mirrored here.
 */
import { createStore } from 'zustand/vanilla';
import { isRemoteLogEnabled, localStore, REMOTE_CONSOLE_KEY, remoteLogDefault, setLowEndForced, setRemoteLogEnabled } from '@/platform';

export type ThemePref = 'system' | 'dark' | 'light';
export type TextSizePref = 'small' | 'default' | 'large' | 'xlarge';
export type RailDestination = 'chats' | 'memory' | 'visuals' | 'settings';
/** Where the floating voice controls snap to (features/voice `VoiceOverlay`). */
export type VoiceOverlayAnchor = 'top-left' | 'top-center' | 'top-right' | 'bottom-left' | 'bottom-center' | 'bottom-right';

export interface Prefs {
  readonly theme: ThemePref;
  readonly textSize: TextSizePref;
  readonly reduceMotion: boolean;
  readonly syntaxHighlighting: boolean;
  readonly listPaneCollapsed: boolean;
  readonly lastRailDestination: RailDestination;
  readonly remoteLogging: boolean;
  readonly toolStepGrouping: boolean;
  /** System notification when an agent session finishes a turn (needs the browser's permission). */
  readonly notifyAgentTurns: boolean;
  readonly voiceOverlayAnchor: VoiceOverlayAnchor;
}

export const PREFS_STORAGE_KEY = 'prefs:v1';

export const DEFAULT_PREFS: Prefs = {
  theme: 'dark', // D3: dark-first; system and light are opt-in
  textSize: 'default',
  reduceMotion: false,
  syntaxHighlighting: true,
  listPaneCollapsed: false,
  lastRailDestination: 'chats',
  remoteLogging: false,
  toolStepGrouping: true,
  notifyAgentTurns: false,
  voiceOverlayAnchor: 'bottom-center', // where the dock sits on the Archie page
};

const THEMES: readonly ThemePref[] = ['system', 'dark', 'light'];
const SIZES: readonly TextSizePref[] = ['small', 'default', 'large', 'xlarge'];
const RAIL: readonly RailDestination[] = ['chats', 'memory', 'visuals', 'settings'];
export const VOICE_OVERLAY_ANCHORS: readonly VoiceOverlayAnchor[] = ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'];

/** The remote-console flag (`archie.remoteConsole`, owned by `@/platform`), with the per-build default. */
function remoteLoggingNow(): boolean {
  const v = localStore.get(REMOTE_CONSOLE_KEY);
  if (v === '1') return true;
  if (v === '0') return false;
  return isRemoteLogEnabled() || remoteLogDefault();
}

/** Defensive parse: unknown or mistyped values fall back to the defaults. */
export function sanitizePrefs(raw: unknown): Prefs {
  const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const bool = (k: keyof Prefs): boolean => (typeof o[k] === 'boolean' ? (o[k] as boolean) : (DEFAULT_PREFS[k] as boolean));
  const pick = <T extends string>(k: keyof Prefs, allowed: readonly T[]): T =>
    allowed.indexOf(o[k] as T) >= 0 ? (o[k] as T) : (DEFAULT_PREFS[k] as T);
  return {
    theme: pick('theme', THEMES),
    textSize: pick('textSize', SIZES),
    reduceMotion: bool('reduceMotion'),
    syntaxHighlighting: bool('syntaxHighlighting'),
    listPaneCollapsed: bool('listPaneCollapsed'),
    lastRailDestination: pick('lastRailDestination', RAIL),
    remoteLogging: remoteLoggingNow(),
    toolStepGrouping: bool('toolStepGrouping'),
    notifyAgentTurns: bool('notifyAgentTurns'),
    voiceOverlayAnchor: pick('voiceOverlayAnchor', VOICE_OVERLAY_ANCHORS),
  };
}

export const prefsStore = createStore<Prefs>(() => sanitizePrefs(localStore.getJSON(PREFS_STORAGE_KEY, null)));

export function setPref<K extends keyof Prefs>(key: K, value: Prefs[K]): void {
  if (prefsStore.getState()[key] === value) return;
  prefsStore.setState({ [key]: value } as Pick<Prefs, K>);
  if (key === 'remoteLogging') setRemoteLogEnabled(value as boolean);
  if (key === 'reduceMotion') setLowEndForced(value as boolean);
  const { remoteLogging: _r, ...rest } = prefsStore.getState();
  localStore.setJSON(PREFS_STORAGE_KEY, rest);
}

/** Re-read storage (tests, another browser tab). */
export function reloadPrefs(): void {
  prefsStore.setState(sanitizePrefs(localStore.getJSON(PREFS_STORAGE_KEY, null)), true);
}
