/**
 * WebSocket voice transport — for providers (Qwen, Gemini Live, future
 * locals) where the backend owns the upstream connection. The frontend's
 * job here is just to capture mic at PCM16 and play back assistant audio
 * delivered by the backend as base64 PCM chunks.
 *
 * Captured chunks are handed off via ``onAudioChunk``; the orchestrator
 * hook ships them over the existing orchestrator WebSocket as
 * ``voice_audio_in`` payloads. Inbound audio chunks are pushed via
 * :func:`WebSocketVoiceTransportHandles.pushAudioOut`.
 */

import { PCMPlayer } from "../audio/pcmPlayer";
import type { WebSocketVoiceTransportHandles } from "./types";

interface ConnectOptions {
  /** Sample rate the provider expects on input (Qwen Plus: 24000; Flash: 16000). */
  inputSampleRate: number;
  /** Sample rate of the audio chunks the provider sends back (Qwen: 24000). */
  outputSampleRate: number;
  /** Called for every captured mic chunk (caller forwards to backend). */
  onAudioChunk: (b64: string) => void;
  /** Optional callback fired ~15fps with speaker RMS level (0–1). */
  onSpeakerLevel?: (level: number) => void;
}

export async function connectWebSocketVoiceSession(
  opts: ConnectOptions,
): Promise<WebSocketVoiceTransportHandles> {
  const micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      sampleRate: opts.inputSampleRate,
    },
  });

  const captureCtx = new AudioContext();
  await captureCtx.audioWorklet.addModule("/pcm-capture-worklet.js");

  const source = captureCtx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(captureCtx, "pcm-capture", {
    processorOptions: {
      targetSampleRate: opts.inputSampleRate,
      chunkMs: 100,
    },
  });

  // Attach the message handler BEFORE wiring up the audio graph so the
  // first chunks emitted by the worklet aren't dropped on the floor.
  // The worklet posts raw ArrayBuffer chunks (PCM16) — base64 happens
  // here on the main thread because AudioWorkletGlobalScope has no
  // `btoa`/`Blob`/`FileReader` etc.
  node.port.onmessage = (e) => {
    const msg = e.data;
    if (msg && msg.type === "pcm" && msg.buffer instanceof ArrayBuffer) {
      const bytes = new Uint8Array(msg.buffer);
      let bin = "";
      const STEP = 4096;
      for (let i = 0; i < bytes.length; i += STEP) {
        bin += String.fromCharCode(...bytes.subarray(i, Math.min(i + STEP, bytes.length)));
      }
      opts.onAudioChunk(btoa(bin));
    }
  };

  // Connect to a muted gain node so the worklet runs on the audio graph
  // without the captured audio feeding back to the speakers.
  const sink = captureCtx.createGain();
  sink.gain.value = 0;
  source.connect(node);
  node.connect(sink);
  sink.connect(captureCtx.destination);

  const player = new PCMPlayer({
    sampleRate: opts.outputSampleRate,
    onLevel: opts.onSpeakerLevel,
  });

  let destroyed = false;
  const disconnect = () => {
    if (destroyed) return;
    destroyed = true;
    try { node.port.onmessage = null; } catch { /* ignore */ }
    try { node.disconnect(); } catch { /* ignore */ }
    try { source.disconnect(); } catch { /* ignore */ }
    try { sink.disconnect(); } catch { /* ignore */ }
    try { micStream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
    captureCtx.close().catch(() => {});
    player.destroy().catch(() => {});
  };

  return {
    kind: "websocket",
    disconnect,
    micStream,
    setMicEnabled: (enabled) => {
      micStream.getAudioTracks().forEach((t) => { t.enabled = enabled; });
    },
    setOutputMuted: (muted) => {
      player.setMuted(muted);
    },
    pushAudioOut: (b64) => {
      player.push(b64);
    },
    flushAudioOut: () => {
      player.flush();
    },
  };
}
