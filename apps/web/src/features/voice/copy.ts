/**
 * Voice dock wording (IA §6; mockups phone (d), (k), components board "Voice dock").
 * Pure, so the dock tests and the gallery share it.
 */
import type { VoiceSnapshot } from '@/voice';

/** "Listening Ns" appears after 3 s of continuous `listening` (spec 12 §7.7; fixes W-4). */
export const VAD_COUNTER_AFTER_MS = 3_000;

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r < 10 ? '0' : ''}${r}`;
}

export interface DockText {
  readonly title: string;
  readonly detail: string;
}

/** Title and supporting line for the own-device dock (live, connecting, ending). */
export function dockText(s: VoiceSnapshot, now: number): DockText {
  switch (s.status) {
    case 'connecting':
      if (s.phase === 'summarizing') return { title: 'Preparing conversation…', detail: 'Archie is catching up on the conversation' };
      if (s.phase === 'preparing') return { title: 'Connecting…', detail: 'Opening the voice service' };
      return { title: 'Connecting…', detail: 'Starting voice' };
    case 'ending':
      return { title: 'Ending…', detail: 'Closing the voice connection' };
    case 'speaking':
      return { title: 'Speaking', detail: bannerText(s) ?? 'Tap the orb to interrupt' };
    case 'thinking':
      return { title: 'Thinking', detail: bannerText(s) ?? 'Working on it' };
    case 'tool_use':
      return { title: 'Using tools', detail: bannerText(s) ?? 'Running a tool' };
    case 'active':
    default: {
      if (s.micMuted) return { title: 'Muted', detail: 'Your microphone is off' };
      const vad = vadSeconds(s, now);
      return { title: 'Listening', detail: bannerText(s) ?? (vad !== null ? `Hearing you · ${vad}s` : 'Speak any time') };
    }
  }
}

/** Seconds of continuous speech once past the 3 s threshold, else null. */
export function vadSeconds(s: VoiceSnapshot, now: number): number | null {
  const v = s.vad;
  if (!v || v.state !== 'listening') return null;
  const ms = v.durationMs + Math.max(0, now - v.at);
  return ms >= VAD_COUNTER_AFTER_MS ? Math.floor(ms / 1000) : null;
}

export function bannerText(s: VoiceSnapshot): string | null {
  const b = s.banner;
  if (!b) return null;
  if (b.kind === 'reconnect_warning') return b.timeLeftS !== null ? `Pausing in ~${Math.round(b.timeLeftS)}s to reconnect…` : 'Pausing briefly to reconnect…';
  return b.message;
}

/** The compact app bar subtitle word (mockups: "Voice · Listening", "Voice · Reconnecting…"). */
export function statusWord(s: VoiceSnapshot): string {
  if (s.link === 'lost') return 'Reconnecting…';
  if (s.link === 'failed') return 'Couldn’t reconnect';
  switch (s.status) {
    case 'connecting':
      return 'Connecting…';
    case 'speaking':
      return 'Speaking';
    case 'thinking':
      return 'Thinking';
    case 'tool_use':
      return 'Using tools';
    case 'ending':
      return 'Ending…';
    case 'error':
      return 'Stopped';
    case 'active':
      return s.micMuted ? 'Muted' : 'Listening';
    default:
      return '';
  }
}

export const RECONNECTING_DETAIL = 'Reconnecting to Archie · you’ll hear a tone when it’s back';
export const ELSEWHERE_TITLE = 'Voice active on another device';
/** VT-2: every provider's live transcript reaches passive viewers (OpenAI's through the backend). */
export const ELSEWHERE_DETAIL = 'Transcripts mirror here';
