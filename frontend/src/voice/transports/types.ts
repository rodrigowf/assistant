/**
 * Voice transport contract — the shape every transport implements so
 * :func:`useVoiceOrchestrator` can dispatch on ``connection_type`` from
 * the backend's ``voice_connection_info`` payload without knowing which
 * specific provider is in use.
 *
 * Providers split along transport, not vendor:
 *
 *   webrtc      → ``connectWebRTCVoiceSession``  (used by OpenAI today)
 *   websocket   → ``connectWebSocketVoiceSession`` (used by Qwen, future Gemini)
 *
 * Adding a new vendor that uses an existing transport requires no
 * changes here — only event-name additions in
 * ``useVoiceOrchestrator``'s normalised event handler if they diverge.
 */

import type { RealtimeEvent } from "../../types";

export type VoiceTransportKind = "webrtc" | "websocket";

/** Common surface area both transports expose. */
export interface VoiceTransportHandles {
  kind: VoiceTransportKind;
  /** Tear down all resources (mic, peer connection / worklet, audio context). */
  disconnect: () => void;
  /** The local mic MediaStream — for muting + level analysis. */
  micStream: MediaStream;
  /** Enable/disable the microphone tracks (mute toggle). */
  setMicEnabled: (enabled: boolean) => void;
  /** Mute the assistant audio output. */
  setOutputMuted: (muted: boolean) => void;
}

/** WebRTC-specific extras: a data channel back to the provider. */
export interface WebRTCVoiceTransportHandles extends VoiceTransportHandles {
  kind: "webrtc";
  /** Send an event to the provider via the WebRTC data channel. */
  sendProviderEvent: (event: RealtimeEvent) => void;
  /** Remote audio MediaStream (for level analysis). May be null until ontrack fires. */
  remoteStream: MediaStream | null;
  /** The hidden <audio> element playing assistant audio (for muting). */
  audioElement: HTMLAudioElement;
}

/** WebSocket-specific extras: backend relays audio in both directions. */
export interface WebSocketVoiceTransportHandles extends VoiceTransportHandles {
  kind: "websocket";
  /** Push a base64 PCM chunk into the playback queue (called by orchestrator). */
  pushAudioOut: (b64: string) => void;
  /** Drop any pending audio (used on barge-in). */
  flushAudioOut: () => void;
}

export type AnyVoiceTransportHandles =
  | WebRTCVoiceTransportHandles
  | WebSocketVoiceTransportHandles;
